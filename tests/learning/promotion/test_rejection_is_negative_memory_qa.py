"""Rejection as negative memory, re-verified UNDER THE LOOSENED GATE (plan §4/§5).

The mechanism landed in PriorArt Slice 2 (`BlueprintCorpus.set_status`, the scheduler's
two terminal edges, terminal filtering in both prior-art readers). Plan §4 called it a
PREREQUISITE for lowering the corroboration threshold, and the reason is a behavioural
one those slice-2 tests could not exercise: while the gate was 3, nothing reached a human
at all, so "the same declined idea comes back every week" was unreachable and unprovable.
At a threshold of 1 it is reachable, so it needs pinning at the layer that changed.

`test_corpus_status_writeback.py` covers the WRITE (who stamps what, and that it fails
open). This file covers the CONSEQUENCE, end to end through `DedupStage`: after a reject,
does the idea come back?

It also names — as a plain passing assertion, per the house convention — the part that is
NOT covered, so it is not rediscovered as new.

Slugs:
  * S6-rejected-key-still-dropped        — a byte-identical re-derivation stays dropped.
  * S6-rejected-artifact-not-prior-art   — it never routes a live candidate to review.
  * S6-paraphrase-of-a-reject-RETURNS    — the residue, pinned, not fixed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DedupVerdict
from data_agent.learning.dedup import (
    CorpusArtifact,
    DedupStage,
    InMemoryBlueprintCorpus,
    compute_canonical_key,
)
from data_agent.learning.promotion.scheduler import PromotionScheduler
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeWarehouseProbe,
    promotion_policy,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"
_INTENT = "total earnings by department"


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-fixture", user_id="u1", scope_ref="scope-1",
        trace_id="trace-fixture", content_hash="hash-fixture", turns=(),
        tool_calls=(), blueprint_usages=(), askuser_exchanges=(),
        failed_fixed_sql=(), accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _envelope(intent: str = _INTENT) -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    env = CandidateEnvelope.from_doc(doc["single"]["envelope"])
    payload = dict(env.payload)
    payload["intent"] = intent
    return replace(env, payload=payload)


def _hard_key(env: CandidateEnvelope) -> str:
    gen = env.payload["generalization"]
    return compute_canonical_key(
        env.payload.get("resolves") or {},
        gen.get("uses_rules") or [],
        gen.get("result_grain") or {},
        gen["canonical_ast_norm"],
    )


def _with_key(env: CandidateEnvelope, key: str | None) -> CandidateEnvelope:
    """Stamp the S6 verdict the terminal edge keys the artifact stamp off.

    Not cosmetic: `_stamp_corpus_status` reads `env.dedup.canonical_key`, so a test that
    seeds an artifact under a computed key but leaves the envelope's verdict pointing
    elsewhere would stamp nothing and then pass for the wrong reason."""
    if key is None:
        return replace(env, dedup=None)
    return replace(
        env,
        dedup=DedupVerdict(
            canonical_key=key, matched_id=None, similarity=0.0, action="insert",
            layer="soft",
        ),
    )


async def _reject_through_the_scheduler(
    corpus: InMemoryBlueprintCorpus, env: CandidateEnvelope
) -> None:
    """Drive the REAL terminal edge rather than hand-writing a `rejected` artifact, so
    this file breaks if the stamp is ever dropped from the reject path."""
    store = InMemoryCandidateStore()
    in_review = replace(env, status=CandidateStatus.IN_REVIEW)
    await store.put(in_review)
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        corpus_status=corpus,
    )
    decision = await scheduler.apply_human_decision(in_review, "reject")
    assert decision.action == "reject"


async def test_a_byte_identical_re_derivation_of_a_rejected_idea_is_still_dropped():
    """The HARD layer deliberately does NOT skip a terminal artifact, and this is why:
    a byte-identical canonical key IS the same idea, and the human said no to exactly
    this thing. Skipping it would let a declined idea walk straight back into the (now
    much shorter) queue on the very next session that re-derives it."""
    env = _envelope()
    key = _hard_key(env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-1", canonical_key=key, intent=_INTENT)]
    )
    await _reject_through_the_scheduler(corpus, _with_key(env, key))
    assert corpus.get_sync(key).status == CandidateStatus.REJECTED

    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(_envelope(), _ctx())

    assert result.control == "drop"
    assert result.envelope.dedup.action == "increment"


async def test_a_rejected_artifact_never_routes_a_live_candidate_to_review():
    """The SOFT layer does skip it, and the asymmetry with the hard layer is deliberate:
    a NEAR-match to a rejected idea is not the same idea, so letting the dead artifact
    band a live candidate as `merge` would turn a settled decision into recurring review
    noise under a different label."""
    rejected_env = _envelope()
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="cand-1", canonical_key=_hard_key(rejected_env), intent=_INTENT
            )
        ]
    )
    await _reject_through_the_scheduler(
        corpus, _with_key(rejected_env, _hard_key(rejected_env))
    )

    # A DIFFERENT candidate (different hard key) whose intent is textually identical.
    other = _envelope()
    payload = dict(other.payload)
    gen = dict(payload["generalization"])
    gen["canonical_ast_norm"] = "SELECT 1 AS a FROM payroll.payroll_fact"
    payload["generalization"] = gen
    other = replace(other, payload=payload, candidate_id="candidate::other::0")

    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(other, _ctx())

    assert result.envelope.dedup.action == "insert"  # NOT `merge` against a dead idea
    assert result.envelope.dedup.matched_id is None


async def test_a_rejected_artifact_stops_accruing_soft_recurrence():
    """The other half of "keeps accruing hits". The plan-§4 counter is dormant today, but
    it is being ACCRUED now precisely so it has history when it is switched on — so it
    must not be accruing evidence in favour of an idea a human already declined."""
    rejected_env = _envelope()
    key = _hard_key(rejected_env)
    corpus = InMemoryBlueprintCorpus(
        [CorpusArtifact(id="cand-1", canonical_key=key, intent=_INTENT)]
    )
    await _reject_through_the_scheduler(corpus, _with_key(rejected_env, key))

    other = replace(_envelope(), candidate_id="candidate::other::0")
    payload = dict(other.payload)
    gen = dict(payload["generalization"])
    gen["canonical_ast_norm"] = "SELECT 1 AS a FROM payroll.payroll_fact"
    payload["generalization"] = gen
    other = replace(other, payload=payload)

    stage = DedupStage(corpus, FakeEmbeddingClient(), recurrence_threshold=0.9)
    await stage.process(other, _ctx())

    assert corpus.recurrence_calls == []
    assert corpus.get_sync(key).recurrence_count == 0


async def test_a_reject_of_a_candidate_that_never_ran_s6_is_a_clean_no_op():
    """A human-approved candidate that skipped S6 has no `canonical_key`, hence no
    artifact, hence nothing to stamp. That is a supported path (OQ-3), not an error — and
    it must not raise inside a human's reject."""
    corpus = InMemoryBlueprintCorpus()
    await _reject_through_the_scheduler(corpus, _with_key(_envelope(), None))
    assert corpus.status_calls == []


