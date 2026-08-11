"""Layer-2 integration — the extractor's PRIOR ART pre-fetch against the real graph.

Why this file exists
--------------------
Plan §3a's pre-fetch is unit-covered at both ends and nowhere in the middle. The unit
suite drives `InMemoryPriorArtIndex`, whose `search` is a token-overlap scan over a list
a test seeded; the neo4j suites drive `Neo4jPriorArtIndex` directly with hand-built
queries. Nothing exercises the actual composition:

    prior_art_query_text(summary)  ->  Neo4jPriorArtIndex.search  ->  render_prior_art_block

which is the only path a production extraction ever takes, and the only one where the
question "does the query text this builder produces actually retrieve anything sensible
from the real vector index?" has an answer. A pre-fetch that embeds well-formed text and
returns nothing relevant is not a crash — it is a silently useless block, and the
extractor would carry on re-proposing artifacts the canon already holds while every
unit test stayed green.

So this asserts the three things a unit test structurally cannot:

  1. the composed chain returns `available=True` with real cards from the real index;
  2. the cosines are PLAUSIBLE — the top hit for a question the canon demonstrably
     covers scores well clear of the field, rather than the whole corpus arriving at an
     undifferentiated ~0.5 (which is what an embedding/model mismatch looks like);
  3. the rendered block carries real node content — ids, intents, rule ids, trust tier
     — and still holds its own structural invariants (one fence, one line per card)
     against text nobody wrote for a test.

READ-ONLY, and deliberately so. It seeds nothing and deletes nothing, so it is safe
beside another agent's destructive live suite — and it is also therefore dependent on
the dev graph already holding the canon. The assertions are written to be honest about
that: no specific blueprint id is required, only that SOMETHING scores highly for a
question the canon covers. `test_..._is_meaningful` skips (does not fail) on an empty
graph, because "nobody has loaded the corpus" is an environment fact, not a defect.

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_extractor_prefetch_live.py -s -v

Slug: PA-extractor-prefetch-live.
"""

from __future__ import annotations

import os

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.extractor.prior_art import (
    _BLOCK_FOOTER,
    _BLOCK_HEADER,
    lookup_prior_art,
    prior_art_query_text,
    render_prior_art_block,
)
from data_agent.learning.priorart import PriorArtCard
from data_agent.learning.priorart.neo4j_index import Neo4jPriorArtIndex
from data_agent.learning.summary.models import SessionSummary, ToolCallSummary, TurnSummary
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"

# A question the seeded canon demonstrably covers (`bp-active-headcount-by-department`),
# phrased the way a user would rather than the way the node's `intent` is written — the
# pre-fetch is only worth anything if the cosine bridges that gap.
_SQL = (
    "SELECT department_name, COUNT(DISTINCT employee_code) AS headcount "
    "FROM dbpcm_warehouse.employee WHERE employee_status = 'A' "
    "GROUP BY department_name"
)
_QUESTION = "how many active employees per department?"


@pytest.fixture
async def index():
    driver = AsyncGraphDatabase.driver(
        os.environ["NEO4J_TEST_URI"],
        auth=(
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
        ),
    )
    try:
        yield Neo4jPriorArtIndex(
            driver=driver,
            embedding_client=HttpEmbeddingClient(
                url=os.environ["EMBEDDING_TEST_URL"], api_key="", model=_MODEL
            ),
            expected_model=_MODEL,
        )
    finally:
        await driver.close()


def _summary(*, question: str | None = _QUESTION, sql: str | None = _SQL):
    turns = (
        (TurnSummary(turn_index=0, user_nl=question, assistant_text=None,
                     tool_call_refs=("tc1",)),)
        if question is not None else ()
    )
    calls = (
        (ToolCallSummary(
            turn_index=0, tool_call_ref="tc1", tool_name="runQuery", args={"sql": sql},
            sql=sql, status="ok", error_code=None, provenance=frozenset(),
            result_columns=("department_name", "headcount"), result_row_count=6,
            result_full_ref=None, full_result_loaded=False),)
        if sql is not None else ()
    )
    return SessionSummary(
        session_id="prefetch-live", user_id="u1", scope_ref="sc", trace_id="tr",
        content_hash="h", turns=turns, tool_calls=calls, blueprint_usages=(),
        askuser_exchanges=(), failed_fixed_sql=(), accepted_signal="no_correction",
    )


