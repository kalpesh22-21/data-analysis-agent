"""contracts-stage-seam-noop-safe (D102, contracts-design §7.1/§9 row 14).

The `CandidateStage` pipeline is ONE injected, defaulted-empty seam. This proves:

  * an EMPTY `stages=()` tuple leaves the S3 behavior behaviorally identical (no
    extra `put`, additive keys only — the persisted envelope is exactly the
    `status=extracted` one) — the stub-fallback invariant that lets tracks branch
    off this base safely;
  * a wired fake stage that fills ONE additive field is honored: its enriched
    envelope is persisted, and the `control` signals (`continue`/`route_inbox`/
    `drop`/`halt`) are respected — an UNKNOWN control raises, never a silent route.

No concrete stage exists yet; the fakes here stand in for S4–S7 to exercise the
seam contract only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import (
    CandidateEnvelope,
    DedupVerdict,
    DriftStamp,
    InMemoryCandidateStore,
)
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.stage import StageContext, StageResult
from data_agent.runtime.session.memory_store import InMemorySessionStore

from .conftest import make_message, make_trail_entry
from .extractor.helpers import KEEP_VERDICT, blueprint_raw, emit_extractor, make_summary


def _keep_loader(summary):
    async def loader(doc, store, *, job):
        return summary
    return loader


def _keep_triage(_summary):
    return KEEP_VERDICT


async def _enqueue_queued(store, queue, seed_session, sid):
    doc = seed_session(
        store, sid, learning_status=LearningStatus.QUEUED,
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok")],
    )
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    return doc


# --- fake stages (stand-ins for S4–S7; contract only) ------------------------


@dataclass(frozen=True)
class _FillDriftStage:
    """A stage that stamps `drift` and returns a control signal."""

    stage_id: str = "drift-faker"
    control: str = "continue"
    seen: list = None  # type: ignore[assignment]

    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult:
        if self.seen is not None:
            self.seen.append((env.candidate_id, ctx.summary.session_id))
        enriched = replace(env, drift=DriftStamp(status="clean", probes=("grain_integrity",)))
        return StageResult(envelope=enriched, control=self.control)


@dataclass(frozen=True)
class _BadControlStage:
    """A stage that returns a control string outside the frozen set (a typo)."""

    stage_id: str = "bad-control-faker"

    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult:
        return StageResult(envelope=env, control="inbox")  # not a valid control


@dataclass(frozen=True)
class _FillDedupStage:
    stage_id: str = "dedup-faker"

    async def process(self, env: CandidateEnvelope, ctx: StageContext) -> StageResult:
        enriched = replace(env, dedup=DedupVerdict(
            canonical_key="sha256:seam", matched_id=None, similarity=0.0,
            action="insert", layer="hard"))
        return StageResult(envelope=enriched)  # control defaults to "continue"


# --- the no-op safety invariant ----------------------------------------------


async def test_empty_stages_tuple_is_behaviorally_identical(store, queue, settings, seed_session):
    """`stages=()` ⇒ exactly one `put` (the extracted envelope), unchanged."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue_queued(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates, stages=(),
    )
    result = await consumer.run_once()

    assert result.done == 1
    assert candidates.put_calls == 1  # NO extra put — behaviorally identical to pre-seam S3
    stored = candidates.all_candidates()[0]
    assert stored.status == "extracted"
    assert stored.dedup is None
    assert stored.drift == DriftStamp()  # unchecked — no stage ran


async def test_omitting_stages_matches_empty_tuple(store, queue, settings, seed_session):
    """Not passing `stages` at all must equal passing `stages=()` — the persisted
    doc is identical, proving the default is the no-op."""
    summary = make_summary(session_id="sess-1", content_hash="hash-1")

    async def run(pass_stages: bool):
        s = InMemorySessionStore()
        q = InMemoryLearningQueue()
        candidates = InMemoryCandidateStore()
        await _enqueue_queued(s, q, seed_session, "sess-1")
        kwargs = dict(
            summary_loader=_keep_loader(summary), triage=_keep_triage,
            extractor=emit_extractor([blueprint_raw()]),
            audit=InMemoryAuditStore(), candidates=candidates,
        )
        if pass_stages:
            kwargs["stages"] = ()
        consumer = LearningConsumer(s, q, settings, **kwargs)
        await consumer.run_once()
        return candidates.all_candidates()[0].to_doc()

    def _normalize(doc: dict) -> dict:
        # `created_at` is a wall-clock default and `evidence_ref` carries a random
        # minted UUID — both non-deterministic across runs. Everything else (incl.
        # the additive dedup/drift defaults) must be identical.
        doc.pop("created_at")
        doc["provenance"]["evidence_ref"] = ["<ref>"]
        return doc

    with_empty = _normalize(await run(pass_stages=True))
    without = _normalize(await run(pass_stages=False))
    assert with_empty == without
    assert with_empty["dedup"] is None
    assert with_empty["drift"] == {"status": "unchecked", "last_drift_check_at": None,
                                   "probes": [], "failed_probe": None}


