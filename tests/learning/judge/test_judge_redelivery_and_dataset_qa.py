"""Redelivery idempotency and the queryable dataset (plan §3b).

Two claims are checked here that nothing else in the judge suite reaches, because both
are about the judge's relationship with things OUTSIDE it:

  1. **Idempotency is the deterministic audit key, not the envelope stamp.** A
     redelivery re-extracts and mints a fresh `CandidateEnvelope` with `judge=None`, so
     an envelope-carried verdict cannot survive one — and the pre-extraction stage has
     no envelope at all. The mechanism has to be the key that is read before every model
     call, so it is driven through the REAL consumer (`run_once`, real queue, real store,
     real CAS state machine) and the model calls are counted.

  2. **The stored row is the product.** The verdict exists so that "do the loop's
     re-derivations skew to `existing-plus-delta`?" is a query, and that question decides
     whether atomic composable blueprints get built. A row that cannot be grouped, or
     that carries the verdict without the bar it was compared against, would answer it
     wrongly and nobody would notice.

Layer-1 throughout — no docker, no live Couchbase. The live N1QL forms these shapes were
checked against are in the QA report; they need `scripts/learning-audit-init.sh` re-run
for the `(record_type, judged_at)` index.
"""

from __future__ import annotations

from dataclasses import replace

from data_agent.learning import state_machine
from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import (
    COVERAGE_VERDICTS,
    JUDGE_RECORD_TYPE,
    CoverageAssessment,
    JudgeRecord,
    judgement_fingerprint,
    post_extraction_ref,
    pre_extraction_ref,
)
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import InMemoryBlueprintCorpus
from data_agent.learning.extractor.prior_art import prior_art_query_text
from data_agent.learning.factory import build_learning_consumer
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.priorart import InMemoryPriorArtIndex
from data_agent.learning.user import InMemoryUserKnowledgeStore
from data_agent.runtime.model.scripted_client import ScriptedModelClient

from ..conftest import make_message, make_trail_entry
from ..extractor.helpers import KEEP_VERDICT, blueprint_raw, make_turn, scripted_turn
from ..extractor.helpers import make_summary as extractor_summary
from ..generalize.helpers import CATALOG
from .helpers import card, make_envelope, make_judge, make_summary, verdict_turn

_SUMMARY = extractor_summary(turns=(make_turn(),))
_QUERY = prior_art_query_text(_SUMMARY)


# ===========================================================================
# 1. Idempotency, driven through the real consumer.
# ===========================================================================


class _CountingModelClient:
    """A `ModelClient` that counts every round trip. The whole claim under test is
    "how many times did we pay a model?", so counting is the assertion."""

    def __init__(self, script) -> None:  # noqa: ANN001
        self._inner = ScriptedModelClient(script)
        self.calls = 0

    async def send_turn(self, messages, tools):  # noqa: ANN001, ANN201
        self.calls += 1
        return await self._inner.send_turn(messages, tools)

    def begin_turn(self):  # noqa: ANN201
        return self


async def _summary_loader(doc, store, *, job):  # noqa: ANN001, ANN201
    return _SUMMARY


def _consumer(store, queue, *, settings, audit, judge_client, extractor_client,
              candidates, corpus, index):  # noqa: ANN001, ANN202
    return build_learning_consumer(
        settings,
        session_store=store,
        queue=queue,
        model_client=extractor_client,
        judge_model_client=judge_client,
        audit_store=audit,
        candidate_store=candidates,
        blueprint_corpus=corpus,
        user_store=InMemoryUserKnowledgeStore(),
        catalog_schema=CATALOG,
        prior_art=index,
        sampler=lambda _env: False,
        summary_loader=_summary_loader,
        triage=lambda _s: KEEP_VERDICT,
    )


async def _enqueue(store, queue, seed_session, session_id):  # noqa: ANN001, ANN202
    doc = seed_session(
        store,
        session_id,
        learning_status=LearningStatus.QUEUED,
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
        tool_trail=[
            make_trail_entry(
                turn_index=0, tool_name="runQuery", args={"sql": "SELECT 1"}, status="ok"
            )
        ],
    )
    job = LearningJob.from_doc(doc, content_hash=compute_content_hash(doc))
    await queue.enqueue(job)
    return job


