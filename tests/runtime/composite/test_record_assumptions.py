"""Backend sanity tests for the `recordAssumptions` runtime tool + the
`assumptions` result field (docs/decisions/ui-assumptions-contract.md).

Covers: `clean_assumptions` normalization; the tool's `ToolResult` shape and
no-raise-on-malformed-args contract; loop accumulation into
`TurnOutcome.assumptions` with the `[] -> None` fork; and the `session_history`
answer-survival scope posture. Thorough coverage is left to the QA pass.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.record_assumptions import (
    RecordAssumptionsTool,
    clean_assumptions,
)
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TrailEntry, TurnMessage
from data_agent.runtime.session_history import project_history

SESSION_ID = "sess-assumptions"
CATALOG = CatalogHandle({"db.t": {"c": "String"}})
# An assumption that ENCODES A VALUE — the leak the cross-turn drop exists to stop.
_SECRETISH = "Employees earning above $100,000 were excluded."


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="SECRET", column_scope=scope)


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "listDatabases", "description": "", "parameters": {}}]


# --- clean_assumptions -----------------------------------------------------


def test_clean_assumptions_keeps_strips_dedupes_first_occurrence() -> None:
    raw = ["  Active means employed  ", "", "Active means employed", "  ", "Default period is 2026"]
    assert clean_assumptions(raw) == ["Active means employed", "Default period is 2026"]


def test_clean_assumptions_drops_non_strings_and_non_list() -> None:
    assert clean_assumptions(["ok", 5, None, {"x": 1}, "  ", "ok2"]) == ["ok", "ok2"]
    assert clean_assumptions(None) == []
    assert clean_assumptions("a plain string is not a list") == []
    assert clean_assumptions(42) == []


def test_clean_assumptions_caps_length_and_count() -> None:
    long = "x" * 5000
    assert len(clean_assumptions([long])[0]) == 2000
    many = [f"assumption {i}" for i in range(200)]
    assert len(clean_assumptions(many)) == 50


def test_clean_assumptions_never_raises_on_dict_or_weird_scalars() -> None:
    # QA: `clean_assumptions` is called on unvalidated model output in BOTH the
    # loop and history — it must be total (never raise) on any shape. A non
    # list/tuple (dict, bool, float, bytes) collapses to `[]`.
    assert clean_assumptions({"assumptions": ["x"]}) == []
    assert clean_assumptions(True) == []
    assert clean_assumptions(3.14) == []
    assert clean_assumptions(b"bytes are not a list") == []


def test_clean_assumptions_accepts_a_tuple_like_a_list() -> None:
    # The helper accepts `list | tuple` — a tuple normalizes identically.
    assert clean_assumptions(("a", "a", "  b  ", 7)) == ["a", "b"]


def test_clean_assumptions_all_blank_or_all_nonstring_yields_empty() -> None:
    # Every item drops out -> `[]` (the source of the loop/history `[] -> None`
    # fork). Booleans are NOT strings, so they drop too.
    assert clean_assumptions(["", "   ", "\t\n"]) == []
    assert clean_assumptions([1, 2.0, None, True, {"x": 1}, ["nested"]]) == []


def test_clean_assumptions_count_cap_counts_unique_not_duplicates() -> None:
    # Dedup happens BEFORE the count cap, so a flood of duplicates does not burn
    # the 50-item budget: 50 uniques survive even when preceded by many dupes.
    raw = ["dup"] * 100 + [f"a{i}" for i in range(50)]
    cleaned = clean_assumptions(raw)
    assert len(cleaned) == 50
    assert cleaned[0] == "dup"
    assert cleaned[-1] == "a48"  # "dup" + a0..a48 fills the 50 slots


def test_clean_assumptions_exactly_at_count_cap_is_kept() -> None:
    exactly = [f"a{i}" for i in range(50)]
    assert len(clean_assumptions(exactly)) == 50


# --- the tool itself -------------------------------------------------------


async def test_tool_returns_ok_confirmation_shape() -> None:
    tool = RecordAssumptionsTool()
    result = await tool.run({"assumptions": ["A was taken to mean B", "A was taken to mean B"]}, _creds())
    assert result.status == "ok"
    assert result.tool_name == "recordAssumptions"
    assert result.error_code is None
    # DETERMINED-EMPTY, not `None`. `None` means UNDETERMINED (fail-closed) and
    # cost a false failure message to the model on every successful call — see
    # `test_successful_call_reaches_the_model_as_its_real_confirmation` below.
    assert result.provenance == frozenset()
    assert result.provenance is not None
    assert result.result_full is None
    # Confirmation carries the DEDUPED count (1, not 2).
    assert result.result_preview is not None
    assert result.result_preview.preview_rows == [[1]]


async def test_tool_never_raises_on_malformed_args() -> None:
    tool = RecordAssumptionsTool()
    # Missing key, wrong types — must not raise, must be status ok.
    for args in ({}, {"assumptions": None}, {"assumptions": "not-a-list"}, {"assumptions": [1, 2]}):
        result = await tool.run(args, _creds())  # type: ignore[arg-type]
        assert result.status == "ok"
        assert result.result_preview.preview_rows == [[0]]


# --- loop accumulation into TurnOutcome.assumptions ------------------------


def _build_loop(model: ScriptedModelClient) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(FakeMCPClient(), CATALOG)
    assembler = ContextAssembler(store)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"recordAssumptions": RecordAssumptionsTool()},
    )
    return loop, store


async def test_loop_surfaces_assumptions_on_done_deduped() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={
                            "assumptions": [
                                "'Active employees' means currently-employed staff",
                                "'Active employees' means currently-employed staff",
                                "Used calendar year 2026 as the default period",
                            ]
                        },
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Here is the count."),
        ]
    )
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    assert outcome.status == "done"
    assert outcome.assumptions == [
        "'Active employees' means currently-employed staff",
        "Used calendar year 2026 as the default period",
    ]


async def test_loop_no_recordassumptions_yields_null_not_empty() -> None:
    model = ScriptedModelClient([ModelTurnResult(assistant_text="chat only")])
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hi")
    assert outcome.status == "done"
    # `[] -> None` fork: a turn that recorded nothing surfaces None, not [].
    assert outcome.assumptions is None


async def test_resume_surfaces_pre_pause_assumptions_on_live_outcome() -> None:
    """resume() seed parity: assumptions recorded in window 1 (BEFORE an askUser
    pause) must surface on the RESUMED turn's live `TurnOutcome.assumptions`, so
    the live result matches what `project_history` reconstructs from the same
    trail. Proves `seed_assumptions=await self._compute_turn_assumptions(...)` in
    resume(). recordAssumptions is in an EARLIER round-trip than askUser (askUser
    short-circuits its own response, so a same-response record would never run)."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={"assumptions": ["Assumed the Sales department"]},
                    )
                ]
            ),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c2", name="askUser", arguments={"question": "Which period?"})
                ]
            ),
            ModelTurnResult(assistant_text="For Sales in 2026, the answer is 7."),
        ]
    )
    loop, store = _build_loop(model)

    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert paused.status == "paused_ask_user"
    # Best-effort: the pause return already carries the window-1 assumption.
    assert paused.assumptions == ["Assumed the Sales department"]

    resumed = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="2026")
    assert resumed.status == "done"
    # The seed rehydrated the pre-pause assumption onto the resumed live outcome.
    assert resumed.assumptions == ["Assumed the Sales department"]

    # And history reconstructs the SAME thing from the trail (live == history).
    doc = await store.get_or_create_session(SESSION_ID)
    body = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    assert body["turns"][0]["assumptions"] == ["Assumed the Sales department"]


