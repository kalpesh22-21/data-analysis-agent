"""QA6 Layer-1: Slice-A scope-honesty gate SURVIVES the Slice-C placeholder paths.

Slice C widened the loader's "undeclared placeholder" check so a node template may
reference a `consumes` upstream-scalar name or a `resolve_via` rule `binds` name
(runblueprint-design §1.2 change). This suite re-runs the Slice-A footprint class
(column ⊄ uses, `SELECT *`, alias-mask, JOIN-to-unlisted) THROUGH a `consumes` /
`resolve_via` template to prove the column/table scope teeth STILL bite — the new
placeholders are value bind-sites, never a licence to read an unadvertised column.

A regression here (any of these LOADING) is a HIGH scope-honesty escape.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _validate_blueprint_dag,
)

_E = "dbpcm_warehouse.employee"
_DEPT = f"{_E}.Department"
_CODE = f"{_E}.EmployeeCode"
_STATUS = f"{_E}.StatusCode"


def _scalar0(output_name: str = "tok", col: str = "Department") -> dict:
    return {
        "order": 0,
        "output": {output_name: "scalar"},
        "sql_template": f"SELECT {col} AS {output_name} FROM dbpcm_warehouse.employee LIMIT 1",
    }


def _consumes_seed(node1_sql: str, *, uses: list[str]) -> BlueprintSeed:
    """A 2-node consumes DAG: node 0 emits scalar `tok`, node 1 consumes it."""
    return BlueprintSeed(
        id="bp-scope",
        intent="scope regression",
        slots_summary="",
        uses=uses,
        result_grain=["Department"],
        composes=[
            _scalar0(),
            {
                "order": 1,
                "feeds_from": [0],
                "consumes": {"tok": "$0.tok"},
                "output": {},
                "sql_template": node1_sql,
            },
        ],
    )


def _rule_seed(node_sql: str, *, uses: list[str]) -> BlueprintSeed:
    """A single-node DAG whose template binds a `resolve_via` rule IN-list."""
    return BlueprintSeed(
        id="bp-rule-scope",
        intent="rule scope regression",
        slots_summary="",
        uses=uses,
        result_grain=["Department"],
        uses_rules=[
            {
                "id": "active",
                "resolve_via": "resolveValues(StatusCode, 'active employee')",
                "table": _E,
                "binds": "status_codes",
            }
        ],
        composes=[{"order": 0, "output": {}, "sql_template": node_sql}],
    )


# ---------------------------------------------------------------------------
# consumes template: column ⊄ uses / star / alias-mask / JOIN-to-unlisted
# ---------------------------------------------------------------------------


def test_consumes_node_reading_column_outside_uses_rejected() -> None:
    # node 1 consumes a scalar AND reads AnnualSalary, which is NOT in uses.
    seed = _consumes_seed(
        "SELECT AnnualSalary AS s FROM dbpcm_warehouse.employee WHERE Department = {tok}",
        uses=[_DEPT, _CODE],
    )
    with pytest.raises(CorpusLoadError, match="outside the declared uses footprint"):
        _validate_blueprint_dag(seed)


def test_consumes_node_select_star_rejected() -> None:
    seed = _consumes_seed(
        "SELECT * FROM dbpcm_warehouse.employee WHERE Department = {tok}",
        uses=[_DEPT, _CODE],
    )
    with pytest.raises(CorpusLoadError, match=r"uses `\*`"):
        _validate_blueprint_dag(seed)


def test_consumes_node_alias_mask_group_by_fails_closed() -> None:
    # GROUP BY an OUTPUT ALIAS (not the real column) must fail-close under
    # expand_alias_refs=False — the alias-mask evasion, in a consumes template.
    seed = _consumes_seed(
        "SELECT Department AS dept, count() AS n FROM dbpcm_warehouse.employee "
        "WHERE Department = {tok} GROUP BY dept",
        uses=[_DEPT, _CODE],
    )
    with pytest.raises(CorpusLoadError):
        _validate_blueprint_dag(seed)


def test_consumes_node_join_to_unlisted_table_rejected() -> None:
    seed = _consumes_seed(
        "SELECT e.Department AS department, p.SSN AS ssn "
        "FROM dbpcm_warehouse.employee AS e "
        "JOIN dbpcm_warehouse.secretpayroll AS p ON e.EmployeeCode = p.EmployeeCode "
        "WHERE e.Department = {tok}",
        uses=[_DEPT, _CODE],
    )
    with pytest.raises(CorpusLoadError, match="source table|uses footprint"):
        _validate_blueprint_dag(seed)


# ---------------------------------------------------------------------------
# resolve_via template: the rule `binds` placeholder is NOT a scope loophole
# ---------------------------------------------------------------------------


def test_resolve_via_node_reading_column_outside_uses_rejected() -> None:
    # The {status_codes} rule bind is allowed as a placeholder, but the template
    # still reads AnnualSalary (⊄ uses) → rejected on the column footprint.
    seed = _rule_seed(
        "SELECT AnnualSalary AS s FROM dbpcm_warehouse.employee "
        "WHERE StatusCode IN {status_codes}",
        uses=[_DEPT, _CODE, _STATUS],
    )
    with pytest.raises(CorpusLoadError, match="outside the declared uses footprint"):
        _validate_blueprint_dag(seed)


def test_resolve_via_node_filtering_column_outside_uses_rejected() -> None:
    # Even the FILTER column (SecretField) behind the rule IN-list must be in uses;
    # StatusCode is in uses here but SecretField is not.
    seed = _rule_seed(
        "SELECT Department AS department FROM dbpcm_warehouse.employee "
        "WHERE SecretField IN {status_codes} GROUP BY Department",
        uses=[_DEPT, _CODE, _STATUS],
    )
    with pytest.raises(CorpusLoadError, match="outside the declared uses footprint"):
        _validate_blueprint_dag(seed)


def test_resolve_via_node_join_to_unlisted_table_rejected() -> None:
    seed = _rule_seed(
        "SELECT e.Department AS department FROM dbpcm_warehouse.employee AS e "
        "JOIN dbpcm_warehouse.secretpayroll AS p ON e.EmployeeCode = p.EmployeeCode "
        "WHERE e.StatusCode IN {status_codes} GROUP BY e.Department",
        uses=[_DEPT, _CODE, _STATUS],
    )
    with pytest.raises(CorpusLoadError, match="source table|uses footprint"):
        _validate_blueprint_dag(seed)


# ---------------------------------------------------------------------------
# Legit counter-cases: a scope-clean consumes / resolve_via template ACCEPTS
# (so the tests above prove the teeth, not a blanket reject).
# ---------------------------------------------------------------------------


def test_scope_clean_consumes_dag_accepts() -> None:
    seed = _consumes_seed(
        "SELECT Department AS department FROM dbpcm_warehouse.employee "
        "WHERE Department = {tok} GROUP BY Department",
        uses=[_DEPT, _CODE],
    )
    _validate_blueprint_dag(seed)  # no raise


def test_scope_clean_resolve_via_node_accepts() -> None:
    seed = _rule_seed(
        "SELECT Department AS department FROM dbpcm_warehouse.employee "
        "WHERE StatusCode IN {status_codes} GROUP BY Department",
        uses=[_DEPT, _CODE, _STATUS],
    )
    _validate_blueprint_dag(seed)  # no raise