async def _lookup(index, summary):
    return await lookup_prior_art(index, prior_art_query_text(summary), limit=5)


async def test_the_composed_prefetch_reaches_the_real_index(index):
    """The chain runs and reports AVAILABLE. `available=False` here would mean the
    embedder or the graph is unreachable — the fail-open path, which is correct
    behaviour but tells us nothing about the pre-fetch."""
    lookup = await _lookup(index, _summary())
    assert lookup.available is True
    assert all(isinstance(c, PriorArtCard) for c in lookup.cards)


async def test_the_prefetch_returns_plausible_cosines_and_real_card_content(index):
    """(2) and (3) together, because they are one observation: the block a real model
    would be shown."""
    lookup = await _lookup(index, _summary())
    if not lookup.cards:
        pytest.skip("dev graph holds no corpus — nothing to score (load_corpus first)")

    scores = [c.confidence for c in lookup.cards]
    assert scores == sorted(scores, reverse=True), scores
    assert all(0.0 < s <= 1.0 for s in scores), scores
    # A question the canon covers should produce a clear top hit, not a flat field of
    # near-ties. A flat field is the signature of a query embedded in a different space
    # from the nodes — which `model_matched` is supposed to catch, so check that too.
    assert scores[0] >= 0.6, scores
    assert all(c.model_matched for c in lookup.cards), [
        (c.id, c.embedding_model) for c in lookup.cards
    ]
    if len(scores) > 1:
        assert scores[0] - scores[-1] >= 0.05, scores

    top = lookup.cards[0]
    assert top.id and top.intent, top
    assert top.tier in ("mcp", "learning", "unsourced")

    block = render_prior_art_block(lookup)
    print("\n" + block)
    assert block.count(_BLOCK_HEADER) == 1
    assert block.count(_BLOCK_FOOTER) == 1
    card_lines = [line for line in block.splitlines() if line.startswith("- id=")]
    assert len(card_lines) == len(lookup.cards)
    assert top.id in block
    assert f"tier={top.tier}" in block
    # Real node text, not a placeholder — the renderer's `(no intent recorded)` /
    # `(unidentified)` fallbacks mean the mapper handed us nothing usable.
    assert "(no intent recorded)" not in block
    assert "(unidentified)" not in block


async def test_a_session_with_nothing_to_search_for_never_touches_the_embedder(index):
    """The NOT-SEARCHED state, against real infra: no question and no accepted SQL must
    short-circuit before the embed, and must not be reported as an outage."""
    lookup = await _lookup(index, _summary(question=None, sql=None))
    assert lookup.available is True
    assert lookup.cards == ()
    assert lookup.query == ""
    assert "NOT SEARCHED" in render_prior_art_block(lookup)


async def test_an_unreachable_graph_degrades_to_could_not_look(index):
    """The fail-open path against real components: a live embedder plus a dead driver.
    Distinct from the empty case, and the distinction is what stops a graph outage from
    reading as a novelty claim."""
    dead = Neo4jPriorArtIndex(
        driver=AsyncGraphDatabase.driver(
            "bolt://127.0.0.1:1", auth=("neo4j", "testpassword")
        ),
        embedding_client=HttpEmbeddingClient(
            url=os.environ["EMBEDDING_TEST_URL"], api_key="", model=_MODEL
        ),
        expected_model=_MODEL,
    )
    try:
        lookup = await _lookup(dead, _summary())
    finally:
        await dead._driver.close()
    assert lookup.available is False
    assert "COULD NOT LOOK" in render_prior_art_block(lookup)
