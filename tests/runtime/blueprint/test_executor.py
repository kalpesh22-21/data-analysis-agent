"""Layer-1: the single-node `BlueprintExecutor` (runblueprint-design §2, Slice B).

All fakes, no infra. Two dispatch harnesses:
  - the REAL `ToolDispatcher` (FakeMCPClient + CatalogHandle) — proves slot
    binding, the runQuery choke point, and GENUINE provenance capture end-to-end;
  - a `FakeDispatcher` returning scripted `ToolResult`s — for the verify-gate and
    fail-closed-provenance paths that need precise control of inner results.

Proofs (per the Slice-B test matrix):
  - single-node happy path (fetch → slot probe → bind → node query → grain probe
    → verified result), provenance = union of inner runQuery provenance;
  - hostile slot value → one ClickHouse-escaped literal end-to-end (D10/F1);
  - a slot resolver `askUser` (missing / domain no-match) → PAUSE, no node runs;
  - D56 verify PASS / FAIL (fan-out canary, no result returned) / SKIP;
  - `NOT_FOUND` non-oracle (absent == out-of-scope, byte-identical);
  - `UNSUPPORTED` multi-node → raw-loop fallback;
  - an inner runQuery denial passes through verbatim (never bypassed);
  - provenance `None` (any inner undetermined) → union `None` (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    NOT_FOUND_CODE,
    UNSUPPORTED_CODE,
    VERIFY_FAILED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.client import MCPToolError
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
    {_E: {"EmployeeCode": "String", "Department": "Nullable(String)", "AnnualSalary": "Nullable(Float64)"}}
)

_AVG_SQL = (
    "SELECT Department AS department, AVG(AnnualSalary) AS avg_salary, "
    "COUNT(DISTINCT EmployeeCode) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE Department = {department} GROUP BY Department"
)


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-bp", jwt="jwt-secret", column_scope=scope)


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
    return BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        vector_index=_index(detail),
    )


# ---------------------------------------------------------------------------
# FakeDispatcher — scripted ToolResults for precise inner-result control
# ---------------------------------------------------------------------------


@dataclass
class _Recorded:
    sql: str


class FakeDispatcher:
    """A `ToolDispatcher` stand-in: returns pre-scripted `ToolResult`s per call,
    recording each dispatched SQL. Lets a test control inner provenance/verify
    numbers exactly (what a real dispatcher computes from the SQL)."""

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
        result_preview=ResultPreview(columns=raw["columns"], row_count=raw["row_count"], truncated=False, preview_rows=raw["rows"]),
        result_full=raw,
    )


# ---------------------------------------------------------------------------
# Happy path — real dispatcher, real provenance capture
# ---------------------------------------------------------------------------


async def test_single_node_happy_path_verified_with_union_provenance() -> None:
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"], ["Sales"]]),          # 1: slot domain probe
                _rq(["department", "avg_salary", "headcount"], [["Warehouse", 50000.0, 3]]),  # 2: node
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),                       # 3: grain probe
            ]
        }
    )
    executor = _real_executor(mcp, detail)

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["status"] == "verified"
    assert outcome.result_full["verify"] == {
        "grain_ok": True,
        "grain_checked": True,
        "signature_ok": True,
        "signature_checked": False,
    }
    assert outcome.result_full["row_count"] == 1
    # §5.3 provenance = union of EVERY inner runQuery (probe + node + grain probe).
    assert outcome.provenance == frozenset(
        {(_E, "Department"), (_E, "AnnualSalary"), (_E, "EmployeeCode")}
    )
    # Three inner runQuery calls issued (probe, node, grain) — the model sees ONE tool.
    assert len(mcp.calls) == 3
    # The node query bound the resolved slot as a real literal.
    node_sql = mcp.calls[1].args["sql"]
    assert "'Warehouse'" in node_sql


async def test_slot_probe_is_scope_enforced_and_credentialed() -> None:
    # The DISTINCT domain probe is an ordinary dispatched runQuery — the JWT reaches
    # the transport boundary (D5), proving the probe is credentialed/scope-enforced.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["Department"], [["Warehouse"]]),
                _rq(["department", "avg_salary", "headcount"], [["Warehouse", 50000.0, 3]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    executor = _real_executor(mcp, detail)
    await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )
    probe = mcp.calls[0]
    assert probe.jwt == "jwt-secret"
    assert "DISTINCT" in probe.args["sql"].upper()


# ---------------------------------------------------------------------------
# Injection boundary — hostile slot value stays one escaped literal (D10/F1)
# ---------------------------------------------------------------------------


async def test_hostile_slot_value_is_one_escaped_literal_end_to_end() -> None:
    # No binds_to → no domain probe → the resolver binds the value directly and the
    # template binder ClickHouse-escapes it into a single typed literal (never SQL-
    # interpolated). Grain skipped (empty) to isolate the binding assertion.
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True}],
        result_grain=[],
    )
    mcp = FakeMCPClient(
        scripted={"runQuery": [_rq(["department", "avg_salary", "headcount"], [["x", 1.0, 1]])]}
    )
    executor = _real_executor(mcp, detail)

    hostile = "War'e--house'; DROP TABLE employee--"
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": hostile}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    node_sql = mcp.calls[0].args["sql"]
    # The single quote is DOUBLED (ClickHouse-escaped) — one literal, no injection.
    assert "'War''e--house''; DROP TABLE employee--'" in node_sql
    # The raw (un-doubled) injection substring never appears verbatim.
    assert "War'e--house'; DROP" not in node_sql


# ---------------------------------------------------------------------------
# Slot resolver askUser → PAUSE (no node runs)
# ---------------------------------------------------------------------------


async def test_missing_required_slot_pauses_before_any_node_runs() -> None:
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    mcp = FakeMCPClient(scripted={"runQuery": []})  # nothing may dispatch
    executor = _real_executor(mcp, detail)

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={}, credentials=_creds()
    )

    assert isinstance(outcome, ExecPaused)
    assert outcome.reason == "blueprint_slot"
    assert "department" in outcome.pending_question["question"]
    assert mcp.calls == []  # PAUSE happens before any node query


async def test_domain_no_match_pauses_never_guesses() -> None:
    # A value not in the probed domain → askUser (D49: never a silent guess).
    detail = _detail(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": _DEPT_COL}]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["Department"], [["Warehouse"], ["Sales"]])]})
    executor = _real_executor(mcp, detail)

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "Marketing"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecPaused)
    assert len(mcp.calls) == 1  # only the domain probe ran; no node query


async def test_paused_carries_raw_slot_bindings_for_resume() -> None:
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"other": "x"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecPaused)
    assert outcome.slot_bindings_json is not None and "other" in outcome.slot_bindings_json


# ---------------------------------------------------------------------------
# D56 verify gate — FakeDispatcher for exact probe control
# ---------------------------------------------------------------------------


async def test_verify_fanout_fail_never_returns_the_result() -> None:
    # The fan-out canary: the grain probe reports MORE rows than distinct grain →
    # verify FAIL → VERIFY_FAILED, the suspect number is NOT returned (D56).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    node_prov = frozenset({(_E, "Department"), (_E, "AnnualSalary")})
    dispatcher = FakeDispatcher(
        [
            _ok_result(node_prov, _rq(["department", "avg_salary", "headcount"], [["a", 1.0, 1], ["b", 2.0, 1]])),
            _ok_result(node_prov, _rq(["__bp_n", "__bp_d"], [[10, 5]])),  # 10 rows, 5 distinct → fan-out
        ]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == VERIFY_FAILED_CODE
    assert outcome.retryable is True


async def test_verify_skipped_for_empty_grain_no_probe_issued() -> None:
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}], result_grain=[])
    dispatcher = FakeDispatcher(
        [_ok_result(frozenset({(_E, "Department")}), _rq(["department"], [["a"]]))]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["verify"]["grain_checked"] is False
    assert len(dispatcher.calls) == 1  # node query only — NO grain probe issued


async def test_verify_grain_probe_denied_fails_closed() -> None:
    # A denied grain probe → the check cannot run → fail-closed (never a silent pass).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    node_prov = frozenset({(_E, "Department")})
    denied = ToolResult(
        status="denied",
        tool_name="runQuery",
        error_code="COLUMN_SCOPE_VIOLATION",
        retryable=False,
        user_message="denied",
        provenance=None,
        result_preview=None,
        result_full=None,
    )
    dispatcher = FakeDispatcher(
        [_ok_result(node_prov, _rq(["department"], [["a"]])), denied]
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))

    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == VERIFY_FAILED_CODE


# ---------------------------------------------------------------------------
# NOT_FOUND non-oracle (absent == out-of-scope)
# ---------------------------------------------------------------------------


async def test_absent_blueprint_is_not_found() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=FakeVectorIndex())
    outcome = await executor.execute(blueprint_id="nope", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == NOT_FOUND_CODE


async def test_out_of_scope_blueprint_is_byte_identical_not_found() -> None:
    # A blueprint whose USES ⊄ a narrow scope → NOT_FOUND, identical to absent
    # (the D88(b) non-oracle — no scope probe, no distinguishing signal).
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}])
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=_index(detail))
    narrow = frozenset({_CODE_COL})  # excludes Department/AnnualSalary
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds(narrow)
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == NOT_FOUND_CODE
    assert mcp.calls == []  # no probe leaked the blueprint's existence


# ---------------------------------------------------------------------------
# UNSUPPORTED: a TABLE intermediate still needs scratch (F2) → raw-loop fallback.
# (Slice C flips the old "any multi-node is UNSUPPORTED" pin — SCALAR-passing DAGs
#  now execute; only TABLE-passing DAGs remain UNSUPPORTED, §2.4.)
# ---------------------------------------------------------------------------


async def test_multi_node_table_intermediate_is_unsupported() -> None:
    detail = _detail(
        sql_template=None,
        composes=[
            # node 0 declares a TABLE output that node 1 consumes → needs scratch.
            {"order": 0, "output": {"dept_rows": "table"}, "sql_template": "SELECT AVG(AnnualSalary) FROM dbpcm_warehouse.employee"},
            {"order": 1, "feeds_from": [0], "sql_template": "SELECT 1"},
        ],
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = BlueprintExecutor(tool_dispatcher=ToolDispatcher(mcp, CATALOG), vector_index=_index(detail))
    outcome = await executor.execute(blueprint_id=detail.id, slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []  # rejected before any node dispatch


# ---------------------------------------------------------------------------
# Inner runQuery denial passes through verbatim (never bypassed)
# ---------------------------------------------------------------------------


async def test_inner_denial_passes_through_verbatim() -> None:
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}], result_grain=[])
    mcp = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "out of scope")]}
    )
    executor = _real_executor(mcp, detail)
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == "COLUMN_SCOPE_VIOLATION"  # the inner denial, not a runBlueprint code


# ---------------------------------------------------------------------------
# Provenance fail-closed: any inner undetermined → union None
# ---------------------------------------------------------------------------


async def test_provenance_none_when_any_inner_undetermined() -> None:
    detail = _detail(slots=[{"name": "department", "type": "string", "required": True}], result_grain=[])
    dispatcher = FakeDispatcher(
        [_ok_result(None, _rq(["department"], [["a"]]))]  # node provenance UNDETERMINED
    )
    executor = BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=_index(detail))
    outcome = await executor.execute(
        blueprint_id=detail.id, slot_bindings={"department": "a"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert outcome.provenance is None  # fail-closed: drops from D44 replay