def _index() -> InMemoryPriorArtIndex:
    return InMemoryPriorArtIndex([card()], scores={(_QUERY, "bp::abc"): 0.85})


async def test_a_real_redelivery_pays_for_exactly_one_judge_call(
    store, queue, settings, seed_session
) -> None:
    """The headline idempotency claim, end-to-end.

    Three deliveries of the SAME session content through `run_once`, over a real queue
    and a real CAS state machine, with a fresh consumer each time (a restarted worker,
    so nothing in-process can be memoising). Exactly one model call.

    The third delivery is the one that matters: the session is back at `queued` with an
    unchanged content hash — a replay or a crash-recovery re-enqueue — so the loop's own
    `done`+same-hash dedup does NOT short-circuit it, `_do_work` runs again in full, the
    extractor is paid a second time, and the ONLY thing standing between the judge and a
    second, possibly different verdict is the deterministic audit key."""
    job = await _enqueue(store, queue, seed_session, "sess-1")
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    corpus = InMemoryBlueprintCorpus()
    judge_client = _CountingModelClient(
        [verdict_turn("existing-plus-delta", confidence=0.99) for _ in range(3)]
    )

    def build(q):  # noqa: ANN001, ANN202
        return _consumer(
            store, q, settings=settings, audit=audit, judge_client=judge_client,
            extractor_client=ScriptedModelClient([scripted_turn([blueprint_raw()])]),
            candidates=candidates, corpus=corpus, index=_index(),
        )

    first = await build(queue).run_once()
    assert first.done == 1
    assert judge_client.calls == 1
    assert len(audit.judgements) == 1

    # Delivery 2: the same job on a fresh stream, session already `done` with the same
    # hash. The loop's own idempotency ACKs it before anything runs.
    queue2 = InMemoryLearningQueue()
    await queue2.enqueue(job)
    second = await build(queue2).run_once()
    assert second.dedup_skips == 1
    assert judge_client.calls == 1

    # Delivery 3: re-queued with the SAME content. `_do_work` runs in full — and the
    # judge is served from the audit key.
    doc = store._docs["sess-1"]
    store._docs["sess-1"] = replace(doc, learning_status=LearningStatus.QUEUED)
    queue3 = InMemoryLearningQueue()
    await queue3.enqueue(job)
    third = await build(queue3).run_once()

    assert third.done == 1
    assert judge_client.calls == 1, "a redelivery must not pay for a second verdict"
    assert len(audit.judgements) == 1, "and must not mint a second row"


async def test_the_pre_extraction_stage_has_no_envelope_and_is_covered_anyway(
    store, queue, settings, seed_session
) -> None:
    """The pre-extraction judgement happens before anything is extracted, so there is no
    `CandidateEnvelope` to stamp — its idempotency can only come from the audit key.

    Asserted by shape as well as by count: the row that served the second delivery is a
    `pre_extraction` row with no `candidate_id`, keyed under the pre namespace."""
    job = await _enqueue(store, queue, seed_session, "sess-2")
    audit = InMemoryAuditStore()
    judge_client = _CountingModelClient(
        [verdict_turn("existing-plus-delta", confidence=0.99) for _ in range(2)]
    )

    def build(q):  # noqa: ANN001, ANN202
        return _consumer(
            store, q, settings=settings, audit=audit, judge_client=judge_client,
            extractor_client=ScriptedModelClient([scripted_turn([blueprint_raw()])]),
            candidates=InMemoryCandidateStore(), corpus=InMemoryBlueprintCorpus(),
            index=_index(),
        )

    await build(queue).run_once()
    doc = store._docs["sess-2"]
    store._docs["sess-2"] = replace(doc, learning_status=LearningStatus.QUEUED)
    queue2 = InMemoryLearningQueue()
    await queue2.enqueue(job)
    await build(queue2).run_once()

    assert judge_client.calls == 1
    row = audit.judgements[0]
    assert row.stage == "pre_extraction"
    assert row.candidate_id is None
    assert row.judgement_ref.startswith("judgement::pre::")


