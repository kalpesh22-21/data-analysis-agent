"""Layer-2 integration — plan §2b blueprint references, against the LIVE stack.

Two claims that only a live run can settle, and that the hermetic tests
(`tests/runtime/retrieval/test_corpus_loader_references{,_qa}.py`) can only approximate:

  1. **Inlining is semantics-preserving, not just text-preserving.** The hermetic
     tests compare the resolver's output against the SQL the composite carried before
     the conversion. This one RUNS both against real ClickHouse and diffs the rows,
     which is the only check that would catch a difference sqlglot and a string
     comparison both consider irrelevant (a cast the executor re-renders, a JOIN order
     the planner reorders). The MCP is unusable on this stack (the running image
     demands a tenant claim the local token service does not mint — 403 on every
     call), so the queries go to ClickHouse's HTTP interface DIRECTLY. That skips the
     scope enforcement the MCP would apply, which is fine here: the question is
     whether two SQL strings produce the same rows, not whether they are authorized.

  2. **The extracted child is a real, retrievable blueprint.** Seeded through the
     PRODUCTION path (`corpus_seeds_from_export` → `load_corpus`, which resolves the
     references), then read back for its `:USES` edges to the catalog-owned `:Column`
     nodes, and finally recalled: a single-period question must rank the child ABOVE
     the composite it was extracted from, which is the retrieval win the extraction
     was for.

`:CorpusMeta` is deliberately absent on this stack, so the seed passes an EMPTY
`corpus_sha` rather than inventing a checksum, and the test asserts the singleton
stayed absent instead of asserting a value.

DESTRUCTIVE (proof 2 only): wipes the corpus graph and reseeds it, the same posture
as `test_neo4j_vector_index_live.py`. Point `NEO4J_TEST_URI` only at the ephemeral l2
compose neo4j. Note that `scripts/run_ui_runtime_real.py` may be holding a driver
against the same instance with self-heal enabled — a concurrent writer between our
write and our reads — so every assertion below is on content THIS test seeded, and
the recall assertion is on relative ORDER rather than on an absolute score.

    CLICKHOUSE_TEST_URL=http://localhost:8123 \
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_blueprint_reference_inlining_live.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    apply_catalog_graph_schema,
    corpus_seeds_from_export,
    load_catalog_graph,
    load_corpus,
    resolve_blueprint_references,
    schema_statements,
)
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex
from tests._catalog_fixture import load_catalog_export

_MODEL = "all-mpnet-base-v2"
_CANON_DIR = Path("/Users/kalpeshmulye/Development/clickhouse-api/app/corpus/data/blueprints")
_COMPOSITE = "bp-compare-employee-check-detail-two-periods"
_CHILD = "bp-employee-check-detail-for-period"

# A live employee/period pair with rows in BOTH periods, so the composite's terminal
# FULL OUTER JOIN actually joins rather than degenerating to one side.
_EMPLOYEE = "'EMP001'"
_PERIOD_A = "toDate('2024-01-15')"
_PERIOD_B = "toDate('2024-01-31')"

# The composite's `composes` list EXACTLY as authored BEFORE §2b, verbatim from the
# pre-conversion YAML. Nodes 0 and 1 are the two that became references. Executing
# THESE and diffing against what the resolver produces is the independent half of the
# byte-preservation claim: everything else available today is downstream of the
# conversion and so cannot witness it.
_PRE_2B_NODE_SQL: dict[int, str] = {
    0: (
        "SELECT toString(register_type) AS register_type,\n"
        "       toString(type_code) AS type_code,\n"
        "       any(toString(type_code_description)) AS type_code_description,\n"
        "       toFloat64(SUM(amount)) AS amount,\n"
        "       toFloat64(SUM(type_hours)) AS hours\n"
        "FROM dbpcm_warehouse.payroll\n"
        "WHERE employee_code = {employee}\n"
        "  AND pay_period_end_date = {period_a}\n"
        "GROUP BY register_type, type_code\n"
    ),
    1: (
        "SELECT toString(register_type) AS register_type,\n"
        "       toString(type_code) AS type_code,\n"
        "       any(toString(type_code_description)) AS type_code_description,\n"
        "       toFloat64(SUM(amount)) AS amount,\n"
        "       toFloat64(SUM(type_hours)) AS hours\n"
        "FROM dbpcm_warehouse.payroll\n"
        "WHERE employee_code = {employee}\n"
        "  AND pay_period_end_date = {period_b}\n"
        "GROUP BY register_type, type_code\n"
    ),
    2: (
        "SELECT coalesce(a.register_type, b.register_type) AS register_type,\n"
        "       coalesce(a.type_code, b.type_code) AS type_code,\n"
        "       coalesce(a.type_code_description, b.type_code_description) AS "
        "type_code_description,\n"
        "       coalesce(a.amount, 0) AS amount_a,\n"
        "       coalesce(b.amount, 0) AS amount_b,\n"
        "       coalesce(b.amount, 0) - coalesce(a.amount, 0) AS amount_delta,\n"
        "       coalesce(a.hours, 0) AS hours_a,\n"
        "       coalesce(b.hours, 0) AS hours_b\n"
        "FROM scratch.detail_a AS a\n"
        "FULL OUTER JOIN scratch.detail_b AS b\n"
        "  ON a.register_type = b.register_type AND a.type_code = b.type_code\n"
        "ORDER BY coalesce(a.register_type, b.register_type),\n"
        "         coalesce(a.type_code, b.type_code)\n"
    ),
}


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _canon_docs() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    if not _CANON_DIR.is_dir():
        pytest.skip(f"real MCP canon not checked out at {_CANON_DIR}")
    docs: dict[str, Any] = {}
    for path in sorted(_CANON_DIR.glob("bp-*.yaml")):
        doc = yaml.safe_load(path.read_text())
        docs[doc["id"]] = doc
    return docs


def _resolved_canon() -> dict[str, Any]:
    seeds, _ = corpus_seeds_from_export({"blueprints": _canon_docs()})
    return {bp.id: bp for bp in resolve_blueprint_references(seeds)}


def _bind(template: str) -> str:
    """Substitute the three bind sites with literal SQL.

    NOT the runtime binder: the executor renders `{slot}` through sqlglot into a typed
    AST literal, and using it here would put the thing under test (the template text)
    behind a second transformation. Textual substitution keeps the comparison honest —
    both sides get the identical treatment, so any row difference is a difference in
    the templates."""
    for token, literal in (
        ("{employee}", _EMPLOYEE),
        ("{period_a}", _PERIOD_A),
        ("{period_b}", _PERIOD_B),
        ("{period}", _PERIOD_A),
    ):
        template = template.replace(token, literal)
    return template


def _clickhouse(sql: str) -> dict[str, Any]:
    url = os.environ["CLICKHOUSE_TEST_URL"]
    params = {
        "user": os.environ.get("CLICKHOUSE_TEST_USER", "default"),
        "password": os.environ.get("CLICKHOUSE_TEST_PASSWORD", ""),
    }
    response = httpx.post(
        url, params=params, content=(sql + "\nFORMAT JSONCompact").encode("utf-8"), timeout=30.0
    )
    response.raise_for_status()
    return json.loads(response.text)


def _run_dag(node_sql: dict[int, str], outputs: dict[int, str]) -> dict[str, Any]:
    """Execute a 3-node DAG against ClickHouse with the scratch hand-off inlined.

    The real executor materializes each `table` output into a session-scoped scratch
    table via the D93 side channel; the MCP that owns that channel is 403-ing on this
    stack, so each `scratch.<name>` source is substituted with the producing node's
    query as a derived table. The substitution is identical on both sides of the
    comparison, so it cannot manufacture agreement."""
    materialized: dict[str, str] = {}
    result: dict[str, Any] = {}
    for order in sorted(node_sql):
        sql = _bind(node_sql[order])
        for name, producer in materialized.items():
            sql = sql.replace(f"scratch.{name}", f"({producer})")
        result = _clickhouse(sql)
        if order in outputs:
            materialized[outputs[order]] = sql
    return result


# --------------------------------------------------------------------------
# proof 1 — the inlined SQL returns the same rows as the SQL it replaced
# --------------------------------------------------------------------------

_clickhouse_only = pytest.mark.skipif(
    not os.environ.get("CLICKHOUSE_TEST_URL"),
    reason="Requires a live ClickHouse (set CLICKHOUSE_TEST_URL, e.g. http://localhost:8123).",
)


@_clickhouse_only
def test_the_inlined_composite_returns_the_same_rows_as_before_the_conversion() -> None:
    """Requirement 1, executed. Both DAGs run against the same live warehouse in the
    same test, so a row difference cannot be blamed on drifting data."""
    resolved = _resolved_canon()[_COMPOSITE]
    post = {node["order"]: node["sql_template"] for node in resolved.composes}
    outputs = {0: "detail_a", 1: "detail_b"}

    before = _run_dag(_PRE_2B_NODE_SQL, outputs)
    after = _run_dag(post, outputs)

    assert before["data"], "the fixture employee/period pair returned no rows — pick another"
    assert [c["name"] for c in after["meta"]] == [c["name"] for c in before["meta"]]
    assert [c["type"] for c in after["meta"]] == [c["type"] for c in before["meta"]]
    assert after["data"] == before["data"]


@_clickhouse_only
def test_the_extracted_child_run_standalone_reproduces_the_node_it_replaced() -> None:
    """The other half: the child is not merely inlinable, it is the SAME query. Run it
    on its own with `{period}` bound to period A and diff against the composite's
    pre-conversion node 0 — which is what makes the child runnable in its own right
    rather than a fragment that only makes sense spliced in."""
    child = _resolved_canon()[_CHILD]
    assert child.sql_template and not child.composes, "the child must be a leaf blueprint"

    standalone = _clickhouse(_bind(child.sql_template))
    node_0 = _clickhouse(_bind(_PRE_2B_NODE_SQL[0]))

    assert standalone["data"], "no rows for the fixture employee/period — pick another"
    assert standalone["meta"] == node_0["meta"]
    assert standalone["data"] == node_0["data"]


# --------------------------------------------------------------------------
# proof 2 — the child lands as its own retrievable blueprint
# --------------------------------------------------------------------------

_neo4j_only = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)


def _uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
    )


def _embedder() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"],
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model=_MODEL,
        timeout_seconds=30.0,
    )


@pytest.fixture(scope="module")
def seeded_canon() -> int:
    """Seed the REAL canon through the production path, once per module.

    `corpus_seeds_from_export` is the projection the online hydrator uses on the MCP
    export, and `load_corpus` is what resolves the references — so this exercises the
    same code an actual reseed would, not a fixture-only shortcut. Returns the seeded
    blueprint count so a test can assert the extraction really added one."""
    docs = _canon_docs()

    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        try:
            async with driver.session() as session:
                # DESTRUCTIVE — see the module docstring.
                await session.run("MATCH (n) DETACH DELETE n")
                for statement in schema_statements(768):
                    await session.run(statement)  # type: ignore[arg-type]
                await session.run("CALL db.awaitIndexes(300)")  # type: ignore[arg-type]
            # The catalog owns :Table/:Column; load_corpus MERGE→MATCHes onto them, so
            # they must exist first or every :USES edge silently fails to bind.
            await apply_catalog_graph_schema(driver)
            await load_catalog_graph(driver, load_catalog_export())
            seeds, knowledge = corpus_seeds_from_export({"blueprints": docs})
            # EMPTY corpus_sha on purpose: `:CorpusMeta` is absent by design on this
            # stack and inventing a checksum would fabricate freshness state.
            await load_corpus(
                driver, _embedder(), seeds, knowledge, model_id=_MODEL, corpus_sha=""
            )
        finally:
            await driver.close()

    asyncio.run(_seed())
    return len(docs)


@_neo4j_only
async def test_the_extracted_child_landed_with_real_uses_edges(seeded_canon: int) -> None:
    """A blueprint with no `:USES` edges is not "mostly seeded" — the edges bind
    MERGE→MATCH onto catalog-owned `:Column` nodes, so zero edges means every scope
    key it declares missed a real column, which the load reports as nothing at all.
    The child's `uses` are new to the graph (they came in with the extraction), so
    this is the assertion that would catch a casing or naming mismatch against the
    catalog."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (b:Blueprint {id: $id}) "
                "OPTIONAL MATCH (b)-[:USES]->(c:Column) "
                "RETURN b.source AS source, b.status AS status, "
                "b.sql_template AS sql_template, b.composes_json AS composes_json, "
                "b.structural_key AS structural_key, "
                "collect(c.key) AS columns",
                id=_CHILD,
            )
            rows = await result.data()
    finally:
        await driver.close()

    assert rows, f"{_CHILD} did not land at all"
    row = rows[0]
    assert row["source"] == "mcp" and row["status"] == "validated"
    # A LEAF in its own right: top-level SQL, no DAG.
    assert row["sql_template"] and row["composes_json"] is None
    assert (row["structural_key"] or "").startswith("sha256:")
    assert set(row["columns"]) == {
        "dbpcm_warehouse.payroll.employee_code",
        "dbpcm_warehouse.payroll.pay_period_end_date",
        "dbpcm_warehouse.payroll.register_type",
        "dbpcm_warehouse.payroll.type_code",
        "dbpcm_warehouse.payroll.type_code_description",
        "dbpcm_warehouse.payroll.amount",
        "dbpcm_warehouse.payroll.type_hours",
    }


