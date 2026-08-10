"""Layer-2 integration — Neo4jPriorArtIndex against a LIVE neo4j + the real D71
embedding mock (PriorArt Slice 2).

The proofs that only a live graph can give, each one a claim the unit suite can only
assert about a string:

  1. **The trust-gate inversion is real.** The SAME query text, through
     `Neo4jVectorIndex.recall` and through `Neo4jPriorArtIndex.search`, returns
     materially different sets: recall sees only `source='mcp'`; prior art sees the
     learning tier AND a sourceless node too. This is the whole slice in one assertion.
  2. **Terminal artifacts are excluded** by real Cypher, and only EXPLICITLY terminal
     ones (a statusless node still counts as prior art).
  3. **The structural-key lookup is a `NodeIndexSeek`**, measured with a real EXPLAIN —
     not assumed. The previous slice found that the obvious index shape silently loses
     the plan property you assumed it had, and only EXPLAIN showed it.
  4. **Embedding-model skew** survives a real round trip: a node stamped with a
     different model still comes back (recall would have dropped it) with
     `model_matched=False` and a discounted confidence.
  5. **Unreachable neo4j RAISES** `PriorArtUnavailableError` rather than returning `[]`
     — the inversion of `VectorIndex`'s degrade contract, which is what lets the dedup
     stage tell "nothing exists" from "I could not look".

Every node this test writes is prefixed `patest-` and removed in the fixture teardown,
so it never touches the seeded corpus.

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_learning_prior_art_live.py -v
"""

from __future__ import annotations

import os

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.priorart import (
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    PriorArtUnavailableError,
)
from data_agent.learning.priorart.neo4j_index import (
    _BY_STRUCTURAL_KEY_QUERY,
    Neo4jPriorArtIndex,
)
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import schema_statements
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_OTHER_MODEL = "patest-some-other-model"
_PREFIX = "patest-"
_KEY = "sha256:patest-structural-key"
_INTENT = "total earnings by department for a calendar year"
_QUESTION = "how much did each department earn in total last year"


def _uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
    )


def _embedding_client() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"], api_key="", model=_MODEL
    )


# Six nodes covering every axis the reader has to get right. Each carries the SAME
# `intent` text so the cosine ranking cannot be what separates them — only the tier /
# status / model stamps can.
#
# A `None` here is written through as a Cypher null, and neo4j REMOVES a property set to
# null — which is exactly how the sourceless `:Blueprint` found in the live dev graph
# came to exist (`corpus_loader` used to pass an export's explicit `"source": null`
# straight into `SET b.source = $source`). So the `nosource`/`nostatus` seeds are
# produced by the real mechanism, not by a hand-crafted approximation of it.
_SEEDS = [
    # id suffix,     source,      status,      verified, model,        structural_key
    ("canon", "mcp", "validated", True, _MODEL, _KEY),
    ("learning", "learning", "validated", False, _MODEL, _KEY),
    ("nosource", None, "validated", None, _MODEL, None),
    ("rejected", "learning", "rejected", False, _MODEL, None),
    ("stalemodel", "learning", "validated", False, _OTHER_MODEL, None),
    ("nostatus", "learning", None, False, _MODEL, None),
]

_SEED_CYPHER = """
MERGE (b:Blueprint {id: $id})
SET b.name = $id,
    b.intent = $intent,
    b.intent_embedding = $embedding,
    b.embedding_model = $model,
    b.slots_summary = 'department, year',
    b.uses = ['dbpcm_warehouse.payroll.Amount'],
    b.drift_status = 'clean',
    b.uses_rules_json = '["patest-rule"]',
    b.result_grain_json = '{"columns": ["Department"], "verifiable": true}',
    b.source = $source,
    b.status = $status,
    b.verified = $verified,
    b.structural_key = $structural_key
RETURN b.id AS id
"""


