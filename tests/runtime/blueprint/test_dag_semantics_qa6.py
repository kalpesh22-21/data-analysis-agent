"""QA6 Layer-1: DAG semantics — D67 resolve_via, when gating, D56 verify across
nodes, provenance-union fail-closure, and telemetry redaction (runblueprint §2-§5).

All fakes; the real `BlueprintExecutor` walk under test.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    UNSUPPORTED_CODE,
    VERIFY_FAILED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
)
from data_agent.runtime.composite.ranking import ResolvedValue
from data_agent.runtime.composite.resolve_values import ResolveOutcome
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {
        _E: {
            "EmployeeCode": "String",
            "Department": "Nullable(String)",
            "AnnualSalary": "Nullable(Float64)",
            "StatusCode": "Nullable(String)",
        }
    }
)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-qa6-sem", jwt="jwt", column_scope=frozenset())


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    composes: list[dict[str, Any]],
    *,
    result_grain: Any = None,
    uses_rules: list[Any] | None = None,
) -> BlueprintDetail:
    return BlueprintDetail(
        id="bp-sem",
        intent="dag semantics probe",
        slots_summary="",
        uses=frozenset({f"{_E}.Department", f"{_E}.AnnualSalary", f"{_E}.EmployeeCode", f"{_E}.StatusCode"}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        composes=composes,
        uses_rules=uses_rules,
        result_grain=result_grain if result_grain is not None else ["Department"],
    )


def _executor(mcp: FakeMCPClient, detail: BlueprintDetail, *, resolve_values: Any = None, observer=None) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    kwargs: dict[str, Any] = {
        "tool_dispatcher": ToolDispatcher(mcp, CATALOG),
        "vector_index": index,
        "resolve_values": resolve_values,
    }
    if observer is not None:
        kwargs["observer"] = observer
    return BlueprintExecutor(**kwargs)


class _FakeResolveHook:
    def __init__(self, outcome: ResolveOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, *, table, column, concept, period, credentials) -> ResolveOutcome:
        self.calls.append({"table": table, "column": column, "concept": concept})
        return self._outcome


_RULE_SQL = (
    "SELECT Department AS department, count() AS n FROM dbpcm_warehouse.employee "
    "WHERE StatusCode IN {status_codes} GROUP BY Department"
)


def _rule_detail() -> BlueprintDetail:
    return _detail(
        [{"order": 0, "sql_template": _RULE_SQL, "output": {}}],
        uses_rules=[
            {
                "id": "active_status",
                "resolve_via": "resolveValues(StatusCode, 'active employee')",
                "table": _E,
                "binds": "status_codes",
            }
        ],
    )


# ---------------------------------------------------------------------------
# D67 resolve_via — empty resolve is NEVER a silent `IN ()` (semantics change)
# ---------------------------------------------------------------------------


async def test_empty_resolve_never_emits_in_empty_set() -> None:
    hook = _FakeResolveHook(ResolveOutcome(status="ok", values=[], provenance=frozenset(), top_margin=None))
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, _rule_detail(), resolve_values=hook).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    # Fall back to raw loop; CRUCIALLY the node query never ran with an empty IN.
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []
    assert all("IN ()" not in c.args["sql"] for c in mcp.calls)  # vacuously true; guards regression


async def test_resolve_via_denial_passes_through_and_carries_provenance() -> None:
    # An inner resolve() denial → raw-loop fallback; the denial's provenance is
    # still folded into the union (§3.4/§5.3), never bypassed.
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="denied",
            values=[],
            provenance=frozenset({(_E, "StatusCode")}),
            error_code="COLUMN_SCOPE_VIOLATION",
        )
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(mcp, _rule_detail(), resolve_values=hook).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert mcp.calls == []  # never runs the query on a denied rule


async def test_resolve_via_provenance_union_includes_resolvevalues_inner() -> None:
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="ok",
            values=[ResolvedValue(value="A", description=None, score=0.9, freq=100)],
            provenance=frozenset({(_E, "StatusCode")}),
            top_margin=None,
        )
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "n"], [["Sales", 3], ["Eng", 2]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(mcp, _rule_detail(), resolve_values=hook).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    # The resolveValues inner provenance is present in the union (§5.3).
    assert outcome.provenance is not None
    assert (_E, "StatusCode") in outcome.provenance


# ---------------------------------------------------------------------------
# when gating — malformed `when` at execution fails closed (never crashes)
# ---------------------------------------------------------------------------


async def test_malformed_when_at_execution_is_unsupported_not_crash() -> None:
    # An entity-valued `when` (a string comparison) is REJECTED at load; a
    # poisoned/legacy record that slipped through must fail-close at execution
    # (evaluate_when raises → UNSUPPORTED), never crash the turn.
    detail = _detail(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "when": {"expr": "$0.n == 'Warehouse'", "on_violation": "skip"},  # entity-valued
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[5]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE


async def test_when_referencing_upstream_scalar_gates_correctly() -> None:
    # A `when` over an UPSTREAM SCALAR ($0.n) — the count drives node 1's gate.
    detail = _detail(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "when": {"expr": "$0.n > 2", "on_violation": "skip"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
            {
                "order": 2,
                "feeds_from": [0],
                "sql_template": "SELECT Department AS department, count() AS c FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ]
    )
    # n = 10 (> 2) → node 1 runs (terminal is node 2, the last in topo order).
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["n"], [[10]]),
                _rq(["department"], [["Sales"], ["Eng"]]),          # node 1 (gate passed)
                _rq(["department", "c"], [["Sales", 3], ["Eng", 2]]),  # node 2 terminal
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert len(mcp.calls) == 4  # node 1 NOT skipped (gate passed)


# ---------------------------------------------------------------------------
# D56 across nodes — a skipped node does not break verify; fan-out still caught
# ---------------------------------------------------------------------------


async def test_when_skipped_node_does_not_break_final_verify() -> None:
    detail = _detail(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "when": {"expr": "$0.n > 100", "on_violation": "skip"},  # 5 < 100 → skipped
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
            {
                "order": 2,
                "feeds_from": [0],
                "sql_template": "SELECT Department AS department, count() AS c FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["n"], [[5]]),                                   # node 0
                _rq(["department", "c"], [["Sales", 3], ["Eng", 2]]),  # node 2 (node 1 skipped)
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),                 # grain probe over node 2
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    assert len(mcp.calls) == 3  # node 1 skipped; verify still runs on the terminal


async def test_final_node_fanout_is_withheld_across_the_dag() -> None:
    detail = _detail(
        [
            {"order": 0, "output": {"avg": "scalar"}, "sql_template": "SELECT AVG(AnnualSalary) AS avg FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"avg": "$0.avg"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "GROUP BY Department HAVING AVG(AnnualSalary) > {avg}"
                ),
                "output": {},
            },
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["avg"], [[50000.0]]),
                _rq(["department"], [["Sales"], ["Sales"], ["Eng"]]),  # 3 rows...
                _rq(["__bp_n", "__bp_d"], [[3, 2]]),                    # ...2 distinct → fan-out
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == VERIFY_FAILED_CODE


# ---------------------------------------------------------------------------
# Provenance union — None if ANY inner call is undetermined (the Slice-B fix,
# now across the DAG). A node reading an UNCATALOGUED table → None provenance.
# ---------------------------------------------------------------------------


async def test_provenance_union_is_none_if_any_node_undetermined() -> None:
    detail = _detail(
        [
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.ghost"},
            {
                "order": 1,
                "feeds_from": [0],
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ]
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["n"], [[5]]),                            # node 0 → ghost table → prov None
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    # One inner call had undetermined provenance → the WHOLE union is None
    # (fail-closed: the answer drops from D44 replay).
    assert outcome.provenance is None


# ---------------------------------------------------------------------------
# Redaction — scalar cell values + resolved codes never reach a telemetry event
# ---------------------------------------------------------------------------


async def test_no_scalar_or_bound_value_reaches_progress_events() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def _observer(name: str, payload: dict[str, Any]) -> None:
        events.append((name, dict(payload)))

    detail = _detail(
        [
            {"order": 0, "output": {"avg": "scalar"}, "sql_template": "SELECT AVG(AnnualSalary) AS avg FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"avg": "$0.avg"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "GROUP BY Department HAVING AVG(AnnualSalary) > {avg}"
                ),
                "output": {},
            },
        ]
    )
    secret_avg = 73137.42
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["avg"], [[secret_avg]]),
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    outcome = await _executor(mcp, detail, observer=_observer).execute(
        blueprint_id="bp-sem", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted)
    # Every emitted blueprint_step payload carries step/shape only — the resolved
    # scalar value never appears in ANY telemetry event (§5.5 PII-safe).
    assert events, "expected progress events"
    blob = json.dumps(events, default=str)
    assert str(secret_avg) not in blob
    assert "73137" not in blob
    # And the events are the shape-only progress steps, not cell payloads.
    assert all(name == "blueprint_step" for name, _ in events)
    for _name, payload in events:
        assert set(payload).issubset({"blueprint_id", "step", "node"})


async def test_resolved_codes_absent_from_progress_events() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="ok",
            values=[ResolvedValue(value="SECRETCODE", description=None, score=0.9, freq=100)],
            provenance=frozenset({(_E, "StatusCode")}),
            top_margin=None,
        )
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "n"], [["Sales", 3]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    outcome = await _executor(
        mcp, _rule_detail(), resolve_values=hook, observer=lambda n, p: events.append((n, dict(p)))
    ).execute(blueprint_id="bp-sem", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecCompleted)
    # The resolved client code is bound into SQL but NEVER into a telemetry event.
    assert "SECRETCODE" not in json.dumps(events, default=str)
