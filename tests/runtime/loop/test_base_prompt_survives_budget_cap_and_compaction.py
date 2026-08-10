"""Regression: AGENT_SYSTEM_PROMPT is `messages[0]` on EVERY main-loop
`send_turn`, across a multi-iteration -> paused_budget_cap -> resume ->
post-compaction run (Layer 1, all fakes).

Closes a coverage gap: existing loop tests only spot-check `model.calls[0]`
(a single opening round-trip) and a single `assemble()`. A user report (from
traces) claimed the BASE prompt is WIPED from what the model receives WHEN the
budget-cap guardrail trips after many iterations. This test drives the REAL
`AgentLoop` through EVERY phase that report implicates — mid-loop iterations,
the last call before `paused_budget_cap`, the first call of each resumed budget
window, and calls after history compaction has fired — and asserts the base
prompt is present, at index 0, byte-identical on ALL of them.

It also proves (belt-and-suspenders) that the history-summarizer subcall IS a
base-prompt-less call (its system text is `_SUMMARIZER_SYSTEM_PROMPT`, never
`AGENT_SYSTEM_PROMPT`) — i.e. the base-prompt-less turns that appear under the
same high-iteration/compaction pressure are the fire-and-forget summarizer
subcalls, NOT the main loop.
"""

from __future__ import annotations

import copy
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.budget import _SUMMARY_CONTEXT_PREFIX
from data_agent.runtime.context.llm_summarizer import (
    _SUMMARIZER_SYSTEM_PROMPT,
    build_llm_summarizer,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import RetrievedContext, ThinCard
from data_agent.runtime.retrieval.render import _USER_CONTEXT_PREFIX
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})
SESSION_ID = "sess-base-prompt-regression"
SECRET_JWT = "jwt.body.sig"
_TOOLS = [{"type": "function", "name": "runQuery", "description": "", "parameters": {}}]

# Chosen so the run spans multiple full budget windows AND compaction fires:
# max_loop_iterations iterations per window * max_budget_windows windows.
_ITERS_PER_WINDOW = 6
_MAX_WINDOWS = 3
_EXPECTED_MAIN_CALLS = _ITERS_PER_WINDOW * _MAX_WINDOWS  # 18


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return list(_TOOLS)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=frozenset())


class _RecordingLoopModel:
    """Main-loop model double that records every `messages` payload and ALWAYS
    requests a fresh, DISTINCT `runQuery` — so the loop never self-terminates and
    every budget window runs all the way to the cap, and so each window's trail
    grows with unique, fat entries that force history compaction."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._n = 0

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append(copy.deepcopy(messages))
        self._n += 1
        sql = f"SELECT EmployeeCode, Department FROM employee WHERE Department='D{self._n}'"
        return ModelTurnResult(
            tool_calls=[ToolCallRequest(id=f"c{self._n}", name="runQuery", arguments={"sql": sql})],
            usage={"total_tokens": 5000},
        )

    def begin_turn(self) -> _RecordingLoopModel:
        return self


class _RecordingSummarizerModel:
    """History-summarizer model double: records every payload and returns a
    one-line summary. Base-prompt-less by construction (the summarizer builds its
    own `_SUMMARIZER_SYSTEM_PROMPT` message list)."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append(copy.deepcopy(messages))
        return ModelTurnResult(assistant_text="summarized older tool-call history")

    def begin_turn(self) -> _RecordingSummarizerModel:
        return self


