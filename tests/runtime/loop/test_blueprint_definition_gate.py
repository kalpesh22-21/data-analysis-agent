"""The getBlueprint-before-runBlueprint gate, driven end-to-end through the real
`AgentLoop` (Release 1; recorded in
`docs/decisions/release-1/02-blueprint-card-enrichment.md`).

THE DEFECT THE GATE CLOSES. A blueprint card carries `intent`, `slots`, `resolves`
and `result_grain` — and NO SQL. So the model has been choosing and running
blueprints on the strength of an AUTHORED PROSE `intent` string; when that string
misdescribes the query underneath it, the wrong analysis runs and is reported
confidently. The D56 grain gate does not catch that: it verifies the result SHAPE
against the declared `result_grain`, never that the blueprint answers the question.

The gate is turn-scoped, satisfied only by a `getBlueprint` the model has ALREADY
SEEN (i.e. from a previous round-trip), refused before the executor is reached, and
structurally absent from the mid-DAG resume path — one test each below.

Fakes throughout: the two blueprint tools are recording doubles, so "the executor
never ran" is an assertion on a call log rather than an inference.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import (
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
)
from data_agent.runtime.dispatch.denial_mapping import classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolPause, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, TurnContext
from data_agent.runtime.loop.blueprint_gate import BLUEPRINT_DEFINITION_NOT_READ_CODE
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import (
    PauseCheckpoint,
    ResultPreview,
    TrailEntry,
    TurnMessage,
)

CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"EmployeeCode": "String"}})
SESSION_ID = "sess-bp-gate"
BP_A = "bp-active-headcount-by-department"
BP_B = "bp-average-salary-by-department"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "runQuery", "description": "", "parameters": {}}]


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="SECRET-JWT", column_scope=frozenset())


class _RecordingGetBlueprint:
    """`getBlueprint` — records each id and returns the FOUND shape."""

    def __init__(self, *, found: bool = True) -> None:
        self.calls: list[str] = []
        self._found = found

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        blueprint_id = str(model_args.get("id") or "")
        self.calls.append(blueprint_id)
        body: dict[str, Any] = (
            {"found": True, "id": blueprint_id, "sql_template": "SELECT 1"}
            if self._found
            else {"found": False}
        )
        return ToolResult(
            status="ok",
            tool_name="getBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full=body,
        )


class _RecordingRunBlueprint:
    """`runBlueprint` — records every id that REACHED the executor.

    A refused call must never appear here: the gate sits above dispatch, so the
    assertion is "the executor was not invoked", not "the result looked wrong"."""

    def __init__(self, *, pause: ToolPause | None = None) -> None:
        self.calls: list[str] = []
        self._pause = pause

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        blueprint_id = str(model_args.get("id") or "")
        self.calls.append(blueprint_id)
        if self._pause is not None:
            return ToolResult(
                status="ok",
                tool_name="runBlueprint",
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=frozenset(),
                result_preview=None,
                result_full=None,
                pause=self._pause,
            )
        return ToolResult(
            status="ok",
            tool_name="runBlueprint",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset({("dbpcm_warehouse.employee", "EmployeeCode")}),
            result_preview=ResultPreview(
                columns=["headcount"], row_count=1, truncated=False, preview_rows=[[7]]
            ),
            result_full={"blueprint_id": blueprint_id, "terminal_sql": "SELECT 7"},
            authoritative=True,
        )


def _build(
    *,
    model: ScriptedModelClient,
    get_tool: Any = None,
    run_tool: Any = None,
    blueprint_executor: Any = None,
    store: InMemorySessionStore | None = None,
    observer: Any = None,
    request_token_budget: int | None = None,
    pinned_recent_tool_pairs: int = 3,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = store or InMemorySessionStore()
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=ContextAssembler(store),
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={
            k: v
            for k, v in (("getBlueprint", get_tool), ("runBlueprint", run_tool))
            if v is not None
        },
        blueprint_executor=blueprint_executor,
        request_token_budget=request_token_budget,
        request_budget_pinned_recent_tool_pairs=pinned_recent_tool_pairs,
        **({"observer": observer} if observer is not None else {}),
    )
    return loop, store


def _run_call(call_id: str, blueprint_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        id=call_id, name="runBlueprint", arguments={"id": blueprint_id, "slot_bindings": {}}
    )


def _get_call(call_id: str, blueprint_id: str) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="getBlueprint", arguments={"id": blueprint_id})


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    def payloads(self, event: str) -> list[dict[str, Any]]:
        return [p for name, p in self.events if name == event]


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


async def test_run_blueprint_without_get_blueprint_is_refused_before_the_executor() -> None:
    """The core case: no `getBlueprint` this turn -> refused, retryable, the
    executor never invoked, and the ONLY trail entry for the call is the refusal
    (there is no entry for a run that did not happen)."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="ok, expanding it first next time."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert outcome.status == "done"
    # The executor was never reached.
    assert run_tool.calls == []
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.status, e.error_code) for e in trail] == [
        ("runBlueprint", "error", BLUEPRINT_DEFINITION_NOT_READ_CODE)
    ]
    entry = trail[0]
    # RETRYABLE, per the denial table the replay path re-derives from.
    assert classify_denial(entry.error_code).retryable is True
    # The model-facing channel is `denial_detail` (TrailEntry has no
    # `user_message`), and it must NAME the blueprint and the exact fix.
    assert entry.denial_detail is not None
    assert BP_A in entry.denial_detail
    assert f"Call getBlueprint('{BP_A}') first, then run it" in entry.denial_detail
    # Determined-empty provenance: the refusal read no warehouse data, and a `None`
    # here would collapse the turn's fail-closed union.
    assert entry.provenance == frozenset()


