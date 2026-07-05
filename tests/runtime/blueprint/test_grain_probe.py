"""Layer-1 — the SHARED D56 grain-probe helper (`runtime/blueprint/grain_probe.py`).

The whole point of extracting this module is that the live executor and the S9
offline golden-replay probe build the grain probe from ONE source, so the offline
oracle can never drift from the live one (`S9-probe-oracle-parity`). These tests pin
the SQL shape + the fail-closed column mapping + the count unpack, and assert the
executor's call sites bind to the exact shared functions (no copy).
"""

from __future__ import annotations

from data_agent.runtime.blueprint import executor, grain_probe
from data_agent.runtime.blueprint.grain_probe import (
    build_grain_probe_sql,
    map_grain_columns,
    unpack_grain_probe,
)

# --- S9-probe-oracle-parity: the executor uses the SHARED builder (no copy) -----


def test_executor_binds_the_shared_grain_probe_helpers() -> None:
    """The executor's private aliases ARE the shared module's functions (identity),
    so the live probe and the offline replay probe can never drift."""
    assert executor._grain_probe_sql is grain_probe.build_grain_probe_sql
    assert executor._map_grain_columns is grain_probe.map_grain_columns
    assert executor._unpack_grain_probe is grain_probe.unpack_grain_probe


# --- build_grain_probe_sql: the COUNT(*) / COUNT(DISTINCT) shape -----------------


def test_build_grain_probe_sql_shape() -> None:
    sql = build_grain_probe_sql(
        "SELECT department, sum(gross_pay) AS total FROM db.t GROUP BY department",
        ["department"],
    )
    low = sql.lower()
    assert "count(*)" in low  # the fan-out canary numerator
    assert "count(distinct department)" in low
    assert "__bp_n" in sql and "__bp_d" in sql  # the aliased count columns
    assert "__bp_sub" in sql  # the inner SQL is a structural subquery, not spliced


def test_build_grain_probe_sql_embeds_inner_as_subquery() -> None:
    """The final SQL is embedded as a subquery (AST), never string-concatenated."""
    inner = "SELECT a AS dept FROM db.t WHERE b = 'x'"
    sql = build_grain_probe_sql(inner, ["dept"])
    assert "from (select" in sql.lower().replace("\n", " ")


# --- map_grain_columns: fail-closed declared-grain → output-column mapping -------


def test_map_grain_columns_exact_and_casefold() -> None:
    assert map_grain_columns("SELECT a AS department FROM t", ("department",)) == [
        "department"
    ]
    # exact wins over a casefold collision
    assert map_grain_columns("SELECT a AS dept, b AS DEPT FROM t", ("dept",)) == ["dept"]


def test_map_grain_columns_fail_closed_none() -> None:
    # ambiguous casefold collision → None (never COUNT(DISTINCT) a guessed column)
    assert map_grain_columns("SELECT a AS dept, b AS DEPT FROM t", ("Dept",)) is None
    # no matching output column → None
    assert map_grain_columns("SELECT a AS dept FROM t", ("headcount",)) is None
    # a SELECT * cannot be mapped → None
    assert map_grain_columns("SELECT * FROM t", ("dept",)) is None


# --- unpack_grain_probe: (total, distinct) with fail-closed surprises ------------


def test_unpack_grain_probe_reads_the_two_counts() -> None:
    raw = {"columns": ["__bp_n", "__bp_d"], "rows": [[5, 3]], "row_count": 1}
    assert unpack_grain_probe(raw) == (5, 3)


def test_unpack_grain_probe_fail_closed_on_surprises() -> None:
    assert unpack_grain_probe("not-a-dict") == (None, None)
    assert unpack_grain_probe({"rows": []}) == (None, None)
    assert unpack_grain_probe({"rows": [[1]]}) == (None, None)  # < 2 columns
    assert unpack_grain_probe({"rows": [["x", "y"]]}) == (None, None)  # non-int cells
