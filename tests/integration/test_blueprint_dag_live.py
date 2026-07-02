"""Layer-2 integration — full-DAG storage round-trips through LIVE neo4j
(runblueprint-design §1, Slice A). The proofs:

  1. `load_corpus` persists the additive full-DAG JSON properties (sql_template,
     slots, resolves, uses_rules, result_grain) on the `:Blueprint` node, and
     `Neo4jVectorIndex.get_blueprint` reads them back BYTE-EXACT;
  2. `getBlueprint` renders the expanded DAG projection additively, and the bound
     `sql_template` binds under the F1 template binder (the stored SQL is real);
  3. recall is UNCHANGED — the D87 recall projection still returns the blueprint
     (the new opaque properties are not selected by recall).

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL; `uv run pytest` with no live
stack stays fully green. Run with the stack up:

    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_blueprint_dag_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.blueprint.template import bind_template
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import load_corpus, load_seed_fixtures
from data_agent.runtime.retrieval.tools import GetBlueprintTool
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_AVG_ID = "bp-average-salary-by-department"
_AVG_USES = frozenset(
    {
        "dbpcm_warehouse.employee.EmployeeCode",
        "dbpcm_warehouse.employee.Department",
        "dbpcm_warehouse.employee.AnnualSalary",
    }
)


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


def _creds(scope: frozenset[str]) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-dag", jwt="jwt", column_scope=scope)


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


async def test_full_dag_persists_and_reads_back(seeded_dag_corpus: bool) -> None:
    index = _index()
    try:
        detail = await index.get_blueprint(_AVG_ID)
    finally:
        await index.close()

    assert detail is not None
    assert detail.uses == _AVG_USES
    # The additive DAG properties round-trip byte-exact through neo4j.
    assert detail.resolves == {"salary": "AnnualSalary"}
    assert detail.slots == [
        {
            "name": "department",
            "type": "string",
            "required": True,
            "binds_to": "dbpcm_warehouse.employee.Department",
        }
    ]
    assert detail.result_grain == ["Department"]
    assert detail.sql_template is not None and "{department}" in detail.sql_template


async def test_get_blueprint_tool_expands_dag_and_sql_binds(seeded_dag_corpus: bool) -> None:
    index = _index()
    tool = GetBlueprintTool(vector_index=index)
    try:
        result = await tool.run({"id": _AVG_ID}, _creds(_AVG_USES))
    finally:
        await index.close()

    rf = result.result_full
    assert rf["found"] is True
    assert rf["resolves"] == {"salary": "AnnualSalary"}
    assert rf["result_grain"] == ["Department"]
    # The stored template is REAL SQL — it binds under the F1 typed-literal binder
    # and the adversarial value is ClickHouse-escaped, never interpolated.
    bound = bind_template(rf["sql_template"], {"department": "War'e--house"})
    assert "'War''e--house'" in bound


async def test_recall_unchanged_by_dag_storage(seeded_dag_corpus: bool) -> None:
    # The D87 recall projection is UNCHANGED — the blueprint still recalls; the
    # new opaque DAG properties are simply not read by recall.
    index = _index()
    try:
        candidates = await index.recall(
            query_vector=(await _embedder().embed(["average salary by department"]))[0],
            kind="blueprint",
            k=10,
        )
    finally:
        await index.close()
    assert _AVG_ID in {c.id for c in candidates}