async def test_refusal_emits_a_loop_prefixed_observer_event() -> None:
    """`loop_`-prefixed so `guardrail_observer` exports it to Phoenix, carrying the
    corpus-authored blueprint id (D25) and nothing else about the request."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    recorder = _Recorder()
    loop, _store = _build(
        model=model,
        get_tool=_RecordingGetBlueprint(),
        run_tool=_RecordingRunBlueprint(),
        observer=recorder,
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    payloads = recorder.payloads("loop_blueprint_definition_not_read")
    assert payloads == [
        {
            "tool_name": "runBlueprint",
            "blueprint_id": BP_A,
            "reason": "no_get_blueprint_this_turn",
        }
    ]


async def test_the_refusal_event_survives_the_guardrail_attribute_allowlist() -> None:
    """`blueprint_id` must be in `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`, or the
    exported span carries the refusal with no way to tell WHICH blueprint."""
    from data_agent.runtime.observability.tracing import _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST

    for key in ("tool_name", "blueprint_id", "reason"):
        assert key in _GUARDRAIL_OBSERVER_ATTR_ALLOWLIST


# ---------------------------------------------------------------------------
# What satisfies the gate
# ---------------------------------------------------------------------------


async def test_a_prior_get_blueprint_for_the_same_id_lets_it_run() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert outcome.status == "done"
    assert run_tool.calls == [BP_A]
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.status) for e in trail] == [("getBlueprint", "ok"), ("runBlueprint", "ok")]


async def test_get_blueprint_for_a_different_id_does_not_satisfy_the_gate() -> None:
    """The gate is per-id. Expanding B tells the model nothing about A's SQL."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_B)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert run_tool.calls == []
    trail = await store.load_trail(SESSION_ID)
    assert trail[-1].error_code == BLUEPRINT_DEFINITION_NOT_READ_CODE
    assert BP_A in (trail[-1].denial_detail or "")


async def test_a_get_blueprint_from_a_prior_turn_does_not_satisfy_the_gate() -> None:
    """TURN-SCOPED, like every other memory in this release. Context is rebuilt per
    turn and trimmed by `fit_request_to_budget`, so a definition fetched in turn 1
    may have been summarized away by turn 5 — the rule is about what the model can
    read RIGHT NOW.

    The accepted cost is one extra `getBlueprint` per turn for a follow-up that
    re-runs the same blueprint with a different slot value."""
    store = InMemorySessionStore()
    await store.append_message(
        SESSION_ID, TurnMessage(turn_index=0, role="user", content="turn 0", ts="2026-01-01T00:00:00Z")
    )
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="g0",
            tool_name="getBlueprint",
            args={"id": BP_A},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="2026-01-01T00:00:01Z",
        ),
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(
        model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool, store=store
    )

    # turn_index 1 — the seeded getBlueprint belongs to turn 0.
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="and now Engineering?")

    assert run_tool.calls == []
    trail = await store.load_trail(SESSION_ID)
    assert trail[-1].error_code == BLUEPRINT_DEFINITION_NOT_READ_CODE


