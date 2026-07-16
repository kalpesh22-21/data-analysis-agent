"""QA Layer-1: end-to-end bind of the windowed-period slot types (§2/§3).

Drives a `relative_window` template to a bound SQL string (an `INTERVAL {n} MONTH`
NUMBER literal) and a `period_range` template to two STRING-literal bounds — both
through the SINGLE-node leaf path AND the DAG `_resolve_all_slots` path. The
injection canary: a hostile `period_range` start never produces executable SQL
(the ISO regex rejects it → PAUSE, no dispatch). Fail-closed SLOT_INVALID when a
`period_range` template references only one of the two bounds (a dropped bound is a
dropped filter, the D56 wrong-answer class).

All fakes, no infra. ADD-only; does not modify the reviewer-owned executor tests.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
_HIRE_COL = f"{_E}.MostRecentHireDate"
_CODE_COL = f"{_E}.EmployeeCode"

CATALOG = CatalogHandle(
    {_E: {"EmployeeCode": "String", "MostRecentHireDate": "Nullable(Date)"}}
)

_USES = frozenset({_HIRE_COL, _CODE_COL})

# A trailing "last N months" template — the {window_months} slot binds as the
# INTEGER inside `INTERVAL {n} MONTH` (a NUMBER literal, never a string).
_WINDOW_SQL = (
    "SELECT toStartOfMonth(MostRecentHireDate) AS month, "
    "COUNT(DISTINCT EmployeeCode) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE MostRecentHireDate >= toStartOfMonth(now()) - INTERVAL {window_months} MONTH "
    "GROUP BY toStartOfMonth(MostRecentHireDate)"
)
_WINDOW_SLOT = {"name": "window_months", "type": "relative_window", "required": True,
                "min_value": 1, "max_value": 36}

# An explicit {start,end} template — the ONE period_range slot expands to the TWO
# tokens {hire_window_start}/{hire_window_end}, each a STRING literal.
_RANGE_SQL = (
    "SELECT toStartOfMonth(MostRecentHireDate) AS month, "
    "COUNT(DISTINCT EmployeeCode) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE MostRecentHireDate >= {hire_window_start} "
    "AND MostRecentHireDate < {hire_window_end} "
    "GROUP BY toStartOfMonth(MostRecentHireDate)"
)
_RANGE_SLOT = {"name": "hire_window", "type": "period_range", "required": True}

_INJECTION = "2026-01-01' OR '1'='1"


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-win", jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _single_detail(*, sql_template: str, slots: list[dict[str, Any]],
                   bid: str = "bp-win") -> BlueprintDetail:
    return BlueprintDetail(
        id=bid,
        intent="hires over a window",
        slots_summary="",
        uses=_USES,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        sql_template=sql_template,
        composes=None,
        result_grain=[],  # empty grain → verify skipped, isolates the bind assertion
    )


def _dag_detail(*, node_sql: str, slots: list[dict[str, Any]],
                bid: str = "bp-win-dag") -> BlueprintDetail:
    # Route through the DAG path (`_resolve_all_slots`): no top-level sql_template,
    # a single terminal query node carrying the window template.
    return BlueprintDetail(
        id=bid,
        intent="hires over a window (dag)",
        slots_summary="",
        uses=_USES,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        sql_template=None,
        composes=[{"order": 0, "output": {}, "sql_template": node_sql}],
        result_grain=[],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=index)


# ===========================================================================
# relative_window → INTERVAL <n> MONTH as a NUMBER literal
# ===========================================================================


async def test_single_node_relative_window_binds_number_interval() -> None:
    detail = _single_detail(sql_template=_WINDOW_SQL, slots=[_WINDOW_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["month", "hires"], [["2026-01-01", 3]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win", slot_bindings={"window_months": 6}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted), outcome
    node_sql = mcp.calls[0].args["sql"]
    # 6 binds as a bare NUMBER literal inside the INTERVAL — not a quoted string.
    assert "INTERVAL 6 MONTH" in node_sql
    assert "INTERVAL '6'" not in node_sql
    assert "'6'" not in node_sql
    assert "{window_months}" not in node_sql


async def test_single_node_relative_window_digit_string_binds_number() -> None:
    # A PURE-digit string still binds as a number literal into the INTERVAL.
    detail = _single_detail(sql_template=_WINDOW_SQL, slots=[_WINDOW_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["month", "hires"], [["2026-01-01", 3]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win", slot_bindings={"window_months": "6"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted), outcome
    assert "INTERVAL 6 MONTH" in mcp.calls[0].args["sql"]


async def test_single_node_relative_window_unit_phrase_pauses() -> None:
    # H2: "6 months" carries a UNIT the template already fixes (INTERVAL {n} MONTH) —
    # binding a leading 6 and dropping "months"/"weeks" would risk a unit mismatch.
    # The resolver asks instead → PAUSE before any node dispatches (no wrong answer).
    detail = _single_detail(sql_template=_WINDOW_SQL, slots=[_WINDOW_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win", slot_bindings={"window_months": "6 weeks"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecPaused), outcome
    assert mcp.calls == []


async def test_single_node_relative_window_out_of_range_pauses() -> None:
    # 99 > max_value 36 → AskUser → PAUSE before any node dispatches.
    detail = _single_detail(sql_template=_WINDOW_SQL, slots=[_WINDOW_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win", slot_bindings={"window_months": 99}, credentials=_creds()
    )
    assert isinstance(outcome, ExecPaused)
    assert mcp.calls == []


async def test_dag_relative_window_binds_number_interval() -> None:
    detail = _dag_detail(node_sql=_WINDOW_SQL, slots=[_WINDOW_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["month", "hires"], [["2026-01-01", 3]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win-dag", slot_bindings={"window_months": 6}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted), outcome
    node_sql = mcp.calls[0].args["sql"]
    assert "INTERVAL 6 MONTH" in node_sql
    assert "'6'" not in node_sql


# ===========================================================================
# period_range → two STRING literals ({name}_start / {name}_end)
# ===========================================================================


async def test_single_node_period_range_binds_two_string_literals() -> None:
    detail = _single_detail(sql_template=_RANGE_SQL, slots=[_RANGE_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["month", "hires"], [["2026-01-01", 3]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win",
        slot_bindings={"hire_window": {"start": "2026-01-01", "end": "2026-03-31"}},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted), outcome
    node_sql = mcp.calls[0].args["sql"]
    # Both bounds bind as quoted STRING literals; the single token expanded to two.
    assert "'2026-01-01'" in node_sql
    assert "'2026-03-31'" in node_sql
    assert "{hire_window_start}" not in node_sql
    assert "{hire_window_end}" not in node_sql


async def test_dag_period_range_binds_two_string_literals() -> None:
    detail = _dag_detail(node_sql=_RANGE_SQL, slots=[_RANGE_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["month", "hires"], [["2026-01-01", 3]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win-dag",
        slot_bindings={"hire_window": ["2026-01-01", "2026-03-31"]},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecCompleted), outcome
    node_sql = mcp.calls[0].args["sql"]
    assert "'2026-01-01'" in node_sql
    assert "'2026-03-31'" in node_sql


# ===========================================================================
# Injection canary — a hostile period_range start never produces executable SQL
# ===========================================================================


async def test_single_node_period_range_injection_never_dispatches_sql() -> None:
    detail = _single_detail(sql_template=_RANGE_SQL, slots=[_RANGE_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})  # nothing may dispatch
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win",
        slot_bindings={"hire_window": {"start": _INJECTION, "end": "2026-03-31"}},
        credentials=_creds(),
    )
    # The ISO regex rejects the injection → AskUser → PAUSE. No SQL is ever built.
    assert isinstance(outcome, ExecPaused)
    assert mcp.calls == []
    assert not any("OR '1'='1" in c.args["sql"] for c in mcp.calls)


async def test_dag_period_range_injection_never_dispatches_sql() -> None:
    detail = _dag_detail(node_sql=_RANGE_SQL, slots=[_RANGE_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win-dag",
        slot_bindings={"hire_window": [_INJECTION, "2026-03-31"]},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecPaused)
    assert mcp.calls == []


# ===========================================================================
# Fail-closed SLOT_INVALID — a period_range template referencing only one bound
# ===========================================================================

# A template that references only {hire_window_start} — the dropped {hire_window_end}
# bound would be a dropped filter (D56 wrong-answer class) → SLOT_INVALID.
_ONE_BOUND_SQL = (
    "SELECT toStartOfMonth(MostRecentHireDate) AS month, "
    "COUNT(DISTINCT EmployeeCode) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE MostRecentHireDate >= {hire_window_start} "
    "GROUP BY toStartOfMonth(MostRecentHireDate)"
)


async def test_single_node_period_range_one_bound_is_slot_invalid() -> None:
    detail = _single_detail(sql_template=_ONE_BOUND_SQL, slots=[_RANGE_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win",
        slot_bindings={"hire_window": {"start": "2026-01-01", "end": "2026-03-31"}},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert mcp.calls == []  # fail-closed BEFORE any dispatch


async def test_dag_period_range_one_bound_is_slot_invalid() -> None:
    detail = _dag_detail(node_sql=_ONE_BOUND_SQL, slots=[_RANGE_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win-dag",
        slot_bindings={"hire_window": {"start": "2026-01-01", "end": "2026-03-31"}},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert mcp.calls == []


# ===========================================================================
# A missing required window slot pauses before any node runs
# ===========================================================================


async def test_relative_window_missing_required_pauses() -> None:
    detail = _single_detail(sql_template=_WINDOW_SQL, slots=[_WINDOW_SLOT])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-win", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecPaused)
    assert outcome.reason == "blueprint_slot"
    assert mcp.calls == []