# --- a wired stage is honored ------------------------------------------------


async def test_single_stage_fills_field_and_is_persisted(store, queue, settings, seed_session):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    seen: list = []
    await _enqueue_queued(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(_FillDriftStage(seen=seen),),
    )
    result = await consumer.run_once()

    assert result.done == 1
    # extracted put + the enriched re-put = 2 puts; the store UPSERTs so 1 doc.
    assert candidates.put_calls == 2
    assert len(candidates.all_candidates()) == 1
    stored = candidates.all_candidates()[0]
    assert stored.drift.status == "clean"
    assert stored.drift.probes == ("grain_integrity",)
    # The stage saw the freshly-extracted envelope + its context.
    assert seen == [(stored.candidate_id, "sess-1")]


async def test_stage_chain_runs_in_order_and_composes_fields(store, queue, settings,
                                                             seed_session):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue_queued(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(_FillDedupStage(), _FillDriftStage()),
    )
    await consumer.run_once()

    stored = candidates.all_candidates()[0]
    # Both stages' fields survive — the pipeline composes, no stage clobbers another.
    assert stored.dedup is not None and stored.dedup.canonical_key == "sha256:seam"
    assert stored.drift.status == "clean"


async def test_control_drop_does_not_persist_enriched(store, queue, settings, seed_session):
    """`control="drop"` (e.g. S8 user-knowledge auto-commit writes elsewhere) ⇒
    the enriched envelope is NOT re-persisted; only the extracted put remains."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue_queued(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(_FillDriftStage(control="drop"),),
    )
    await consumer.run_once()

    assert candidates.put_calls == 1  # dropped — no enriched re-put
    stored = candidates.all_candidates()[0]
    assert stored.drift == DriftStamp()  # the enriched drift was NOT persisted


async def test_control_halt_stops_remaining_candidates(store, queue, settings, seed_session):
    """`control="halt"` stops the pipeline: a second candidate is not processed by
    the halted run (the first is persisted; the halt breaks the outer loop)."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-multi")
    await _enqueue_queued(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw(), blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(_FillDriftStage(control="halt"),),
    )
    await consumer.run_once()

    # Only the first candidate reached the store (extracted put); the halt broke
    # the loop before the second candidate's build/put.
    ids = {c.candidate_id for c in candidates.all_candidates()}
    assert len(ids) == 1


async def test_control_route_inbox_persists_and_next_candidate_still_processed(
    store, queue, settings, seed_session
):
    """`control="route_inbox"` stops THIS candidate's pipeline but persists its
    enriched envelope (inbox-bound); the NEXT candidate is still processed — the
    route is per-candidate, not a batch halt."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-multi")
    seen: list = []
    await _enqueue_queued(store, queue, seed_session, "sess-1")

    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw(), blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(_FillDriftStage(control="route_inbox", seen=seen),),
    )
    await consumer.run_once()

    # BOTH candidates ran through the stage (route is per-candidate, no halt) ...
    assert len(seen) == 2
    stored = {c.candidate_id: c for c in candidates.all_candidates()}
    assert len(stored) == 2
    # ... and BOTH had their enriched (routed) envelope persisted.
    for env in stored.values():
        assert env.drift.status == "clean"


async def test_unknown_control_string_raises_not_silent_route(store, queue, settings,
                                                              seed_session):
    """A typo'd control string is a programming error: `_run_stages` raises rather
    than silently behaving like route_inbox. Exercised directly (the consumer's
    outer dispatch would otherwise swallow it as a transient failure)."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    consumer = LearningConsumer(
        store, queue, settings, summary_loader=_keep_loader(summary), triage=_keep_triage,
        extractor=emit_extractor([blueprint_raw()]),
        audit=InMemoryAuditStore(), candidates=candidates,
        stages=(_BadControlStage(),),
    )
    env = CandidateEnvelope(
        candidate_id="candidate::hash-1::0", type="blueprint", status="extracted",
        payload={"intent": "x"}, source_session="sess-1", source_trace="trace-1",
        evidence_refs=(), extractor_rationale="r",
        entity_scan={"result": "pending"}, confidence=0.9, proposed_action="new",
        depends_on=(), content_hash="hash-1",
    )
    with pytest.raises(ValueError, match="unknown control"):
        await consumer._run_stages(env, summary, KEEP_VERDICT)
