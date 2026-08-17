"""Adversarial D47/D55 budget-termination coverage at the full `AgentLoop` level
(QA hardening pass).

`tests/runtime/loop/test_agent_loop.py` already proves the core budget-cap
pause -> exactly-one-fresh-window-per-continue -> hard-ceiling-force-stop
sequence with `max_loop_iterations=1`. This file adds the harsher cases the
QA brief calls out explicitly:

  - A `ScriptedModelClient` that requests a tool call FOREVER (an
    unboundedly long script) never actually loops unboundedly — the guard
    fires at exactly the configured cap, proven with a boundary value > 1 so
    the "exactly at the cap, not before/after" property is meaningfully
    tested (a cap of 1 cannot distinguish "off by one" from "correct").
  - The wall-clock cap (not just the iteration cap) terminates the loop via
    an injected deterministic clock, even with an effectively-infinite
    iteration budget — proving the loop is bounded by wall-clock alone.
  - After the hard outer ceiling (`max_budget_windows`) is hit, the runtime
    must NOT "offer another continue": this is checked not just narratively
    (no `pending_question`) but operationally — attempting to `resume()`
    again after a hard-ceiling stop must raise `AlreadyConsumedError`,
    proving there is no live checkpoint left to consume.
"""

from __future__ import annotations

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.store import AlreadyConsumedError

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String"}})
SESSION_ID = "sess-budget-adversarial"

TOOLS_SCHEMA = [
    {"type": "function", "name": "listDatabases", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="jwt-not-under-test", column_scope=frozenset())


class _FakeClock:
    """A controllable monotonic clock — advances by a fixed step on every call
    (i.e. every time `BudgetGuard.exceeded` is checked), so wall-clock
    exhaustion is deterministic and does not depend on real elapsed time."""

    def __init__(self, *, step: float = 0.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


def _forever_tool_call_turn(call_id: str) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id=call_id, name="listDatabases", arguments={})],
        usage={"total_tokens": 1},
    )


def _build_loop(
    *,
    model_client: ScriptedModelClient,
    mcp_client: FakeMCPClient,
    store: InMemorySessionStore,
    max_loop_iterations: int,
    max_wall_clock_seconds: float,
    max_budget_windows: int,
    clock=None,
) -> AgentLoop:
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    assembler = ContextAssembler(store)
    kwargs = dict(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=max_wall_clock_seconds,
        max_budget_windows=max_budget_windows,
    )
    if clock is not None:
        kwargs["clock"] = clock
    return AgentLoop(**kwargs)


async def test_iteration_cap_fires_at_exact_boundary_not_before_or_after() -> None:
    """max_loop_iterations=4: the guard must fire on the 4th tool-call
    round-trip, never the 3rd (early) nor the 5th (late/unbounded)."""
    n = 4
    # Script enough "forever" turns to prove it would keep going past n if not
    # capped — but the loop must stop consuming the script at exactly n.
    model = ScriptedModelClient([_forever_tool_call_turn(f"c{i}") for i in range(n + 10)])
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "db"}] for _ in range(n + 10)]})
    loop = _build_loop(
        model_client=model,
        mcp_client=mcp,
        store=InMemorySessionStore(),
        max_loop_iterations=n,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    assert outcome.status == "paused_budget_cap"
    assert outcome.tool_calls_made == n  # exactly n dispatches, not n-1, not n+1
    assert len(model.calls) == n  # exactly n model round-trips this window


async def test_wall_clock_cap_terminates_loop_even_with_huge_iteration_budget() -> None:
    """A near-infinite `max_loop_iterations` must NOT let the loop run
    forever — the wall-clock cap alone must bound it, proven with a
    deterministic injected clock (no real sleeping)."""
    model = ScriptedModelClient([_forever_tool_call_turn(f"c{i}") for i in range(50)])
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "db"}] for _ in range(50)]})
    # Clock advances 5 seconds every time it is sampled; wall_clock cap is 12s
    # -> BudgetGuard.exceeded first becomes True on the 3rd check (t=10s is
    # still < 12s at the 2nd check's *start* sample, but record_iteration
    # doesn't sample the clock — .exceeded does, so it fires once elapsed
    # since window-start >= 12s).
    clock = _FakeClock(step=5.0)
    loop = _build_loop(
        model_client=model,
        mcp_client=mcp,
        store=InMemorySessionStore(),
        max_loop_iterations=1_000_000,  # effectively unbounded by iteration count alone
        max_wall_clock_seconds=12.0,
        max_budget_windows=3,
        clock=clock,
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    assert outcome.status == "paused_budget_cap"
    # Bounded — nowhere near the 50-entry script, let alone unbounded.
    assert len(model.calls) < 50
    assert outcome.tool_calls_made < 50


async def test_hard_ceiling_leaves_no_live_checkpoint_to_resume() -> None:
    """After the outer hard ceiling force-stops the turn, the design states
    the runtime must NOT "offer another continue". Proven operationally: a
    resume() attempt after the hard-ceiling stop must raise
    AlreadyConsumedError (there is no live, unconsumed checkpoint left)."""
    model = ScriptedModelClient(
        [_forever_tool_call_turn("c1"), _forever_tool_call_turn("c2"), _forever_tool_call_turn("c3")]
    )
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "db"}] for _ in range(3)]})
    store = InMemorySessionStore()
    loop = _build_loop(
        model_client=model,
        mcp_client=mcp,
        store=store,
        max_loop_iterations=1,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )

    window1 = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")
    assert window1.status == "paused_budget_cap"
    window2 = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="continue")
    assert window2.status == "paused_budget_cap"
    window3 = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="continue")
    assert window3.status == "stopped_hard_ceiling"
    assert window3.pending_question is None

    try:
        await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="continue")
        raise AssertionError(
            "expected AlreadyConsumedError — the hard ceiling must not leave a "
            "resumable checkpoint (no further 'continue' may be offered)"
        )
    except AlreadyConsumedError:
        pass

    # No further model round-trips were triggered by the rejected resume.
    assert len(model.calls) == 3


async def test_never_more_than_max_budget_windows_worth_of_model_calls() -> None:
    """A pathological script that would keep requesting tool calls
    indefinitely must still be bounded by max_budget_windows * max_loop_iterations
    total model round-trips across the whole turn+resume chain — never more."""
    max_iterations = 2
    max_windows = 3
    ceiling = max_iterations * max_windows
    # Provide far more scripted turns than the ceiling allows.
    model = ScriptedModelClient([_forever_tool_call_turn(f"c{i}") for i in range(ceiling + 20)])
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "db"}] for _ in range(ceiling + 20)]})
    store = InMemorySessionStore()
    loop = _build_loop(
        model_client=model,
        mcp_client=mcp,
        store=store,
        max_loop_iterations=max_iterations,
        max_wall_clock_seconds=999,
        max_budget_windows=max_windows,
    )

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")
    for _ in range(max_windows):  # drive every possible "continue" resume
        if outcome.status != "paused_budget_cap":
            break
        outcome = await loop.resume(session_id=SESSION_ID, credentials=_credentials(), answer="continue")

    assert outcome.status == "stopped_hard_ceiling"
    assert len(model.calls) == ceiling  # never exceeds windows * iterations-per-window
