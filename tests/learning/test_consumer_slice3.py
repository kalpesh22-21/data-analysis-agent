"""S3 consumer integration (Layer 1) — the KEEP extraction path: real evidence
snapshot + ref-only candidate persistence, retry-mismatch → dead-letter vs
decline → done, redelivery idempotency, unconfigured fallback, D72.
Task items 7, 8, 9, 10, 11.
"""

from __future__ import annotations

import copy
import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore, mint_candidate_id
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash

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
    doc = seed_session(store, sid, learning_status=LearningStatus.QUEUED,
                       messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
                       tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                                    args={"sql": "SELECT 1"}, status="ok")])
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    return doc


# --- item 8: evidence ref-only through the consumer (CRITICAL) ---------------


async def test_keep_path_snapshots_evidence_but_persists_refs_only(store, queue, settings,
                                                                    seed_session):
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    raw = blueprint_raw(evidence=[{"turn_ref": 0, "tool_call_ref": "tc1", "quote": _SECRET}])
    extractor = emit_extractor([raw])

    await _enqueue_queued(store, queue, seed_session, "sess-1")
    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=extractor, audit=audit, candidates=candidates,
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


async def test_persistent_mismatch_dead_letters_not_false_done(store, queue, settings,
                                                               clock, seed_session):
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1")
    # 3 malformed turns (max_retries=2) → extract() raises SchemaMismatchError.
    extractor = make_extractor([malformed_turn(), malformed_turn(), malformed_turn()],
                               max_retries=2)
    await _enqueue_queued(store, queue, seed_session, "sess-1")
    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=extractor, audit=audit, candidates=candidates,
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
    audit = InMemoryAuditStore()
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1")
    # A no-evidence candidate → validation DECLINE (no raise) → session done.
    extractor = emit_extractor([blueprint_raw(evidence=[])])
    await _enqueue_queued(store, queue, seed_session, "sess-1")
    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=extractor, audit=audit, candidates=candidates,
    )
    result = await consumer.run_once()

    assert result.done == 1
    assert result.dead_letters == 0
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE
    # A declined candidate is never snapshotted or persisted.
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
            store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
            extractor=extractor, audit=InMemoryAuditStore(), candidates=candidates,
        )
        return await consumer.run_once()

    await run_once_fresh()
    await run_once_fresh()

    # Two extractions, but the content-hash-derived id UPSERTs → ONE candidate.
    assert candidates.put_calls == 2
    assert len(candidates.all_candidates()) == 1
    assert candidates.all_candidates()[0].candidate_id == mint_candidate_id("hash-stable", 0)


# --- MEDIUM-3: supersede — a re-run replaces the prior attempt's set --------


async def test_supersede_drops_orphaned_candidates_from_prior_attempt(store, settings,
                                                                      seed_session):
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
            store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
            extractor=extractor, audit=InMemoryAuditStore(), candidates=candidates,
        )
        await consumer.run_once()

    await run_with(3)   # attempt 1 → ::0, ::1, ::2
    assert len(candidates.all_candidates()) == 3
    await run_with(2)   # attempt 2 → supersede then ::0, ::1

    ids = {c.candidate_id for c in candidates.all_candidates()}
    assert ids == {mint_candidate_id("hash-super", 0), mint_candidate_id("hash-super", 1)}
    assert mint_candidate_id("hash-super", 2) not in ids  # no orphan from attempt 1
    assert await candidates.get(mint_candidate_id("hash-super", 2)) is None
    assert len(await candidates.list_by_status("extracted")) == 2


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
        store, queue, settings, tracer=tracer, summary_loader=_keep_loader(summary),
        triage=_keep_triage, extractor=None, audit=audit, candidates=candidates,
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
        store, "sess-1", learning_status=LearningStatus.QUEUED,
        last_activity="2000-01-01T00:00:00+00:00",
        messages=[make_message(0, "user", "show earnings"),
                  make_message(0, "assistant", "Analytics earned $1.2M")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok",
                                     result_full_ref="result::abc")],
    )
    before_messages = copy.deepcopy(doc.messages)
    before_trail = copy.deepcopy(doc.tool_trail)
    before_last_activity = doc.last_activity
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=InMemoryCandidateStore(),
    )
    await consumer.run_once()

    after = store._docs["sess-1"]
    assert after.learning_status == LearningStatus.DONE      # only the lifecycle flag
    assert after.messages == before_messages
    assert after.tool_trail == before_trail
    assert after.last_activity == before_last_activity        # NOT bumped
    assert after.tool_trail[0].result_full_ref == "result::abc"