async def test_a_get_blueprint_from_an_earlier_window_of_the_same_turn_satisfies_it() -> None:
    """The seed comes from the PERSISTED trail, not from in-memory state, so the
    D45 per-round-trip rebuild and a budget-window resume cannot lose it. Modelled
    as a pre-seeded same-turn entry: `run()` computes `turn_index=0` for a session
    with no messages, which is the turn the entry belongs to."""
    store = InMemorySessionStore()
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="g0",
            tool_name="getBlueprint",
            args={"id": BP_A},
            status="ok",
            error_code=None,
            provenance=frozenset(),
            result_preview=None,
            result_full_ref=None,
            ts="2026-01-01T00:00:01Z",
        ),
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(
        model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool, store=store
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert run_tool.calls == [BP_A]


async def test_a_failed_get_blueprint_does_not_satisfy_the_gate() -> None:
    """Only a SUCCESSFUL expansion counts — an errored one returned no definition,
    exactly as `seen_read_calls` only records `ok` reads."""

    class _FailingGetBlueprint:
        async def run(
            self,
            model_args: dict[str, Any],
            credentials: RuntimeCredentials,
            turn: TurnContext | None = None,
        ) -> ToolResult:
            return ToolResult(
                status="error",
                tool_name="getBlueprint",
                error_code="RETRIEVAL_TOOL_INTERNAL_ERROR",
                retryable=False,
                user_message="boom",
                provenance=frozenset(),
                result_preview=None,
                result_full=None,
            )

    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_FailingGetBlueprint(), run_tool=run_tool)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert run_tool.calls == []
    trail = await store.load_trail(SESSION_ID)
    assert trail[-1].error_code == BLUEPRINT_DEFINITION_NOT_READ_CODE


