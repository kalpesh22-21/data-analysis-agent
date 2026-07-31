"""Layer-2 integration — the D62 provenance false-reject oracle replayed against
a LIVE ClickHouse `system.query_log`.

What this proves
----------------
The oracle harness (`data_agent.sqlparse.oracle`) runs end-to-end against real
SQL pulled from a real ClickHouse instance: it connects, pulls the available
`system.query_log` SELECTs, classifies each through the column-provenance
extractor with the real Semantic Catalog schema, and produces a reject-rate
report.  The test asserts the harness *completes* and surfaces a well-formed
number, and PRINTS the full redacted report (rate + rejected samples).

HONEST FRAMING — the measured rate here is NOT the production false-reject rate
--------------------------------------------------------------------------------
The Layer-2 ClickHouse `system.query_log` contains only OUR OWN test queries
(the ones the l2 suite and seed scripts have run), NOT production or staging
traffic.  So the number this test prints is:

  * a proof the oracle harness works against real infra, and
  * a first signal on a tiny, non-representative corpus,

and it is explicitly NOT a trustworthy estimate of how often D63 would wrongly
block a real user's query in production.  A MEANINGFUL false-reject rate needs
this same oracle replayed over a production/staging `query_log` (or an exported
snapshot of real analyst SQL).  The oracle module is deliberately reusable for
exactly that: point `run_oracle(...)` at any corpus of query strings.

We therefore DO NOT assert a particular rate or a zero-reject outcome — that
would either be brittle or would overclaim.  We assert only that the harness
ran, found at least one SELECT, and produced a rate in [0, 1].  Any reject the
harness surfaces is printed (redacted) for human triage; a reject on an in-scope
warehouse query would be a genuine parser gap worth a D70 backlog entry.

Corpus selection
----------------
We pull DISTINCT SELECTs that do NOT reference `system.` / `information_schema`
tables — i.e. queries that plausibly target the warehouse.  Those are the
population where a reject is a *candidate false reject*; a reject on a
system-table query is a correct out-of-scope reject and would only add noise.

Skip-guarded on `CLICKHOUSE_TEST_URL`; `uv run pytest` with no live stack stays
fully green.  Run with the l2 stack up:

    docker compose -f docker-compose.integration.yml up -d --wait clickhouse
    CLICKHOUSE_TEST_URL=http://localhost:8123 \
        uv run pytest tests/integration/test_provenance_oracle_live.py -s -v
"""

from __future__ import annotations

import os

import httpx
import pytest

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.sqlparse.oracle import run_oracle
from tests._catalog_fixture import fixture_catalog

pytestmark = pytest.mark.skipif(
    not os.environ.get("CLICKHOUSE_TEST_URL"),
    reason="Requires a live ClickHouse (set CLICKHOUSE_TEST_URL, e.g. http://localhost:8123).",
)

# Pull DISTINCT warehouse-ish SELECTs from the query_log. Excluding system /
# information_schema keeps the corpus to queries where a reject is a candidate
# false reject (see module docstring). LIMIT is a generous cap for the tiny l2 log.
_QUERY_LOG_SQL = (
    "SELECT DISTINCT query FROM system.query_log "
    "WHERE type = 'QueryFinish' "
    "AND trimLeft(query) ILIKE 'SELECT%' "
    "AND query NOT ILIKE '%system.%' "
    "AND query NOT ILIKE '%information_schema%' "
    "LIMIT 2000 "
    "FORMAT JSONEachRow"
)


def _clickhouse_post(sql: str) -> str:
    """POST *sql* to the live ClickHouse HTTP interface and return the body text."""
    url = os.environ["CLICKHOUSE_TEST_URL"]
    params = {
        "user": os.environ.get("CLICKHOUSE_TEST_USER", "default"),
        "password": os.environ.get("CLICKHOUSE_TEST_PASSWORD", ""),
    }
    response = httpx.post(url, params=params, content=sql.encode("utf-8"), timeout=30.0)
    response.raise_for_status()
    return response.text


def _pull_query_log_selects() -> list[str]:
    """Return the DISTINCT SELECT texts from the live query_log (JSONEachRow rows).

    JSONEachRow is used (not TSV) so queries with embedded newlines/tabs round-trip
    intact.
    """
    import json

    body = _clickhouse_post(_QUERY_LOG_SQL)
    queries: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        query = row.get("query")
        if isinstance(query, str) and query.strip():
            queries.append(query)
    return queries


def test_provenance_oracle_replays_live_query_log() -> None:
    catalog_schema = build_sqlglot_schema_from_catalog(fixture_catalog())
    assert catalog_schema, "catalog schema must load — the oracle needs it to qualify columns"

    queries = _pull_query_log_selects()

    report = run_oracle(queries, catalog_schema)

    # --- Surface the number (this is the point of the harness) ---
    print()  # keep the report readable under `-s`
    print(f"[D62 oracle] pulled {len(queries)} DISTINCT non-system SELECT(s) from query_log")
    print(report.render())
    print(
        "[D62 oracle] NOTE: this rate is measured on OUR test queries only, NOT "
        "production traffic — it proves the harness runs and is a first signal, "
        "not the production false-reject rate."
    )

    # --- Assert the harness ran and produced a well-formed measurement ---
    # We deliberately assert NOTHING about the specific rate (see module docstring:
    # asserting a value here would be brittle or would overclaim on a corpus that
    # is not representative of production).
    assert report.total == len(queries)
    assert report.total >= 1, "expected at least one SELECT in the live query_log"
    assert report.extracted_ok + report.reject_count == report.total
    assert 0.0 <= report.reject_rate <= 1.0
