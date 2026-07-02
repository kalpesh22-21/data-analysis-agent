"""QA5 Layer-1 adversarial: `BlueprintExecutor` (runblueprint-design §2/§4/§5, Slice B).

Attacks the headline invariants the happy-path suite (`test_executor.py`) proves
only positively:

  - **D56 verify gate — false-pass hunting**: an unexpected grain-probe shape
    (missing count column / null / non-int) must FAIL-CLOSED, never silent-pass;
    `grain_verifiable:false` skip must be flagged DISTINCTLY from a real pass; the
    `_map_grain_columns` casefold collision (declared "Dept" vs outputs "dept"/"DEPT")
    is pinned as an ambiguous mapping (an xfail records the desired fail-closed).
  - **Slot binding end-to-end**: hostile slot values through BOTH the node template
    AND the grain-probe COUNT(DISTINCT) wrapping stay ONE ClickHouse-escaped literal;
    a list slot with hostile elements binds as an escaped IN-tuple; a domain-matched
    hostile value stays escaped.
  - **Multi-query scope**: EVERY inner runQuery (domain probe + node + grain probe)
    carries the JWT *and* session_id (scan recorded calls).
  - **Provenance**: the grain-probe None poisons the union (fail-closed); a successful
    domain probe whose provenance is UNDETERMINED also poisons the union to None
    (§5.3 fail-closed), while a denied/errored probe contributes nothing.
  - **Poisoned / fail-soft**: malformed stored `result_grain` → UNSUPPORTED; a
    non-parsing `sql_template` → SLOT_INVALID; a nested-object slot value → PAUSE.
  - **Determinism**: same blueprint + bindings → byte-identical bound node SQL.

Fakes only (FakeMCPClient / FakeVectorIndex / FakeDispatcher); no infra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    SLOT_INVALID_CODE,
    UNSUPPORTED_CODE,
    VERIFY_FAILED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
    _map_grain_columns,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.fake_client import FakeMCPClient
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
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-bp-qa5", jwt="jwt-secret-qa5", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]], *, truncated: bool = False) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def _detail(
    *,
    slots: list[dict[str, Any]] | None = None,
    sql_template: str | None = _AVG_SQL,
    result_grain: Any = None,
    uses: frozenset[str] | None = frozenset({_DEPT_COL, _SAL_COL, _CODE_COL}),
    composes: list[dict[str, Any]] | None = None,
    bid: str = "bp-average-salary-by-department",
) -> BlueprintDetail:
    return BlueprintDetail(
        id=bid,
        intent="Average annual salary by department",
        slots_summary="department",
        uses=uses,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        resolves={"salary": "AnnualSalary"},
        slots=slots,
        sql_template=sql_template,
        composes=composes,
        result_grain=result_grain if result_grain is not None else ["Department"],
    )


def _index(detail: BlueprintDetail) -> FakeVectorIndex:
    idx = FakeVectorIndex()
    idx.add_detail(detail)
    return idx


def _real_executor(mcp: FakeMCPClient, detail: BlueprintDetail) -> BlueprintExecutor:
    return BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=_index(detail))


# ---------------------------------------------------------------------------
# FakeDispatcher — scripted ToolResults for precise inner-result control
# ---------------------------------------------------------------------------


@dataclass
class _Recorded:
    sql: str


class FakeDispatcher:
    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[_Recorded] = []

    async def dispatch(
        self, tool_name: str, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        assert tool_name == "runQuery"
        self.calls.append(_Recorded(sql=model_args["sql"]))
        assert self._results, "FakeDispatcher ran out of scripted results"
        return self._results.pop(0)


def _ok_result(provenance: frozenset[tuple[str, str]] | None, raw: dict[str, Any]) -> ToolResult:
    return ToolResult(
        status="ok",
        tool_name="runQuery",
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=provenance,
        result_preview=ResultPreview(
            columns=raw["columns"], row_count=raw["row_count"], truncated=False, preview_rows=raw["rows"]
        ),
        result_full=raw,
    )


# ===========================================================================
# D56 verify gate — unexpected grain-probe shapes MUST fail closed
# ===========================================================================


@pytest.mark.parametrize(
    "grain_probe_rows",
    [
        [[5]],  # missing the distinct column entirely
        [[None, None]],  # both counts null
        [[10, None]],  # distinct null
        [["not", "ints"]],  # non-int counts
        [[10, "x"]],  # distinct non-int
        [],  # empty result set — no row at all
    ],
    ids=["missing-col", "both-null", "distinct-null", "non-int", "distinct-non-int", "no-rows"],
)
async def test_grain_probe_bad_shape_fails_closed_not_silent_pass(grain_probe_rows: list) -> None:
    # A grain is declared+verifiable, the NODE returned rows, but the grain probe's
    # shape is un-parseable → the teeth cannot run → FAIL-CLOSED (never a silent pass).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    node_prov = frozenset({(_E, "Department")})
    dispatcher = FakeDispatcher(
        [
            _ok_result(node_prov, _rq(["department", "avg_salary", "headcount"], [["a", 1.0, 1]])),
            _ok_result(node_prov, _rq(["__bp_n", "__bp_d"], grain_probe_rows)),
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == VERIFY_FAILED_CODE  # never ExecCompleted


async def test_grain_probe_string_numeric_counts_are_tolerated() -> None:
    # A defensive counterpoint: string-but-numeric counts ("10"/"10") DO coerce to
    # ints and pass — proving the fail-closed path above is about UNPARSEABLE shapes,
    # not merely non-native-int types (documents the coercion boundary).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    node_prov = frozenset({(_E, "Department")})
    dispatcher = FakeDispatcher(
        [
            _ok_result(node_prov, _rq(["department"], [["a"]])),
            _ok_result(node_prov, _rq(["__bp_n", "__bp_d"], [["1", "1"]])),
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["verify"]["grain_ok"] is True


async def test_grain_verifiable_false_skip_is_flagged_distinctly_from_pass() -> None:
    # `grain_verifiable:false` (dict form) → the row-count teeth are SKIPPED. The
    # result must be flagged `grain_checked: False` so a caller can distinguish a
    # SKIP from a genuine PASS (grain_checked: True) — the two must never look alike.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True}],
        result_grain={"columns": ["Department"], "verifiable": False},
    )
    dispatcher = FakeDispatcher(
        [_ok_result(frozenset({(_E, "Department")}), _rq(["department"], [["a"], ["b"]]))]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    verify = outcome.result_full["verify"]
    assert verify["grain_checked"] is False  # SKIP, not a checked pass
    assert verify["grain_ok"] is True  # vacuously ok
    assert len(dispatcher.calls) == 1  # NO grain probe issued for an unverifiable grain


# ===========================================================================
# _map_grain_columns — casefold collision (the false-pass hunt, §4.2)
# ===========================================================================


def test_map_grain_columns_exact_match_wins_over_casefold() -> None:
    # An EXACT output-name match is taken before any case-insensitive fallback.
    assert _map_grain_columns("SELECT a AS dept, b AS DEPT FROM t", ("dept",)) == ["dept"]
    assert _map_grain_columns("SELECT a AS dept, b AS DEPT FROM t", ("DEPT",)) == ["DEPT"]


def test_map_grain_columns_missing_output_is_fail_closed_none() -> None:
    # A declared grain column with NO output match at all → None (the check cannot
    # run → the executor fail-closes on distinct=None). A `SELECT *` names zero
    # outputs, so any declared grain fails closed too.
    assert _map_grain_columns("SELECT a AS dept FROM t", ("headcount",)) is None
    assert _map_grain_columns("SELECT * FROM t", ("dept",)) is None


def test_map_grain_columns_casefold_collision_fails_closed() -> None:
    # PIN UPDATED (V1 fix): when a declared grain col ("Dept") matches NO output
    # exactly but casefold-collides with TWO outputs ("dept" AND "DEPT"), the
    # mapping is AMBIGUOUS → fail-closed to None (the verify gate must not
    # COUNT(DISTINCT) a GUESSED output column). Previously it silently picked the
    # last-seen output ("DEPT"); that fail-OPEN was the reviewer/QA V1 blocker.
    assert _map_grain_columns("SELECT a AS dept, b AS DEPT FROM t", ("Dept",)) is None


def test_map_grain_columns_casefold_collision_should_fail_closed() -> None:
    # PROMOTED from xfail (V1 fix landed): an ambiguous casefold collision (two
    # outputs collapse to one key) is NOT a determinate mapping and fails closed so
    # the verify gate cannot silently count the wrong column.
    assert _map_grain_columns("SELECT a AS dept, b AS DEPT FROM t", ("Dept",)) is None


# ===========================================================================
# Slot binding — hostile values through node template AND the grain probe
# ===========================================================================

_HOSTILE = "War'e--house'; DROP TABLE employee--"


async def test_hostile_slot_value_escaped_in_grain_probe_wrapping() -> None:
    # The bound hostile literal appears inside the grain-probe subquery too (it wraps
    # the node SQL) — it must stay ONE ClickHouse-escaped literal there as well, never
    # SQL-spliced into the COUNT(DISTINCT) wrapping. Grain declared → probe fires.
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "avg_salary", "headcount"], [["x", 1.0, 1]]),  # node
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
            ]
        }
    )
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": _HOSTILE}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    grain_sql = mcp.calls[1].args["sql"]  # the grain probe (call index 1: no domain probe)
    assert "COUNT(DISTINCT" in grain_sql.upper()
    assert "'War''e--house''; DROP TABLE employee--'" in grain_sql  # doubled = escaped
    assert "War'e--house'; DROP" not in grain_sql  # the raw injection never appears


async def test_hostile_value_matched_from_domain_stays_escaped() -> None:
    # A hostile value that IS present in the probed domain resolves via the domain
    # match and STILL binds as an escaped literal (warehouse origin ≠ trust to splice).
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}],
        result_grain=[],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [[_HOSTILE], ["Sales"]]),  # domain probe returns the hostile value
                _rq(["department", "avg_salary", "headcount"], [["x", 1.0, 1]]),  # node
            ]
        }
    )
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": _HOSTILE}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    # The domain probe SQL never carries the value (it is a bare SELECT DISTINCT).
    assert _HOSTILE not in mcp.calls[0].args["sql"]
    # The node SQL binds the value as one escaped literal.
    node_sql = mcp.calls[1].args["sql"]
    assert "'War''e--house''; DROP TABLE employee--'" in node_sql


async def test_list_slot_hostile_elements_bind_as_escaped_in_tuple() -> None:
    # A list/IN slot with hostile elements → each becomes one escaped literal inside a
    # single IN-tuple, never a spliced fragment (the §3.2 list path, D10/F1).
    template = "SELECT EmployeeCode AS code FROM dbpcm_warehouse.employee WHERE Department IN {depts}"
    detail = _detail(
        slots=[{"name": "depts", "type": "list", "required": True}],
        sql_template=template,
        result_grain=[],
    )
    mcp = FakeMCPClient(
        scripted={"runQuery": [_rq(["code"], [["E1"]])]}
    )
    executor = _real_executor(mcp, detail)
    hostile_list = ["a') OR 1=1--", "Sales"]
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"depts": hostile_list}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    node_sql = mcp.calls[0].args["sql"]
    assert "IN ('a'') OR 1=1--', 'Sales')" in node_sql
    assert "a') OR 1=1--" not in node_sql.replace("''", "\x00")  # no un-doubled quote survives


async def test_same_blueprint_and_bindings_bind_identical_sql_determinism() -> None:
    # Determinism: two executions with the SAME blueprint + bindings dispatch the
    # byte-identical node SQL (no ordering / nondeterministic literal rendering).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}], result_grain=[])

    async def _run() -> str:
        mcp = FakeMCPClient(scripted={"runQuery": [_rq(["department"], [["Sales"]])]})
        ex = _real_executor(mcp, detail)
        await ex.execute(blueprint_id=detail.id, slot_bindings={"department": "Sales"}, credentials=_creds())
        return mcp.calls[0].args["sql"]

    assert await _run() == await _run()


# ===========================================================================
# Multi-query scope — every inner runQuery is credentialed (JWT + session_id)
# ===========================================================================


async def test_every_inner_query_carries_jwt_and_session_id() -> None:
    # D5: the domain probe, the node query, AND the grain probe each reach the
    # transport boundary carrying BOTH the JWT and the session_id — no inner query
    # is issued un-credentialed / un-scoped.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"]]),  # domain probe
                _rq(["department", "avg_salary", "headcount"], [["Warehouse", 5.0, 1]]),  # node
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),  # grain probe
            ]
        }
    )
    executor = _real_executor(mcp, detail)
    await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )
    assert len(mcp.calls) == 3
    for call in mcp.calls:
        assert call.jwt == "jwt-secret-qa5"
        assert call.session_id == "s-bp-qa5"


# ===========================================================================
# Provenance union — fail-closed on grain-probe None
# ===========================================================================


async def test_grain_probe_none_provenance_poisons_union() -> None:
    # The grain probe is an inner runQuery; if ITS provenance is undetermined the
    # whole union must be None (fail-closed, §5.3 — drops from D44 replay).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    dispatcher = FakeDispatcher(
        [
            _ok_result(frozenset({(_E, "Department")}), _rq(["department"], [["a"]])),  # node OK prov
            _ok_result(None, _rq(["__bp_n", "__bp_d"], [[1, 1]])),  # grain probe UNDETERMINED
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.provenance is None


async def test_domain_probe_none_provenance_should_poison_union() -> None:
    # PROMOTED from xfail (B2 fix landed): a SUCCESSFUL domain probe whose
    # provenance is undetermined (None) now POISONS the union to None (fail-closed,
    # §5.3 — "if ANY inner call had undetermined provenance, the union is None").
    # Previously it was silently dropped (fail-OPEN, the node-only footprint).
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}],
        result_grain=[],
    )
    dispatcher = FakeDispatcher(
        [
            _ok_result(None, _rq(["Department"], [["Warehouse"]])),  # domain probe UNDETERMINED
            _ok_result(frozenset({(_E, "Department")}), _rq(["department"], [["Warehouse"]])),  # node OK
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.provenance is None  # fail-closed: a successful None-provenance probe poisons the union


# ===========================================================================
# Poisoned / legacy stored DAG — fail-soft, never crash
# ===========================================================================


async def test_malformed_result_grain_is_unsupported_fail_soft() -> None:
    # A stored result_grain with a non-bool `verifiable` → BlueprintParseError on read
    # → UNSUPPORTED (fall back to the raw loop), never a crash.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True}],
        result_grain={"columns": ["Department"], "verifiable": "yes-please"},
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []  # nothing dispatched


async def test_non_parsing_sql_template_is_slot_invalid_fail_soft() -> None:
    # A stored sql_template that does not parse under ClickHouse → bind_template
    # raises TemplateBindError → SLOT_INVALID, never a crash and never dispatched.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True}],
        sql_template="THIS IS NOT SQL {department} {{{",
        result_grain=[],
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    assert mcp.calls == []


async def test_nested_object_slot_value_pauses_not_crash() -> None:
    # A hostile/nonsensical nested-object slot value handed to a scalar slot → the
    # resolver returns AskUser("invalid") → PAUSE (never a Python-repr binding, never
    # a crash). Review FIX 4a regression guard, exercised through the executor.
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id,
        slot_bindings={"department": {"nested": {"deep": 1}}},
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecPaused)
    assert mcp.calls == []  # paused before any node ran


async def test_domain_probe_fires_on_binds_to_outside_declared_uses_pin() -> None:
    # PIN (§5.3 footprint): the executor probes a slot's `binds_to` column WITHOUT
    # cross-checking it against the blueprint's declared `uses`. Here `binds_to`
    # points at AnnualSalary while `uses` still advertises it, but the point is the
    # executor issues the DISTINCT probe purely from `binds_to` — the declared USES
    # set is never consulted to gate WHICH column the probe reads. Flagged: a
    # blueprint could probe a column outside its advertised footprint (server RLS is
    # the only backstop; the live provenance union captures it after the fact).
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _SAL_COL}],
        result_grain=[],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["AnnualSalary"], [["50000"]]),  # probe fired against binds_to column
                _rq(["department"], [["50000"]]),  # node
            ]
        }
    )
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "50000"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    # The probe read AnnualSalary purely because binds_to said so.
    assert "AnnualSalary" in mcp.calls[0].args["sql"]