async def test_same_response_get_then_run_is_still_refused() -> None:
    """`[getBlueprint(x), runBlueprint(x)]` in ONE message does not satisfy the
    gate. The result of the first call does not reach the model until the next
    round-trip, so a mid-batch fold would pass a model that is still blind to the
    SQL — the exact failure the gate exists to stop. The `getBlueprint` itself
    still succeeds, so the cost is the one round-trip the model owed anyway."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A), _run_call("b1", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b2", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    # Refused in round 1, allowed in round 2.
    assert run_tool.calls == [BP_A]
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.error_code) for e in trail] == [
        ("getBlueprint", None),
        ("runBlueprint", BLUEPRINT_DEFINITION_NOT_READ_CODE),
        ("runBlueprint", None),
    ]


# ---------------------------------------------------------------------------
# Cost: the batched shape stays 2 round-trips for N deliverables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("deliverables", [2, 3])
async def test_batched_expand_then_run_costs_two_round_trips_for_n_deliverables(
    deliverables: int,
) -> None:
    """The round-trip claim in the prompt and in 02's record of the trade,
    measured rather than asserted in prose: N `getBlueprint`s in one response, N
    `runBlueprint`s in the next, nothing refused."""
    ids = [f"bp-{n}" for n in range(deliverables)]
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[_get_call(f"g{n}", bp) for n, bp in enumerate(ids)]
            ),
            ModelTurnResult(
                tool_calls=[_run_call(f"b{n}", bp) for n, bp in enumerate(ids)]
            ),
            ModelTurnResult(assistant_text="here are all of them."),
        ]
    )
    get_tool = _RecordingGetBlueprint()
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=get_tool, run_tool=run_tool)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="three asks")

    assert outcome.status == "done"
    assert get_tool.calls == ids
    assert run_tool.calls == ids
    # Two tool-bearing round-trips, plus the final prose one. NOT 2N.
    assert len(model.calls) == 3
    trail = await store.load_trail(SESSION_ID)
    assert [e.error_code for e in trail] == [None] * (2 * deliverables)


# ---------------------------------------------------------------------------
# Resume paths
# ---------------------------------------------------------------------------


async def test_a_mid_dag_resume_is_not_gated() -> None:
    """A mid-DAG checkpoint resume re-enters `blueprint_executor.resume(...)`
    DIRECTLY from `_resume_blueprint` and appends its own `runBlueprint` trail
    entry — it never reaches the dispatch site the gate lives at, so it is
    structurally ungated rather than exempted by a branch.

    That is the correct outcome (a resume continues an already-gated invocation
    rather than deciding to run a blueprint), and this test pins it so a refactor
    that routes resumes through dispatch cannot silently start gating them and
    strand a paused DAG. NOTE the loop is built with NO `getBlueprint` in the trail
    at all, so the gate would fire if the path went through it."""
    from data_agent.runtime.blueprint.executor import ExecCompleted

    class _ResumingExecutor:
        def __init__(self) -> None:
            self.resumed: list[str] = []

        async def resume(self, **kwargs: Any) -> ExecCompleted:
            self.resumed.append(str(kwargs["blueprint_id"]))
            return ExecCompleted(
                result_full={"blueprint_id": BP_A, "terminal_sql": "SELECT 7"},
                preview=ResultPreview(
                    columns=["headcount"], row_count=1, truncated=False, preview_rows=[[7]]
                ),
                provenance=frozenset({("dbpcm_warehouse.employee", "EmployeeCode")}),
            )

    store = InMemorySessionStore()
    await store.append_message(
        SESSION_ID,
        TurnMessage(turn_index=0, role="user", content="headcount?", ts="2026-01-01T00:00:00Z"),
    )
    await store.write_pause_checkpoint(
        SESSION_ID,
        PauseCheckpoint(
            reason="blueprint_approval",
            pending_question={"question": "Approve the scan?", "options": ["yes", "no"]},
            awaiting="user_answer",
            consumed=False,
            budget_window_count=1,
            blueprint_id=BP_A,
            slot_bindings_json="{}",
            completed_nodes_json="[]",
            awaiting_node=1,
        ),
    )
    executor = _ResumingExecutor()
    model = ScriptedModelClient([ModelTurnResult(assistant_text="7 people.")])
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(
        model=model,
        get_tool=_RecordingGetBlueprint(),
        run_tool=run_tool,
        blueprint_executor=executor,
        store=store,
    )

    outcome = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="yes")

    assert outcome.status == "done"
    assert executor.resumed == [BP_A]
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.status, e.error_code) for e in trail] == [("runBlueprint", "ok", None)]


async def test_a_slot_pause_resume_re_running_the_blueprint_is_not_refused() -> None:
    """The OTHER pause flavour (`awaiting_node is None`) DOES come back through the
    model loop, so the model re-issues `runBlueprint` and the gate does see it.

    It passes because the gate seeds from the persisted trail and `turn_index` is
    stable across a pause/resume: the `getBlueprint` the gate forced BEFORE the
    pause is still this turn's. Without the trail seed the resumed window would
    start with an empty in-memory set and refuse work the model had already done."""
    pause = ToolPause(
        reason="blueprint_slot",
        pending_question={"question": "Which department?", "options": None},
        blueprint_id=BP_A,
        slot_bindings_json="{}",
        completed_nodes_json=None,
        awaiting_node=None,
    )
    pausing_run = _RecordingRunBlueprint(pause=pause)
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
        ]
    )
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=pausing_run)
    paused = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")
    assert paused.status == "paused_ask_user"

    # The resume re-enters the model loop; the model re-issues the run.
    settled_run = _RecordingRunBlueprint()
    resumed_model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_run_call("b2", BP_A)]),
            ModelTurnResult(assistant_text="7 people in Sales."),
        ]
    )
    resumed_loop, store = _build(
        model=resumed_model,
        get_tool=_RecordingGetBlueprint(),
        run_tool=settled_run,
        store=store,
    )
    outcome = await resumed_loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="Sales")

    assert outcome.status == "done"
    assert settled_run.calls == [BP_A]
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.error_code) for e in trail] == [
        ("getBlueprint", None),
        ("runBlueprint", None),
    ]


# ---------------------------------------------------------------------------
# The gate does not disturb anything else
# ---------------------------------------------------------------------------


async def test_a_blank_blueprint_id_is_not_gated_it_is_the_executor_s_error() -> None:
    """Nothing to name and nothing to expand — the invalid-args path owns it, and
    the gate must not convert a malformed call into a misleading "expand it first"."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="b1", name="runBlueprint", arguments={"id": "  ", "slot_bindings": {}}
                    )
                ]
            ),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="?")

    assert run_tool.calls == ["  "]
    trail = await store.load_trail(SESSION_ID)
    assert trail[0].error_code != BLUEPRINT_DEFINITION_NOT_READ_CODE


