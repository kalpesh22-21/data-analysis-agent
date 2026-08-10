"""S9 terminal transitions → the `learning_corpus` artifact status stamp (PriorArt S2).

`CorpusArtifact` gained `status`/`source` in PriorArt Slice 1 but NOTHING wrote them.
`BlueprintCorpus` is get/seed/increment/list — no delete, no status write — so a
rejected candidate's artifact survived, kept accruing hits toward the promotion
threshold, and kept surfacing to the dedup soft layer as live prior art. The same
declined idea therefore came back indefinitely.

The two TERMINAL edges now stamp it. Nothing else does: an intermediate state lives on
the envelope, and mirroring it onto the artifact would create a second, divergent
lifecycle for the same thing. What the artifact needs to know is only whether it is dead.

Slugs:
  * S9-terminal-stamps-corpus-artifact
  * S9-corpus-stamp-fails-open
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.dedup import CorpusArtifact, InMemoryBlueprintCorpus
from data_agent.learning.promotion.scheduler import PromotionScheduler

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
)

_KEY = "sha256:single-bp"


def _corpus() -> InMemoryBlueprintCorpus:
    return InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-1", canonical_key=_KEY, intent="total earnings", hit_count=4)]
    )


def _scheduler(corpus, *, store=None) -> PromotionScheduler:
    return PromotionScheduler(
        store or InMemoryCandidateStore(),
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({_KEY: 4}),
        dependency_resolver=FakeDependencyResolver(),
        corpus_status=corpus,
    )


# --- S9-terminal-stamps-corpus-artifact ---------------------------------------


async def test_a_human_reject_stamps_the_corpus_artifact_rejected():
    corpus = _corpus()
    scheduler = _scheduler(corpus)
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)

    decision = await scheduler.apply_human_decision(env, "reject")

    assert decision.to_status == CandidateStatus.REJECTED
    assert corpus.status_calls == [(_KEY, CandidateStatus.REJECTED)]
    artifact = corpus.get_sync(_KEY)
    assert artifact.status == "rejected"
    assert artifact.is_terminal
    # The NARROW write must not disturb the counter: a full upsert would clobber a
    # `hit_count` another worker incremented between the scan read and this write.
    assert artifact.hit_count == 4


async def test_a_retract_stamps_the_corpus_artifact_retired():
    """A retract is the highest-stakes human edge (pulling a LEAKED blueprint from
    recall). It must also kill the prior-art artifact, or the loop will happily offer the
    retracted idea back as "we already have this"."""
    corpus = _corpus()
    scheduler = _scheduler(corpus)
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED)

    decision = await scheduler.apply_retract(env)

    assert decision.to_status == CandidateStatus.RETIRED
    assert corpus.get_sync(_KEY).status == "retired"


async def test_a_rejected_artifact_stops_being_live_prior_art():
    """The end-to-end property the stamp exists for, asserted through the dedup stage's
    own terminal filter rather than by reading the field back."""
    corpus = _corpus()
    await _scheduler(corpus).apply_human_decision(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW), "reject"
    )
    live = [a for a in await corpus.list_artifacts() if not a.is_terminal]
    assert live == []


async def test_an_approve_does_not_stamp_the_artifact():
    """Only the TERMINAL edges write. An approve produces `validated`, which is an
    ENVELOPE state; mirroring it would start a second lifecycle for the same artifact."""
    corpus = _corpus()
    store = InMemoryCandidateStore()
    scheduler = _scheduler(corpus, store=store)
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)
    await store.put(env)

    await scheduler.apply_human_decision(env, "approve")

    assert corpus.status_calls == []
    assert corpus.get_sync(_KEY).status == "extracted"


async def test_a_candidate_that_never_ran_s6_has_no_artifact_to_stamp():
    """No dedup verdict ⇒ no canonical key ⇒ no artifact. A clean no-op, not an error: a
    human-approved candidate that skipped S6 is a supported path (OQ-3)."""
    corpus = _corpus()
    scheduler = _scheduler(corpus)
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=None)

    await scheduler.apply_human_decision(env, "reject")

    assert corpus.status_calls == []


async def test_stamping_never_resurrects_a_vanished_artifact():
    """Parity with the durable store's sub-document REPLACE semantics: a missing
    document is a tolerated no-op, never a create. A resurrected artifact would carry a
    `rejected` status and a hit_count of nothing, confusing the promotion guard."""
    corpus = InMemoryBlueprintCorpus()  # empty — the artifact is gone
    scheduler = _scheduler(corpus)

    await scheduler.apply_human_decision(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW), "reject"
    )

    assert corpus.get_sync(_KEY) is None


# --- S9-corpus-stamp-fails-open -----------------------------------------------


class _RaisingCorpusStatus:
    async def set_status(self, canonical_key: str, status: str) -> None:
        raise RuntimeError("couchbase unreachable")


async def test_a_corpus_stamp_failure_never_blocks_a_human_reject():
    """FAIL-OPEN. The candidate-store transition is source of truth: a Couchbase hiccup
    must not turn a human's reject into an error. The cost of a lost stamp is bounded
    and in the safe direction — the artifact stays visible as prior art, so the worst
    case is one extra candidate reaching a human, never a bad landing."""
    store = InMemoryCandidateStore()
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        corpus_status=_RaisingCorpusStatus(),
    )
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)

    decision = await scheduler.apply_human_decision(env, "reject")  # must NOT raise

    assert decision.action == "reject"
    assert decision.to_status == CandidateStatus.REJECTED
    assert (await store.get(env.candidate_id)).status == CandidateStatus.REJECTED


async def test_a_corpus_stamp_failure_never_blocks_a_retract():
    store = InMemoryCandidateStore()
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        corpus_status=_RaisingCorpusStatus(),
    )
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED)

    decision = await scheduler.apply_retract(env)  # must NOT raise

    assert decision.action == "retire"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.RETIRED


async def test_no_writer_wired_is_byte_identical_to_the_pre_slice_behaviour():
    """`corpus_status` is optional so every existing caller is unaffected — the whole
    point of adding it as a narrow port rather than folding it into `HitCountReader`."""
    store = InMemoryCandidateStore()
    scheduler = PromotionScheduler(
        store, probe=FakeWarehouseProbe(), hit_counts=FakeHitCountReader()
    )
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)
    decision = await scheduler.apply_human_decision(env, "reject")
    assert decision.to_status == CandidateStatus.REJECTED


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_a_mis_routed_decision_from_the_wrong_status_stamps_nothing(decision):
    """An approve from a non-`in_review` status is a no-op hold, and a reject is only
    reachable from a real terminal transition. Neither may touch the artifact on a path
    that did not actually change the candidate's fate."""
    corpus = _corpus()
    scheduler = _scheduler(corpus)
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE)
    if decision == "approve":
        await scheduler.apply_human_decision(env, "approve")
        assert corpus.status_calls == []
    else:
        # A reject IS honoured from any status (it is the human's explicit kill), so the
        # stamp fires — pinned here so the asymmetry is deliberate rather than accidental.
        await scheduler.apply_human_decision(env, "reject")
        assert corpus.status_calls == [(_KEY, CandidateStatus.REJECTED)]