@_neo4j_only
async def test_no_reference_survives_the_write_and_corpus_meta_stays_absent(
    seeded_canon: int,
) -> None:
    """Requirement 6 at the STORE, past every projection between the YAML and neo4j.
    `composes_json` is the property the executor and the learning tier's prior-art view
    both read, so a reference id surviving here would leak whatever those two serve."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (b:Blueprint) WHERE b.composes_json IS NOT NULL "
                "RETURN b.id AS id, b.composes_json AS composes_json"
            )
            composed = await result.data()
            meta = await (await session.run("MATCH (m:CorpusMeta) RETURN m")).data()
    finally:
        await driver.close()

    assert composed, "no composed blueprint landed — the assertion below would be vacuous"
    for row in composed:
        assert '"ref"' not in row["composes_json"], f"{row['id']} stored a reference"
        assert _CHILD not in row["composes_json"], f"{row['id']} stored a reference target id"
    assert meta == [], "seeded with an empty corpus_sha; no :CorpusMeta should be written"


@_neo4j_only
async def test_recall_now_returns_the_child_for_a_single_period_question(
    seeded_canon: int,
) -> None:
    """The retrieval win the extraction was for. Before it, a one-period question had
    only the two-period COMPARISON to match against — a blueprint whose second required
    period the asker never mentioned.

    Asserted as an ORDERING, not a score threshold: the embedding service is a live
    dependency and a concurrent writer shares this graph, so a pinned float would be a
    flake generator. The reverse direction is asserted too — the composite must still
    win its own question — because "the child ranks first" is only good news if it has
    not simply displaced the composite everywhere."""
    embedder = _embedder()
    index = Neo4jVectorIndex(
        url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0
    )
    try:
        one_period = "show me this employee's paycheck line items for one pay period"
        two_period = "compare this employee's two paychecks line by line across two pay periods"
        single = {
            c.id: c.score
            for c in await index.recall(
                query_vector=(await embedder.embed([one_period]))[0], kind="blueprint", k=30
            )
        }
        compare = {
            c.id: c.score
            for c in await index.recall(
                query_vector=(await embedder.embed([two_period]))[0], kind="blueprint", k=30
            )
        }
    finally:
        await index.close()

    assert _CHILD in single, "the extracted child is not recallable at all"
    assert single[_CHILD] > single[_COMPOSITE], (
        f"a single-period question still prefers the two-period composite "
        f"(child={single[_CHILD]:.4f} composite={single[_COMPOSITE]:.4f})"
    )
    assert compare[_COMPOSITE] > compare[_CHILD], (
        f"the extraction displaced the composite on its OWN question "
        f"(composite={compare[_COMPOSITE]:.4f} child={compare[_CHILD]:.4f})"
    )
