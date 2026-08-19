"""QA4 Layer-1: corpus-loader full-DAG validation — adversarial (§1.2).

Extends `test_corpus_loader_dag.py`. The load-time footprint check (§1.2(c)) is a
SCOPE GUARD: the design states it guarantees "a blueprint cannot reference a column
outside its own advertised footprint." This file attacks that guarantee.

Three constructs currently EVADE the footprint ⊆ uses check and are filed as
`xfail(strict=True)` scope-bypass repros (they SHOULD raise `CorpusLoadError`):
  1. alias-mask   — `SELECT <in-scope> AS <out-of-scope-name>` masks a real read of
                    the out-of-scope column elsewhere (the footprint subtraction
                    removes it by name).  Severity: HIGH.
  2. SELECT *     — a star has an empty column footprint, so it passes trivially
                    while reading every column, in-scope or not. Severity: HIGH.
  3. subquery *   — the same star bypass inside a subquery. Severity: HIGH.

Plus DAG-shape attacks (self-edge, duplicate order, forward-ref deep-chain
recursion) and a determinism/round-trip check.

ADD-only; does not modify the reviewer-owned `test_corpus_loader_dag.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
)

_USES = [
    "dbpcm_warehouse.employee.Department",
    "dbpcm_warehouse.employee.EmployeeCode",
]


def _seed(**overrides: object) -> BlueprintSeed:
    base = {
        "id": "bp-x",
        "intent": "x",
        "slots_summary": "department",
        "uses": list(_USES),
        "slots": [{"name": "department", "type": "string", "required": True}],
        "result_grain": ["Department"],
        "sql_template": (
            "SELECT Department FROM dbpcm_warehouse.employee WHERE Department = {department}"
        ),
    }
    base.update(overrides)
    return BlueprintSeed(**base)  # type: ignore[arg-type]


# -- SCOPE-BYPASS repros (should reject; currently accept) ------------------


# PROMOTED (review FIX 1): the table-aware qualify-against-uses footprint check
# (expand_alias_refs=False) now REJECTS the alias-mask evasion.
def test_alias_mask_reading_out_of_scope_column_should_be_rejected() -> None:
    # AnnualSalary is NOT in uses. Aliasing `Department AS AnnualSalary` puts the
    # name "AnnualSalary" into the DEFINED set, so the WHERE `AnnualSalary > 0`
    # read of the REAL out-of-scope column is subtracted from the footprint.
    bp = _seed(
        sql_template=(
            "SELECT Department AS AnnualSalary FROM dbpcm_warehouse.employee "
            "WHERE AnnualSalary > 0 AND Department = {department}"
        )
    )
    with pytest.raises(CorpusLoadError):
        validate_blueprint_dag(bp)


# PROMOTED (review FIX 1a): a `*` is rejected outright — a blueprint must name
# its columns so its scope footprint is verifiable.
def test_select_star_reading_all_columns_should_be_rejected() -> None:
    bp = _seed(
        sql_template="SELECT * FROM dbpcm_warehouse.employee WHERE Department = {department}"
    )
    with pytest.raises(CorpusLoadError):
        validate_blueprint_dag(bp)


# PROMOTED (review FIX 1a): the `*` guard walks the whole tree, so a star inside
# a subquery is rejected too.
def test_subquery_select_star_should_be_rejected() -> None:
    bp = _seed(
        sql_template=(
            "SELECT Department FROM (SELECT * FROM dbpcm_warehouse.employee) "
            "WHERE Department = {department}"
        )
    )
    with pytest.raises(CorpusLoadError):
        validate_blueprint_dag(bp)


# -- DAG-shape attacks (these ARE rejected — confirm) -----------------------


def test_self_edge_rejected_as_cycle() -> None:
    bp = _seed(
        sql_template=None,
        slots=[],  # B3(a): no department filter → no declared slot
        composes=[
            {
                "order": 1,
                "feeds_from": [1],  # self-edge
                "output": {"a": "scalar"},
                "sql_template": "SELECT Department FROM dbpcm_warehouse.employee",
            }
        ],
    )
    with pytest.raises(CorpusLoadError, match="cycle"):
        validate_blueprint_dag(bp)


def test_duplicate_node_order_rejected() -> None:
    bp = _seed(
        sql_template=None,
        slots=[],  # B3(a): no department filter → no declared slot
        composes=[
            {
                "order": 1,
                "output": {"a": "scalar"},
                "sql_template": "SELECT Department FROM dbpcm_warehouse.employee",
            },
            {
                "order": 1,  # duplicate order
                "output": {"b": "scalar"},
                "sql_template": "SELECT Department FROM dbpcm_warehouse.employee",
            },
        ],
    )
    with pytest.raises(CorpusLoadError, match="duplicate node 'order'"):
        validate_blueprint_dag(bp)


# PROMOTED (review FIX 3): a hard node-count cap + iterative DFS — a huge/adversarial
# DAG now fails LOUD with a clean CorpusLoadError, never a RecursionError.
def test_deep_forward_ref_chain_fails_cleanly_not_with_recursionerror() -> None:
    n = 4000
    composes = [
        {
            "order": i,
            "feeds_from": [i + 1] if i < n else [],
            "output": {"a": "scalar"},
            "sql_template": "SELECT Department FROM dbpcm_warehouse.employee",
        }
        for i in range(1, n + 1)
    ]
    bp = _seed(sql_template=None, slots=[], composes=composes)
    with pytest.raises(CorpusLoadError):
        validate_blueprint_dag(bp)


# -- declared-but-unreferenced slot (accepted at load — pin) ----------------


def test_declared_but_unreferenced_required_slot_is_now_rejected() -> None:
    # PIN FLIPPED (reviewer B3(a) fix landed): a declared REQUIRED slot the template
    # never references is now a CorpusLoadError — the load/execute contract mismatch
    # this pin flagged in Slice A is closed. An unreferenced required slot is a
    # silent dropped filter (the D56 wrong-answer class), so it fails LOUD at write.
    bp = _seed(
        slots=[
            {"name": "department", "type": "string"},
            {"name": "ghost", "type": "string"},  # required (default), never referenced
        ]
    )
    with pytest.raises(CorpusLoadError, match="ghost"):
        validate_blueprint_dag(bp)


# -- result_grain columns are NOT checked against uses (pin) ----------------


def test_result_grain_column_not_in_uses_is_accepted_at_load() -> None:
    # PIN: `result_grain` is a declared OUTPUT grain (aliases), not warehouse
    # columns, so it is intentionally not checked against `uses`. But it is ALSO
    # not checked against the template's output columns — a grain naming a column
    # absent from the SELECT would only fail at the Slice-B probe. Flagged.
    bp = _seed(result_grain=["TotallyMadeUpColumn"])
    validate_blueprint_dag(bp)  # no raise today
