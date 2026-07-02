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
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import load_catalog_handle
from data_agent.runtime.retrieval.corpus_loader import load_corpus, load_seed_fixtures
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex

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
_E = "dbpcm_warehouse.employee"


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


def _executor(index: Neo4jVectorIndex) -> BlueprintExecutor:
    dispatcher = ToolDispatcher(RealMCPClient(os.environ["MCP_TEST_URL"]), load_catalog_handle())
    return BlueprintExecutor(tool_dispatcher=dispatcher, vector_index=index)


@pytest.fixture(scope="module")
def seeded_dag_corpus() -> bool:
    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(os.environ["NEO4J_TEST_URI"], auth=_auth())
        try:
            async with driver.session() as session:
                await session.run("MATCH (n) DETACH DELETE n")  # DESTRUCTIVE — l2 only
            blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
            await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        finally:
            await driver.close()

    asyncio.run(_seed())
    return True


async def test_single_node_blueprint_runs_and_verifies_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    jwt = await mint()  # allow-all scope
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
    jwt = await mint()  # allow-all scope
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
    jwt = await mint([f"{_E}.EmployeeCode"])
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