async def test_a_pel_reclaim_of_an_in_flight_session_never_reaches_the_judge(
    store, queue, settings, seed_session
) -> None:
    """DOCUMENTS which redelivery actually exercises the key, and which does not.

    `judgement.py` motivates the deterministic key with "a crash between `processing`
    and `done` puts the message back in the PEL". That message does come back — but
    `_process` transitions `queued → processing`, and `VALID_TRANSITIONS` has no
    `processing → processing` edge, so the reclaim CAS-mismatches and returns `skip`
    before `_do_work` is ever entered. The judge is not reached, let alone re-asked.

    So the key earns its keep on the OTHER redeliveries — a replay, a re-enqueue, a peer
    race — and not on the one the docstring names. Recorded because "we tested the
    scenario in the comment" would otherwise be a false reassurance."""
    job = await _enqueue(store, queue, seed_session, "sess-3")
    _doc, cas = await store.get_session_with_cas("sess-3")
    await state_machine.transition(
        store, "sess-3", LearningStatus.QUEUED, LearningStatus.PROCESSING, cas
    )
    judge_client = _CountingModelClient([verdict_turn("duplicate", confidence=1.0)])
    consumer = _consumer(
        store, queue, settings=settings, audit=InMemoryAuditStore(),
        judge_client=judge_client,
        extractor_client=ScriptedModelClient([scripted_turn([blueprint_raw()])]),
        candidates=InMemoryCandidateStore(), corpus=InMemoryBlueprintCorpus(),
        index=_index(),
    )

    result = await consumer.run_once()

    assert result.skipped == 1
    assert judge_client.calls == 0
    assert store._docs["sess-3"].learning_status == LearningStatus.PROCESSING
    assert job.content_hash  # the message is still in the PEL for a later reclaim


async def test_the_post_extraction_stage_is_also_served_from_the_key() -> None:
    """The dedup-side judgement, twice over, with a fresh judge each time (a restarted
    worker) and the SAME audit store. One model call."""
    audit = InMemoryAuditStore()
    env = make_envelope()

    first_judge, first_client, _a, _i = make_judge(
        [verdict_turn("existing-plus-delta", confidence=0.99)], audit=audit
    )
    await first_judge.adjudicate_candidate(env, make_summary(), [card()])
    assert first_client.calls_made == 1

    second_judge, second_client, _a, _i = make_judge([], audit=audit)
    outcome = await second_judge.adjudicate_candidate(env, make_summary(), [card()])

    assert second_client.calls_made == 0
    assert outcome.assessment is not None
    assert outcome.assessment.verdict == "existing-plus-delta"
    assert len(audit.judgements) == 1


async def test_the_envelope_stamp_is_not_the_idempotency_mechanism() -> None:
    """`CandidateEnvelope.judge` is a review-inbox convenience and a dataset field, and
    its own docstring says so: "Do not read this field as the idempotency mechanism."
    Pinned by construction — a re-extraction mints an envelope with `judge=None`, and the
    second judgement is still served without a model call."""
    audit = InMemoryAuditStore()
    first_judge, _c, _a, _i = make_judge(
        [verdict_turn("existing-plus-delta", confidence=0.99)], audit=audit
    )
    stamped = await first_judge.adjudicate_candidate(make_envelope(), make_summary(), [card()])
    assert stamped.assessment is not None

    fresh = make_envelope()          # exactly what a re-extraction produces
    assert fresh.judge is None
    second_judge, second_client, _a, _i = make_judge([], audit=audit)
    await second_judge.adjudicate_candidate(fresh, make_summary(), [card()])
    assert second_client.calls_made == 0


async def test_a_verdict_rendered_about_different_content_is_never_reused() -> None:
    """The key binds the rendered brief, so a stored row whose fingerprint does not match
    is treated as no row at all.

    This is the guard on the guard. A position-keyed cache (the earlier `candidate_id`
    form) could hand candidate A the verdict rendered about candidate B after a
    re-extraction reordered the ordinals — a drop taken on a `reason` that describes a
    different session, which is exactly the unauditable drop `_resolve_covered_by`
    refuses when the model does it."""
    audit = InMemoryAuditStore()
    env = make_envelope()
    seed_judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.99)], audit=audit
    )
    await seed_judge.adjudicate_candidate(env, make_summary(), [card()])
    stored = audit.judgements[0]

    # Corrupt only the fingerprint, leaving the key and every other field intact — a
    # hand-edited doc, or a key-format change that outlived its data.
    audit._judgements[stored.judgement_ref] = replace(stored, fingerprint="not-this-content")

    replay_judge, replay_client, _a, _i = make_judge(
        [verdict_turn("new", confidence=0.2)], audit=audit
    )
    outcome = await replay_judge.adjudicate_candidate(env, make_summary(), [card()])

    assert replay_client.calls_made == 1, "a fingerprint mismatch must re-ask, not reuse"
    assert outcome.assessment is not None
    assert outcome.assessment.verdict == "new"


