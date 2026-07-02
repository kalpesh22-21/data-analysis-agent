"""Layer-1: the Slice-C multi-node DAG executor (runblueprint-design §2/§3, Slice C).

All fakes, no infra. Proves the scalar-passing DAG walk, `when` gating, approval
pause/resume (D45 mid-DAG durability, completed nodes never re-run), and the D67
`resolve_via` expansion — every one a Layer-1-authoritative property the design
pins for this slice.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    ABORTED_CODE,
    SLOT_INVALID_CODE,
    UNSUPPORTED_CODE,
    VERIFY_FAILED_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecPaused,
    _approval_decision,
)
from data_agent.runtime.composite.ranking import ResolvedValue
from data_agent.runtime.composite.resolve_values import ResolveOutcome
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

_E = "dbpcm_warehouse.employee"
_DEPT_COL = f"{_E}.Department"
_SAL_COL = f"{_E}.AnnualSalary"
_CODE_COL = f"{_E}.EmployeeCode"
_STATUS_COL = f"{_E}.StatusCode"

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


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-bp-dag", jwt="jwt-secret", column_scope=scope)


def _rq(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}


def _detail(
    *,
    composes: list[dict[str, Any]],
    result_grain: Any,
    slots: list[dict[str, Any]] | None = None,
    uses_rules: list[Any] | None = None,
    uses: frozenset[str] | None = frozenset({_DEPT_COL, _SAL_COL, _CODE_COL, _STATUS_COL}),
    bid: str = "bp-dag",
) -> BlueprintDetail:
    return BlueprintDetail(
        id=bid,
        intent="A multi-node blueprint",
        slots_summary="",
        uses=uses,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=slots,
        uses_rules=uses_rules,
        sql_template=None,
        composes=composes,
        result_grain=result_grain,
    )


def _executor(
    mcp: FakeMCPClient, detail: BlueprintDetail, *, resolve_values: Any = None
) -> BlueprintExecutor:
    index = FakeVectorIndex()
    index.add_detail(detail)
    return BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        vector_index=index,
        resolve_values=resolve_values,
    )


# ---------------------------------------------------------------------------
# D59a — scalar intermediate passed as a typed AST literal into a downstream node
# ---------------------------------------------------------------------------

_COMPANY_AVG_SQL = "SELECT AVG(AnnualSalary) AS company_avg FROM dbpcm_warehouse.employee"
_ABOVE_AVG_SQL = (
    "SELECT Department AS department FROM dbpcm_warehouse.employee "
    "GROUP BY Department HAVING AVG(AnnualSalary) > {company_avg}"
)


def _scalar_dag_detail() -> BlueprintDetail:
    return _detail(
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
        result_grain=["Department"],
    )


async def test_scalar_intermediate_binds_as_typed_literal_and_verifies() -> None:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["company_avg"], [[55000.0]]),            # node 0 → scalar
                _rq(["department"], [["Sales"], ["Eng"]]),    # node 1 → final table
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),          # grain probe
            ]
        }
    )
    executor = _executor(mcp, _scalar_dag_detail())

    outcome = await executor.execute(
        blueprint_id="bp-dag", slot_bindings={}, credentials=_creds()
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["status"] == "verified"
    assert outcome.result_full["row_count"] == 2
    # The scalar was bound into node 1 as a real numeric literal (never f-string).
    node1_sql = mcp.calls[1].args["sql"]
    assert "55000.0" in node1_sql
    assert "{company_avg}" not in node1_sql
    # Two per-node SQLs surfaced for transparency (D56 "SQL stays visible").
    assert len(outcome.result_full["sql"]) == 2
    # §5.3 provenance = union across BOTH node queries + the grain probe.
    assert outcome.provenance is not None
    assert (_E, "AnnualSalary") in outcome.provenance
    assert (_E, "Department") in outcome.provenance


async def test_hostile_scalar_is_never_reached_here_but_binding_is_ast_typed() -> None:
    # A scalar that came back as adversarial TEXT still binds as a typed string
    # literal (ClickHouse-escaped) — never concatenated (D10/F1). node 1 consumes
    # a string scalar into a string comparison.
    detail = _detail(
        composes=[
            {"order": 0, "output": {"tok": "scalar"}, "sql_template": "SELECT Department AS tok FROM dbpcm_warehouse.employee LIMIT 1"},
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"tok": "$0.tok"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee WHERE Department = {tok} GROUP BY Department",
                "output": {},
            },
        ],
        result_grain=["Department"],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["tok"], [["x' OR '1'='1"]]),           # adversarial scalar text
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    executor = _executor(mcp, detail)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecCompleted)
    node1_sql = mcp.calls[1].args["sql"]
    # ClickHouse-escaped single-quote doubling — the injection is inert data.
    assert "''" in node1_sql
    assert "OR '1'='1'" not in node1_sql.replace("''", "\x00")


# ---------------------------------------------------------------------------
# when gating — skip / abort
# ---------------------------------------------------------------------------


async def test_when_gated_node_is_skipped_when_upstream_empty() -> None:
    detail = _detail(
        composes=[
            {"order": 0, "output": {"flagged": "scalar"}, "sql_template": "SELECT count() AS flagged FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "when": {"expr": "$0.flagged > 100", "on_violation": "skip"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
            {
                "order": 2,
                "feeds_from": [0],
                "sql_template": "SELECT Department AS department, count() AS n FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ],
        result_grain=["Department"],
    )
    # node 0 returns 5 (< 100) → node 1 is skipped; node 2 runs and is the terminal.
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["flagged"], [[5]]),                         # node 0
                _rq(["department", "n"], [["Sales", 3], ["Eng", 2]]),  # node 2 (node 1 skipped)
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),             # grain probe
            ]
        }
    )
    executor = _executor(mcp, detail)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecCompleted)
    # Only node 0, node 2, and the grain probe ran — node 1 was gated out.
    assert len(mcp.calls) == 3
    assert "GROUP BY Department" in mcp.calls[1].args["sql"]


async def test_when_abort_falls_back_to_raw_loop() -> None:
    detail = _detail(
        composes=[
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "when": {"expr": "$0.n > 100", "on_violation": "abort"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ],
        result_grain=["Department"],
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[3]])]})  # 3 < 100 → abort
    executor = _executor(mcp, detail)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == ABORTED_CODE
    assert len(mcp.calls) == 1  # aborted before node 1 dispatched


async def test_when_on_violation_ask_pauses_then_resume_proceeds() -> None:
    detail = _detail(
        composes=[
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "feeds_from": [0],
                "when": {"expr": "$0.n > 100", "on_violation": "ask", "message": "Few rows — continue?"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ],
        result_grain=["Department"],
    )
    # First run: node 0 = 3 (< 100) → the when fails → pause to ask.
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[3]])]})
    executor = _executor(mcp, detail)
    paused = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(paused, ExecPaused)
    assert paused.reason == "blueprint_when_ask"
    assert paused.awaiting_node == 1
    assert paused.pending_question["question"] == "Few rows — continue?"

    # Resume with "yes" → the gate is bypassed (not re-evaluated) and node 1 runs.
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    executor2 = _executor(resume_mcp, detail)
    done = await executor2.resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=1,
        approval_answer="yes",
        credentials=_creds(),
    )
    assert isinstance(done, ExecCompleted)
    assert len(resume_mcp.calls) == 2  # node 0 not re-run


# ---------------------------------------------------------------------------
# Approval pause/resume (D45) — completed nodes never re-run, exactly-once
# ---------------------------------------------------------------------------


def _approval_detail() -> BlueprintDetail:
    return _detail(
        composes=[
            {"order": 0, "output": {"n": "scalar"}, "sql_template": "SELECT count() AS n FROM dbpcm_warehouse.employee"},
            {
                "order": 1,
                "node_kind": "approval",
                "feeds_from": [0],
                "requires_approval": {"prompt": "About to flag departments — proceed?"},
                "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department",
                "output": {},
            },
        ],
        result_grain=["Department"],
    )


async def test_approval_node_pauses_with_checkpoint_state() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[42]])]})  # node 0 only
    executor = _executor(mcp, _approval_detail())

    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())

    assert isinstance(outcome, ExecPaused)
    assert outcome.reason == "blueprint_approval"
    assert outcome.awaiting_node == 1
    # D59b — the approval shows UPSTREAM outputs only (the count), never a
    # downstream/base cell.
    assert outcome.pending_question["show"] == {"$0.n": 42}
    assert outcome.pending_question["options"] == ["approve", "deny"]
    # The completed SCALAR outputs are serialized for a restart-durable resume.
    assert outcome.completed_nodes_json is not None
    assert '"n": 42' in outcome.completed_nodes_json
    assert len(mcp.calls) == 1  # only node 0 ran; the approval query has NOT run


async def test_resume_reenters_at_awaiting_node_completed_never_rerun() -> None:
    # A FRESH executor (restart) resumes purely from the checkpoint state.
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),   # node 1 query (approved)
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),         # grain probe
            ]
        }
    )
    executor = _executor(resume_mcp, _approval_detail())

    outcome = await executor.resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {"n": 42}}]',
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )

    assert isinstance(outcome, ExecCompleted)
    assert outcome.result_full["row_count"] == 2
    # node 0 is NOT re-run — only node 1 + the grain probe dispatch (exactly-once).
    assert len(resume_mcp.calls) == 2
    assert "count()" not in resume_mcp.calls[0].args["sql"].lower()


async def test_resume_deny_stops_without_running_the_gated_node() -> None:
    resume_mcp = FakeMCPClient(scripted={"runQuery": []})  # nothing should dispatch
    executor = _executor(resume_mcp, _approval_detail())

    outcome = await executor.resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {"n": 42}}]',
        awaiting_node=1,
        approval_answer="deny",
        credentials=_creds(),
    )

    # No terminal query ran before the (denied) approval → clean abort → raw loop.
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == ABORTED_CODE
    assert resume_mcp.calls == []


# ---------------------------------------------------------------------------
# D56 — a final node that fans out is caught; the number never returns
# ---------------------------------------------------------------------------


async def test_final_node_grain_mismatch_withholds_result() -> None:
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["company_avg"], [[55000.0]]),
                _rq(["department"], [["Sales"], ["Sales"], ["Eng"]]),  # 3 rows...
                _rq(["__bp_n", "__bp_d"], [[3, 2]]),                    # ...but 2 distinct → fan-out
            ]
        }
    )
    executor = _executor(mcp, _scalar_dag_detail())
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == VERIFY_FAILED_CODE  # never returns the fan-out number


# ---------------------------------------------------------------------------
# D67 resolve_via — expands via resolve(), binds as an IN-list, no model round-trip
# ---------------------------------------------------------------------------


class _FakeResolveHook:
    """A stand-in for `ResolveValuesComposite.resolve()` (the D67 typed hook).
    Records every call so a test can assert NO model round-trip + exact args."""

    def __init__(self, outcome: ResolveOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, *, table, column, concept, period, credentials) -> ResolveOutcome:
        self.calls.append(
            {"table": table, "column": column, "concept": concept, "period": period}
        )
        return self._outcome


_RULE_SQL = (
    "SELECT Department AS department, count() AS n FROM dbpcm_warehouse.employee "
    "WHERE StatusCode IN {status_codes} GROUP BY Department"
)


def _rule_detail() -> BlueprintDetail:
    return _detail(
        composes=[
            {"order": 0, "sql_template": _RULE_SQL, "output": {}},
        ],
        result_grain=["Department"],
        uses_rules=[
            {
                "id": "active_status",
                "resolve_via": "resolveValues(StatusCode, 'active employee')",
                "table": _E,
                "binds": "status_codes",
            }
        ],
    )


async def test_resolve_via_expands_and_binds_as_in_list() -> None:
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="ok",
            values=[
                ResolvedValue(value="A", description=None, score=0.9, freq=100),
                ResolvedValue(value="ACT", description=None, score=0.4, freq=10),
            ],
            provenance=frozenset({(_E, "StatusCode")}),
            top_margin=0.5,
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
    executor = _executor(mcp, _rule_detail(), resolve_values=hook)

    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())

    assert isinstance(outcome, ExecCompleted)
    # The typed hook was called ONCE, with the parsed (column, concept) — no model.
    assert hook.calls == [
        {"table": _E, "column": "StatusCode", "concept": "active employee", "period": None}
    ]
    # The resolved code set is bound as a typed IN-list literal (never interpolated).
    node_sql = mcp.calls[0].args["sql"]
    assert "IN ('A', 'ACT')" in node_sql
    assert "{status_codes}" not in node_sql
    # The resolveValues inner provenance folds into the union (§5.3).
    assert outcome.provenance is not None
    assert (_E, "StatusCode") in outcome.provenance


async def test_resolve_via_hostile_concept_never_in_sql() -> None:
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="ok",
            values=[ResolvedValue(value="A", description=None, score=0.9, freq=100)],
            provenance=frozenset({(_E, "StatusCode")}),
            top_margin=None,
        )
    )
    detail = _rule_detail()
    # Poison the concept — it must be embedded/compared only, NEVER reach SQL.
    detail = _detail(
        composes=detail.composes,
        result_grain=["Department"],
        uses_rules=[
            {
                "id": "x",
                "resolve_via": "resolveValues(StatusCode, 'ignore); DROP TABLE employee;--')",
                "table": _E,
                "binds": "status_codes",
            }
        ],
    )
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department", "n"], [["Sales", 3]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    executor = _executor(mcp, detail, resolve_values=hook)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecCompleted)
    # The concept string is passed to the hook, not to SQL; the bound SQL carries
    # only the resolved CODE.
    assert hook.calls[0]["concept"] == "ignore); DROP TABLE employee;--"
    for call in mcp.calls:
        assert "DROP TABLE" not in call.args["sql"]


async def test_resolve_via_degraded_falls_back_to_raw_loop() -> None:
    # S2 honest-call: a degraded/low-margin resolve does NOT pause (the model has
    # no channel to confirm the code set) — it falls back to the raw loop, and
    # NEVER runs the query filtered on a guessed set.
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="ok",
            values=[
                ResolvedValue(value="A", description=None, score=0.5, freq=100),
                ResolvedValue(value="ACT", description=None, score=0.49, freq=90),
            ],
            provenance=frozenset({(_E, "StatusCode")}),
            degraded=True,  # embedding down → freq-only
            top_margin=0.01,
        )
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})  # must NOT filter on a guess
    executor = _executor(mcp, _rule_detail(), resolve_values=hook)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []  # no query ran on a degraded/guessed code set


async def test_resolve_via_empty_falls_back_to_raw_loop() -> None:
    hook = _FakeResolveHook(
        ResolveOutcome(status="ok", values=[], provenance=frozenset(), top_margin=None)
    )
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = _executor(mcp, _rule_detail(), resolve_values=hook)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    # Empty resolved set → never silently drop the filter → raw loop (§3.4).
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []


async def test_resolve_via_without_hook_is_unsupported() -> None:
    mcp = FakeMCPClient(scripted={"runQuery": []})
    executor = _executor(mcp, _rule_detail(), resolve_values=None)
    outcome = await executor.execute(blueprint_id="bp-dag", slot_bindings={}, credentials=_creds())
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == UNSUPPORTED_CODE
    assert mcp.calls == []  # never runs the query unfiltered


# ---------------------------------------------------------------------------
# Review Blocker 2 — provenance + SQL carried across pause/resume
# ---------------------------------------------------------------------------


def _disjoint_footprint_approval_detail() -> BlueprintDetail:
    # node 0 reads AnnualSalary (a scalar avg); node 2 reads ONLY Department. The
    # approval (node 1) pauses AFTER node 0 → node 0's footprint must survive the
    # checkpoint or the resumed union fails OPEN (misses AnnualSalary → D44 replay).
    return _detail(
        composes=[
            {"order": 0, "output": {"company_avg": "scalar"}, "sql_template": _COMPANY_AVG_SQL},
            {"order": 1, "node_kind": "approval", "feeds_from": [0], "output": {}},
            {
                "order": 2,
                "feeds_from": [0],
                "consumes": {"company_avg": "$0.company_avg"},
                "sql_template": (
                    "SELECT Department AS department FROM dbpcm_warehouse.employee "
                    "GROUP BY Department HAVING AVG(AnnualSalary) > {company_avg}"
                ),
                "output": {},
            },
        ],
        result_grain=["Department"],
    )


async def test_resume_provenance_union_spans_pre_pause_nodes() -> None:
    pause_mcp = FakeMCPClient(scripted={"runQuery": [_rq(["company_avg"], [[55000.0]])]})
    paused = await _executor(pause_mcp, _disjoint_footprint_approval_detail()).execute(
        blueprint_id="bp-dag", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(paused, ExecPaused)
    # The checkpoint carries node 0's provenance + SQL (not just its scalar output).
    assert "AnnualSalary" in (paused.completed_nodes_json or "")

    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"], ["Eng"]]),
                _rq(["__bp_n", "__bp_d"], [[2, 2]]),
            ]
        }
    )
    done = await _executor(resume_mcp, _disjoint_footprint_approval_detail()).resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        completed_nodes_json=paused.completed_nodes_json,
        awaiting_node=paused.awaiting_node,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(done, ExecCompleted)
    # Union spans ALL nodes: AnnualSalary (pre-pause node 0) + Department (node 2).
    assert done.provenance is not None
    assert (_E, "AnnualSalary") in done.provenance
    assert (_E, "Department") in done.provenance
    # BOTH node SQLs surfaced for transparency (pre-pause node 0 + resumed node 2).
    assert len(done.result_full["sql"]) == 2


async def test_resume_null_provenance_completed_node_poisons_union() -> None:
    # A completed node whose provenance was UNDETERMINED (null) must poison the
    # resumed union → None (fail-closed, the Slice-B B2 rule) so the answer DROPS
    # under scope narrowing rather than fail-open.
    resume_mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq(["department"], [["Sales"]]),
                _rq(["__bp_n", "__bp_d"], [[1, 1]]),
            ]
        }
    )
    done = await _executor(resume_mcp, _disjoint_footprint_approval_detail()).resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        # node 0 completed with a NULL provenance (undetermined footprint).
        completed_nodes_json='[{"order": 0, "output": {"company_avg": 55000.0}, "provenance": null, "sql": "SELECT 1"}]',
        awaiting_node=1,
        approval_answer="approve",
        credentials=_creds(),
    )
    assert isinstance(done, ExecCompleted)
    assert done.provenance is None  # poisoned → drops from D44 replay


# ---------------------------------------------------------------------------
# Review Blocker 3 — approval consent is opt-IN (ambiguous never proceeds)
# ---------------------------------------------------------------------------


def test_approval_decision_three_way() -> None:
    for affirmative in ("approve", "yes", "ok proceed", "confirm", "go ahead"):
        assert _approval_decision(affirmative) == "approve"
    for negative in ("please don't", "do not proceed", "I'd rather not", "never", "deny", "no"):
        assert _approval_decision(negative) == "deny"
    for garbage in ("asdfgh", "", "what?", None):
        assert _approval_decision(garbage) == "repause"


async def test_ambiguous_denial_does_not_run_the_gated_node() -> None:
    for ambiguous in ("please don't", "do not proceed", "I'd rather not", "never"):
        resume_mcp = FakeMCPClient(scripted={"runQuery": []})  # must NOT dispatch
        outcome = await _executor(resume_mcp, _approval_detail()).resume(
            blueprint_id="bp-dag",
            slot_bindings={},
            completed_nodes_json='[{"order": 0, "output": {"n": 42}, "provenance": [], "sql": "SELECT count() AS n FROM dbpcm_warehouse.employee"}]',
            awaiting_node=1,
            approval_answer=ambiguous,
            credentials=_creds(),
        )
        assert isinstance(outcome, ExecFailed), ambiguous
        assert outcome.error_code == ABORTED_CODE
        assert resume_mcp.calls == []


async def test_garbage_answer_repauses_same_gate() -> None:
    resume_mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(resume_mcp, _approval_detail()).resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {"n": 42}, "provenance": [], "sql": "SELECT count() AS n FROM dbpcm_warehouse.employee"}]',
        awaiting_node=1,
        approval_answer="hmm not sure yet",  # unrecognized → re-pause, never proceed
        credentials=_creds(),
    )
    # "not sure" contains a negation marker → deny (safe: does not proceed).
    assert isinstance(outcome, ExecFailed)
    assert resume_mcp.calls == []


async def test_pure_garbage_answer_repauses() -> None:
    resume_mcp = FakeMCPClient(scripted={"runQuery": []})
    outcome = await _executor(resume_mcp, _approval_detail()).resume(
        blueprint_id="bp-dag",
        slot_bindings={},
        completed_nodes_json='[{"order": 0, "output": {"n": 42}, "provenance": [], "sql": "SELECT count() AS n FROM dbpcm_warehouse.employee"}]',
        awaiting_node=1,
        approval_answer="asdfghjkl",  # pure garbage → re-pause the SAME gate
        credentials=_creds(),
    )
    assert isinstance(outcome, ExecPaused)
    assert outcome.reason == "blueprint_approval"
    assert outcome.awaiting_node == 1
    assert resume_mcp.calls == []


# ---------------------------------------------------------------------------
# Review Blocker 4b — a dead top-level template's slot is not counted (executor)
# ---------------------------------------------------------------------------


async def test_dead_toplevel_template_slot_is_not_a_hidden_referenced_slot() -> None:
    # A hybrid record (both sql_template and composes) reaches the executor (e.g. a
    # poisoned/legacy record the loader now rejects). The DAG path runs; the dead
    # top-level template's {department} slot must NOT count as "referenced" — if a
    # resolved-but-unreferenced slot slipped through it would be a silent filter
    # drop → company-wide "verified". Here the slot IS resolved but referenced by
    # NO node template → SLOT_INVALID (never a silent drop).
    detail = BlueprintDetail(
        id="bp-dag",
        intent="hybrid",
        slots_summary="",
        uses=frozenset({_DEPT_COL, _SAL_COL, _CODE_COL}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        slots=[{"name": "department", "type": "string", "required": True}],
        sql_template="SELECT AVG(AnnualSalary) FROM dbpcm_warehouse.employee WHERE Department = {department}",
        composes=[
            {"order": 0, "output": {}, "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department"},
        ],
        result_grain=["Department"],
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq(["department"], [["Sales"]]), _rq(["__bp_n", "__bp_d"], [[1, 1]])]})
    outcome = await _executor(mcp, detail).execute(
        blueprint_id="bp-dag", slot_bindings={"department": "Warehouse"}, credentials=_creds()
    )
    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == SLOT_INVALID_CODE
    # The user's 'Warehouse' filter was NEVER silently dropped into a company-wide run.
    assert not any("Warehouse" in c.args["sql"] for c in mcp.calls)


# ---------------------------------------------------------------------------
# Review S1 — single-node resolve_via expands, binds an IN-list, and executes
# ---------------------------------------------------------------------------


async def test_single_node_resolve_via_expands_and_executes() -> None:
    hook = _FakeResolveHook(
        ResolveOutcome(
            status="ok",
            values=[ResolvedValue(value="A", description=None, score=0.9, freq=100)],
            provenance=frozenset({(_E, "StatusCode")}),
            top_margin=None,
        )
    )
    detail = _detail(
        composes=[],
        result_grain=[],
        uses_rules=[
            {"id": "active", "resolve_via": "resolveValues(StatusCode, 'active status')", "table": _E, "binds": "status_codes"}
        ],
    )
    # Single-node: a top-level sql_template with the rule IN-list placeholder.
    detail = BlueprintDetail(
        id="bp-single-rule",
        intent="single-node rule",
        slots_summary="",
        uses=frozenset({_CODE_COL, _STATUS_COL}),
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="",
        uses_rules=[
            {"id": "active", "resolve_via": "resolveValues(StatusCode, 'active status')", "table": _E, "binds": "status_codes"}
        ],
        sql_template="SELECT count() AS n FROM dbpcm_warehouse.employee WHERE StatusCode IN {status_codes}",
        composes=None,
        result_grain=[],
    )
    index = FakeVectorIndex()
    index.add_detail(detail)
    executor = BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(FakeMCPClient(scripted={"runQuery": [_rq(["n"], [[7]])]}), CATALOG),
        vector_index=index,
        resolve_values=hook,
    )
    outcome = await executor.execute(
        blueprint_id="bp-single-rule", slot_bindings={}, credentials=_creds()
    )
    assert isinstance(outcome, ExecCompleted), outcome
    # The D67 hook ran once (no model round-trip); its provenance folds in.
    assert len(hook.calls) == 1
    assert outcome.provenance is not None
    assert (_E, "StatusCode") in outcome.provenance