@pytest.fixture
async def graph():
    """Seed the six probe nodes, yield (driver, embedding_client), then remove them.

    The `blueprint_structural_key` index is created from `schema_statements` — which
    also proves that DDL string is valid Cypher against a real neo4j, something no unit
    test can check."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    embedder = _embedding_client()
    vector = (await embedder.embed([_INTENT]))[0]
    async with driver.session() as session:
        for statement in schema_statements(len(vector)):
            if "blueprint_structural_key" in statement:
                await session.run(statement)  # type: ignore[arg-type]
        await session.run("CALL db.awaitIndexes(60)")  # type: ignore[arg-type]
        for suffix, source, status, verified, model, key in _SEEDS:
            await session.run(  # type: ignore[arg-type]
                _SEED_CYPHER,
                {
                    "id": f"{_PREFIX}{suffix}",
                    "intent": _INTENT,
                    "embedding": vector,
                    "model": model,
                    "source": source,
                    "status": status,
                    "verified": verified,
                    "structural_key": key,
                },
            )
    try:
        yield driver, embedder
    finally:
        async with driver.session() as session:
            await session.run(  # type: ignore[arg-type]
                "MATCH (b:Blueprint) WHERE b.id STARTS WITH $p DETACH DELETE b",
                {"p": _PREFIX},
            )
        await driver.close()


def _index(driver, embedder, *, model: str = _MODEL) -> Neo4jPriorArtIndex:
    return Neo4jPriorArtIndex(
        driver=driver, embedding_client=embedder, expected_model=model
    )


def _ours(items) -> dict:
    return {i.id: i for i in items if i.id.startswith(_PREFIX)}


# --- 1. the trust-gate inversion, measured against recall itself --------------


async def test_prior_art_sees_what_recall_is_gated_away_from(graph):
    """THE slice, in one assertion. Recall's bare `source='mcp'` gate is fail-closed and
    correct FOR RECALL; the loop's dedup stage inherited that blindness only because it
    never read the graph at all. Run the same question through both readers over the SAME
    nodes and the difference is exactly the tiers the loop was missing."""
    driver, embedder = graph
    recall = Neo4jVectorIndex(
        url=_uri(), auth=_auth(), expected_model=_MODEL, driver=driver
    )
    recalled = _ours(
        await recall.recall(
            query_vector=(await embedder.embed([_QUESTION]))[0], kind="blueprint", k=50
        )
    )
    prior_art = _ours(await _index(driver, embedder).search(_QUESTION, limit=50))

    # Recall serves ONLY the canon node (fail-closed on source).
    assert set(recalled) == {f"{_PREFIX}canon"}
    # Prior art sees the canon AND the landed learning tier AND the sourceless node.
    assert {f"{_PREFIX}canon", f"{_PREFIX}learning", f"{_PREFIX}nosource"} <= set(prior_art)
    assert prior_art[f"{_PREFIX}canon"].tier == TIER_MCP
    assert prior_art[f"{_PREFIX}learning"].tier == TIER_LEARNING
    assert prior_art[f"{_PREFIX}nosource"].tier == TIER_UNSOURCED
    # A sourceless node is NEVER canon, so it can never trigger the drop verdict.
    assert not prior_art[f"{_PREFIX}nosource"].is_canon


async def test_the_card_projection_round_trips_through_real_properties(graph):
    """The stored `*_json` properties decode, and the grain goes through the SAME
    normalizer the structural key hashes (`Department` → `department`) so a card's grain
    and its key can never disagree about what the grain is."""
    driver, embedder = graph
    card = _ours(await _index(driver, embedder).search(_QUESTION, limit=50))[
        f"{_PREFIX}canon"
    ]
    assert card.intent == _INTENT
    assert card.uses_rules == ("patest-rule",)
    assert card.result_grain == ("department",)
    assert card.structural_key == _KEY
    assert card.status == "validated"
    assert card.verified is True
    assert card.drift_status == "clean"
    assert 0.0 < card.similarity <= 1.0


# --- 2. terminal exclusion, by real Cypher ------------------------------------


async def test_a_rejected_node_is_excluded_but_a_statusless_one_is_not(graph):
    """The polarity is the OPPOSITE of recall's eligibility filter, deliberately. Prior
    art asks "was this positively KILLED?", so only an explicit terminal status is
    dropped: under-including means re-proposing something we already have an opinion
    about, while over-including costs a human a glance."""
    driver, embedder = graph
    found = _ours(await _index(driver, embedder).search(_QUESTION, limit=50))
    assert f"{_PREFIX}rejected" not in found
    assert f"{_PREFIX}nostatus" in found
    assert found[f"{_PREFIX}nostatus"].status == ""
    assert not found[f"{_PREFIX}nostatus"].is_terminal


# --- 3. the structural-key plan, MEASURED ------------------------------------


async def test_the_structural_key_lookup_seeks_the_index_rather_than_scanning(graph):
    """MEASURED, not assumed. A previous slice found the obvious index shape silently
    losing `index_order` and LIMIT pushdown, and only EXPLAIN showed it — so the plan
    operator is asserted here rather than inferred from the DDL existing."""
    driver, _embedder = graph
    async with driver.session() as session:
        result = await session.run(  # type: ignore[arg-type]
            f"EXPLAIN {_BY_STRUCTURAL_KEY_QUERY}",
            {"key": _KEY, "terminal_statuses": ["rejected", "retired"]},
        )
        summary = await result.consume()
        plan = summary.plan

    operators = _plan_operators(plan)
    assert "NodeIndexSeek" in operators, operators
    assert "NodeByLabelScan" not in operators, operators


def _plan_operators(plan: dict | None) -> list[str]:
    """Flatten the driver's plan tree to operator names. The server suffixes them with
    the runtime (`NodeIndexSeek@neo4j`), so strip it — an assertion on the raw string
    would pass or fail on a runtime label rather than on the plan."""
    if not plan:
        return []
    out = [str(plan.get("operatorType", "")).split("@", 1)[0]]
    for child in plan.get("children", []) or []:
        out.extend(_plan_operators(child))
    return out


async def test_the_canon_wins_a_cross_tier_structural_key_collision(graph):
    """The collision the LOOSE key exists to detect: `patest-canon` and
    `patest-learning` carry the same structural key. `mcp` must win — "the canon already
    has this" is the stronger answer, and the only one that justifies dropping."""
    driver, embedder = graph
    card = await _index(driver, embedder).get_by_structural_key(_KEY)
    assert card is not None
    assert card.id == f"{_PREFIX}canon"
    assert card.is_canon
    assert card.score_basis == "structural_key"
    assert card.confidence == 1.0