async def test_a_paraphrase_of_a_rejected_idea_still_returns_is_a_known_limitation():
    """KNOWN LIMITATION, named rather than fixed — and it MATTERS MORE at a threshold of
    1 than it did at 3, which is why it is recorded here.

    Negative memory is keyed. A rejected artifact blocks a re-derivation that mints the
    SAME canonical key (the hard layer above) and stops surfacing as prior art. It cannot
    block a genuinely different derivation of the same business question: that mints a
    different key, the graph holds no node for it (a rejected candidate never landed, so
    layer 2 has nothing to match), and the soft layer deliberately skips the dead
    artifact. So the candidate is a clean `insert` and — now that the gate is 1 — reaches
    a human.

    Why it is not fixed here. The only mechanism that could catch it is a COSINE against
    a rejected artifact's intent, and dropping a candidate on a cosine is exactly what
    this codebase refuses to do everywhere else: "a cosine over intent prose is not an
    identity claim" (`DedupStage._adjudicate_cards`). Making the loop's most destructive
    decision on its weakest evidence to save a reviewer one glance is the wrong trade.

    The honest fix is a rejection REASON a human can express once ("this whole class of
    question is not worth learning"), which is a review-UI change, not a dedup one."""
    rejected_env = _envelope("total earnings by department")
    corpus = InMemoryBlueprintCorpus(
        [
            CorpusArtifact(
                id="cand-1", canonical_key=_hard_key(rejected_env),
                intent="total earnings by department",
            )
        ]
    )
    await _reject_through_the_scheduler(
        corpus, _with_key(rejected_env, _hard_key(rejected_env))
    )

    paraphrase = _envelope("sum of gross pay grouped by the department column")
    payload = dict(paraphrase.payload)
    gen = dict(payload["generalization"])
    gen["canonical_ast_norm"] = "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact"
    payload["generalization"] = gen
    paraphrase = replace(paraphrase, payload=payload, candidate_id="candidate::para::0")

    stage = DedupStage(corpus, FakeEmbeddingClient())
    result = await stage.process(paraphrase, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "insert"  # it WILL reach a human again
