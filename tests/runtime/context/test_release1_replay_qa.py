"""Release-1 replay: D45 determinism, the never-persisted ephemera, and two
paths where a scope narrowing does NOT drop what it should (targets 8 and 9).

03 §D.1 closed one cross-turn leak — the `updateAnalysisState` trail entry whose
`args` carry the intent descriptions — by generalising `_is_stale_model_text_entry`
to a tool SET. That predicate keys on `tool_name`. This file asks whether the same
text reaches a later turn under a DIFFERENT tool name, and whether the enriched
`searchBlueprints` provenance actually covers every column identifier its own card
now prints.

Both cases arrived here as `xfail(strict=True)` defects — each measured a concrete
string arriving in a later turn's model context after the caller's `column_scope`
narrowed. BOTH ARE NOW FIXED and the tests assert the fix; the reasoning is kept in
each docstring, because the defect is easier to reintroduce than to find.
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import UpdateAnalysisStateTool
from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
from data_agent.runtime.context.assembly import ContextAssembler, render_analysis_state_block
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import (
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
    AgentLoop,
)
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import SearchBlueprintsTool
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    AnalysisState,
    TrackedIntent,
    TrailEntry,
    TurnMessage,
)
from data_agent.runtime.session_history import project_history

SESSION_ID = "sess-r1-replay-qa"
_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
CATALOG = CatalogHandle(
    {
        _E: {"EmployeeCode": "String", "Department": "Nullable(String)"},
        _P: {"Amount": "Nullable(Float64)"},
    }
)
STATE = "updateAnalysisState"
ANSWER = "answerWithTable"

# A description that names a VALUE derived from a column the caller may later
# lose — exactly the exposure `_is_stale_model_text_entry`'s own docstring cites.
SECRET_INTENT = "everyone earning above 100000 in Radiology"
OTHER_INTENT = "attrition by department"


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt", column_scope=scope)


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return []


def _init_call(call_id: str, *descriptions: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=STATE, arguments={"intents": [{"description": d} for d in descriptions]}
    )


def _answer_call(call_id: str, answer: str = "Here is the answer.") -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name=ANSWER, arguments={"answer": answer, "tables": [{"sql": "SELECT 1"}]}
    )


def _build(script: list[ModelTurnResult], store: InMemorySessionStore) -> AgentLoop:
    return AgentLoop(
        model_client=ScriptedModelClient(script),
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store, history_token_budget=100_000),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={
            STATE: UpdateAnalysisStateTool(session_store=store),
            ANSWER: AnswerWithTableTool(),
        },
    )


async def _refused_turn(store: InMemorySessionStore) -> None:
    """Turn 0: two intents declared, one `answerWithTable` refused because both are
    still pending — the shape that persists a `FINALIZATION_BLOCKED_PENDING_INTENTS`
    trail entry whose `denial_detail` names every pending intent."""
    loop = _build(
        [
            ModelTurnResult(
                assistant_text=None,
                tool_calls=[_init_call("s1", SECRET_INTENT, OTHER_INTENT)],
            ),
            ModelTurnResult(assistant_text=None, tool_calls=[_answer_call("a1")]),
            ModelTurnResult(assistant_text="giving up"),
        ],
        store,
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_creds(), user_message="two things"
    )
    assert outcome.status == "done"
    refusal = next(
        e for e in await store.load_trail(SESSION_ID) if e.tool_call_id == "a1"
    )
    assert refusal.error_code == FINALIZATION_BLOCKED_PENDING_INTENTS_CODE
    assert SECRET_INTENT in (refusal.denial_detail or "")
    # ...and it is `frozenset()` provenance, by design (05 §B.1) — which is what
    # makes it unconditionally in scope for the D44 filter, forever.
    assert refusal.provenance == frozenset()


# ---------------------------------------------------------------------------
# D45 determinism and the never-persisted ephemera
# ---------------------------------------------------------------------------


async def test_the_state_block_and_the_nudge_are_never_persisted_anywhere() -> None:
    """05 §B.2 / 03 §D: both are within-turn control flow. A persisted nudge would
    appear in `/session/history` as something the user said, and a persisted state
    block would do the same for the ledger. Checked on BOTH surfaces — `doc.messages`
    and the projection `/session/history` actually serves."""
    store = InMemorySessionStore()
    await _refused_turn(store)

    doc = await store.get_or_create_session(SESSION_ID)
    persisted = json.dumps([m.__dict__ for m in doc.messages], default=str)
    assert "re-send your final answer" not in persisted
    assert "[Analysis state" not in persisted
    assert SECRET_INTENT not in persisted

    history = project_history(
        doc.messages,
        doc.tool_trail,
        column_scope=frozenset(),
        pause_checkpoint=doc.pause_checkpoint,
    )
    rendered = json.dumps(history, default=str)
    assert "re-send your final answer" not in rendered
    assert "[Analysis state" not in rendered


async def test_the_state_block_renders_byte_identically_on_every_rebuild() -> None:
    """D45: the same state must produce the same request bytes on a rebuild, a
    resume and a restart. The renderer is pure, so this pins it directly — and
    pins the ORDER of the intents, which is what makes the ids stable across
    round-trips."""
    state = AnalysisState(
        turn_index=4,
        intents=(
            TrackedIntent(intent_id="i1", description=SECRET_INTENT, status="pending"),
            TrackedIntent(
                intent_id="i2",
                description=OTHER_INTENT,
                status="blocked",
                evidence_tool_call_id="q7",
                reason_code="NO_ACCESS",
            ),
        ),
    )
    first = render_analysis_state_block(state)
    second = render_analysis_state_block(state)
    assert first == second
    assert first["role"] == "user"
    # A round-trip through the store must not change a byte either.
    revived = AnalysisState.from_doc(state.to_doc())
    assert render_analysis_state_block(revived) == first


async def test_the_assembled_request_is_byte_identical_across_two_rebuilds() -> None:
    """The whole assembled message list, not just the block — the level D45
    actually promises. Two `assemble` calls against an unchanged document must
    produce identical bytes, or a resume would re-send a different request than the
    one it is resuming."""
    store = InMemorySessionStore()
    await _refused_turn(store)
    doc = await store.get_or_create_session(SESSION_ID)
    doc.analysis_state = AnalysisState(
        turn_index=1,
        intents=(TrackedIntent(intent_id="i1", description=SECRET_INTENT, status="pending"),),
    )
    await store.append_message(
        SESSION_ID, TurnMessage(turn_index=1, role="user", content="next question", ts="t")
    )
    assembler = ContextAssembler(store, history_token_budget=100_000)

    one = await assembler.assemble(SESSION_ID, frozenset(), current_turn_index=1)
    two = await assembler.assemble(SESSION_ID, frozenset(), current_turn_index=1)

    assert json.dumps(one.messages, default=str) == json.dumps(two.messages, default=str)
    # The live state IS rendered for its own turn, immediately before the question.
    blob = json.dumps(one.messages, default=str)
    assert "[Analysis state" in blob
    positions = [i for i, m in enumerate(one.messages) if "[Analysis state" in str(m.get("content"))]
    question_at = [
        i for i, m in enumerate(one.messages) if str(m.get("content")) == "next question"
    ]
    assert positions and question_at and positions[-1] < question_at[-1]


# ---------------------------------------------------------------------------
# Target 9 — a column name / question text surviving a narrowing
# ---------------------------------------------------------------------------


async def test_a_prior_turns_finalization_refusal_does_not_replay_the_descriptions() -> None:
    """WAS a strict xfail (D25/D44, introduced by Release 1) — now fixed.

    03 §D.1's cross-turn drop was keyed on `tool_name` alone, and the finalization
    refusal is persisted under `answerWithTable` — whose SUCCESSFUL entries must keep
    replaying, so it could not simply join that set. `_finalization_blocked` builds a
    `denial_detail` naming every pending intent, the entry's `frozenset()` provenance
    passes `is_entry_in_scope` under any scope forever, and `_render_entry` has no
    turn awareness. Measured before the fix: turn 1 assembled under the NARROWED
    scope `{employee.EmployeeCode}` still contained
    'everyone earning above 100000 in Radiology'.

    `_is_stale_model_text_entry` now also matches on ERROR CODE
    (`_STALE_CROSS_TURN_ERROR_CODES`). This asserts all three carriers are gone: the
    descriptions, the dead intent IDS the detail issues an imperative about, and the
    refused DRAFT PROSE that rides in `args`.
    """
    store = InMemorySessionStore()
    await _refused_turn(store)
    await store.append_message(
        SESSION_ID, TurnMessage(turn_index=1, role="user", content="something else", ts="t")
    )
    assembler = ContextAssembler(store, history_token_budget=100_000)

    narrowed = await assembler.assemble(
        SESSION_ID, frozenset({f"{_E}.EmployeeCode"}), current_turn_index=1
    )

    blob = json.dumps(narrowed.messages, default=str)
    assert SECRET_INTENT not in blob, (
        "a prior turn's intent description reached a later turn's model context "
        "through the finalization refusal's denial_detail, under a scope that has "
        "since narrowed"
    )
    assert OTHER_INTENT not in blob
    # The imperative and the dead ids it names. "Resolve each one with
    # updateAnalysisState — i1 (…), i2 (…)" against a state that no longer exists is
    # how a later turn gets steered into calling the tool on nothing.
    assert "updateAnalysisState" not in blob
    assert "You cannot finish yet" not in blob
    # The refused draft answer, which rides in the entry's `args` and may quote
    # warehouse figures gathered under the WIDER scope.
    assert "Here is the answer." not in blob


async def test_the_refusal_is_dropped_cross_turn_even_under_an_allow_all_scope() -> None:
    """The drop is TURN-scoped, not scope-scoped. `frozenset()` provenance is
    unconditionally in scope, so an assertion made only under a narrowed scope would
    pass for the wrong reason and miss the replay entirely on the common path — every
    later turn of an ordinary, never-narrowed session."""
    store = InMemorySessionStore()
    await _refused_turn(store)
    await store.append_message(
        SESSION_ID, TurnMessage(turn_index=1, role="user", content="something else", ts="t")
    )
    assembler = ContextAssembler(store, history_token_budget=100_000)

    wide = await assembler.assemble(SESSION_ID, frozenset(), current_turn_index=1)

    blob = json.dumps(wide.messages, default=str)
    assert SECRET_INTENT not in blob
    assert "You cannot finish yet" not in blob


async def test_the_refusal_keeps_determined_provenance_so_the_turn_answer_still_replays()\
        -> None:
    """Why the fix is a turn-scoped DROP and not `provenance=None` on the refusal.

    `_compute_turn_provenance_union` is fail-closed: one `None`-provenance entry
    collapses the whole turn's union, which tags that turn's own final assistant
    message undetermined and drops the USER'S ANSWER from every later turn's replay —
    on exactly the multi-intent turns Release 1 exists to serve. The refusal read no
    warehouse data, so `frozenset()` is also the honest value; "do not replay this
    later" is a different question and has its own channel
    (`_is_stale_model_text_entry`'s docstring makes the same point for
    `recordAssumptions`)."""
    store = InMemorySessionStore()
    await _refused_turn(store)

    refusal = next(e for e in await store.load_trail(SESSION_ID) if e.tool_call_id == "a1")
    assert refusal.provenance == frozenset()

    doc = await store.get_or_create_session(SESSION_ID)
    answers = [m for m in doc.messages if m.role == "assistant"]
    assert answers, "the refused-then-abandoned turn still ended with an answer"
    assert all(m.provenance is not None for m in answers), (
        "the turn's own answer was tagged undetermined by the refusal entry and "
        "would be dropped from every later turn's replay"
    )


async def test_the_same_refusal_is_correctly_kept_within_its_own_turn() -> None:
    """The other half, and the reason the fix must be turn-scoped rather than a
    blanket drop: within its OWN turn the refusal is exactly what the model must
    read to know why it was refused."""
    store = InMemorySessionStore()
    await _refused_turn(store)
    assembler = ContextAssembler(store, history_token_budget=100_000)

    same_turn = await assembler.assemble(SESSION_ID, frozenset(), current_turn_index=0)

    assert SECRET_INTENT in json.dumps(same_turn.messages, default=str)


def _candidate(bp_id: str, *, uses: set[str], resolves: dict[str, str]) -> object:
    """One recalled blueprint candidate carrying the RAW decoded enrichment keys
    the Neo4j record mapper now writes — the fixture shape `test_read_tools.py`
    uses, so the card is built by the real pipeline projection."""
    return Candidate(
        id=bp_id,
        kind="blueprint",
        text="salary rollup",
        uses=frozenset(uses),
        payload={
            "intent": "salary rollup",
            "slots_summary": "",
            "resolves": resolves,
            "slots": None,
            "result_grain": None,
        },
    )


def _search_tool(candidate) -> SearchBlueprintsTool:  # type: ignore[no-untyped-def]
    return SearchBlueprintsTool(
        pipeline=RetrievalPipeline(
            embedding_client=FakeEmbeddingClient({"salary": [1.0, 0.0]}),
            reranker=None,
            vector_index=FakeVectorIndex([(candidate, [1.0, 0.0])]),
            user_memory=NullUserMemoryProvider(),
            recall_k=30,
            top_k_blueprints=3,
            top_k_knowledge=3,
        ),
        default_k=5,
        max_k=20,
    )


async def test_a_resolves_column_outside_uses_does_not_survive_a_narrowing() -> None:
    """FIXED (was xfail): `_cards_to_provenance` used to derive the searchBlueprints
    entry's provenance from the cards' `uses` footprint ALONE, while the enriched
    card also PRINTS `resolves` (term -> column NAME). Nothing guarantees those
    columns are members of `uses` — `uses` is the transitive set the DAG READS,
    `resolves` is authored disambiguation metadata — so a card with
    uses={payroll.Amount} and resolves={'salary': 'AnnualSalary'} claimed
    provenance={('dbpcm_warehouse.payroll','Amount')} and stayed in scope under a
    narrowing to exactly that column, replaying 'AnnualSalary' after the caller
    lost it. The guard now derives the printed column identifiers from the
    SERIALISED card and fails closed to `None` when the claimed footprint does not
    cover them."""
    from data_agent.runtime.context import scope_filter

    tool = _search_tool(
        _candidate("bp-1", uses={f"{_P}.Amount"}, resolves={"salary": "AnnualSalary"})
    )
    result = await tool.run({"query": "salary"}, _creds())

    assert result.status == "ok"
    assert "AnnualSalary" in json.dumps(result.result_full, default=str)
    # The caller later loses access to AnnualSalary but keeps payroll.Amount.
    narrowed = frozenset({f"{_P}.Amount"})
    assert not scope_filter.is_provenance_in_scope(result.provenance, narrowed), (
        "the entry stays in scope under a narrowing that removed a column its own "
        "card prints, so 'AnnualSalary' replays after the caller lost it"
    )


async def test_a_prior_turns_empty_designation_refusal_does_not_replay_its_draft() -> None:
    """The FOURTH instance of the same class (08 §O), pinned the day it was added
    rather than after it leaked.

    `ANSWER_TABLE_NO_TABLE_DESIGNATED` is the refusal for an `answerWithTable` that
    named no table on a turn holding untabled multi-row results. Like the
    finalization refusal above it is persisted under `answerWithTable` — whose
    SUCCESSFUL entries must keep replaying, so it cannot be dropped by tool name —
    and like it, its `args` carry the model's REFUSED DRAFT ANSWER: prose written
    from warehouse rows read under whatever scope applied at the time.

    It only ever needs to survive its own turn; that is the entire mechanism (the
    model reads it on the next round-trip and re-sends with a table). So it joins
    `_STALE_CROSS_TURN_ERROR_CODES`, and this asserts the drop directly — under an
    ALLOW-ALL scope, because `frozenset()` provenance is unconditionally in scope
    and an assertion made only under a narrowed one would pass for the wrong reason.
    """
    from data_agent.runtime.dispatch.denial_mapping import (
        ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
    )

    draft = "Radiology has 41 people earning above 100000."
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="a1",
            tool_name=ANSWER,
            args={"answer": draft, "tables": []},
            status="error",
            error_code=ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="t0",
        ),
    )
    await store.append_message(
        SESSION_ID, TurnMessage(turn_index=1, role="user", content="something else", ts="t1")
    )
    assembler = ContextAssembler(store, history_token_budget=100_000)

    # Its OWN turn still sees it — that is what makes the nudge work at all.
    own_turn = await assembler.assemble(SESSION_ID, frozenset(), current_turn_index=0)
    assert ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE in json.dumps(own_turn.messages, default=str)

    # A LATER turn does not, draft prose included.
    later = await assembler.assemble(SESSION_ID, frozenset(), current_turn_index=1)
    blob = json.dumps(later.messages, default=str)
    assert ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE not in blob
    assert draft not in blob
