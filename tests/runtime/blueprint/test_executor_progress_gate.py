"""A blueprint is ONE step in the UI: no internal dispatch may reach progress.

`BlueprintExecutor` runs its slot domain probes, its node queries and its D56
grain probe through the SHARED `ToolDispatcher`, whose `tool_dispatch_*` observer
events are UI progress labels (`observability/progress.py::_STEP_LABELS`). Left
ungated, one `runBlueprint` painted the user "running runQuery…" once per
internal node — telling them their single saved analysis is really N SQL queries
over internal tables, which is exactly the internal structure they must not see.

Two harnesses, deliberately:
  - a STUB dispatcher, which proves the executor passes `emit_progress=False` on
    EVERY internal dispatch (the mechanism), including a path a real dispatcher
    would never reach;
  - the REAL `ToolDispatcher` with a recording observer, which proves the
    resulting event stream carries nothing the UI would render (the outcome) —
    the assertion that survives a refactor of how the flag is spelled.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.observability.progress import to_progress_event
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.models import ResultPreview

_E = "dbpcm_warehouse.employee"
_DEPT_COL = f"{_E}.Department"
_SAL_COL = f"{_E}.AnnualSalary"
_CODE_COL = f"{_E}.EmployeeCode"

CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
        }
    }
)

_AVG_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)
_COMPANY_AVG_SQL = "SELECT AVG(AnnualSalary) AS company_avg FROM dbpcm_warehouse.employee"
_ABOVE_AVG_SQL = (
    "SELECT Department AS department FROM dbpcm_warehouse.employee "
    "GROUP BY Department HAVING AVG(AnnualSalary) > {company_avg}"
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-bp-progress", jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    *,
    sql_template: str | None = _AVG_SQL,
    composes: list[dict[str, Any]] | None = None,
    slots: list[dict[str, Any]] | None = None,
    result_grain: Any = None,
    bid: str = "bp-progress-gate",
) -> BlueprintDetail:
    return BlueprintDetail(
        id=bid,
        intent="Average annual salary by department",
        slots_summary="department",
        uses=frozenset({_DEPT_COL, _SAL_COL, _CODE_COL}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        sql_template=sql_template,
        composes=composes,
        result_grain=result_grain if result_grain is not None else ["Department"],
    )


def _index(detail: BlueprintDetail) -> FakeVectorIndex:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return index


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, dict(payload)))

    @property
    def names(self) -> list[str]:
        return [name for name, _payload in self.events]


class _StubDispatcher:
    """Records `(tool_name, emit_progress)` per call and returns scripted rows."""

    def __init__(self, raws: list[dict[str, Any]]) -> None:
        self._raws = list(raws)
        self.dispatches: list[tuple[str, bool]] = []

    async def dispatch(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        *,
        emit_progress: bool = True,
    ) -> ToolResult:
        self.dispatches.append((tool_name, emit_progress))
        assert self._raws, "stub dispatcher ran out of scripted results"
        raw = self._raws.pop(0)
        return ToolResult(
            status="ok",
            tool_name=tool_name,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset({(_E, "Department")}),
            result_preview=ResultPreview(
                columns=raw["columns"],
                row_count=raw["row_count"],
                truncated=False,
                preview_rows=raw["rows"],
            ),
            result_full=raw,
        )


async def test_single_node_execution_gates_every_internal_dispatch() -> None:
    """Slot domain probe, node query and grain probe — all three, not just the node."""
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}]
    )
    dispatcher = _StubDispatcher(
        [
            _rq(["Department"], [["Warehouse"], ["Sales"]]),  # slot domain probe
            _rq(["department", "avg_salary"], [["Warehouse", 50000.0]]),  # node
            _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert len(dispatcher.dispatches) == 3
    assert dispatcher.dispatches == [("runQuery", False)] * 3


async def test_composed_execution_gates_every_node_and_the_grain_probe() -> None:
    detail = _detail(
        sql_template=None,
        composes=[
            {"order": 0, "output": {"company_avg": "scalar"}, "sql_template": _COMPANY_AVG_SQL},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"company_avg": "$0.company_avg"},
                "sql_template": _ABOVE_AVG_SQL,
                "output": {},
            },
        ],
    )
    dispatcher = _StubDispatcher(
        [
            _rq(["company_avg"], [[55000.0]]),  # node 0
            _rq(["department"], [["Sales"], ["Eng"]]),  # node 1 (terminal)
            _rq(["__bp_n", "__bp_d"], [[2, 2]]),  # grain probe
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))

    outcome = await executor.execute(blueprint_id=detail.id, slot_bindings={}, credentials=_creds())

    assert isinstance(outcome, ExecCompleted)
    assert len(dispatcher.dispatches) == 3
    assert all(emit_progress is False for _tool, emit_progress in dispatcher.dispatches)


async def test_no_ui_progress_event_escapes_a_real_dispatched_execution() -> None:
    """The outcome, over the REAL dispatcher: every event the execution produced
    renders to nothing in the UI (`to_progress_event` -> None).

    `blueprint_step` events still fire — they have no `_STEP_LABELS` entry and are
    the executor's own internal telemetry, which is why the assertion is on what
    the UI would RENDER rather than on the events being absent.
    """
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}]
    )
    observer = _RecordingObserver()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"], ["Sales"]]),
                _rq(["department", "avg_salary"], [["Warehouse", 50000.0]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG, observer=observer),
        vector_index=_index(detail),
        observer=observer,
    )

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert len(mcp.calls) == 3  # three real inner queries ran...
    assert [name for name in observer.names if name.startswith("tool_dispatch_")] == []
    rendered = [to_progress_event(name, payload) for name, payload in observer.events]
    assert all(event is None for event in rendered), (
        f"a blueprint-internal step reached the UI: {[e for e in rendered if e is not None]}"
    )


async def test_the_outer_run_blueprint_dispatch_still_shows_progress() -> None:
    """The gate is per call: the tool the MODEL called is still narrated, so a
    turn running a blueprint is never silent."""
    observer = _RecordingObserver()
    dispatcher = ToolDispatcher(
        FakeMCPClient(scripted={"runBlueprint": [{"status": "verified"}]}),
        CATALOG,
        observer=observer,
    )

    await dispatcher.dispatch("runBlueprint", {"id": "bp-progress-gate"}, _creds())

    assert observer.names == ["tool_dispatch_start", "tool_dispatch_ok"]
    assert [to_progress_event(name, payload) for name, payload in observer.events] != [None, None]