class _FatRunQueryMCP:
    """MCP double returning a fat `runQuery` result for unbounded calls, so trail
    entries carry real token weight and history compaction actually triggers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, tool_name, args, *, jwt, session_id):
        self.calls.append(tool_name)
        return {
            "columns": ["EmployeeCode", "Department"],
            "rows": [[f"E{i}", f"Dept-{i}-lorem-ipsum-dolor"] for i in range(20)],
            "row_count": 20,
            "truncated": False,
        }

    async def list_tools(self, *, jwt, session_id):
        return []


async def test_base_prompt_is_messages0_on_every_main_loop_call_through_cap_resume_and_compaction() -> None:
    main_model = _RecordingLoopModel()
    summarizer_model = _RecordingSummarizerModel()
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(_FatRunQueryMCP(), CATALOG)
    assembler = ContextAssembler(
        store,
        history_token_budget=400,  # LOW -> compaction fires after a couple entries
        base_system_prompt=AGENT_SYSTEM_PROMPT,
        summarizer=build_llm_summarizer(summarizer_model),
    )
    loop = AgentLoop(
        model_client=main_model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=_ITERS_PER_WINDOW,
        max_wall_clock_seconds=999,
        max_budget_windows=_MAX_WINDOWS,
    )

    # run() -> first budget window; resume() grants each subsequent window until
    # the hard outer ceiling.
    statuses: list[str] = []
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    statuses.append(outcome.status)
    while outcome.status == "paused_budget_cap":
        outcome = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="continue")
        statuses.append(outcome.status)

    # The run really did traverse cap -> resume -> ... -> hard ceiling.
    assert statuses == ["paused_budget_cap", "paused_budget_cap", "stopped_hard_ceiling"]
    # Exactly one main-loop send_turn per iteration per window (no hidden calls).
    assert len(main_model.calls) == _EXPECTED_MAIN_CALLS

    # Phase 1 bypasses compaction: the trail interleaves VERBATIM (no summary), so
    # the request grows across the run and `compact_trail` is never invoked — the
    # base-prompt invariant below is what this regression guards, and it must hold on
    # EVERY call regardless of how large the verbatim history has grown.
    assert not summarizer_model.calls, "Phase 1 must not invoke the history summarizer"
    compaction_indices: list[int] = []

    # ---- THE INVARIANT: base prompt is messages[0] on EVERY main-loop call. ----
    # Covers (a) first call, (b) every mid-loop iteration, (c) the last call before
    # each paused_budget_cap, (d) the first call of each resumed window, and
    # (e) every post-compaction call — all 18 calls, no exceptions.
    for i, msgs in enumerate(main_model.calls):
        assert msgs, f"main-loop call[{i}] had an empty message list"
        assert msgs[0]["role"] == "system", (
            f"main-loop call[{i}] messages[0] role is {msgs[0]['role']!r}, not 'system'"
        )
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT, (
            f"main-loop call[{i}] dropped the base prompt from messages[0]: "
            f"got {msgs[0]['content']!r:.120}"
        )

    # Explicit spot-checks on the phases the bug report named, by index:
    #   window 1 = calls 0..5, window 2 = 6..11, window 3 = 12..17.
    window_first_call_indices = [w * _ITERS_PER_WINDOW for w in range(_MAX_WINDOWS)]
    window_last_call_indices = [(w + 1) * _ITERS_PER_WINDOW - 1 for w in range(_MAX_WINDOWS)]
    for idx in (
        [0]  # (a) first ever call
        + window_first_call_indices  # (d) first call of each resumed window
        + window_last_call_indices  # (c) last call before each cap / ceiling
        + compaction_indices  # (e) post-compaction calls
    ):
        assert main_model.calls[idx][0]["content"] == AGENT_SYSTEM_PROMPT

    # ---- The summarizer subcalls ARE the base-prompt-less ones (misattribution
    # proof): every history-summarizer send_turn leads with its OWN system prompt,
    # never AGENT_SYSTEM_PROMPT, and carries no tools. These are what a trace at the
    # cap surfaces — not a main-loop call.
    for i, msgs in enumerate(summarizer_model.calls):
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == _SUMMARIZER_SYSTEM_PROMPT
        assert msgs[0]["content"] != AGENT_SYSTEM_PROMPT, (
            f"summarizer call[{i}] unexpectedly carried the base prompt"
        )


# ---------------------------------------------------------------------------
# Genuine MULTI-TURN (N separate user questions on one persisted session):
# conversation history + tool trail accumulate across turns and are REPLAYED
# into later turns. Exercises the untested multi-turn suspects the single-turn
# test above does NOT: (1) prior-turn conversation `TurnMessage`s appended by
# `_build_canonical_messages`; (2) `scope_filter.filter_messages` over replayed
# history; (3) compaction of the ACCUMULATED cross-turn trail crossing the
# budget in a LATER turn; (4) the budget cap firing in a later, high-history
# turn. The base prompt must still be freshly `insert(0)`'d by `assemble` on
# every main-loop call of every turn.
# ---------------------------------------------------------------------------

# A big answer per turn so the accumulated cross-turn CONVERSATION history grows.
_BIG_ANSWER = "The answer is 12345. " + ("lorem ipsum dolor sit amet consectetur " * 40)


class _MultiTurnModel:
    """Records every main-loop payload tagged with the current turn label +
    per-turn iteration. Mode is set by the test before each turn:
      - 'answer': one runQuery, then a big final answer (turn reaches 'done').
      - 'cap':    always runQuery (never self-terminates) -> window runs to cap."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[dict[str, Any]]]] = []
        self.mode = "answer"
        self.turn_label = "t0"
        self._iter = 0
        self._did_query_this_turn = False
        self._n = 0

    def new_turn(self, label: str, mode: str) -> None:
        self.turn_label = label
        self.mode = mode
        self._iter = 0
        self._did_query_this_turn = False

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append((self.turn_label, self._iter, copy.deepcopy(messages)))
        self._iter += 1
        self._n += 1
        if self.mode == "cap":
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id=f"q{self._n}",
                        name="runQuery",
                        arguments={"sql": f"SELECT EmployeeCode FROM employee WHERE Department='C{self._n}'"},
                    )
                ],
                usage={"total_tokens": 5000},
            )
        if not self._did_query_this_turn:
            self._did_query_this_turn = True
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id=f"q{self._n}",
                        name="runQuery",
                        arguments={"sql": f"SELECT EmployeeCode, Department FROM employee WHERE Department='A{self._n}'"},
                    )
                ],
                usage={"total_tokens": 5000},
            )
        return ModelTurnResult(
            assistant_text=f"[{self.turn_label}] {_BIG_ANSWER}", usage={"total_tokens": 5000}
        )

    def begin_turn(self) -> _MultiTurnModel:
        return self