async def test_the_drop_gate_is_re_applied_rather_than_the_outcome_replayed() -> None:
    """What is REUSED is the model's assessment; the gate is re-applied every delivery.

    Both directions. A stored `duplicate @ 0.80` drops under a 0.75 bar and does not
    under a 0.90 one, with no model call either way — so retuning a bar takes effect on
    the next delivery instead of being frozen into a stored outcome."""
    audit = InMemoryAuditStore()
    env = make_envelope()
    seed_judge, _c, _a, _i = make_judge(
        [verdict_turn("duplicate", confidence=0.80)],
        audit=audit,
        config=_config(post=0.75),
    )
    assert (await seed_judge.adjudicate_candidate(env, make_summary(), [card()])).drop is True

    strict_judge, strict_client, _a, _i = make_judge(
        [], audit=audit, config=_config(post=0.95)
    )
    strict = await strict_judge.adjudicate_candidate(env, make_summary(), [card()])

    assert strict_client.calls_made == 0
    assert strict.drop is False
    assert audit.judgements[0].outcome == "proceeded"     # the row follows the new bar


def _config(*, post: float):  # noqa: ANN202
    from data_agent.learning.judge import JudgeConfig

    return JudgeConfig(post_drop_confidence=post)


# ===========================================================================
# 2. The dataset the verdict exists for.
# ===========================================================================


def _record(verdict: str, *, outcome: str, confidence: float, stage="pre_extraction",
            threshold=0.90, similarity=0.85) -> JudgeRecord:  # noqa: ANN001
    # Outside shadow mode `would_drop` and `dropped` are always equal — the judge sets
    # both from the same gate result, so the fixture does too.
    return JudgeRecord(
        would_drop=outcome == "dropped",
        judgement_ref=pre_extraction_ref(f"fp-{verdict}-{confidence}"),
        stage=stage,
        session_id=f"sess-{verdict}",
        content_hash=f"hash-{verdict}",
        trace_id="trace-1",
        assessment=CoverageAssessment(
            verdict=verdict,  # type: ignore[arg-type]
            covered_by="bp::abc",
            covered_by_tier="mcp",
            reason="prose about a real session",
            confidence=confidence,
        ),
        outcome=outcome,  # type: ignore[arg-type]
        threshold=threshold,
        best_similarity=similarity,
        cards_shown=3,
        model="gpt-x-mini",
        judged_at="2026-08-10T00:00:00+00:00",
        covered_by_known=True,
    )


def test_the_row_is_flat_scalars_with_an_exact_discriminator() -> None:
    """`GROUP BY verdict` against a bucket with one GSI is the point of the record, so
    every value has to be a scalar N1QL can group on — no nested assessment object, no
    list, no dict. And the discriminator has to be EXACT rather than merely selective:
    `learning_audit` is one default collection shared with the evidence snapshots, which
    carry no `record_type` at all and are never rewritten."""
    doc = _record("duplicate", outcome="dropped", confidence=0.94).to_doc()

    assert doc["record_type"] == JUDGE_RECORD_TYPE
    for key, value in doc.items():
        assert not isinstance(value, (dict, list, tuple, set)), f"{key} is not a scalar"
    # The four model fields are FLATTENED onto the row, not nested under `assessment`.
    for flattened in ("verdict", "covered_by", "covered_by_tier", "reason", "confidence"):
        assert flattened in doc
    assert "assessment" not in doc


