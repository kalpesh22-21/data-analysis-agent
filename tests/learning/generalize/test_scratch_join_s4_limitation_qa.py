"""CLOSED: S4 can now validate a table intermediate, so the loop CAN learn a scratch join.

This file was written to document the opposite. `check_dag` accepted the canon's
`bp-earnings-by-department-via-scratch-join` shape, but `explain_ok` is consulted FIRST and
failed, so the shape stayed unlearnable:

    builder._provenance_uses(templates, catalog_schema)
      -> sqlparse.extract_column_provenance(node_1_template, catalog)
      -> ProvenanceExtractionError  (D64, scratch fail-closed)
      -> explain_ok=False -> ("fail_to_review", "explain_failed")

And the mechanism was harder than "the scratch table is not in the catalog": the extractor
rejected ANY `scratch.*` reference with no bound `session_id`, so registering the table in
`catalog_schema` did not help either — it raises before any schema is consulted.

THE FIX IS THE ONE THIS FILE PREDICTED: `_provenance_uses` now hands the extractor the scratch
placeholders each node DECLARES it consumes (`declared_scratch`), plus a per-node column schema
built the loader's way (`compiler._scratch_schema_for_node` / `_template_output_columns`). A
declared placeholder names an intermediate the blueprint produces itself — no session owns it
and nothing is materialized until the DAG runs — so the ownership question does not apply.
Anything NOT declared still fail-closes, so the omit-the-header bypass D64 exists for stays shut.

The tests below are kept, flipped from documenting the limitation to guarding the fix.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.rewrite import rewrite_sql_to_template
from data_agent.learning.generalize.validate import check_dag
from data_agent.sqlparse import ProvenanceExtractionError, extract_column_provenance

_CATALOG: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.payroll": {
        "employee_code": "String",
        "register_type": "String",
        "amount": "Float64",
    },
    "dbpcm_warehouse.employee": {
        "employee_code": "String",
        "department_name": "String",
    },
}

# The accepted SQL a scratch-join session would leave in the tool trail — the canon's
# `bp-earnings-by-department-via-scratch-join` templates with their literals back in.
_SQL_PRODUCER = (
    "SELECT toString(p.employee_code) AS employee_code, toFloat64(SUM(p.amount)) AS earnings "
    "FROM dbpcm_warehouse.payroll AS p WHERE p.register_type = 'EARN' GROUP BY p.employee_code"
)
_SQL_JOIN = (
    "SELECT e.department_name AS department, SUM(x.earnings) AS total_earnings "
    "FROM scratch.emp_earnings AS x "
    "JOIN dbpcm_warehouse.employee AS e ON e.employee_code = x.employee_code "
    "WHERE e.department_name = 'Analytics' GROUP BY e.department_name"
)

_PARAMETERIZATION: list[dict[str, Any]] = [
    {
        "locator": {
            "table": "dbpcm_warehouse.employee",
            "column": "department_name",
            "value": "Analytics",
        },
        "role": "slot",
        "slot": {
            "name": "department",
            "type": "string",
            "binds_to": "dbpcm_warehouse.employee.department_name",
            "required": True,
            "optional_pattern": None,
        },
    },
    {
        "locator": {
            "table": "dbpcm_warehouse.payroll",
            "column": "register_type",
            "value": "EARN",
        },
        "role": "inline",
        "why": "defines the 'earnings' metric",
    },
]


def _scratch_join_payload() -> dict[str, Any]:
    return {
        "intent": "total earnings by department via a materialized per-employee join",
        "kind": "composite",
        "resolves": {},
        "source_tool_call_refs": ["tc0", "tc1"],
        "accepted_signal": "no_correction",
        "parameterization": [dict(p) for p in _PARAMETERIZATION],
        "composes": [
            {
                "order": 0, "node_kind": "query", "step_intent": "per-employee earnings",
                "feeds_from": [], "consumes": {}, "output": {"emp_earnings": "table"},
                "source_tool_call_ref": "tc0", "when": None, "requires_approval": None,
            },
            {
                "order": 1, "node_kind": "query", "step_intent": "join to departments",
                "feeds_from": [0], "consumes": {"emp_earnings": "$0"}, "output": {},
                "source_tool_call_ref": "tc1", "when": None, "requires_approval": None,
            },
        ],
        "result_signature": {
            "shape": [], "grain": {"columns": ["department"], "verifiable": True},
            "invariants": [],
        },
        "notes": "",
    }


def _generalize() -> Any:
    return generalize_blueprint(
        _scratch_join_payload(), {"tc0": _SQL_PRODUCER, "tc1": _SQL_JOIN}, _CATALOG
    )


# --- the limitation, end to end ------------------------------------------------


def test_the_dag_gate_now_passes_for_the_scratch_join_shape() -> None:
    """Fix 1 did what it says: `dag_ok` is True where it used to be False."""
    gen = _generalize()
    assert gen.static_validation.dag_ok is True
    assert check_dag(_scratch_join_payload()["composes"]) is True


def test_a_scratch_join_composite_now_validates_instead_of_failing_on_explain() -> None:
    """CLOSED. `_provenance_uses` now declares the node's TABLE consumes, so the extractor
    exempts them from the D64 ownership check and the walk reaches the warehouse columns."""
    sv = _generalize().static_validation
    assert sv.outcome == "ok"
    assert sv.reason is None
    assert sv.explain_ok is True


def test_the_whole_generalization_is_populated_not_just_the_explain_flag() -> None:
    """The cascade ran the other way before: `(False, ())` emptied `uses`, which forced
    `binds_to_subset_uses` and `read_only_select` False too, so nothing downstream had
    anything to work with. All three recover together.

    ⚠ `uses` NAMES ONLY WAREHOUSE COLUMNS. The scratch table is this blueprint's own
    intermediate, so including it would inflate a footprint that access control reads — the
    D69/OQ-4 scope-honesty split the loader makes, made the same way in S4."""
    gen = _generalize()
    assert gen.uses
    assert not any(u.startswith("scratch.") for u in gen.uses)
    assert gen.static_validation.binds_to_subset_uses is True
    assert gen.static_validation.read_only_select is True
    assert len(gen.node_templates) == 2


def test_the_blocker_is_the_d64_scratch_fail_closed_rule_not_a_missing_catalog_entry() -> None:
    """The precise mechanism, pinned so the next slice fixes the right thing.
    `extract_column_provenance` rejects a `scratch.*` reference with no bound
    `session_id` OUTRIGHT — adding the scratch table to `catalog_schema` (the obvious
    first attempt) changes nothing."""
    template = rewrite_sql_to_template(_SQL_JOIN, _PARAMETERIZATION, strict=False)
    assert "scratch.emp_earnings" in template

    with pytest.raises(ProvenanceExtractionError) as bare:
        extract_column_provenance(template, _CATALOG)
    assert "session_id" in str(bare.value)

    catalog_with_scratch = dict(_CATALOG)
    catalog_with_scratch["scratch.emp_earnings"] = {"employee_code": "TEXT", "earnings": "TEXT"}
    with pytest.raises(ProvenanceExtractionError):
        extract_column_provenance(template, catalog_with_scratch)


def test_the_loader_has_the_seam_s4_lacks() -> None:
    """The asymmetry, stated as a test so the next slice knows where to look: the
    loader validates a scratch-join node by synthesizing the producing node's output
    columns as a `scratch.<placeholder>` schema. `builder`/`validate` import no such
    helper — S4 goes straight to `extract_column_provenance`."""
    import data_agent.learning.generalize.builder as builder
    from data_agent.runtime.blueprint.compiler import _scratch_schema_for_node

    class _Src:
        sql_template = _SQL_PRODUCER

    class _Consumer:
        consumes = {"emp_earnings": "$0"}

    schema = _scratch_schema_for_node(_Consumer(), {0: _Src()})
    assert schema == {"emp_earnings": {"employee_code": "TEXT", "earnings": "TEXT"}}
    assert not hasattr(builder, "_scratch_schema_for_node")
    assert builder.extract_column_provenance is extract_column_provenance


def test_the_loop_can_learn_the_canon_scratch_join_blueprint() -> None:
    gen = _generalize()
    assert gen.static_validation.outcome == "ok"
    assert set(gen.uses) >= {
        "dbpcm_warehouse.employee.department_name",
        "dbpcm_warehouse.payroll.amount",
    }


# --- what fix 1 DOES buy today -------------------------------------------------


_SQL_COUNT = "SELECT COUNT(employee_code) AS n FROM dbpcm_warehouse.employee"
_SQL_BY_DEPT = (
    "SELECT department_name AS department, COUNT(employee_code) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE department_name = 'Analytics' "
    "GROUP BY department_name"
)


def _terminal_output_payload(terminal_output: dict[str, str]) -> dict[str, Any]:
    return {
        "intent": "headcount by department against the company total",
        "kind": "composite",
        "resolves": {},
        "source_tool_call_refs": ["tc0", "tc1"],
        "accepted_signal": "no_correction",
        "parameterization": [dict(_PARAMETERIZATION[0])],
        "composes": [
            {"order": 0, "node_kind": "query", "step_intent": "", "feeds_from": [],
             "consumes": {}, "output": {"n": "scalar"}, "source_tool_call_ref": "tc0",
             "when": None, "requires_approval": None},
            {"order": 1, "node_kind": "query", "step_intent": "", "feeds_from": [0],
             "consumes": {"n": "$0.n"}, "output": terminal_output,
             "source_tool_call_ref": "tc1", "when": None, "requires_approval": None},
        ],
        "result_signature": None,
        "notes": "",
    }


@pytest.mark.parametrize(
    "terminal_output",
    [{"rows": "table"}, {"rows": "table", "n": "scalar"}],
    ids=["terminal-table", "terminal-table-and-scalar"],
)
def test_a_terminal_table_output_composite_now_validates_ok(
    terminal_output: dict[str, str],
) -> None:
    """The ONE capability fix 1 delivers today. The old rule rejected `output` values
    other than `scalar` outright, so a SINK node declaring the rowset it returns went
    to `fail_to_review/dag_invalid`. Its templates read only warehouse tables, so the
    explain check passes and the candidate now reaches `ok`."""
    gen = generalize_blueprint(
        _terminal_output_payload(terminal_output),
        {"tc0": _SQL_COUNT, "tc1": _SQL_BY_DEPT},
        _CATALOG,
    )
    assert gen.static_validation.outcome == "ok"
    assert gen.static_validation.dag_ok is True


def test_the_old_scalar_only_rule_would_have_rejected_that_same_shape() -> None:
    """Non-vacuity guard for the test above: reproduce the PRE-fix predicate and show
    it rejects the terminal-table composite. Without this, `outcome == "ok"` above
    could be true for reasons unrelated to the change."""
    composes = _terminal_output_payload({"rows": "table"})["composes"]
    old_rule_rejects = any(
        isinstance(node.get("output"), dict)
        and any(v != "scalar" for v in node["output"].values())
        for node in composes
    )
    assert old_rule_rejects is True
    assert check_dag(composes) is True


def test_a_placeholder_shaped_like_a_materialized_table_is_not_declared() -> None:
    """DEFENCE IN DEPTH, and deliberately conservative in the fail-closed direction.

    A `consumes` key is LLM-authored on the mined path, so nothing stops a model naming a
    placeholder `s_victim_bp_abc`. Traced end to end such a name is inert — every use is a
    membership key or the identifier `_rewrite_scratch_tables` REPLACES with the caller's own
    materialized table, so it never reaches warehouse SQL — but it is a lie to anyone auditing
    the corpus later, and an intermediate should carry a semantic name anyway.

    Refusing to DECLARE it means it falls through to the ordinary D64 fail-closed path rather
    than being special-cased: the blueprint is refused, not quietly accepted.
    """
    from data_agent.learning.generalize.builder import _declared_scratch

    assert _declared_scratch({"consumes": {"emp_earnings": "$0"}}) == {"emp_earnings"}
    assert _declared_scratch({"consumes": {"s_victim_bp_abc": "$0"}}) == frozenset()
    # A SCALAR consume is a bound token, never a table source, so it is never declared either.
    assert _declared_scratch({"consumes": {"total": "$0.total"}}) == frozenset()
