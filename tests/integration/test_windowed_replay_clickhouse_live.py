"""Layer-2 integration — golden replay's WINDOWED slot sampling, against live ClickHouse.

What this proves, and why a unit test could not
-----------------------------------------------
Plan §3a widened the extractor's `SLOT_TYPES` with `relative_window` / `period_range`
and fixed `promotion/replay.py`'s sampler, which had one default branch ("every other
type ⇒ a synthetic STRING token"). The pre-fix sampler produced

    ... >= now() - INTERVAL '__replay_sample_window_months__' MONTH
    ... >= '__replay_sample_hire_window_start__'

and the unit suite could not see it, for a reason that outlives this particular bug:
`FakeWarehouseProbe` never executes the SQL. It returns the
`(row_count, distinct_grain_count, columns)` triple the D56 gate wants, so the pre-fix
replay reported **`passed=True`** on SQL that ClickHouse rejects outright. The sampler's
whole job is to produce values a REAL warehouse accepts; a fake probe is definitionally
unable to test it, and that is what let a wrong default branch sit under a green suite.

So this test binds the templates through the real `bind_template` (the same call
`golden_replay` makes) and sends the result to a live ClickHouse. It asserts the
statement EXECUTES — not what it returns. Replay is a structure oracle (D98); there is
no value oracle here, and the seeded `dbpcm_warehouse.employee` may legitimately hold
zero matching rows.

The negative controls are the point as much as the positives: they send the PRE-FIX
sampled SQL and assert ClickHouse rejects it. Without them, a future sampler change that
happens to produce something else valid would keep these tests green while re-breaking
the property. Their expected errors on ClickHouse 24.8:

    relative_window   Code 62  SYNTAX_ERROR   — `INTERVAL '<str>' MONTH` does not parse
    period_range      Code 53  TYPE_MISMATCH  — cannot convert the token to DateTime64(6)

Skip-guarded on `CLICKHOUSE_TEST_URL`; `uv run pytest` with no live stack stays green.
Read-only: every statement is a SELECT, nothing is created, seeded or dropped, so this
is safe to run alongside another agent's destructive live suite.

    docker compose -f docker-compose.integration.yml up -d --wait clickhouse
    CLICKHOUSE_TEST_URL=http://localhost:8123 \
        uv run pytest tests/integration/test_windowed_replay_clickhouse_live.py -v

Slug: PA-windowed-replay-clickhouse.
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_agent.learning.promotion.replay import _sample_value, _slot_types
from data_agent.runtime.blueprint.template import bind_template, referenced_slots

pytestmark = pytest.mark.skipif(
    not os.environ.get("CLICKHOUSE_TEST_URL"),
    reason="Requires a live ClickHouse (set CLICKHOUSE_TEST_URL).",
)

# `bp-hires-per-month` and `bp-hires-in-range` (tests/fixtures/corpus/blueprints.yaml),
# reduced to the clause the slot binds. The point is the BIND SITE's type, so the rest
# of the canon SQL (the data-anchored subquery, the status filter) is noise here.
_WINDOW_TEMPLATE = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS month, "
    "COUNT(DISTINCT employee_code) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= now() - INTERVAL {window_months} MONTH "
    "GROUP BY toStartOfMonth(most_recent_hire_date)"
)
_RANGE_TEMPLATE = (
    "SELECT toStartOfMonth(most_recent_hire_date) AS month, "
    "COUNT(DISTINCT employee_code) AS hires "
    "FROM dbpcm_warehouse.employee "
    "WHERE most_recent_hire_date >= {hire_window_start} "
    "AND most_recent_hire_date < {hire_window_end} "
    "GROUP BY toStartOfMonth(most_recent_hire_date)"
)

# What the sampler produced BEFORE the fix — a synthetic string token at both sites.
_PRE_FIX_WINDOW_SQL = _WINDOW_TEMPLATE.replace(
    "{window_months}", "'__replay_sample_window_months__'"
)
_PRE_FIX_RANGE_SQL = (
    _RANGE_TEMPLATE
    .replace("{hire_window_start}", "'__replay_sample_hire_window_start__'")
    .replace("{hire_window_end}", "'__replay_sample_hire_window_end__'")
)


def _plan(name: str, slot_type: str) -> dict:
    """A minimal S3 plan carrying one windowed slot — the shape `_slot_types` reads."""
    return {
        "parameterization": [
            {"role": "slot", "slot": {"name": name, "type": slot_type}}
        ]
    }


def _replay_sql(template: str, plan: dict) -> str:
    """Exactly what `golden_replay` builds: sample per referenced TOKEN, then bind
    through the runtime binder. Deliberately reuses the production helpers rather than
    reimplementing the sampling, so a regression in either shows up here."""
    slot_types = _slot_types(plan)
    bindings = {
        token: _sample_value(token, slot_types.get(token, ""))
        for token in referenced_slots(template)
    }
    return bind_template(template, bindings)


def _run(sql: str) -> httpx.Response:
    url = os.environ["CLICKHOUSE_TEST_URL"].rstrip("/") + "/"
    return httpx.post(url, content=sql.encode(), timeout=30.0)


# --- the fixed sampler produces SQL a real warehouse accepts ---------------------


def test_a_relative_window_replay_executes_on_clickhouse():
    """`INTERVAL {n} MONTH` needs a NUMBER at the bind site — the type's whole contract
    is that the unit stays in the template."""
    sql = _replay_sql(_WINDOW_TEMPLATE, _plan("window_months", "relative_window"))
    assert "__replay_sample" not in sql, sql
    response = _run(sql)
    assert response.status_code == 200, response.text


def test_a_period_range_replay_executes_on_clickhouse():
    """Both bounds compare against a `Nullable(DateTime64(6))` column, so both need a
    date. This is the half that a name-keyed type map missed entirely."""
    sql = _replay_sql(_RANGE_TEMPLATE, _plan("hire_window", "period_range"))
    assert "__replay_sample" not in sql, sql
    assert sql.count("'2020-01-01'") == 2, sql
    response = _run(sql)
    assert response.status_code == 200, response.text


# --- the negative controls: the pre-fix SQL is genuinely rejected ----------------


def test_the_pre_fix_relative_window_sql_is_a_clickhouse_syntax_error():
    response = _run(_PRE_FIX_WINDOW_SQL)
    assert response.status_code != 200
    assert "SYNTAX_ERROR" in response.text, response.text


def test_the_pre_fix_period_range_sql_is_a_clickhouse_type_mismatch():
    response = _run(_PRE_FIX_RANGE_SQL)
    assert response.status_code != 200
    assert "TYPE_MISMATCH" in response.text, response.text


# --- the control: the sampler was never wrong for the scalar types ---------------


@pytest.mark.parametrize(
    ("template", "name", "slot_type"),
    [
        (
            "SELECT COUNT(DISTINCT employee_code) AS n FROM dbpcm_warehouse.employee "
            "WHERE most_recent_hire_date >= {as_of}",
            "as_of",
            "as_of_date",
        ),
        (
            "SELECT COUNT(DISTINCT employee_code) AS n FROM dbpcm_warehouse.employee "
            "WHERE department_name = {dept}",
            "dept",
            "entity",
        ),
        (
            "SELECT COUNT(DISTINCT employee_code) AS n FROM dbpcm_warehouse.employee "
            "WHERE department_name IN {depts}",
            "depts",
            "list",
        ),
    ],
)
def test_the_unchanged_slot_types_still_bind_and_execute(template, name, slot_type):
    """A widened vocabulary must not disturb the types that already worked; asserted
    against the same live warehouse rather than against the fake probe."""
    sql = _replay_sql(template, _plan(name, slot_type))
    response = _run(sql)
    assert response.status_code == 200, response.text
