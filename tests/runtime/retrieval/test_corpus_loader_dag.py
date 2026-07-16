"""Layer-1: full-DAG corpus-loader write-time validation (runblueprint-design §1.2).

Full-DAG round-trip through the seeds + the validation matrix: an inconsistent
template↔slots, a column ⊄ uses, an unparseable template, a malformed composes /
cycle, a bad result_grain, and an entity-valued `when` all ⇒ `CorpusLoadError`.
The existing D87/D88 fixtures still load (additive, no migration).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _dag_properties,
    _validate_blueprint_dag,
    load_seed_fixtures,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"


def _seed(**overrides: object) -> BlueprintSeed:
    base = {
        "id": "bp-x",
        "intent": "x",
        "slots_summary": "department",
        "uses": ["dbpcm_warehouse.employee.Department", "dbpcm_warehouse.employee.EmployeeCode"],
        "slots": [{"name": "department", "type": "string", "required": True}],
        "result_grain": ["Department"],
        "sql_template": (
            "SELECT Department FROM dbpcm_warehouse.employee WHERE Department = {department}"
        ),
    }
    base.update(overrides)
    return BlueprintSeed(**base)  # type: ignore[arg-type]


# -- happy path + round-trip ------------------------------------------------


def test_seed_fixtures_all_validate_and_serialize() -> None:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    assert {b.id for b in blueprints} == {
        "bp-overtime-by-department",
        "bp-active-headcount-by-department",
        "bp-average-salary-by-department",
        "bp-total-earnings-by-department",
        "bp-departments-above-company-average-salary",
        "bp-earnings-by-department-via-scratch-join",
        "bp-hires-per-month",
        "bp-hires-in-range",
    }
    # The Department-grained, {department}-filtered seeds — the windowed-period
    # seeds (relative_window / period_range) have a `month` grain and their own
    # window tokens, so the content assertions below scope to this set.
    department_seeds = {
        "bp-overtime-by-department",
        "bp-active-headcount-by-department",
        "bp-average-salary-by-department",
        "bp-total-earnings-by-department",
        "bp-departments-above-company-average-salary",
        "bp-earnings-by-department-via-scratch-join",
    }
    for bp in blueprints:
        _validate_blueprint_dag(bp)  # no raise
        props = _dag_properties(bp)
        if bp.id not in department_seeds:
            continue
        # result_grain round-trips as a JSON list; sql_template stored verbatim.
        assert json.loads(props["result_grain_json"]) == ["Department"]
        # Single-node seeds carry a top-level {department}-parameterized template;
        # the Slice-C multi-node seed stores its SQL per-node in composes instead.
        if bp.sql_template is not None:
            assert "{department}" in props["sql_template"]
        else:
            assert props["sql_template"] is None
            assert props["composes_json"] is not None


def test_valid_synthetic_seed_passes() -> None:
    _validate_blueprint_dag(_seed())


def test_dag_less_seed_still_valid_and_serializes_null() -> None:
    # A D87/D88-style seed with NO DAG fields (additive back-compat).
    bp = BlueprintSeed(
        id="legacy", intent="x", slots_summary="", uses=["a.b.c"],
    )
    _validate_blueprint_dag(bp)
    props = _dag_properties(bp)
    assert props["sql_template"] is None
    assert props["result_grain_json"] is None
    assert props["slots_json"] is None


# -- validation matrix (each ⇒ CorpusLoadError) -----------------------------


def test_undeclared_slot_token_rejected() -> None:
    bp = _seed(
        sql_template="SELECT Department FROM dbpcm_warehouse.employee "
        "WHERE Department = {department} AND x = {undeclared}"
    )
    with pytest.raises(CorpusLoadError, match="undeclared slot"):
        _validate_blueprint_dag(bp)


def test_column_outside_uses_rejected() -> None:
    # AnnualSalary is referenced but NOT in `uses`.
    bp = _seed(
        sql_template="SELECT AnnualSalary FROM dbpcm_warehouse.employee "
        "WHERE Department = {department}"
    )
    with pytest.raises(CorpusLoadError, match="outside the declared uses footprint"):
        _validate_blueprint_dag(bp)


def test_unparseable_template_rejected() -> None:
    bp = _seed(sql_template="SELECT FROM WHERE {department}")
    with pytest.raises(CorpusLoadError, match="does not parse"):
        _validate_blueprint_dag(bp)


def test_bad_result_grain_rejected() -> None:
    bp = _seed(result_grain=[1, 2, 3])
    with pytest.raises(CorpusLoadError, match="malformed DAG"):
        _validate_blueprint_dag(bp)


def test_bad_slot_type_rejected() -> None:
    bp = _seed(slots=[{"name": "department", "type": "nonsense"}])
    with pytest.raises(CorpusLoadError, match="malformed DAG"):
        _validate_blueprint_dag(bp)


def test_composes_cycle_rejected() -> None:
    bp = _seed(
        sql_template=None,
        slots=[],  # B3(a): a composes DAG that uses no department filter declares no slot
        composes=[
            {"order": 1, "feeds_from": [2], "output": {"a": "scalar"},
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee"},
            {"order": 2, "feeds_from": [1], "output": {"b": "scalar"},
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee"},
        ],
    )
    with pytest.raises(CorpusLoadError, match="cycle"):
        _validate_blueprint_dag(bp)


def test_composes_dangling_feeds_from_rejected() -> None:
    bp = _seed(
        sql_template=None,
        slots=[],  # B3(a): no department filter → no declared slot
        composes=[
            {"order": 1, "feeds_from": [99], "output": {"a": "scalar"},
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee"},
        ],
    )
    with pytest.raises(CorpusLoadError, match="feeds_from unknown node"):
        _validate_blueprint_dag(bp)


def test_entity_valued_when_clause_rejected() -> None:
    bp = _seed(
        sql_template=None,
        slots=[],  # B3(a): no department filter → no declared slot
        composes=[
            {"order": 1, "output": {"a": "scalar"},
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee",
             "when": {"expr": "$1.department = 'Warehouse'", "on_violation": "abort"}},
        ],
    )
    with pytest.raises(CorpusLoadError, match="when-clause invalid"):
        _validate_blueprint_dag(bp)


def test_node_template_column_outside_uses_rejected() -> None:
    bp = _seed(
        sql_template=None,
        composes=[
            {"order": 1, "output": {"a": "scalar"},
             "sql_template": "SELECT AnnualSalary FROM dbpcm_warehouse.employee"},
        ],
    )
    with pytest.raises(CorpusLoadError, match="node 1 sql_template"):
        _validate_blueprint_dag(bp)


def test_multi_statement_injection_rejected() -> None:
    # FIX 2: a trailing DDL contributes no columns, so it must be caught by the
    # statement-kind guard, not waved through the footprint check.
    bp = _seed(
        sql_template="SELECT Department FROM dbpcm_warehouse.employee; DROP TABLE payroll"
    )
    with pytest.raises(CorpusLoadError, match="does not parse"):
        _validate_blueprint_dag(bp)


def test_dict_function_rejected() -> None:
    # FIX 1: a dictGet-family function reads a source invisible to the column walk.
    bp = _seed(
        sql_template="SELECT dictGet('d', 'x', EmployeeCode) FROM dbpcm_warehouse.employee "
        "WHERE Department = {department}"
    )
    with pytest.raises(CorpusLoadError, match="dictionary function"):
        _validate_blueprint_dag(bp)


def test_select_star_rejected() -> None:
    bp = _seed(sql_template="SELECT * FROM dbpcm_warehouse.employee WHERE Department = {department}")
    with pytest.raises(CorpusLoadError, match=r"uses `\*`"):
        _validate_blueprint_dag(bp)


_USES2 = [
    "dbpcm_warehouse.employee.Department",
    "dbpcm_warehouse.employee.EmployeeCode",
    "dbpcm_warehouse.payroll.EmployeeCode",
    "dbpcm_warehouse.payroll.Amount",
]


def _seed2(template: str) -> BlueprintSeed:
    return BlueprintSeed(
        id="bp-j", intent="x", slots_summary="department", uses=list(_USES2),
        slots=[{"name": "department", "type": "string", "required": True}],
        result_grain=["Department"], sql_template=template,
    )


# -- JOIN source-table scope (re-review BLOCKER) ----------------------------
# These vectors are ACCEPTED without the source-table check (a JOIN to a table
# absent from `uses` reads arbitrary columns via alias-qualified refs) and must
# now REJECT. The legit-JOIN (both tables in uses) must still ACCEPT.


def test_qualified_join_to_unlisted_table_rejected() -> None:
    bp = _seed(
        sql_template=(
            "SELECT e.Department, p.SSN FROM dbpcm_warehouse.employee AS e "
            "JOIN dbpcm_warehouse.secretpayroll AS p ON e.EmployeeCode = p.EmployeeCode "
            "WHERE e.Department = {department}"
        )
    )
    with pytest.raises(CorpusLoadError, match="source table 'dbpcm_warehouse.secretpayroll'"):
        _validate_blueprint_dag(bp)


def test_fully_qualified_join_to_unlisted_table_rejected() -> None:
    bp = _seed(
        sql_template=(
            "SELECT dbpcm_warehouse.employee.Department, dbpcm_warehouse.secretpayroll.SSN "
            "FROM dbpcm_warehouse.employee "
            "JOIN dbpcm_warehouse.secretpayroll "
            "ON dbpcm_warehouse.employee.EmployeeCode = dbpcm_warehouse.secretpayroll.EmployeeCode "
            "WHERE dbpcm_warehouse.employee.Department = {department}"
        )
    )
    with pytest.raises(CorpusLoadError, match="not in the declared uses footprint"):
        _validate_blueprint_dag(bp)


def test_cross_join_to_unlisted_table_rejected() -> None:
    bp = _seed(
        sql_template=(
            "SELECT e.Department, p.SSN FROM dbpcm_warehouse.employee AS e "
            "CROSS JOIN dbpcm_warehouse.secretpayroll AS p WHERE e.Department = {department}"
        )
    )
    with pytest.raises(CorpusLoadError, match="source table"):
        _validate_blueprint_dag(bp)


def test_legit_join_both_tables_in_uses_accepted() -> None:
    # A JOIN across two tables BOTH declared in `uses`, every column in uses → OK.
    _validate_blueprint_dag(
        _seed2(
            "SELECT e.Department AS dept, SUM(p.Amount) AS total "
            "FROM dbpcm_warehouse.employee AS e "
            "JOIN dbpcm_warehouse.payroll AS p ON e.EmployeeCode = p.EmployeeCode "
            "WHERE e.Department = {department} GROUP BY e.Department"
        )
    )


def test_cte_source_is_not_treated_as_an_unlisted_table() -> None:
    # A CTE name is the query's own derived table, not a warehouse source — it must
    # not be flagged as "not in uses"; the CTE's REAL table columns are still checked.
    _validate_blueprint_dag(
        _seed(
            sql_template=(
                "WITH x AS (SELECT Department, EmployeeCode FROM dbpcm_warehouse.employee) "
                "SELECT Department FROM x WHERE Department = {department}"
            )
        )
    )


def test_ambiguous_bare_column_across_two_in_scope_tables_rejected() -> None:
    # PIN (conscious choice): an unqualified column that exists in TWO in-scope
    # tables (EmployeeCode in both employee + payroll, both in uses) is ambiguous →
    # qualify fail-closes → CorpusLoadError. Acceptable authoring hygiene: qualify
    # the column. Pinned so the fail-closed behaviour is deliberate, not accidental.
    bp = _seed2(
        "SELECT Department, EmployeeCode FROM dbpcm_warehouse.employee AS e "
        "JOIN dbpcm_warehouse.payroll AS p ON e.EmployeeCode = p.EmployeeCode "
        "WHERE Department = {department}"
    )
    with pytest.raises(CorpusLoadError):
        _validate_blueprint_dag(bp)


def test_group_by_output_alias_rejected_constraint_pin() -> None:
    # PIN (conscious constraint, documented in runblueprint-design §1.2): with
    # expand_alias_refs=False (which closes the alias-mask scope hole), a GROUP BY
    # that references a SELECT OUTPUT ALIAS (not a real column) fail-closes. Authors
    # must GROUP BY the real column. Fail-closed (ergonomics, not security); pinned
    # as a deliberate trade-off rather than weakening the scope check.
    bp = _seed(
        sql_template=(
            "SELECT Department AS dept, COUNT(EmployeeCode) AS c "
            "FROM dbpcm_warehouse.employee GROUP BY dept"
        )
    )
    with pytest.raises(CorpusLoadError):
        _validate_blueprint_dag(bp)


def test_group_by_real_column_accepted() -> None:
    _validate_blueprint_dag(
        _seed(
            slots=[],  # B3(a): this template uses no {department} filter → no declared slot
            sql_template=(
                "SELECT Department AS dept, COUNT(EmployeeCode) AS c "
                "FROM dbpcm_warehouse.employee GROUP BY Department"
            )
        )
    )


def test_cross_database_reference_rejected() -> None:
    # FIX 1 (reviewer cross-db case): a read from a table in a DIFFERENT database
    # than any `uses` key (even if a bare column name matches) is caught at the
    # SOURCE-TABLE level — the db-blind hole is closed.
    bp = _seed(
        uses=["otherdb.employee.Department", "otherdb.employee.EmployeeCode"],
        sql_template="SELECT Department FROM dbpcm_warehouse.employee "
        "WHERE Department = {department}",
    )
    with pytest.raises(CorpusLoadError, match="source table 'dbpcm_warehouse.employee'"):
        _validate_blueprint_dag(bp)


def test_deep_compose_dag_over_cap_rejected() -> None:
    # FIX 3: an adversarially large composes DAG fails loud (node-count cap), never
    # a RecursionError.
    composes = [
        {"order": i, "feeds_from": [i + 1] if i < 200 else [], "output": {"a": "scalar"},
         "sql_template": "SELECT Department FROM dbpcm_warehouse.employee"}
        for i in range(1, 201)
    ]
    bp = _seed(sql_template=None, slots=[], composes=composes)
    with pytest.raises(CorpusLoadError, match="node cap"):
        _validate_blueprint_dag(bp)


def test_valid_when_clause_accepted() -> None:
    bp = _seed(
        sql_template=None,
        slots=[],  # B3(a): no department filter → no declared slot
        composes=[
            # S5: a `count(...)` threshold is valid over a TABLE-shaped output (a
            # scalar output's row count is ≤1 by contract and is rejected).
            {"order": 1, "output": {"a": "table"},
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee",
             "when": {"expr": "count($1) > 0", "on_violation": "skip"}},
        ],
    )
    _validate_blueprint_dag(replace(bp))  # no raise


# -- Slice-C review: additional load-time gates ------------------------------

_STATUS = "dbpcm_warehouse.employee.EmployeeStatus"


def test_hybrid_sql_template_and_composes_rejected() -> None:
    # B4: a blueprint with BOTH a top-level sql_template AND composes has no
    # execution mode (the top template is dead) — reject outright.
    bp = _seed(
        slots=[],
        composes=[
            {"order": 0, "output": {}, "sql_template": "SELECT Department FROM dbpcm_warehouse.employee GROUP BY Department"},
        ],
        # inherits the base top-level sql_template → hybrid
    )
    with pytest.raises(CorpusLoadError, match="BOTH a top-level sql_template and a composes"):
        _validate_blueprint_dag(bp)


def test_resolve_via_rule_probing_column_outside_uses_rejected() -> None:
    # S6a: a rule's probed column must be within the declared uses — even when the
    # TEMPLATE footprint is clean (the rule probes a DIFFERENT table's column).
    bp = _seed(
        uses=["dbpcm_warehouse.employee.EmployeeCode", "dbpcm_warehouse.employee.Department"],
        slots=[],
        sql_template="SELECT count() AS n FROM dbpcm_warehouse.employee WHERE Department IN {codes}",
        uses_rules=[
            # probes payroll.RegisterType, which is NOT in uses (template is clean).
            {"id": "s", "resolve_via": "resolveValues(RegisterType, 'earnings')", "table": "dbpcm_warehouse.payroll", "binds": "codes"}
        ],
        result_grain=[],
    )
    with pytest.raises(CorpusLoadError, match="resolve_via rule .* probes .* NOT in the declared uses"):
        _validate_blueprint_dag(bp)


def test_consumes_referencing_non_scalar_upstream_rejected() -> None:
    # S6b: consumes $P.name must name a declared SCALAR output of an upstream P.
    bp = _seed(
        slots=[],
        sql_template=None,
        composes=[
            {"order": 0, "output": {"tbl": "table"}, "sql_template": "SELECT Department FROM dbpcm_warehouse.employee GROUP BY Department"},
            {"order": 1, "feeds_from": [0], "consumes": {"v": "$0.tbl"},
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee WHERE Department = {v} GROUP BY Department", "output": {}},
        ],
    )
    with pytest.raises(CorpusLoadError, match="no declared SCALAR output"):
        _validate_blueprint_dag(bp)


def test_consumes_from_non_feeds_from_node_rejected() -> None:
    bp = _seed(
        slots=[],
        sql_template=None,
        composes=[
            {"order": 0, "output": {"v": "scalar"}, "sql_template": "SELECT count() AS v FROM dbpcm_warehouse.employee"},
            {"order": 1, "output": {"v2": "scalar"}, "sql_template": "SELECT count() AS v2 FROM dbpcm_warehouse.employee"},
            {"order": 2, "feeds_from": [1], "consumes": {"v": "$0.v"},  # consumes 0 but feeds_from 1
             "sql_template": "SELECT Department FROM dbpcm_warehouse.employee WHERE Department = {v} GROUP BY Department", "output": {}},
        ],
    )
    with pytest.raises(CorpusLoadError, match="not in its feeds_from"):
        _validate_blueprint_dag(bp)


def test_terminal_approval_node_rejected() -> None:
    # S4: an approval gate that is a topo sink with no query gates nothing.
    bp = _seed(
        slots=[],
        sql_template=None,
        composes=[
            {"order": 0, "output": {"v": "scalar"}, "sql_template": "SELECT count() AS v FROM dbpcm_warehouse.employee"},
            {"order": 1, "node_kind": "approval", "feeds_from": [0], "output": {}},  # terminal approval
        ],
    )
    with pytest.raises(CorpusLoadError, match="TERMINAL approval gate"):
        _validate_blueprint_dag(bp)


def test_non_terminal_approval_gating_a_query_is_accepted() -> None:
    bp = _seed(
        slots=[],
        sql_template=None,
        composes=[
            {"order": 0, "output": {"v": "scalar"}, "sql_template": "SELECT count() AS v FROM dbpcm_warehouse.employee"},
            {"order": 1, "node_kind": "approval", "feeds_from": [0], "output": {},
             "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department"},  # approval WITH its own query
        ],
    )
    _validate_blueprint_dag(bp)  # no raise — the approval runs its own query


def test_count_threshold_over_scalar_output_rejected() -> None:
    # S5: count($N) over a scalar-output node is meaningless (row count ≤ 1).
    bp = _seed(
        slots=[],
        sql_template=None,
        composes=[
            {"order": 0, "output": {"v": "scalar"}, "sql_template": "SELECT count() AS v FROM dbpcm_warehouse.employee"},
            {"order": 1, "feeds_from": [0], "when": {"expr": "count($0) > 5", "on_violation": "skip"},
             "sql_template": "SELECT Department AS department FROM dbpcm_warehouse.employee GROUP BY Department", "output": {}},
        ],
    )
    with pytest.raises(CorpusLoadError, match="count.* to a SCALAR-output node"):
        _validate_blueprint_dag(bp)