def test_the_row_carries_the_bar_and_the_score_the_verdict_was_taken_against() -> None:
    """Both bars are operator-tunable, so a verdict with no record of the bar it was
    compared against cannot be re-read after a retune — "how many of last quarter's
    drops would still drop at 0.95?" is answerable only if the row says what 0.90 meant
    at the time. `best_similarity` is the retrieval score that decided the judge was
    worth calling, which is what makes the band itself tunable from evidence."""
    doc = _record("duplicate", outcome="dropped", confidence=0.94).to_doc()

    assert doc["threshold"] == 0.90
    assert doc["best_similarity"] == 0.85
    assert doc["confidence"] == 0.94
    assert doc["dropped"] is True          # denormalized so the headline needs no string cmp
    assert doc["outcome"] == "dropped"
    assert doc["model"] == "gpt-x-mini"    # verdicts are pooled across months otherwise
    assert doc["stage"] == "pre_extraction"


def test_the_skew_question_the_whole_slice_exists_for_is_answerable_from_the_rows() -> None:
    """The decision this dataset feeds: a skew to `existing-plus-delta` means the loop
    keeps re-deriving near-misses of what it owns, which is the documented trigger for
    building atomic composable blueprints; a skew to `duplicate` means RECALL is failing;
    a skew to `new` means the corpus is young. Three different projects, so the field has
    to distinguish all three — a `covered: bool` would have collapsed the two that
    matter.

    The grouping is done here in Python over `to_doc()` output, which is exactly what the
    N1QL `GROUP BY verdict` does over the same documents."""
    docs = [
        _record("duplicate", outcome="dropped", confidence=0.94).to_doc(),
        _record("existing-plus-delta", outcome="proceeded", confidence=0.88).to_doc(),
        _record("existing-plus-delta", outcome="proceeded", confidence=0.71).to_doc(),
        _record("new", outcome="proceeded", confidence=0.62).to_doc(),
    ]

    by_verdict: dict[str, int] = {}
    for doc in docs:
        assert doc["record_type"] == JUDGE_RECORD_TYPE
        by_verdict[doc["verdict"]] = by_verdict.get(doc["verdict"], 0) + 1

    assert by_verdict == {"duplicate": 1, "existing-plus-delta": 2, "new": 1}
    assert set(by_verdict) <= set(COVERAGE_VERDICTS)
    # "how many drops, and would they still drop at a tighter bar?"
    drops = [d for d in docs if d["dropped"]]
    assert len(drops) == 1
    assert [d for d in drops if d["confidence"] >= 0.95] == []


def test_every_refusal_reason_for_a_drop_is_recoverable_from_the_row() -> None:
    """A `duplicate` at high confidence that did NOT drop is the row a reader will stare
    at, and the five-fact gate has five ways to produce it. Four are on the row directly
    (`verdict`, `confidence` vs `threshold`, `covered_by_known`, `covered_by_tier`) and
    the fifth — the card's ORIGIN — is the one nobody could infer from the others, which
    is why it is stored rather than reduced to a boolean at the gate."""
    doc = _record("duplicate", outcome="proceeded", confidence=0.99).to_doc()

    for field in ("verdict", "confidence", "threshold", "covered_by_known",
                  "covered_by_tier", "covered_by_origin", "would_drop"):
        assert field in doc, f"{field} missing — a refused drop would be inexplicable"


def test_a_shadow_row_is_the_row_the_real_run_would_have_written() -> None:
    """Shadow mode answers "what WOULD we have thrown away?" BEFORE anything is thrown
    away, which only works if the shadow row is comparable to a real one: same fields,
    same fingerprint rule, differing only in `dropped`/`outcome` and flagged as shadow."""
    real = _record("duplicate", outcome="dropped", confidence=0.94)
    shadow = replace(real, outcome="proceeded", would_drop=True, shadow=True)

    real_doc, shadow_doc = real.to_doc(), shadow.to_doc()
    assert set(real_doc) == set(shadow_doc)
    differing = {k for k in real_doc if real_doc[k] != shadow_doc[k]}
    assert differing == {"outcome", "dropped", "shadow"}
    assert shadow_doc["would_drop"] is True


def test_a_stored_row_round_trips_exactly() -> None:
    """The reuse path reads this doc back and feeds the assessment straight into a drop
    decision, so a lossy round trip would silently re-key or re-bar a stored verdict."""
    original = _record("duplicate", outcome="dropped", confidence=0.94)
    assert JudgeRecord.from_doc(original.to_doc()) == original


