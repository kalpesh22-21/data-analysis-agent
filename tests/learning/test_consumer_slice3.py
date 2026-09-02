"""S3 consumer integration (Layer 1) — the KEEP extraction path: real evidence
snapshot + ref-only candidate persistence, retry-mismatch → dead-letter vs
decline → done, redelivery idempotency, unconfigured fallback, D72.
Task items 7, 8, 9, 10, 11.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore, mint_candidate_id
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.stage import StageResult

from .conftest import make_message, make_trail_entry
from .extractor.helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    emit_extractor,
    make_extractor,
    make_summary,
    malformed_turn,
)

_SECRET = "SSN-424-11-9090-Jane-Doe"


def _keep_loader(summary):
    async def loader(doc, store, *, job):
        return summary

    return loader


def _keep_triage(_summary):
    return KEEP_VERDICT


async def _enqueue_queued(store, queue, seed_session, sid, summary_hash="hash-1"):
    doc = seed_session(
        store,
        sid,
        learning_status=LearningStatus.QUEUED,
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
        tool_trail=[
            make_trail_entry(
                turn_index=0, tool_name="runQuery", args={"sql": "SELECT 1"}, status="ok"
            )
        ],
    )
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    return doc


# --- item 8: evidence ref-only through the consumer (CRITICAL) ---------------


async def test_keep_path_snapshots_evidence_but_persists_refs_only(
    store, queue, settings, seed_session
):
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    raw = blueprint_raw(evidence=[{"turn_ref": 0, "tool_call_ref": "tc1", "quote": _SECRET}])
    extractor = emit_extractor([raw])

    await _enqueue_queued(store, queue, seed_session, "sess-1")
    consumer = LearningConsumer(
        store,
        queue,
        settings,
        summary_loader=_keep_loader(summary),
        triage=_keep_triage,
        extractor=extractor,
        audit=audit,
        candidates=candidates,
    )
    result = await consumer.run_once()

    assert result.done == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    # The secret quote is snapshotted to learning_audit ...
    assert audit.snapshot_calls == 1
    audit_quotes = [s.quote for s in audit._snapshots.values()]
    assert _SECRET in audit_quotes
    # ... but appears NOWHERE in the persisted candidate (only evidence_refs).
    assert candidates.put_calls == 1
    envelope = candidates.all_candidates()[0]
    blob = json.dumps(envelope.to_doc())
    assert _SECRET not in blob
    # The candidate carries the minted ref, which IS an audit key.
    assert envelope.evidence_refs
    assert set(envelope.evidence_refs) == set(audit._snapshots.keys())


# --- item 7: retry-on-mismatch → dead-letter; decline → done ----------------


async def test_persistent_mismatch_dead_letters_not_false_done(
    store, queue, settings, clock, seed_session
):
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1")
    # 3 malformed turns (max_retries=2) → extract() raises SchemaMismatchError.
    extractor = make_extractor(
        [malformed_turn(), malformed_turn(), malformed_turn()], max_retries=2
    )
    await _enqueue_queued(store, queue, seed_session, "sess-1")
    consumer = LearningConsumer(
        store,
        queue,
        settings,
        summary_loader=_keep_loader(summary),
        triage=_keep_triage,
        extractor=extractor,
        audit=audit,
        candidates=candidates,
    )

    outcome = None
    for _ in range(10):
        outcome = await consumer.run_once()
        clock.advance(10)
        if store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER:
            break

    # NO false done — a raised extraction is a processing failure → dead-letter.
    assert store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER
    assert store._docs["sess-1"].learning_status != LearningStatus.DONE
    assert outcome.dead_letters == 1
    assert candidates.put_calls == 0  # nothing persisted on a failed extraction


async def test_decline_completes_done_not_dead_letter(store, queue, settings, seed_session):
    """A MERIT-FAILED decline writes nothing, and the session still completes.

    `no_evidence` is the D31 primary guard: the candidate cited nothing, so there is no
    artifact to review and nothing a human could complete. This is the behaviour the
    fail-to-review slice deliberately left alone — only a merit-PASSED decline whose
    parameterization form could not be filled in becomes a durable review item
    (`test_consumer_fail_to_review.py`), and it takes a `proceeded` judge verdict to get
    there. Neither holds here: no judge screened this session at all."""
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1")
    # A no-evidence candidate → validation DECLINE (no raise) → session done.
    extractor = emit_extractor([blueprint_raw(evidence=[])])
    await _enqueue_queued(store, queue, seed_session, "sess-1")
    consumer = LearningConsumer(
        store,
        queue,
        settings,
        summary_loader=_keep_loader(summary),
        triage=_keep_triage,
        extractor=extractor,
        audit=audit,
        candidates=candidates,
    )
    result = await consumer.run_once()

    assert result.done == 1
    assert result.dead_letters == 0
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    # A merit-failed declined candidate is never snapshotted or persisted.
    assert audit.snapshot_calls == 0
    assert candidates.put_calls == 0


# --- item 9: redelivery idempotency -----------------------------------------


async def test_reprocessing_same_session_upserts_same_candidate_id(store, settings, seed_session):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-stable")

    async def run_once_fresh():
        # Fresh queue each pass (a real redelivery); shared candidate store.
        queue = InMemoryLearningQueue()
        doc = store._docs.get("sess-1")
        if doc is None:
            await _enqueue_queued(store, queue, seed_session, "sess-1")
            doc = store._docs["sess-1"]
        else:
            # Simulate a crash-before-done reprocess: reset to queued.
            doc.learning_status = LearningStatus.QUEUED
            doc.learning_content_hash = None
            store._bump_version("sess-1")
            await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
        extractor = emit_extractor([blueprint_raw()])
        consumer = LearningConsumer(
            store,
            queue,
            settings,
            summary_loader=_keep_loader(summary),
            triage=_keep_triage,
            extractor=extractor,
            audit=InMemoryAuditStore(),
            candidates=candidates,
        )
        return await consumer.run_once()

    await run_once_fresh()
    await run_once_fresh()

    # Two extractions, but the content-hash-derived id UPSERTs → ONE candidate.
    assert candidates.put_calls == 2
    assert len(candidates.all_candidates()) == 1
    assert candidates.all_candidates()[0].candidate_id == mint_candidate_id("hash-stable", 0)


# --- MEDIUM-3: supersede — a re-run replaces the prior attempt's set --------


async def test_supersede_drops_orphaned_candidates_from_prior_attempt(
    store, settings, seed_session
):
    """Re-processing the same session where the model emits a DIFFERENT count on
    the 2nd attempt (3 → 2) must leave `learning_candidates` holding ONLY the 2nd
    attempt's set — the orphaned `::2` from attempt 1 is superseded, not left."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-super")

    async def run_with(candidate_count: int):
        queue = InMemoryLearningQueue()
        doc = store._docs.get("sess-1")
        if doc is None:
            await _enqueue_queued(store, queue, seed_session, "sess-1")
            doc = store._docs["sess-1"]
        else:
            doc.learning_status = LearningStatus.QUEUED
            doc.learning_content_hash = None
            store._bump_version("sess-1")
            await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
        extractor = emit_extractor([blueprint_raw() for _ in range(candidate_count)])
        consumer = LearningConsumer(
            store,
            queue,
            settings,
            summary_loader=_keep_loader(summary),
            triage=_keep_triage,
            extractor=extractor,
            audit=InMemoryAuditStore(),
            candidates=candidates,
        )
        await consumer.run_once()

    await run_with(3)  # attempt 1 → ::0, ::1, ::2
    assert len(candidates.all_candidates()) == 3
    await run_with(2)  # attempt 2 → supersede then ::0, ::1

    ids = {c.candidate_id for c in candidates.all_candidates()}
    assert ids == {mint_candidate_id("hash-super", 0), mint_candidate_id("hash-super", 1)}
    assert mint_candidate_id("hash-super", 2) not in ids  # no orphan from attempt 1
    assert await candidates.get(mint_candidate_id("hash-super", 2)) is None
    assert len(await candidates.list_by_status("extracted")) == 2


