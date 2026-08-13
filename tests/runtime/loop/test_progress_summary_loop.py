"""Loop integration for the opt-in LLM progress-summary feature (Layer 1, all fakes).

With a summarizer wired, a tool dispatch emits BOTH the instant
`tool_dispatch_start` template label AND an additive, value-rich
`tool_progress_summary` event (fired concurrently, never awaited before
dispatch). A SLOW summarizer never blocks the tool or the turn result. With no
summarizer (the default), the loop emits neither the extra event nor calls the
summarizer — byte-identical to before the feature.
"""

from __future__ import annotations

import asyncio

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.observability.progress import to_progress_event
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String"}})
SECRET_JWT = "eyJ.secret.sig"
SESSION_ID = "sess-progress-summary"

TOOLS_SCHEMA = [
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=frozenset())


class _FakeSummarizer:
    """Records each call; returns a canned line (or awaits an event to stall)."""

    def __init__(self, *, line: str | None = "Querying employee codes", stall: bool = False) -> None:
        self._line = line
        self._stall = stall
        self.calls: list[tuple[str, dict]] = []
        # Captured (from inside the fire-and-forget task) so a test can assert the
        # straggler was actually cancelled at turn end.
        self.task: asyncio.Task | None = None

    async def summarize(self, tool_name: str, arguments: dict) -> str | None:
        self.task = asyncio.current_task()
        self.calls.append((tool_name, dict(arguments)))
        if self._stall:
            # Never resolves within the turn — proves dispatch is not awaited on it.
            await asyncio.sleep(3600)
        return self._line


def _build_loop(
    *,
    model_client: ScriptedModelClient,
    mcp_client: FakeMCPClient,
    observer,
    progress_summarizer=None,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp_client, CATALOG, observer=observer)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    loop = AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        observer=observer,
        progress_summarizer=progress_summarizer,
    )
    return loop, store


def _query_then_done() -> ScriptedModelClient:
    return ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="runQuery",
                        arguments={"sql": "SELECT EmployeeCode FROM employee"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="All done."),
        ]
    )


def _run_query_mcp() -> FakeMCPClient:
    return FakeMCPClient(
        scripted={
            "runQuery": [
                {"columns": ["EmployeeCode"], "rows": [["E1"]], "row_count": 1, "truncated": False}
            ]
        }
    )


async def test_dispatch_emits_both_template_label_and_summary() -> None:
    events: list[tuple[str, dict]] = []
    summarizer = _FakeSummarizer(line="Querying employee codes")
    loop, _store = _build_loop(
        model_client=_query_then_done(),
        mcp_client=_run_query_mcp(),
        observer=lambda e, p: events.append((e, dict(p))),
        progress_summarizer=summarizer,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="show codes"
    )
    # Let the fire-and-forget summary task (already scheduled) settle if needed.
    await asyncio.sleep(0)

    assert outcome.status == "done"
    # The summarizer saw the CALL (name + args), not the result.
    assert summarizer.calls == [("runQuery", {"sql": "SELECT EmployeeCode FROM employee"})]

    names = [e for e, _ in events]
    # The instant template label still fires (unchanged).
    assert "tool_dispatch_start" in names
    # AND the additive value-rich summary event fired.
    assert "tool_progress_summary" in names

    summary_payload = next(p for e, p in events if e == "tool_progress_summary")
    assert summary_payload["summary"] == "Querying employee codes"
    assert summary_payload["tool_name"] == "runQuery"
    # It maps to a verbatim, value-rich progress line (bypassing _STEP_LABELS).
    progress = to_progress_event("tool_progress_summary", summary_payload)
    assert progress is not None
    assert progress.step == "Querying employee codes"
    assert progress.shape == {"tool_name": "runQuery"}