def test_a_hand_edited_verdict_is_refused_rather_than_defaulted() -> None:
    """The bucket is one a human can edit with `cbq`, and `verdict` has no safe default:
    coercing an unrecognized value to `new` would put a verdict in the dataset that no
    model ever gave, which is the same fabrication `parse_assessment` refuses on the
    model's side. `None` propagates to `read_judgement` as "no record on file", and the
    judge re-asks — one small model call, which is the affordable direction."""
    hostile = _record("duplicate", outcome="dropped", confidence=0.94).to_doc()
    hostile["verdict"] = "DEFINITELY-A-DUPLICATE"
    assert JudgeRecord.from_doc(hostile) is None

    for bad in (None, 42, ["duplicate"], "", "Duplicate"):
        doc = _record("duplicate", outcome="dropped", confidence=0.94).to_doc()
        doc["verdict"] = bad
        assert JudgeRecord.from_doc(doc) is None, bad


def test_every_other_hand_edited_field_degrades_to_a_harmless_value() -> None:
    """The verdict refuses; everything else normalizes, because this runs inside a queue
    worker and a raise there costs the session. Each coercion has to land somewhere the
    drop gate refuses — a stored `true` confidence must not read as 1.0 (bool is an int
    subclass, so `True >= 0.9` is a perfect false positive)."""
    hostile = _record("duplicate", outcome="dropped", confidence=0.94).to_doc()
    hostile.update(
        {
            "confidence": True,
            "threshold": "0.5",
            "cards_shown": "many",
            "covered_by": ["bp::abc"],
            "covered_by_known": "yes",
            "covered_by_origin": 7,
            "fingerprint": None,
            "stage": "somewhere_else",
            "outcome": "obliterated",
        }
    )
    degraded = JudgeRecord.from_doc(hostile)

    assert degraded is not None
    assert degraded.assessment.verdict in COVERAGE_VERDICTS
    assert degraded.assessment.confidence == 0.0
    assert degraded.threshold == 0.0
    assert degraded.cards_shown == 0
    assert degraded.assessment.covered_by == ""
    assert degraded.covered_by_known is False
    assert degraded.covered_by_origin == ""
    assert degraded.fingerprint == ""      # can never match a real fingerprint
    assert degraded.stage == "pre_extraction"
    assert degraded.outcome == "proceeded"


def test_the_two_key_namespaces_cannot_collide() -> None:
    """One fingerprint rule, two prefixes. Both stages hash `(stage, content_hash,
    brief)`, so even a session and a candidate that somehow rendered identical briefs
    land under different keys — the stage is inside the digest AND in the prefix."""
    pre_fp = judgement_fingerprint("pre_extraction", "hash-1", "brief")
    post_fp = judgement_fingerprint("post_extraction", "hash-1", "brief")

    assert pre_fp != post_fp
    assert pre_extraction_ref(pre_fp).startswith("judgement::pre::")
    assert post_extraction_ref(post_fp).startswith("judgement::post::")
    assert pre_extraction_ref(pre_fp) != post_extraction_ref(post_fp)
    # Length-prefixed parts: ("ab","c") and ("a","bc") must not collide.
    assert judgement_fingerprint("ab", "c") != judgement_fingerprint("a", "bc")


def test_a_stamped_envelope_survives_the_candidate_doc_round_trip() -> None:
    """The verdict on the envelope is part of the same dataset (a human reads it in the
    review inbox), so it has to survive the store — and its ABSENCE has to stay
    distinguishable from a stored `new`, which is a positive statement a model made."""
    assessment = CoverageAssessment(
        verdict="existing-plus-delta",
        covered_by="bp::abc",
        covered_by_tier="mcp",
        reason="adds a second grouping column",
        confidence=0.82,
    )
    stamped = replace(make_envelope(), judge=assessment)
    assert CandidateEnvelope.from_doc(stamped.to_doc()).judge == assessment

    unjudged = make_envelope()
    assert "judge" not in unjudged.to_doc()
    assert CandidateEnvelope.from_doc(unjudged.to_doc()).judge is None
