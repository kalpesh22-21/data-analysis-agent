"""Layer-2 integration — a real TABLE-INTERMEDIATE blueprint executed END-TO-END
through the live stack (table-intermediate-design §2.2/§Q2, Slice 2):

    getBlueprint (real neo4j) → parse → slot DISTINCT-domain probe (real MCP →
    real ClickHouse) → node 0 runQuery (per-employee earnings TABLE) → MATERIALIZE
    to a real `scratch.s_<sid>_bp_<uuid>` table via the D93 side-channel (real MCP
    scratch route → real scratch-only ClickHouse credential) → node 1's JOIN
    AST-rewritten to that scratch table → node 1 runQuery (scratch ⨝ warehouse) →
    D56 grain probe → verified result.

Also proves session isolation on a REAL written table (invariant #3): session B
cannot read session A's materialized scratch table (SCRATCH_SESSION_VIOLATION).

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL + MCP_TEST_URL; `uv run
pytest` with no live stack stays fully green. Run with the stack up (the l2-mcp
image must be REBUILT so /scratch/v1/* is live, and SCRATCH_CH_* provisioned):

    docker compose -f docker-compose.integration.yml up -d --build
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
    MCP_TEST_URL=http://localhost:18090/mcp \
        uv run pytest tests/integration/test_table_intermediate_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.executor import BlueprintExecutor, ExecCompleted
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.mcp.scratch_client import ScratchClient
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
_TABLE_ID = "bp-earnings-by-department-via-scratch-join"
_E = "dbpcm_warehouse.employee"
_P = "dbpcm_warehouse.payroll"
# Underscore-free, identifier-safe session ids (the Slice-2 contract) so the
# scratch table `s_<sid>_bp_<uuid>` parses AND the tightened D64 read gate can
# extract the owning session unambiguously.
_SID_A = "sscratchlivea"
_SID_B = "sscratchliveb"


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


def _scratch_base() -> str:
    """Derive the …/scratch/v1 base from the MCP host (contract §Q6)."""
    parts = urlsplit(os.environ["MCP_TEST_URL"])
    return urlunsplit((parts.scheme, parts.netloc, "/scratch/v1", "", ""))


def _executor(index: Neo4jVectorIndex) -> BlueprintExecutor:
    return BlueprintExecutor(
        tool_dispatcher=ToolDispatcher(
            RealMCPClient(os.environ["MCP_TEST_URL"]), fixture_catalog_handle()
        ),
        vector_index=index,
        scratch_client=ScratchClient(_scratch_base()),
    )


@pytest.fixture(scope="module")
def seeded_dag_corpus() -> bool:
    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(os.environ["NEO4J_TEST_URI"], auth=_auth())
        try:
            async with driver.session() as session:
                await session.run("MATCH (n) DETACH DELETE n")  # DESTRUCTIVE — l2 only
            # Catalog-owned :Table/:Column graph first (load_corpus MATCHes these).
            await load_catalog_graph(driver, load_catalog_export())
            blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
            await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        finally:
            await driver.close()

    asyncio.run(_seed())
    return True


async def test_table_intermediate_blueprint_materializes_joins_and_verifies_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    jwt = await mint(session_id=_SID_A)  # allow-all scope, session-bound
    creds = RuntimeCredentials(session_id=_SID_A, jwt=jwt, column_scope=frozenset())
    index = _index()
    try:
        outcome = await _executor(index).execute(
            blueprint_id=_TABLE_ID, slot_bindings={"department": "Sales"}, credentials=creds
        )
    finally:
        await index.close()

    assert isinstance(outcome, ExecCompleted), outcome
    rf = outcome.result_full
    assert rf["status"] == "verified"
    assert rf["verify"]["grain_ok"] is True
    assert rf["verify"]["grain_checked"] is True
    # One department (Sales) → one verified result row.
    assert rf["row_count"] == 1

    # The verified answer is the TRUE per-department earnings JOIN result:
    # Sales EARN = EMP001 (3125.00) + EMP003 (3000.00) = 6125.0 — computed by
    # materializing per-employee earnings then row-JOINing to `employee`.
    cols = [c.lower() for c in rf["columns"]]
    total_idx = cols.index("total_earnings")
    total = float(rf["preview_rows"][0][total_idx])
    assert total == 6125.0, f"expected Sales earnings 6125.0, got {total}"

    # TWO per-node SQLs surfaced (producer + consumer) for transparency (D56).
    assert len(rf["sql"]) == 2
    consumer_sql = rf["sql"][1]
    # The consumer's JOIN reads a REAL materialized scratch table for THIS session
    # (the AST rewrite ran against a live-returned name), not the placeholder.
    assert f"s_{_SID_A}_bp_" in consumer_sql
    assert "emp_earnings" not in consumer_sql

    # Provenance is the LIVE warehouse footprint ONLY — scratch columns EXCLUDED
    # (D69/OQ-4), the union honest + fail-closed.
    assert outcome.provenance is not None
    assert (_P, "Amount") in outcome.provenance
    assert (_E, "Department") in outcome.provenance
    assert not any(db.startswith("scratch.") for db, _ in outcome.provenance)


async def test_cross_session_scratch_read_is_rejected_on_a_real_written_table_live(
    seeded_dag_corpus: bool, mint: Mint
) -> None:
    # Session A materializes a real scratch table via the D93 side-channel.
    jwt_a = await mint(session_id=_SID_A)
    scratch = ScratchClient(_scratch_base())
    table = await scratch.materialize(
        [{"name": "k", "type": "Int64"}, {"name": "v", "type": "Float64"}],
        [[1, 10.0], [2, 20.0]],
        jwt=jwt_a,
        session_id=_SID_A,
    )
    assert table.startswith(f"scratch.s_{_SID_A}_bp_"), table

    mcp = RealMCPClient(os.environ["MCP_TEST_URL"])
    # Alias + qualify the scratch columns (the realistic JOIN pattern the seed
    # blueprint uses): a qualified `s.k` resolves via the extractor's alias map,
    # which validates scratch session ownership BEFORE column resolution — and is
    # unaffected by the MCP catalog picking up other sessions' scratch tables.
    read_sql = f"SELECT s.k, s.v FROM {table} AS s"

    # Session A CAN read its OWN materialized table (the D64 gate accepts it).
    own = await mcp.call_tool("runQuery", {"sql": read_sql}, jwt=jwt_a, session_id=_SID_A)
    assert isinstance(own, dict)
    assert own.get("row_count") == 2

    # Session B (its own token + sid) CANNOT read session A's scratch table — the
    # live D64 read gate rejects it (invariant #3, on a REAL written table).
    jwt_b = await mint(session_id=_SID_B)
    with pytest.raises(MCPToolError) as excinfo:
        await mcp.call_tool("runQuery", {"sql": read_sql}, jwt=jwt_b, session_id=_SID_B)
    # The isolation violation surfaces as the scratch-session / scope-violation code.
    assert excinfo.value.code in {"SCRATCH_SESSION_VIOLATION", "COLUMN_SCOPE_VIOLATION"}, (
        excinfo.value.code,
        str(excinfo.value),
    )