async def test_slow_summarizer_does_not_block_dispatch_or_turn() -> None:
    events: list[tuple[str, dict]] = []
    mcp = _run_query_mcp()
    summarizer = _FakeSummarizer(stall=True)
    loop, _store = _build_loop(
        model_client=_query_then_done(),
        mcp_client=mcp,
        observer=lambda e, p: events.append((e, dict(p))),
        progress_summarizer=summarizer,
    )

    # A stalling summarizer must NOT prevent the tool from dispatching or the turn
    # from completing — the whole run resolves promptly despite the 3600s stall.
    outcome = await asyncio.wait_for(
        loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="show codes"),
        timeout=5.0,
    )

    assert outcome.status == "done"
    # The tool DID dispatch (not gated on the summary).
    assert [c.tool_name for c in mcp.calls] == ["runQuery"]
    # The summarizer was invoked, but its (cancelled) stall produced no summary event.
    assert summarizer.calls == [("runQuery", {"sql": "SELECT EmployeeCode FROM employee"})]
    assert "tool_dispatch_start" in [e for e, _ in events]
    assert "tool_progress_summary" not in [e for e, _ in events]

    # Lifecycle: the loop drained its task set at turn end (no leak) …
    assert loop._summary_tasks == set()
    # … and the stalled straggler task was actually cancelled (let the requested
    # cancellation propagate one tick, then assert it ended cancelled — not merely
    # abandoned still-running).
    await asyncio.sleep(0)
    assert summarizer.task is not None
    assert summarizer.task.cancelled()


async def test_raising_observer_on_summary_emit_does_not_break_turn() -> None:
    # The emit of `tool_progress_summary` is guarded inside the loop, so an
    # observer that RAISES on it must not surface into the turn — the turn still
    # completes normally (the fire-and-forget task swallows the observer error).
    def _observer(event: str, _payload: dict) -> None:
        if event == "tool_progress_summary":
            raise RuntimeError("boom in emit")

    summarizer = _FakeSummarizer(line="Querying employee codes")
    loop, _store = _build_loop(
        model_client=_query_then_done(),
        mcp_client=_run_query_mcp(),
        observer=_observer,
        progress_summarizer=summarizer,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="show codes"
    )
    await asyncio.sleep(0)

    assert outcome.status == "done"
    assert outcome.assistant_text == "All done."
    # The emit was attempted (summarizer ran) but its raise was swallowed …
    assert summarizer.calls == [("runQuery", {"sql": "SELECT EmployeeCode FROM employee"})]
    # … and cleanup still drained the task set.
    assert loop._summary_tasks == set()


async def test_a_raising_summarizer_does_not_break_the_turn() -> None:
    """`ProgressSummarizer.summarize` swallows its own failures, but the loop must
    not DEPEND on that: this task is fire-and-forget, so an exception escaping any
    summarizer implementation would surface as an un-retrieved task exception (and,
    with a stricter handler, as a failed turn). The guard in `_summarize_and_emit`
    is asserted with a summarizer that raises outright."""

    class _RaisingSummarizer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def summarize(self, tool_name: str, arguments: dict) -> str | None:
            self.calls.append(tool_name)
            raise RuntimeError("summarizer exploded")

    events: list[tuple[str, dict]] = []
    summarizer = _RaisingSummarizer()
    loop, _store = _build_loop(
        model_client=_query_then_done(),
        mcp_client=_run_query_mcp(),
        observer=lambda e, p: events.append((e, dict(p))),
        progress_summarizer=summarizer,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="show codes"
    )
    await asyncio.sleep(0)

    assert outcome.status == "done"
    assert outcome.assistant_text == "All done."
    assert summarizer.calls == ["runQuery"]
    # Nothing was emitted — not even a fallback line (the failure is silent).
    assert "tool_progress_summary" not in [e for e, _ in events]
    # The instant template label still carried the turn.
    assert "tool_dispatch_start" in [e for e, _ in events]
    assert loop._summary_tasks == set()


async def test_no_summarizer_means_no_summary_events_and_no_calls() -> None:
    events: list[tuple[str, dict]] = []
    loop, _store = _build_loop(
        model_client=_query_then_done(),
        mcp_client=_run_query_mcp(),
        observer=lambda e, p: events.append((e, dict(p))),
        progress_summarizer=None,
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="show codes"
    )
    await asyncio.sleep(0)

    assert outcome.status == "done"
    assert "tool_dispatch_start" in [e for e, _ in events]
    assert "tool_progress_summary" not in [e for e, _ in events]
