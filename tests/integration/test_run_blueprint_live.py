"""Layer-2 integration — a real SINGLE-NODE blueprint executed END-TO-END through
the live stack (runblueprint-design §2, Slice B):

    getBlueprint (real neo4j) → parse → slot DISTINCT-domain probe (real MCP →
    real ClickHouse, scope-enforced) → typed-literal bind → node runQuery (real
    MCP → real ClickHouse) → D56 grain-integrity probe → verified result.

Everything below the executor is REAL: `RealMCPClient` ↔ the D83 clickhouse-api
MCP ↔ seeded ClickHouse (`dbpcm_warehouse.employee`, 5 rows), a real JWT minted
by the token IdP, and `Neo4jVectorIndex` reading the full-DAG blueprint seeded by
the corpus loader. NOTHING is faked.

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL + MCP_TEST_URL; `uv run
pytest` with no live stack stays fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --build
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
    MCP_TEST_URL=http://localhost:18090/mcp \
        uv run pytest tests/integration/test_run_blueprint_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import (
    NOT_FOUND_CODE,
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
)
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    load_catalog_graph,
    load_corpus,
    load_seed_fixtures,
)
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex
from tests._catalog_fixture import fixture_catalog_handle, load_catalog_export

from .conftest import Mint

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("NEO4J_TEST_URI")
        and os.environ.get("EMBEDDING_TEST_URL")
        and os.environ.get("MCP_TEST_URL")
    ),
    reason="Requires live neo4j + embedding API + clickhouse-api MCP "
    "(set NEO4J_TEST_URI + EMBEDDING_TEST_URL + MCP_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_AVG_ID = "bp-average-salary-by-department"
_ABOVE_AVG_ID = "bp-departments-above-company-average-salary"
_EARN_ID = "bp-total-earnings-by-department"
_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
# The D67 concept the resolve_via rule maps to the earnings register code set. The
# seed's DISTINCT RegisterType domain is the catalog-faithful set
# {EARN, EETAX, DDUCT, NETPAYDIST, EEBEN, ERTAX} (value-only ranking — no sibling
# description column). "earnings" must rank EARN above a deduction/tax code (DDUCT).
_EARN_CONCEPT = "earnings"


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


def _index() -> Neo4jVectorIndex:
    return Neo4jVectorIndex(
        url=os.environ["NEO4J_TEST_URI"], auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0
    )


def _dispatcher() -> ToolDispatcher:
    return ToolDispatcher(RealMCPClient(os.environ["MCP_TEST_URL"]), fixture_catalog_handle())


def _composite(dispatcher: ToolDispatcher) -> ResolveValuesComposite:
    """The D67 `resolve_via` hook wired to the SAME dispatcher (so its inner
    runQuery shares the scope-enforced path) + the REAL embedder for ranking."""
    return ResolveValuesComposite(
        tool_dispatcher=dispatcher, catalog=fixture_catalog_handle(), embedding_client=_embedder()
    )


def _executor(
    index: Neo4jVectorIndex, *, resolve_values: ResolveValuesComposite | None = None
) -> BlueprintExecutor:
    return BlueprintExecutor(
        tool_dispatcher=_dispatcher(), vector_index=index, resolve_values=resolve_values
    )


@pytest.fixture(scope="module")
def seeded_dag_corpus() -> bool:
    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(os.environ["NEO4J_TEST_URI"], auth=_auth())
        try:
            async with driver.session() as session:
                await session.run("MATCH (n) DETACH DELETE n")  # DESTRUCTIVE — l2 only
            # Hydrate the enriched :Table/:Column catalog graph FIRST so load_corpus'
            # MERGE→MATCH :USES edges bind to real catalog nodes (columns are now
            # catalog-owned, no longer minted by the corpus loader).
            await load_catalog_graph(driver, load_catalog_export())
            blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
            await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        finally:
            await driver.close()

    asyncio.run(_seed())
    return True


async def test_single_node_blueprint_runs_and_verifies_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    jwt = await mint(session_id="sess-bp-live")  # allow-all scope, session-bound
    creds = RuntimeCredentials(session_id="sess-bp-live", jwt=jwt, column_scope=frozenset())
    index = _index()
    try:
        outcome = await _executor(index).execute(
            blueprint_id=_AVG_ID,
            slot_bindings={"department": "Sales"},
            credentials=creds,
        )
    finally:
        await index.close()

    assert isinstance(outcome, ExecCompleted), outcome
    rf = outcome.result_full
    assert rf["status"] == "verified"
    assert rf["verify"]["grain_ok"] is True
    assert rf["verify"]["grain_checked"] is True
    # One department (Sales) → one result row; the seed has 2 Sales employees.
    assert rf["row_count"] == 1
    assert "department" in [c.lower() for c in rf["columns"]]
    # Provenance is the LIVE union of every inner runQuery (probe + node + grain).
    assert outcome.provenance is not None
    assert (_E, "Department") in outcome.provenance
    assert (_E, "AnnualSalary") in outcome.provenance


async def test_multi_node_scalar_dag_runs_and_verifies_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    # Slice C: a real SCALAR-passing DAG end-to-end. node 0 computes the
    # company-wide average as a scalar; node 1 CONSUMES it (bound as a typed AST
    # literal, F1/D10 — never interpolated) to select the above-average
    # departments. No table intermediate (F2-clean). Both node queries + the D56
    # grain probe run through the live MCP ↔ ClickHouse; the result is verified.
    jwt = await mint(session_id="sess-bp-dag-live")  # allow-all scope, session-bound
    creds = RuntimeCredentials(session_id="sess-bp-dag-live", jwt=jwt, column_scope=frozenset())
    index = _index()
    try:
        outcome = await _executor(index).execute(
            blueprint_id=_ABOVE_AVG_ID, slot_bindings={}, credentials=creds
        )
    finally:
        await index.close()

    assert isinstance(outcome, ExecCompleted), outcome
    rf = outcome.result_full
    assert rf["status"] == "verified"
    assert rf["verify"]["grain_ok"] is True
    assert rf["verify"]["grain_checked"] is True
    assert "department" in [c.lower() for c in rf["columns"]]
    # TWO per-node SQLs surfaced for transparency (D56 "SQL stays visible").
    assert len(rf["sql"]) == 2
    # Provenance is the LIVE union across BOTH node queries + the grain probe.
    assert outcome.provenance is not None
    assert (_E, "AnnualSalary") in outcome.provenance
    assert (_E, "Department") in outcome.provenance


async def test_narrow_scope_blueprint_is_not_found_no_data_leaks_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    # A scope covering only EmployeeCode: the blueprint's USES (Department/
    # AnnualSalary/EmployeeCode) ⊄ scope, so the FETCH scope-check returns the
    # byte-identical NOT_FOUND (the D88(b) non-oracle) — no node query runs, no
    # out-of-scope value ever leaks, and the fast path degrades to the raw loop.
    jwt = await mint([f"{_E}.EmployeeCode"], session_id="sess-bp-live-scope")
    creds = RuntimeCredentials(
        session_id="sess-bp-live-scope", jwt=jwt, column_scope=frozenset({f"{_E}.EmployeeCode"})
    )
    index = _index()
    try:
        outcome = await _executor(index).execute(
            blueprint_id=_AVG_ID, slot_bindings={"department": "Sales"}, credentials=creds
        )
    finally:
        await index.close()

    assert isinstance(outcome, ExecFailed)
    assert outcome.error_code == NOT_FOUND_CODE


async def test_single_node_resolve_via_earnings_runs_and_verifies_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    # D67 end-to-end on real infra: the `gross_earnings` rule maps the concept
    # "earnings" → the earnings RegisterType code set via the REAL
    # `resolveValues.resolve()` (real embedder ranks the live DISTINCT domain
    # {EARN, DEDUCTION}, value-only), and the executor folds the ranked codes into
    # `RegisterType IN {earn_codes}` as a typed AST IN-list (F1/D10 — never
    # interpolated), then runs + D56-verifies the query through the live MCP ↔
    # ClickHouse. Nothing below the executor is faked.
    jwt = await mint(session_id="sess-bp-earn-live")  # allow-all scope, session-bound
    creds = RuntimeCredentials(session_id="sess-bp-earn-live", jwt=jwt, column_scope=frozenset())
    dispatcher = _dispatcher()
    composite = _composite(dispatcher)

    # (a) The concept resolves to a real code set: EARN is present AND ranked above
    # DDUCT by the real embedder over the live DISTINCT RegisterType domain.
    resolved = await composite.resolve(
        table=_P, column="RegisterType", concept=_EARN_CONCEPT, period=None, credentials=creds
    )
    assert resolved.status == "ok", resolved
    ranked = [v.value for v in resolved.values]
    assert "EARN" in ranked, f"EARN missing from resolved code set {ranked!r}"
    assert ranked.index("EARN") < ranked.index("DDUCT"), (
        f"EARN must rank above DDUCT for concept {_EARN_CONCEPT!r}; got {ranked!r} "
        f"(scores={[(v.value, round(v.score, 4)) for v in resolved.values]})"
    )

    # (b)-(d) Run the blueprint through the executor (its OWN internal resolve()
    # call re-does the resolution) → verified result with the IN-list bind.
    index = _index()
    try:
        outcome = await _executor(index, resolve_values=composite).execute(
            blueprint_id=_EARN_ID,
            slot_bindings={"department": "Sales"},
            credentials=creds,
        )
    finally:
        await index.close()

    assert isinstance(outcome, ExecCompleted), outcome
    rf = outcome.result_full
    # (c) the query executed AND passed the D56 grain-integrity gate.
    assert rf["status"] == "verified"
    assert rf["verify"]["grain_ok"] is True
    assert rf["verify"]["grain_checked"] is True
    # (b) D67 concept-subset selection binds {EARN} ONLY as typed literals — the
    # exact `IN ('EARN')` set, NOT the whole {EARN, EETAX, DDUCT, NETPAYDIST, EEBEN,
    # ERTAX} domain, never the `{earn_codes}` placeholder nor interpolation. Binding a
    # non-earnings register (e.g. DDUCT/EETAX) into the "earnings" total would corrupt
    # it — the D67 correctness gap this asserts is closed.
    assert len(rf["sql"]) == 1
    node_sql = rf["sql"][0]
    # KEEP this: it encodes the intended D67 behavior (bind EARN only). NOTE: the
    # resolve-domain grew from 2 → 6 catalog codes with the new fixture, so this exact
    # `IN ('EARN')` binding must be re-confirmed on the next live L2 run.
    assert "IN ('EARN')" in node_sql, node_sql
    assert "DDUCT" not in node_sql, node_sql
    assert "{earn_codes}" not in node_sql
    # The concept NEVER reaches SQL as a value (D10) — it would only appear as a
    # quoted string literal if interpolated (`total_earnings` alias is authored SQL).
    assert f"'{_EARN_CONCEPT}'" not in node_sql
    # One department (Sales) → one verified result row.
    assert rf["row_count"] == 1
    assert "department" in [c.lower() for c in rf["columns"]]
    # (b') The verified TOTAL is the TRUE earnings sum: Sales = Alice (EMP001 EARN
    # 3125.00) + Carol (EMP003 EARN 3000.00) = 6125.0. Binding any non-EARN register
    # into this total would corrupt it — the assertion whose absence hid the bug.
    total_idx = [c.lower() for c in rf["columns"]].index("total_earnings")
    total = float(rf["preview_rows"][0][total_idx])
    assert total == 6125.0, f"expected true earnings 6125.0 (EARN only), got {total}"
    # (d) provenance is the LIVE union across the resolveValues probe + the slot
    # domain probe + the node query + the grain probe.
    assert outcome.provenance is not None
    assert (_P, "RegisterType") in outcome.provenance
    assert (_P, "Amount") in outcome.provenance