# ---------------------------------------------------------------------------
# The dedup guard: `getBlueprint` is now an IDEMPOTENT_READ_TOOL
# ---------------------------------------------------------------------------


async def test_get_blueprint_is_a_guarded_idempotent_read() -> None:
    """It is a keyed fetch by id, so it satisfies `read_guard`'s own criterion —
    result depends only on the arguments — as well as `getTableSchema` does. It was
    added when the always-expand-before-run rule made it the most repeated read of a
    blueprint turn."""
    from data_agent.runtime.loop.read_guard import (
        IDEMPOTENT_READ_TOOLS,
        idempotent_read_signature,
    )

    assert "getBlueprint" in IDEMPOTENT_READ_TOOLS
    # The signature key is the tool name + canonicalized args; `getBlueprint`'s only
    # argument is `id`, so two fetches of the same blueprint collide and two of
    # different blueprints do not.
    assert idempotent_read_signature("getBlueprint", {"id": BP_A}) == idempotent_read_signature(
        "getBlueprint", {"id": BP_A}
    )
    assert idempotent_read_signature("getBlueprint", {"id": BP_A}) != idempotent_read_signature(
        "getBlueprint", {"id": BP_B}
    )


async def test_a_duplicate_get_blueprint_is_guarded_not_re_dispatched() -> None:
    """The second identical expansion is served locally with the data-free nudge,
    and the guard event names the blueprint (corpus-authored, D25-safe) rather than
    rendering an empty `dedup_target`."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_get_call("g2", BP_A)]),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    get_tool = _RecordingGetBlueprint()
    recorder = _Recorder()
    loop, store = _build(
        model=model, get_tool=get_tool, run_tool=_RecordingRunBlueprint(), observer=recorder
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    # Fetched ONCE; the repeat never reached the tool.
    assert get_tool.calls == [BP_A]
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.error_code) for e in trail] == [
        ("getBlueprint", None),
        ("getBlueprint", IDEMPOTENT_READ_ALREADY_SERVED_CODE),
    ]
    payload = recorder.payloads("loop_repeated_idempotent_read_guarded")[0]
    assert payload["blueprint_id"] == BP_A
    assert payload["dedup_target"] == BP_A
    assert payload["deduped"] is True


async def test_a_dedup_guarded_get_blueprint_satisfies_the_run_gate() -> None:
    """Being deduped means the definition is already in context, which is exactly
    what the gate asks. This is the OPPOSITE of 04 condition 5, which rejects the
    same marker as completion evidence — that gate asks whether WORK happened."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_get_call("g2", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert run_tool.calls == [BP_A]
    trail = await store.load_trail(SESSION_ID)
    assert trail[-1].error_code is None