async def test_recordassumptions_does_not_poison_answer_in_history_e2e() -> None:
    """REGRESSION (provenance-union carve-out): a turn whose ONLY tool call is
    recordAssumptions (ok + None provenance) must NOT collapse the turn's
    provenance union to None. If it did, the assistant answer would be tagged
    undetermined and DROPPED from history even under allow-all scope — losing the
    answer AND its assumptions on every use of the tool. This runs the REAL loop
    (so the union is computed live and the assistant message is persisted with its
    real provenance) and then reads it back through `project_history`, unlike the
    hand-crafted-`frozenset()` history unit tests, which cannot catch this."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={"assumptions": ["Assumed the current fiscal year"]},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="The answer is 42."),
        ]
    )
    loop, store = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q?")
    assert outcome.status == "done"
    assert outcome.assumptions == ["Assumed the current fiscal year"]

    # Read the ACTUAL persisted messages/trail back under allow-all scope.
    doc = await store.get_or_create_session(SESSION_ID)
    body = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    turn = body["turns"][0]
    # BOTH the answer and the assumptions must survive (non-None).
    assert turn["answer"] == "The answer is 42."
    assert turn["assumptions"] == ["Assumed the current fiscal year"]
    # The turn's provenance union is determined-empty (a pure recordAssumptions
    # turn read no warehouse data), NOT None — the carve-out held.
    assert turn["provenance_union"] == []


async def test_loop_single_recordassumptions_counts_as_exactly_one_tool_call() -> None:
    # The core seam: a runtime `recordAssumptions` is intercepted in the loop and
    # counts as EXACTLY one `tool_calls_made` (never dispatched to the MCP under
    # its own name), same discipline as any other runtime tool.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={"assumptions": ["Assumed the fiscal year is 2026"]},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Done."),
        ]
    )
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")
    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1
    assert outcome.assumptions == ["Assumed the fiscal year is 2026"]


async def test_loop_multiple_recordassumptions_calls_union_and_dedupe() -> None:
    # Two separate recordAssumptions calls across two model turns: the turn
    # accumulator UNIONS them, deduping by first-occurrence. Each call is its own
    # tool_calls_made increment (2), but the assumptions merge into one list.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={"assumptions": ["First assumption", "Shared assumption"]},
                    )
                ]
            ),
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c2",
                        name="recordAssumptions",
                        arguments={
                            "assumptions": ["Shared assumption", "Second assumption"]
                        },
                    )
                ]
            ),
            ModelTurnResult(assistant_text="Answer."),
        ]
    )
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")
    assert outcome.status == "done"
    assert outcome.tool_calls_made == 2
    # UNION across both calls, "Shared assumption" appears once (first-occurrence).
    assert outcome.assumptions == [
        "First assumption",
        "Shared assumption",
        "Second assumption",
    ]


async def test_loop_two_recordassumptions_calls_in_one_turn_union_and_dedupe() -> None:
    # Both recordAssumptions calls emitted in a SINGLE model turn (the schema says
    # "call once", but the loop must still union+dedupe if the model over-calls).
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={"assumptions": ["A", "B"]},
                    ),
                    ToolCallRequest(
                        id="c2",
                        name="recordAssumptions",
                        arguments={"assumptions": ["B", "C"]},
                    ),
                ]
            ),
            ModelTurnResult(assistant_text="Answer."),
        ]
    )
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")
    assert outcome.status == "done"
    assert outcome.tool_calls_made == 2
    assert outcome.assumptions == ["A", "B", "C"]


async def test_loop_empty_and_all_blank_recordassumptions_yields_null() -> None:
    # The `[] -> None` fork at the SEAM: a recordAssumptions call whose payload is
    # all-blank (cleans to `[]`) still counts as a tool call, but the turn's
    # `assumptions` remains None (not `[]`) since nothing landed in the accumulator.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="recordAssumptions",
                        arguments={"assumptions": ["", "   ", "\t"]},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="No assumptions."),
        ]
    )
    loop, _ = _build_loop(model)
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="q")
    assert outcome.status == "done"
    assert outcome.tool_calls_made == 1  # the call happened
    assert outcome.assumptions is None   # ...but nothing survived cleaning


# --- session_history answer-survival posture -------------------------------


def _rec_entry(assumptions: list[Any]) -> TrailEntry:
    return TrailEntry(
        turn_index=0,
        tool_call_id="a1",
        tool_name="recordAssumptions",
        args={"assumptions": assumptions},
        status="ok",
        error_code=None,
        provenance=None,  # the tool returns None provenance
        result_preview=None,
        result_full_ref=None,
        ts="t",
    )


def test_history_surfaces_assumptions_when_answer_survives() -> None:
    messages = [
        TurnMessage(turn_index=0, role="user", content="q", ts="t"),
        TurnMessage(
            turn_index=0, role="assistant", content="a", ts="t", provenance=frozenset()
        ),
    ]
    trail = [_rec_entry(["Assumed the fiscal year", "  "])]
    body = project_history(messages, trail, frozenset(), None)
    assert body["turns"][0]["assumptions"] == ["Assumed the fiscal year"]


def test_history_withholds_assumptions_when_answer_withheld() -> None:
    # Assistant answer has undetermined (None) provenance → dropped by the message
    # scope filter → its assumptions are withheld with it (assistant is None).
    messages = [
        TurnMessage(turn_index=0, role="user", content="q", ts="t"),
        TurnMessage(turn_index=0, role="assistant", content="a", ts="t", provenance=None),
    ]
    trail = [_rec_entry(["Assumed the fiscal year"])]
    body = project_history(messages, trail, frozenset({"db.t.c"}), None)
    turn = body["turns"][0]
    assert turn["answer"] is None
    assert turn["assumptions"] is None


# --- #5 SCOPE GATE (critical): assumptions inherit the ANSWER's scope fate -----

# Warehouse provenance pairs mirroring tests/runtime/test_session_history.py.
_SALARY = ("hr.employees", "salary")
_DEPT = ("hr.employees", "department")


def test_history_withholds_assumptions_when_answer_scope_dropped() -> None:
    # THE SCOPE-GATE ASSERTION: the assistant answer's provenance (salary) falls
    # OUTSIDE the caller's narrowed scope (department+id), so `filter_messages`
    # drops the answer. Its assumptions — even though the recordAssumptions trail
    # entry itself carries `None` provenance and is read from the RAW trail — must
    # inherit that fate and NOT surface. A leak here would expose a withheld
    # turn's model-authored reasoning under a narrowed scope.
    messages = [
        TurnMessage(turn_index=0, role="user", content="what is the salary?", ts="t"),
        TurnMessage(
            turn_index=0,
            role="assistant",
            content="Jane earns $85,000.",
            ts="t",
            provenance=frozenset({_SALARY}),
        ),
    ]
    trail = [_rec_entry(["'Active' was taken to mean currently-employed", "Used FY2026"])]
    scope = frozenset({"hr.employees.department", "hr.employees.id"})

    body = project_history(messages, trail, scope, None)

    turn = body["turns"][0]
    assert turn["question"] == "what is the salary?"  # user question always survives
    assert turn["answer"] is None                     # answer scope-dropped
    assert turn["provenance_union"] is None
    assert turn["assumptions"] is None                # ...assumptions withheld WITH it


def test_history_surfaces_assumptions_when_answer_survives_narrowed_scope() -> None:
    # The survive side of the gate under a *narrowed* (non allow-all) scope: the
    # answer's provenance (department) is IN scope, so the answer survives and its
    # assumptions surface — proving assumptions track the answer's survival, not
    # the recordAssumptions entry's own (None) provenance which `filter_trail`
    # would otherwise drop.
    messages = [
        TurnMessage(turn_index=0, role="user", content="headcount by dept?", ts="t"),
        TurnMessage(
            turn_index=0,
            role="assistant",
            content="Engineering 3, Sales 3.",
            ts="t",
            provenance=frozenset({_DEPT}),
        ),
    ]
    trail = [_rec_entry(["Counted only currently-active staff"])]
    scope = frozenset({"hr.employees.department"})

    body = project_history(messages, trail, scope, None)

    turn = body["turns"][0]
    assert turn["answer"] == "Engineering 3, Sales 3."
    assert turn["assumptions"] == ["Counted only currently-active staff"]


# --- #4 SSE serialization: `_outcome_to_dict` passes `assumptions` through ------


def test_outcome_to_dict_includes_assumptions_list_and_null() -> None:
    from data_agent.runtime.app import _outcome_to_dict
    from data_agent.runtime.loop.agent_loop import TurnOutcome

    with_list = _outcome_to_dict(
        TurnOutcome(
            status="done",
            assistant_text="a",
            pending_question=None,
            tool_calls_made=1,
            assumptions=["Assumed FY2026", "Assumed active = employed"],
        )
    )
    assert with_list["assumptions"] == ["Assumed FY2026", "Assumed active = employed"]

    # A turn that recorded nothing serializes `assumptions` to JSON null (the
    # `[] -> None` fork; an old client ignores the key either way).
    without = _outcome_to_dict(
        TurnOutcome(
            status="done",
            assistant_text="a",
            pending_question=None,
            tool_calls_made=0,
        )
    )
    assert without["assumptions"] is None


async def test_successful_call_reaches_the_model_as_its_real_confirmation() -> None:
    """REGRESSION: a SUCCESSFUL recordAssumptions must reach the model as its own
    `recorded: N` confirmation — NOT as the D94 withheld sentinel.

    The tool used to return `provenance=None` (UNDETERMINED). `scope_filter.
    filter_trail`'s current-turn exemption is status-gated to `status != "ok"`, so an
    `ok`+`None` entry was never exempt: it was dropped and re-materialised as the
    stranded sentinel. The model was shown

        "result withheld: provenance could not be determined for this call … Do not
         retry the identical call — it will be withheld again."

    in place of its confirmation on EVERY successful recording, immediately before
    writing its final answer. Asserted here under an ALLOW-ALL scope, because the bug
    was not scope-dependent — it fired on every call.
    """
    store = InMemorySessionStore()
    session_id = "sess-ra-render"
    await store.get_or_create_session(session_id)
    await store.append_message(
        session_id,
        TurnMessage(
            turn_index=0, role="user", content="How many hires?", ts="t0",
            provenance=frozenset(),
        ),
    )
    # Persist EXACTLY what the tool returns, through the real ToolResult.
    result = await RecordAssumptionsTool().run(
        {"assumptions": ["'Hired' was taken to mean the most recent hire date."]}, _creds()
    )
    await store.append_trail_entry(
        session_id,
        TrailEntry(
            turn_index=0,
            tool_call_id="ra1",
            tool_name="recordAssumptions",
            args={"assumptions": ["'Hired' was taken to mean the most recent hire date."]},
            status=result.status,
            error_code=result.error_code,
            provenance=result.provenance,
            result_preview=result.result_preview,
            result_full_ref=None,
            ts="t1",
        ),
    )

    assembled = await ContextAssembler(
        session_store=store,
        base_system_prompt="BASE",
        preview_row_count=20,
    ).assemble(session_id, frozenset(), current_turn_index=0)

    rendered = [m for m in assembled.messages if m.get("tool_name") == "recordAssumptions"]
    assert len(rendered) == 1, "the recordAssumptions entry must reach the model exactly once"
    entry = rendered[0]
    # THE POINT: a real result, not the sentinel.
    assert not entry.get("withheld_sentinel")
    assert "result withheld" not in json.dumps(entry)
    assert entry["status"] == "ok"
    assert entry["result_preview"]["preview_rows"] == [[1]]


async def _store_with_assumptions_at_turn0() -> InMemorySessionStore:
    """A 2-turn session whose turn 0 recorded an assumption and turn 1 is live."""
    store = InMemorySessionStore()
    await store.get_or_create_session(SESSION_ID)
    result = await RecordAssumptionsTool().run({"assumptions": [_SECRETISH]}, _creds())
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=0, role="user", content="Q0?", ts="t0", provenance=frozenset()),
    )
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0, tool_call_id="ra0", tool_name="recordAssumptions",
            args={"assumptions": [_SECRETISH]}, status=result.status,
            error_code=result.error_code, provenance=result.provenance,
            result_preview=result.result_preview, result_full_ref=None, ts="t1",
        ),
    )
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=0, role="assistant", content="A0.", ts="t2", provenance=frozenset()),
    )
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=1, role="user", content="Q1?", ts="t3", provenance=frozenset()),
    )
    return store


def _assemble(store: InMemorySessionStore, current_turn_index: int | None):
    return ContextAssembler(
        session_store=store, base_system_prompt="BASE",
        preview_row_count=20,
    ).assemble(SESSION_ID, frozenset(), current_turn_index=current_turn_index)


async def test_prior_turn_assumptions_never_re_enter_model_context() -> None:
    """Turn 0's assumption text must NOT be replayed into turn 1's context.

    An assumption is plain English but MAY encode a value ("employees earning above
    $100,000 were excluded"); by turn 1 the caller's `column_scope` may no longer
    cover the column it came from. Asserted on the TEXT, not just the entry, because
    the sentence rides in `args` — the old `None`-provenance mechanism withheld the
    RESULT while still rendering `args`, so an entry-only assertion would have passed
    against a design that leaked.
    """
    assembled = await _assemble(await _store_with_assumptions_at_turn0(), 1)

    blob = json.dumps(assembled.messages)
    assert _SECRETISH not in blob
    assert not [m for m in assembled.messages if m.get("tool_name") == "recordAssumptions"]
    # The rest of turn 0 still replays — this drops one entry, not the history.
    assert [m.get("content") for m in assembled.messages if m.get("role") == "user"] == ["Q0?", "Q1?"]


async def test_dropping_a_prior_turn_assumption_never_orphans_a_tool_call() -> None:
    """Both halves of the pair are synthesized from the SAME entry, so dropping it
    removes the assistant `tool_calls` and the `tool` result together. A leftover
    announcement with no result is an API 400 that poisons every round-trip."""
    assembled = await _assemble(await _store_with_assumptions_at_turn0(), 1)

    assert not [m for m in assembled.messages if m.get("tool_call_id") == "ra0"]
    assert not [m for m in assembled.messages if m.get("withheld_sentinel")]


async def test_strict_replay_with_no_current_turn_also_drops_assumptions() -> None:
    """`current_turn_index=None` is a strict replay with no live turn — every
    recordAssumptions entry is non-current, so it drops the same fail-safe way."""
    assembled = await _assemble(await _store_with_assumptions_at_turn0(), None)

    assert _SECRETISH not in json.dumps(assembled.messages)


async def test_the_drop_is_context_only_history_and_resume_still_see_them() -> None:
    """The drop is a RENDER-time rule over the replayed context. The paths that
    legitimately need the assumptions read the RAW persisted trail and must be
    untouched — otherwise the UI silently loses the `assumptions` field and a
    resumed window forgets what it already recorded."""
    store = await _store_with_assumptions_at_turn0()

    # (a) session_history — the UI's per-turn assumptions.
    doc = await store.get_or_create_session(SESSION_ID)
    body = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    assert body["turns"][0]["assumptions"] == [_SECRETISH]

    # (b) the raw trail still carries the entry for resume seeding.
    trail = await store.load_trail(SESSION_ID)
    assert [e.tool_name for e in trail] == ["recordAssumptions"]
    assert trail[0].args["assumptions"] == [_SECRETISH]
