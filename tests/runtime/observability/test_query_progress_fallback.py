"""Query-shaped fallback copy is varied, stable, and never includes SQL values."""

import pytest

from data_agent.runtime.observability.progress_summarizer import (
    _QUERY_FALLBACKS,
    _query_kind,
    _static_line,
)


@pytest.mark.parametrize(
    "sql,kind",
    [
        ("SELECT count(*) FROM payroll WHERE code = 'private'", "count"),
        ("SELECT avg(amount) FROM payroll", "average"),
        ("SELECT sum(amount) FROM payroll", "total"),
        ("SELECT department, count(*) FROM payroll GROUP BY department", "grouped"),
        (
            "SELECT department, sum(amount) AS total FROM payroll GROUP BY department ORDER BY total DESC LIMIT 5",
            "ranked",
        ),
        (
            "SELECT toStartOfMonth(paid_at), sum(amount) FROM payroll GROUP BY toStartOfMonth(paid_at)",
            "trend",
        ),
        ("SELECT employee FROM payroll WHERE amount > (SELECT avg(amount) FROM payroll)", "lookup"),
    ],
)
def test_query_shape_selects_safe_equivalent_copy(sql, kind):
    assert _query_kind(sql) == kind
    line = _static_line("runQuery", {"sql": sql})
    assert line in _QUERY_FALLBACKS[kind]
    assert line == _static_line("runQuery", {"sql": sql})
    assert line[0].isupper()
    assert not any(value in line for value in ("payroll", "private", "amount", "paid_at"))


@pytest.mark.parametrize(
    "sql", [None, "", 123, "SELECT (", "x" * 16001, "SELECT avg(amount), sum(amount) FROM payroll"]
)
def test_unknown_shape_remains_generic(sql):
    assert _static_line("runQuery", {"sql": sql}) == "Finding the requested information"


def test_similar_queries_can_select_different_equivalent_phrasings():
    lines = {
        _static_line("runQuery", {"sql": f"SELECT count(*) FROM t WHERE id = {n}"})
        for n in range(30)
    }
    assert lines == set(_QUERY_FALLBACKS["count"])
