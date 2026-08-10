"""Layer-2 integration — a human's REJECT/RETRACT actually kills prior art (item 6).

The unit suite proves the scheduler stamps a `learning_corpus` artifact. That is one of
TWO stores a rejected idea lives in, and the other one — the neo4j node — is the one the
prior-art reader actually queries. The end-to-end property nobody had measured is the
composite:

    a human rejects  ⇒  the LANDED NODE is stamped terminal
                     ⇒  `search()` and `get_by_structural_key()` stop returning it
                     ⇒  a candidate re-deriving it is adjudicated on its own merits

Every arrow there is a separate mechanism (`CorpusLandingWriter.update_status`, the
Cypher `NOT coalesce(status,'') IN $terminal`, and the dedup stage's layer ordering), and
all three are wired fail-open, which means a break in any of them is SILENT.

The fail-open posture is itself part of the contract and is measured, not assumed: a
human's reject must complete even when neo4j is unreachable and even when the Couchbase
artifact write raises. Fail-open here is the right call — the candidate store is source
of truth and a review UI that cannot reject during a database blip is worse than a
declined idea resurfacing — but it means the ONLY thing standing between a broken
write-back and a permanent inconsistency is that the store transition still happens. So
that is asserted explicitly on both failure paths.

Everything this module writes is prefixed `qaterm-` (nodes) and removed in teardown.

    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_learning_prior_art_terminal_exclusion_live.py -v
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.dedup import CorpusArtifact, InMemoryBlueprintCorpus
from data_agent.learning.priorart.neo4j_index import Neo4jPriorArtIndex
from data_agent.learning.promotion.landing import CorpusLandingWriter, landing_id
from data_agent.learning.promotion.scheduler import PromotionScheduler
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from tests.learning.promotion.helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_KEY = "sha256:qaterm-blueprint"
_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "learning"
    / "s4_enriched_blueprint.json"
)
_INTENT = "total earnings for a department in a given year"


def _uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
    )


def _embedder() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"], api_key="", model=_MODEL, timeout_seconds=30.0
    )


def _candidate(*, status: str) -> CandidateEnvelope:
    """A landed-shaped candidate whose deterministic landing id is `qaterm-`-prefixed, so
    the node it creates is unambiguously this module's and teardown can find it."""
    doc = copy.deepcopy(json.loads(_FIXTURE.read_text())["single"]["envelope"])
    doc["status"] = status
    doc["dedup"] = {
        "canonical_key": _KEY,
        "matched_id": None,
        "similarity": 1.0,
        "action": "increment",
        "layer": "hard",
    }
    return CandidateEnvelope.from_doc(doc)


def _scheduler(driver, corpus, *, store=None, writer=None) -> PromotionScheduler:
    return PromotionScheduler(
        store or InMemoryCandidateStore(),
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({_KEY: 4}),
        dependency_resolver=FakeDependencyResolver(),
        landing_writer=writer
        if writer is not None
        else CorpusLandingWriter(driver, _embedder(), model_id=_MODEL),
        corpus_status=corpus,
    )


def _corpus() -> InMemoryBlueprintCorpus:
    return InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-1", canonical_key=_KEY, intent=_INTENT, hit_count=4)]
    )


def _index(driver) -> Neo4jPriorArtIndex:
    return Neo4jPriorArtIndex(
        driver=driver, embedding_client=_embedder(), expected_model=_MODEL
    )


