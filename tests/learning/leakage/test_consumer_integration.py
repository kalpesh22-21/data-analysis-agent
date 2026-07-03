"""S5 leakage gate wired into the real consumer pipeline seam (D102 §7.1).

Proves `LeakageGateStage` is a conforming `CandidateStage`: registered in the
consumer's `stages` tuple, it runs over each freshly-extracted envelope and its
settled `LeakageVerdict` is persisted into the candidate holding store.
"""

from __future__ import annotations

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash

from ..conftest import make_message, make_trail_entry
from ..extractor.helpers import KEEP_VERDICT, blueprint_raw, emit_extractor, make_summary


def _keep_loader(summary):
    async def loader(doc, store, *, job):
        return summary

    return loader


async def _enqueue(store, queue, seed_session, sid):
    doc = seed_session(
        store, sid, learning_status=LearningStatus.QUEUED,
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok")],
    )
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))


async def test_leakage_stage_persists_settled_verdict(store, queue, settings, seed_session):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings,
        summary_loader=_keep_loader(summary), triage=lambda _s: KEEP_VERDICT,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(LeakageGateStage(candidate_store=candidates),),
    )
    result = await consumer.run_once()
    assert result.done == 1

    stored = candidates.all_candidates()[0]
    # the gate overwrote S3's pending self-check with a settled verdict
    assert LeakageVerdict.is_settled(stored.entity_scan)
    verdict = LeakageVerdict.from_doc(stored.entity_scan)
    # a clean blueprint intent => pass, and it stays a candidate-track envelope
    assert verdict.result == "pass"
    assert stored.status == "extracted"