async def test_the_guarded_marker_alone_satisfies_the_gate() -> None:
    """The deadlock this closes, isolated: the ONLY `getBlueprint` entry the window
    seeds from is a guard marker (the entry it deduped belongs to a window whose
    in-memory state is gone). It must still satisfy the gate — a marker means the
    model HAS the definition, so refusing here would leave the model with no move
    that can ever work."""
    store = InMemorySessionStore()
    await store.append_trail_entry(
        SESSION_ID,
        TrailEntry(
            turn_index=0,
            tool_call_id="g-marker",
            tool_name="getBlueprint",
            args={"id": BP_A},
            status="ok",
            error_code=IDEMPOTENT_READ_ALREADY_SERVED_CODE,
            provenance=None,
            result_preview=None,
            result_full_ref=None,
            ts="2026-01-01T00:00:01Z",
        ),
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    run_tool = _RecordingRunBlueprint()
    loop, store = _build(
        model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool, store=store
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert run_tool.calls == [BP_A]


async def test_expand_refused_run_defensive_re_expand_then_run_succeeds() -> None:
    """The full deadlock shape. Expand; the run is refused for an UNRELATED reason
    (the executor rejects a missing slot); the model defensively re-expands and gets
    a data-free marker; it runs again. Without the marker satisfying the gate the
    third round could never succeed."""

    class _SlotComplainingRunBlueprint(_RecordingRunBlueprint):
        async def run(
            self,
            model_args: dict[str, Any],
            credentials: RuntimeCredentials,
            turn: TurnContext | None = None,
        ) -> ToolResult:
            result = await super().run(model_args, credentials, turn)
            if len(self.calls) == 1:
                return ToolResult(
                    status="error",
                    tool_name="runBlueprint",
                    error_code="RUN_BLUEPRINT_SLOT_INVALID",
                    retryable=True,
                    user_message="that slot value is not valid",
                    provenance=frozenset(),
                    result_preview=None,
                    result_full=None,
                )
            return result

    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(tool_calls=[_get_call("g2", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b2", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    run_tool = _SlotComplainingRunBlueprint()
    loop, store = _build(model=model, get_tool=_RecordingGetBlueprint(), run_tool=run_tool)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert outcome.status == "done"
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.error_code) for e in trail] == [
        ("getBlueprint", None),
        ("runBlueprint", "RUN_BLUEPRINT_SLOT_INVALID"),
        ("getBlueprint", IDEMPOTENT_READ_ALREADY_SERVED_CODE),
        ("runBlueprint", None),
    ]
    # Never refused by the definition gate — the marker carried it.
    assert all(e.error_code != BLUEPRINT_DEFINITION_NOT_READ_CODE for e in trail)


async def test_a_get_blueprint_whose_result_was_trimmed_away_is_re_fetched_for_real() -> None:
    """The tension the two mechanisms create, closed.

    The guard's premise is "the already-served result is in the history above"; the
    gate's turn-scoping exists because `fit_request_to_budget` may have trimmed it
    OUT of the rebuilt window. Combined naively, a model whose definition was
    trimmed re-fetches, gets a data-free "you already have this", and then runs a
    blueprint whose SQL it can no longer read.

    So a `getBlueprint` repeat is exempted from the guard when the entry that served
    it is no longer visible. Driven through the REAL trim here — a request token
    budget with zero pinned recent tool pairs, so the round-1 pair is dropped from
    round 2's rebuild — rather than by faking an absent id."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_get_call("g2", BP_A)]),
            ModelTurnResult(tool_calls=[_run_call("b1", BP_A)]),
            ModelTurnResult(assistant_text="7 people."),
        ]
    )
    get_tool = _RecordingGetBlueprint()
    run_tool = _RecordingRunBlueprint()
    recorder = _Recorder()
    loop, store = _build(
        model=model,
        get_tool=get_tool,
        run_tool=run_tool,
        observer=recorder,
        request_token_budget=1,
        pinned_recent_tool_pairs=0,
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    # The round-1 pair was trimmed out of round 2's window, so the repeat
    # RE-DISPATCHED instead of being answered with a data-free nudge.
    assert get_tool.calls == [BP_A, BP_A]
    assert recorder.payloads("loop_repeated_idempotent_read_guarded") == []
    allowed = recorder.payloads("loop_trimmed_read_refetch_allowed")
    assert len(allowed) == 1
    assert allowed[0]["tool_name"] == "getBlueprint"
    assert allowed[0]["blueprint_id"] == BP_A
    assert allowed[0]["reason"] == "result_not_readable_in_window"
    assert allowed[0]["deduped"] is False
    # ...and the run still went through: the gate is satisfied by the expansion.
    assert run_tool.calls == [BP_A]


async def test_a_visible_get_blueprint_repeat_is_still_guarded() -> None:
    """The other side of the exemption: when the definition IS still in the window,
    the repeat is deduped exactly as before, so the exemption cannot quietly become
    "never guard getBlueprint"."""
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_get_call("g1", BP_A)]),
            ModelTurnResult(tool_calls=[_get_call("g2", BP_A)]),
            ModelTurnResult(assistant_text="done."),
        ]
    )
    get_tool = _RecordingGetBlueprint()
    recorder = _Recorder()
    loop, _store = _build(
        model=model,
        get_tool=get_tool,
        run_tool=_RecordingRunBlueprint(),
        observer=recorder,
        request_token_budget=1_000_000,
    )

    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="headcount?")

    assert get_tool.calls == [BP_A]
    assert len(recorder.payloads("loop_repeated_idempotent_read_guarded")) == 1
    assert recorder.payloads("loop_trimmed_read_refetch_allowed") == []