@pytest.fixture
async def landed():
    """Land the candidate through the REAL writer, yield (driver, env, node_id), then
    remove the node. The land is what makes this test meaningful — the write-back is a
    MATCH-by-id, so a test that never landed would pass on a no-op."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    env = _candidate(status=CandidateStatus.VALIDATED)
    node_id = landing_id(env)
    writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
    await writer.land(env)
    try:
        yield driver, env, node_id
    finally:
        async with driver.session() as session:
            await session.run(  # type: ignore[arg-type]
                "MATCH (b:Blueprint {id: $id}) DETACH DELETE b", {"id": node_id}
            )
        await driver.close()


async def _node_status(driver, node_id: str) -> str | None:
    async with driver.session() as session:
        result = await session.run(  # type: ignore[arg-type]
            "MATCH (b:Blueprint {id: $id}) RETURN b.status AS status", {"id": node_id}
        )
        rows = await result.data()
    return rows[0]["status"] if rows else None


async def _prior_art_ids(driver) -> set[str]:
    return {c.id for c in await _index(driver).search(_INTENT, limit=100)}


async def _node_structural_key(driver, node_id: str) -> str | None:
    async with driver.session() as session:
        result = await session.run(  # type: ignore[arg-type]
            "MATCH (b:Blueprint {id: $id}) RETURN b.structural_key AS key",
            {"id": node_id},
        )
        rows = await result.data()
    return rows[0]["key"] if rows else None


# --- the landed node IS prior art, before anything kills it -------------------


async def test_a_landed_learning_blueprint_is_live_prior_art(landed):
    """The precondition every test below rests on. Asserted separately so a failure
    downstream cannot be explained away as "it was never visible anyway" — which is
    exactly how a broken exclusion test passes for the wrong reason."""
    driver, _env, node_id = landed
    assert node_id in await _prior_art_ids(driver)
    key = await _node_structural_key(driver, node_id)
    assert key, "the landing writer stamped no structural_key"
    card = await _index(driver).get_by_structural_key(key)
    assert card is not None
    assert card.id == node_id
    assert not card.is_terminal


# --- reject ------------------------------------------------------------------


async def test_a_human_reject_removes_the_node_from_prior_art_end_to_end(landed):
    """Reject → node stamped `rejected` → BOTH prior-art reads stop seeing it. The keyed
    lookup is checked as well as the vector search because they are separate Cypher and
    only one of them can drop a candidate: an exclusion that works for `search` but not
    for `get_by_structural_key` would leave a REJECTED blueprint able to drop new
    candidates as `redundant_with_canon` for ever."""
    driver, env, node_id = landed
    key = await _node_structural_key(driver, node_id)
    corpus = _corpus()

    decision = await _scheduler(driver, corpus).apply_human_decision(env, "reject")

    assert decision.to_status == CandidateStatus.REJECTED
    assert await _node_status(driver, node_id) == "rejected"
    assert node_id not in await _prior_art_ids(driver)
    assert await _index(driver).get_by_structural_key(key) is None
    # ... and the OTHER store the same idea lives in is dead too.
    assert corpus.get_sync(_KEY).is_terminal


async def test_a_retract_removes_the_node_from_prior_art_end_to_end(landed):
    """The highest-stakes edge: a retract pulls a LEAKED blueprint out of recall. It must
    also stop the loop offering the retracted idea back as "we already have this"."""
    driver, env, node_id = landed
    key = await _node_structural_key(driver, node_id)
    corpus = _corpus()

    decision = await _scheduler(driver, corpus).apply_retract(env)

    assert decision.to_status == CandidateStatus.RETIRED
    assert await _node_status(driver, node_id) == "retired"
    assert node_id not in await _prior_art_ids(driver)
    assert await _index(driver).get_by_structural_key(key) is None
    assert corpus.get_sync(_KEY).status == "retired"


async def test_the_two_stores_agree_about_which_statuses_are_terminal(landed):
    """The bucket and the graph must share one definition of "dead", or the same
    candidate is dead in one reader and alive in the other — and the loop's verdict then
    depends on which one happened to answer. The scheduler writes the SAME literal to
    both, so this pins the literals rather than the mechanism."""
    from data_agent.learning.priorart.models import TERMINAL_STATUSES

    assert CandidateStatus.REJECTED in TERMINAL_STATUSES
    assert CandidateStatus.RETIRED in TERMINAL_STATUSES
    driver, env, node_id = landed
    await _scheduler(driver, _corpus()).apply_human_decision(env, "reject")
    assert await _node_status(driver, node_id) in TERMINAL_STATUSES


# --- fail-open, on both write-backs -------------------------------------------


class _RaisingCorpusStatus:
    """A `learning_corpus` that is down — the Couchbase hiccup."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def set_status(self, canonical_key: str, status: str) -> None:
        self.calls.append((canonical_key, status))
        raise RuntimeError("couchbase is unreachable")