async def test_a_structural_key_miss_is_none(graph):
    driver, embedder = graph
    assert await _index(driver, embedder).get_by_structural_key("sha256:nope") is None


# --- 4. embedding-model skew, over a real vector index ------------------------


async def test_a_model_skewed_node_still_surfaces_flagged_and_discounted(graph):
    """The hydrator preserves `source='learning'` nodes across an embedding-model change
    WITHOUT re-embedding them (its parity accounting is scoped to `source='mcp'`). Recall
    never notices because it filters on the model; a cross-tier search must NOT filter,
    so the skew has to be reported instead. Here `patest-stalemodel` carries a foreign
    model stamp over a real vector — it comes back, flagged, discounted."""
    driver, embedder = graph
    found = _ours(await _index(driver, embedder).search(_QUESTION, limit=50))
    stale = found[f"{_PREFIX}stalemodel"]
    assert stale.embedding_model == _OTHER_MODEL
    assert stale.model_matched is False
    # The raw cosine is real and high (identical intent text) ...
    assert stale.similarity > 0.5
    # ... but discounted below BOTH dedup bands, so a skewed corpus can never auto-route
    # a genuinely-new blueprint to the inbox as a duplicate.
    assert stale.confidence == pytest.approx(stale.similarity * 0.5)
    assert stale.confidence < 0.83

    # And recall, by contrast, drops it entirely — that filter is what hides the skew.
    recall = Neo4jVectorIndex(
        url=_uri(), auth=_auth(), expected_model=_MODEL, driver=driver
    )
    recalled = _ours(
        await recall.recall(
            query_vector=(await embedder.embed([_QUESTION]))[0], kind="blueprint", k=50
        )
    )
    assert f"{_PREFIX}stalemodel" not in recalled


async def test_a_wholly_mismatched_query_model_flags_every_card(graph):
    """The operator-visible symptom of misconfiguring `expected_model` (or of a corpus
    that has not been re-embedded): a 100% mismatch rate, with every card still returned
    rather than an empty result that looks like an empty corpus."""
    driver, embedder = graph
    cards = _ours(
        await _index(driver, embedder, model="a-model-nothing-uses").search(
            _QUESTION, limit=50
        )
    )
    assert cards
    assert all(not c.model_matched for c in cards.values())


# --- 5. unavailable RAISES ----------------------------------------------------


async def test_an_unreachable_graph_raises_rather_than_asserting_novelty():
    """The inversion of `VectorIndex`'s degrade-to-`[]` contract. Returning `[]` here
    would tell the dedup stage "nothing like this exists", and it would mint a duplicate
    of something the canon already carries and record it as novel."""
    driver = AsyncGraphDatabase.driver(
        "bolt://127.0.0.1:1",
        auth=("neo4j", "nope"),
        connection_timeout=2.0,
        connection_acquisition_timeout=2.0,
        max_transaction_retry_time=2.0,
    )
    try:
        index = _index(driver, _embedding_client())
        with pytest.raises(PriorArtUnavailableError):
            await index.search(_QUESTION)
        with pytest.raises(PriorArtUnavailableError):
            await index.get_by_structural_key(_KEY)
    finally:
        await driver.close()