async def test_failed_replacement_does_not_erase_the_last_good_generation() -> None:
    """Replacement is publish-then-supersede: an audit outage cannot turn retry into loss."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-atomic")
    good = LearningConsumer(
        object(),
        InMemoryLearningQueue(),
        LearningSettings(_env_file=None),
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(),
        candidates=candidates,
    )
    await good._run_extractor(summary, KEEP_VERDICT)
    before = candidates.all_candidates()[0]

    class FailingAudit(InMemoryAuditStore):
        async def snapshot(self, ref, snapshot):
            raise RuntimeError("audit unavailable")

    failing = LearningConsumer(
        object(),
        InMemoryLearningQueue(),
        LearningSettings(_env_file=None),
        extractor=emit_extractor([blueprint_raw()]),
        audit=FailingAudit(),
        candidates=candidates,
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await failing._run_extractor(summary, KEEP_VERDICT)

    assert candidates.all_candidates() == [before]


# --- the KEEPER LIST: exactly which rows a re-run is allowed to erase -------
#
# `_run_extractor` publishes the replacement generation, then calls
# `supersede(content_hash, keep_candidate_ids=...)` with every id this run actually PUT.
# The tests above prove the two easy ends of that: a clean re-run replaces the set, and a
# run that writes NOTHING loses nothing. What follows pins the MIDDLE — the partial runs
# the code comment admits to — because each of them is a state a retry can leave in the
# store, and "which generation is this row from" is not otherwise recoverable.


def _generation(tag: str, count: int = 3) -> list:
    """*count* distinguishable candidates. The intent is the only thing that says which
    RUN a stored row came from, and every assertion below is about exactly that."""
    return [blueprint_raw(intent=f"{tag} candidate {i}") for i in range(count)]


def _intents_by_id(candidates: InMemoryCandidateStore) -> dict[str, str]:
    return {c.candidate_id: c.payload["intent"] for c in candidates.all_candidates()}


def _run_extractor_consumer(candidates, raws, *, audit=None, stages=()):
    """A consumer wired for a DIRECT `_run_extractor` call — no queue, no session store.

    The session store is `object()` on purpose: `_run_extractor` must not touch it (D72),
    and an attribute access would fail loudly rather than silently mutating a fake.
    """
    return LearningConsumer(
        object(),
        InMemoryLearningQueue(),
        LearningSettings(_env_file=None),
        extractor=emit_extractor(raws),
        audit=audit if audit is not None else InMemoryAuditStore(),
        candidates=candidates,
        stages=stages,
    )


@dataclass
class _ControlAtOrdinal:
    """A stage that returns *control* for the *at*-th candidate it sees and continues on
    the rest — the per-candidate control the whole-run fakes in `test_consumer_stage_seam`
    cannot express."""

    control: str = "continue"
    at: int = 0
    stage_id: str = "control-at-ordinal"
    seen: list = field(default_factory=list)

    async def process(self, env, ctx):
        self.seen.append(env.candidate_id)
        enriched = replace(env, drift=DriftStamp(status="clean", probes=("grain_integrity",)))
        return StageResult(enriched, self.control if len(self.seen) - 1 == self.at else "continue")


async def test_a_failure_on_the_second_candidate_leaves_a_deliberately_mixed_generation() -> None:
    """THE STATE THE COMMENT ADMITS TO, pinned as an exact candidate set rather than as a
    reassuring inequality.

    `test_failed_replacement_does_not_erase_the_last_good_generation` raises inside
    `_snapshot_evidence` for the FIRST candidate, so nothing is ever `put` and all it can
    prove is "nothing written ⇒ nothing lost". The interesting failure is the one that
    happens PART-WAY: candidate 0 has been published, candidate 1 dies in the audit store,
    and the exception unwinds before `supersede` runs at all.

    What survives is a MIXED generation — the new `::0` beside the previous run's `::1` and
    `::2` — and that is the DESIGNED outcome of publish-then-supersede, not a bug: the
    alternative (delete first) leaves the session with neither generation. It is pinned
    here so that a future "tidy up the retry path" cannot quietly turn it into either of
    the two states this ordering was chosen to avoid — an all-new generation missing its
    middle, or an all-old one that lost the replacement.
    """
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-mixed")

    await _run_extractor_consumer(candidates, _generation("gen-1"))._run_extractor(
        summary, KEEP_VERDICT
    )
    assert _intents_by_id(candidates) == {
        mint_candidate_id("hash-mixed", 0): "gen-1 candidate 0",
        mint_candidate_id("hash-mixed", 1): "gen-1 candidate 1",
        mint_candidate_id("hash-mixed", 2): "gen-1 candidate 2",
    }

    class _FailsOnTheSecondSnapshot(InMemoryAuditStore):
        """One evidence quote per candidate, so the second snapshot IS candidate ::1."""

        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def snapshot(self, ref, snapshot):
            self.attempts += 1
            if self.attempts == 2:
                raise RuntimeError("audit unavailable")
            return await super().snapshot(ref, snapshot)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _run_extractor_consumer(
            candidates, _generation("gen-2"), audit=_FailsOnTheSecondSnapshot()
        )._run_extractor(summary, KEEP_VERDICT)

    # EXACTLY three rows, and exactly this mix: the replacement that landed, and the two
    # prior-generation rows the aborted run never got far enough to supersede.
    assert _intents_by_id(candidates) == {
        mint_candidate_id("hash-mixed", 0): "gen-2 candidate 0",  # published before the fault
        mint_candidate_id("hash-mixed", 1): "gen-1 candidate 1",  # never reached ⇒ kept
        mint_candidate_id("hash-mixed", 2): "gen-1 candidate 2",  # never reached ⇒ kept
    }


async def test_a_halt_on_the_first_candidate_supersedes_the_rest_of_the_prior_generation() -> None:
    """A `halt` DOES cost the prior generation's tail, and the comment says so — so it is
    pinned, because it is the one keeper-list outcome that deletes rows with no successor.

    `halt` breaks the candidate loop, so candidates 1 and 2 of this run are never built and
    never `put`; their ids are therefore absent from the keeper list, and the trailing
    `supersede` removes the PREVIOUS run's `::1` and `::2` even though nothing replaced
    them. That is deliberate (a halt means "stop processing this session", not "roll back")
    and it is exactly the kind of behaviour that drifts silently: a future change that
    started passing the un-run ordinals as keepers, or that moved `supersede` before the
    loop, would leave no test failing unless the surviving SET is asserted.
    """
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-halt")

    await _run_extractor_consumer(candidates, _generation("gen-1"))._run_extractor(
        summary, KEEP_VERDICT
    )
    assert len(candidates.all_candidates()) == 3

    halting = _ControlAtOrdinal(control="halt", at=0)
    await _run_extractor_consumer(
        candidates, _generation("gen-2"), stages=(halting,)
    )._run_extractor(summary, KEEP_VERDICT)

    # The halt stopped the loop after the first candidate ...
    assert halting.seen == [mint_candidate_id("hash-halt", 0)]
    # ... and the store now holds ONLY it: `::1` and `::2` of the prior generation were
    # superseded with nothing put in their place.
    assert _intents_by_id(candidates) == {
        mint_candidate_id("hash-halt", 0): "gen-2 candidate 0",
    }
    # The halted candidate keeps its ENRICHED envelope (halt persists, unlike drop).
    survivor = candidates.all_candidates()[0]
    assert survivor.status == "extracted"
    assert survivor.drift.status == "clean"


async def test_a_dropped_candidate_keeps_its_extracted_row_while_orphans_are_swept() -> None:
    """THE REGRESSION THE KEEPER LIST WAS MOVED FOR, in the shape that mixes it with a
    genuine orphan sweep.

    A stage's `drop` means only "do not persist the ENRICHED envelope" — the `extracted`
    row was already written a few lines earlier and is meant to survive (`user/commit_stage`
    and the dedup layer-3b judge are the production stages that return it). Deriving the
    keeper set from the stage CONTROL instead made `supersede` delete a row this very run
    had just written.

    This run also emits FEWER candidates than the last (2 after 3), so the sweep it
    performs is real: `::2` is a true orphan with no successor and must go, while the
    dropped `::1` must stay. Both halves are asserted together because the failure mode is
    that one of them is derived from the other.
    """
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-drop")

    await _run_extractor_consumer(candidates, _generation("gen-1"))._run_extractor(
        summary, KEEP_VERDICT
    )
    assert len(candidates.all_candidates()) == 3

    dropping = _ControlAtOrdinal(control="drop", at=1)
    await _run_extractor_consumer(
        candidates, _generation("gen-2", 2), stages=(dropping,)
    )._run_extractor(summary, KEEP_VERDICT)

    assert _intents_by_id(candidates) == {
        mint_candidate_id("hash-drop", 0): "gen-2 candidate 0",
        mint_candidate_id("hash-drop", 1): "gen-2 candidate 1",  # dropped, NOT superseded
    }
    # The dropped candidate is present at `extracted` with the stage's enrichment NOT
    # persisted — the exact combination that says "the row survived, the drop was honoured".
    dropped = await candidates.get(mint_candidate_id("hash-drop", 1))
    assert dropped.status == "extracted"
    assert dropped.drift == DriftStamp()
    # ... while its sibling, which continued, kept the enriched envelope.
    kept = await candidates.get(mint_candidate_id("hash-drop", 0))
    assert kept.drift.status == "clean"
    # And the genuine orphan from the longer prior run is gone.
    assert await candidates.get(mint_candidate_id("hash-drop", 2)) is None


# --- item 10: unconfigured fallback → S2 would_extract stub -----------------


async def test_unconfigured_extractor_falls_back_to_stub(store, queue, settings, seed_session):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")

    candidates = InMemoryCandidateStore()
    audit = InMemoryAuditStore()
    summary = make_summary(session_id="sess-1")
    await _enqueue_queued(store, queue, seed_session, "sess-1")
    # NO extractor wired → KEEP falls back to the S2 would_extract stub.
    consumer = LearningConsumer(
        store,
        queue,
        settings,
        tracer=tracer,
        summary_loader=_keep_loader(summary),
        triage=_keep_triage,
        extractor=None,
        audit=audit,
        candidates=candidates,
    )
    result = await consumer.run_once()

    assert result.done == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    # Stub wrote nothing (S2 behavior): no candidate, no evidence snapshot.
    assert candidates.put_calls == 0
    assert audit.snapshot_calls == 0
    names = [s.name for s in exporter.get_finished_spans()]
    assert "learning.extract" in names  # the would_extract stub span


# --- item 11: D72 read-only under the KEEP extraction path ------------------


async def test_keep_extraction_does_not_mutate_session(store, queue, settings, seed_session):
    summary = make_summary(session_id="sess-1")
    doc = seed_session(
        store,
        "sess-1",
        learning_status=LearningStatus.QUEUED,
        last_activity="2000-01-01T00:00:00+00:00",
        messages=[
            make_message(0, "user", "show earnings"),
            make_message(0, "assistant", "Analytics earned $1.2M"),
        ],
        tool_trail=[
            make_trail_entry(
                turn_index=0,
                tool_name="runQuery",
                args={"sql": "SELECT 1"},
                status="ok",
                result_full_ref="result::abc",
            )
        ],
    )
    before_messages = copy.deepcopy(doc.messages)
    before_trail = copy.deepcopy(doc.tool_trail)
    before_last_activity = doc.last_activity
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))

    consumer = LearningConsumer(
        store,
        queue,
        settings,
        summary_loader=_keep_loader(summary),
        triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(),
        candidates=InMemoryCandidateStore(),
    )
    await consumer.run_once()

    after = store._docs["sess-1"]
    assert after.learning_status == LearningStatus.DONE  # only the lifecycle flag
    assert after.messages == before_messages
    assert after.tool_trail == before_trail
    assert after.last_activity == before_last_activity  # NOT bumped
    assert after.tool_trail[0].result_full_ref == "result::abc"