async def test_a_couchbase_hiccup_never_blocks_a_human_reject(landed):
    """Fail-OPEN, measured against a real graph so the neo4j half is genuinely running.
    The Couchbase artifact write raises; the store transition and the NODE write-back
    must both still complete, because the review UI must not be unusable during a blip."""
    driver, env, node_id = landed
    store = InMemoryCandidateStore()
    failing = _RaisingCorpusStatus()

    decision = await _scheduler(driver, failing, store=store).apply_human_decision(
        env, "reject"
    )

    assert decision.to_status == CandidateStatus.REJECTED
    assert failing.calls == [(_KEY, CandidateStatus.REJECTED)]
    # The authoritative transition happened ...
    assert (await store.get(env.candidate_id)).status == CandidateStatus.REJECTED
    # ... and the graph — the store the prior-art reader queries — is consistent.
    assert await _node_status(driver, node_id) == "rejected"
    assert node_id not in await _prior_art_ids(driver)


async def test_an_unreachable_neo4j_never_blocks_a_human_reject(landed):
    """The mirror case, and the one that leaves a real inconsistency behind: with neo4j
    unreachable the node keeps its old status, so the rejected idea DOES stay visible as
    prior art. That is the accepted cost of fail-open, pinned here so the exposure is a
    known quantity rather than a surprise — the recovery path is the warning log plus the
    scheduler's periodic re-assert on subsequent examinations."""
    driver, env, node_id = landed
    dead_driver = AsyncGraphDatabase.driver(
        "bolt://127.0.0.1:1",
        auth=("neo4j", "nope"),
        connection_timeout=2.0,
        connection_acquisition_timeout=2.0,
        max_transaction_retry_time=2.0,
    )
    store = InMemoryCandidateStore()
    corpus = _corpus()
    try:
        scheduler = _scheduler(
            driver,
            corpus,
            store=store,
            writer=CorpusLandingWriter(dead_driver, _embedder(), model_id=_MODEL),
        )
        decision = await scheduler.apply_human_decision(env, "reject")
    finally:
        await dead_driver.close()

    # The human's decision stands.
    assert decision.to_status == CandidateStatus.REJECTED
    assert (await store.get(env.candidate_id)).status == CandidateStatus.REJECTED
    # The Couchbase half still landed — the two write-backs are independent.
    assert corpus.get_sync(_KEY).is_terminal
    # KNOWN, ACCEPTED EXPOSURE: the node was not stamped, so it is still live prior art.
    assert await _node_status(driver, node_id) != "rejected"
    assert node_id in await _prior_art_ids(driver)


async def test_a_candidate_with_no_dedup_verdict_stamps_nothing_and_still_rejects(
    landed,
):
    """No S6 verdict ⇒ no canonical key ⇒ no artifact to stamp. A supported path (a
    human-approved candidate that skipped dedup), so it must be a clean no-op rather than
    an error that blocks the reject."""
    driver, _env, _node_id = landed
    doc = copy.deepcopy(json.loads(_FIXTURE.read_text())["single"]["envelope"])
    doc["status"] = CandidateStatus.IN_REVIEW
    doc["dedup"] = None
    env = CandidateEnvelope.from_doc(doc)
    corpus = _corpus()

    decision = await _scheduler(driver, corpus).apply_human_decision(env, "reject")

    assert decision.to_status == CandidateStatus.REJECTED
    assert corpus.status_calls == []
    assert corpus.get_sync(_KEY).status == "extracted"