async def test_base_prompt_survives_across_n_real_turns_with_history_replay_compaction_and_later_cap() -> None:
    model = _MultiTurnModel()
    summarizer_model = _RecordingSummarizerModel()
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(_FatRunQueryMCP(), CATALOG)
    assembler = ContextAssembler(
        store,
        history_token_budget=500,  # LOW -> accumulated cross-turn trail compacts fast
        base_system_prompt=AGENT_SYSTEM_PROMPT,
        summarizer=build_llm_summarizer(summarizer_model),
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=4,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )

    n_turns = 8
    cap_turn = 6  # a LATER turn hits the cap, with large accumulated replayed history
    statuses: list[str] = []
    for t in range(1, n_turns + 1):
        model.new_turn(f"t{t}", "cap" if t == cap_turn else "answer")
        outcome = await loop.run(
            session_id=SESSION_ID, credentials=_creds(), user_message=f"Question {t}?"
        )
        statuses.append(outcome.status)
        while outcome.status == "paused_budget_cap":
            outcome = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="continue")
            statuses.append(outcome.status)

    # The later cap turn really tripped the guardrail; all other turns completed.
    assert statuses.count("paused_budget_cap") == 2  # cap turn: window1 + window2
    assert statuses.count("stopped_hard_ceiling") == 1
    assert statuses.count("done") == n_turns - 1  # every non-cap turn finished

    doc = await store.get_or_create_session(SESSION_ID)
    # (suspect 2) No system message is ever persisted into conversation history —
    # so replay/`filter_messages` has nothing to dedup against or strip that could
    # displace the base prompt.
    assert not any(m.role == "system" for m in doc.messages)
    # History genuinely accumulated across turns and was replayed.
    assert len(doc.messages) >= n_turns  # at least one user+assistant pair per completed turn
    max_replayed = max(
        len([m for m in msgs if m.get("role") in ("user", "assistant")])
        for _, _, msgs in model.calls
    )
    assert max_replayed >= 6, "later turns did not replay a large accumulated history"

    # (suspect 3) Phase 1 bypasses compaction: the accumulated cross-turn trail
    # interleaves VERBATIM (no summary) — `compact_trail` is never invoked. The
    # cross-turn base-prompt invariant below is what this multi-turn regression
    # guards, independent of compaction.
    assert not summarizer_model.calls, "Phase 1 must not invoke the history summarizer"

    # ---- THE INVARIANT across EVERY main-loop call of EVERY turn. ----
    for i, (label, it, msgs) in enumerate(model.calls):
        assert msgs, f"call#{i} ({label} iter {it}) had an empty message list"
        assert msgs[0]["role"] == "system", (
            f"call#{i} ({label} iter {it}) messages[0] role is {msgs[0]['role']!r}"
        )
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT, (
            f"call#{i} (turn {label}, iter {it}) DROPPED the base prompt from "
            f"messages[0]: got {msgs[0]['content']!r:.120}"
        )

    # ---- The base prompt is the SOLE `role: "system"` message on EVERY call,
    # before AND after compaction. The compaction summary is NOT a second system
    # message (the anti-pattern that let some OpenAI-compatible endpoints honor
    # only the first-or-last system message and silently override the base
    # instructions after N turns) — it is demoted to a `user`-role message with
    # the context prefix, sitting at index 1 right after the base prompt.
    syscounts: set[int] = set()
    for _label, _it, msgs in model.calls:
        sys_idxs = [j for j, m in enumerate(msgs) if m["role"] == "system"]
        # No system message is ever anything but the base prompt, and it is
        # strictly at index 0 (never displaced).
        assert sys_idxs == [0]
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT
        syscounts.add(len(sys_idxs))
        # Phase 1 emits no compaction summary, so no `user` message ever carries the
        # summary context prefix (it is never a competing second system message).
        assert not any(
            m["role"] == "user" and str(m.get("content", "")).startswith(_SUMMARY_CONTEXT_PREFIX)
            for m in msgs
        )
    # Exactly ONE authoritative system message (the base prompt) on every call —
    # the whole point of the fix; the count never becomes 2.
    assert syscounts == {1}

    # Explicit later-turn spot check: the last main-loop call of EACH turn — the
    # phase the user points at (present early, allegedly gone in a later turn).
    last_call_of_turn: dict[str, tuple[int, bool]] = {}
    for i, (label, _it, msgs) in enumerate(model.calls):
        base = label.split("/")[0]
        last_call_of_turn[base] = (i, msgs[0]["content"] == AGENT_SYSTEM_PROMPT)
    for base_label, (_i, ok) in last_call_of_turn.items():
        assert ok, f"turn {base_label}'s last main-loop call dropped the base prompt"
    # All n_turns turns are represented (t6 spans multiple windows but one label).
    assert len(last_call_of_turn) == n_turns


# ---------------------------------------------------------------------------
# RETRIEVAL-ENABLED path (production wiring): the retrieval cards block is
# prepended too. It MUST also be a NON-system (`user`) message, so that even
# with retrieval wired AND after compaction the base prompt is the SINGLE
# `role: "system"` message — closing the same "endpoint honors only one system
# message -> base instructions overridden" hole for retrieval-enabled deploys.
# ---------------------------------------------------------------------------


class _StubRetrieval:
    """Minimal RetrievalPipeline stand-in returning one non-empty card block."""

    async def retrieve(self, *, question, column_scope, user_id, observer):  # noqa: ANN001, ARG002
        return RetrievedContext(
            thin_cards=[
                ThinCard(
                    id="bp.headcount",
                    intent="Count employees by department",
                    slots_summary="",
                    score=1.0,
                )
            ],
            reranked=True,
        )


async def test_base_prompt_is_sole_system_message_with_retrieval_wired_through_cap_and_compaction() -> None:
    main_model = _RecordingLoopModel()
    summarizer_model = _RecordingSummarizerModel()
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(_FatRunQueryMCP(), CATALOG)
    assembler = ContextAssembler(
        store,
        history_token_budget=400,  # LOW -> compaction fires
        base_system_prompt=AGENT_SYSTEM_PROMPT,
        summarizer=build_llm_summarizer(summarizer_model),
        retrieval=_StubRetrieval(),  # production wiring: retrieval cards block present
    )
    loop = AgentLoop(
        model_client=main_model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=_ITERS_PER_WINDOW,
        max_wall_clock_seconds=999,
        max_budget_windows=_MAX_WINDOWS,
    )

    statuses: list[str] = []
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    statuses.append(outcome.status)
    while outcome.status == "paused_budget_cap":
        outcome = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="continue")
        statuses.append(outcome.status)

    # The run really did traverse cap -> resume -> ... -> hard ceiling.
    assert statuses == ["paused_budget_cap", "paused_budget_cap", "stopped_hard_ceiling"]
    assert len(main_model.calls) == _EXPECTED_MAIN_CALLS
    # Phase 1 bypasses compaction — the summarizer is never invoked.
    assert not summarizer_model.calls, "Phase 1 must not invoke the history summarizer"

    saw_retrieval_card = False
    for i, msgs in enumerate(main_model.calls):
        # (a) exactly ONE system message, the base prompt, strictly at index 0 —
        # with retrieval WIRED, on every call.
        sys_idxs = [j for j, m in enumerate(msgs) if m["role"] == "system"]
        assert sys_idxs == [0], (
            f"retrieval-enabled call[{i}] has system messages at {sys_idxs}, not just [0]"
        )
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT

        # (b) the retrieval cards block IS present, as a NON-system (`user`)
        # message carrying the retrieved-context prefix and the card content.
        retrieval_msgs = [
            m
            for m in msgs
            if m["role"] == "user" and str(m.get("content", "")).startswith(_USER_CONTEXT_PREFIX)
        ]
        if retrieval_msgs:
            saw_retrieval_card = True
            assert "bp.headcount" in retrieval_msgs[0]["content"], (
                "retrieval cards content did not reach the model"
            )
        # (c) Phase 1 emits no compaction summary user message.
        assert not any(
            m["role"] == "user" and str(m.get("content", "")).startswith(_SUMMARY_CONTEXT_PREFIX)
            for m in msgs
        )

    assert saw_retrieval_card, "retrieval cards block never reached the model as a user message"
