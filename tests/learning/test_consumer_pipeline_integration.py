"""Wave 3a — the factory-built consumer driving the FULL write-router pipeline
(D102 §7.1) end-to-end through `run_once` (Layer 1, all fakes; no docker).

Proves `build_learning_consumer` wires the six stages in the FROZEN order
(generalize → leakage → dedup → schema_edit_pr → user_commit → writer) onto the
consumer seam, shares the candidate/user stores as singletons, and that a KEEP
session flows extract → pipeline → the right terminal `status`, landing enriched:

  * a clean blueprint auto-lands `candidate` (generalization + settled pass scan +
    insert dedup, corpus seeded at hit_count=1);
  * a global_knowledge lands `in_review` (human pre-gate), never auto-retrievable;
  * an entity-leaking global_knowledge is `reject` terminal;
  * a reroute commits the per-user fact into the user store (scoped to the session
    user) and the residual global flows on to `in_review`;
  * a schema_edit gets its `schema_edit_review` PR marker before the writer.

Plus: the S1 invariants still hold with the stages wired (kill-switch halts, CAS
single-writer skip, redelivery dedup_skip, dead-letter after N).
"""

from __future__ import annotations

import pytest

from data_agent.learning import state_machine
from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.dedup import InMemoryBlueprintCorpus
from data_agent.learning.factory import (
    LearningWiringError,
    build_learning_consumer,
    build_promotion_plane,
)
from data_agent.learning.inbox import ParameterizationCompleter
from data_agent.learning.memory_queue import InMemoryLearningQueue
from data_agent.learning.models import LearningJob, LearningStatus, compute_content_hash
from data_agent.learning.user import InMemoryUserKnowledgeStore
from data_agent.runtime.model.scripted_client import ScriptedModelClient

from .conftest import make_message, make_trail_entry
from .extractor.helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    evidence_item,
    make_summary,
    malformed_turn,
    scripted_turn,
)
from .generalize.helpers import CATALOG
from .leakage.helpers import ScriptedSemanticScanner
from .schema_edit.test_pr_bot import ScriptedGitClient

# Frozen order (D102 §7.1); the two target-specific writers use their *_writer ids.
FROZEN_STAGE_IDS = (
    "generalize",
    "leakage",
    "dedup",
    "schema_edit_writer",
    "user_knowledge_writer",
    "writer",
)


# --- builders ----------------------------------------------------------------


def _keep_loader(summary):
    async def loader(doc, store, *, job):
        return summary

    return loader


def _model(raws):
    return ScriptedModelClient([scripted_turn(raws)])


async def _enqueue(store, queue, seed_session, sid):
    doc = seed_session(
        store, sid, learning_status=LearningStatus.QUEUED,
        messages=[make_message(0, "user", "q"), make_message(0, "assistant", "a")],
        tool_trail=[make_trail_entry(turn_index=0, tool_name="runQuery",
                                     args={"sql": "SELECT 1"}, status="ok")],
    )
    await queue.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    return doc


def _consumer(
    store, queue, settings, *,
    model_client,
    candidates,
    corpus=None,
    user_store=None,
    semantic_scanner=None,
    sampler=None,
    git_client=None,
    catalog=CATALOG,
    summary,
):
    return build_learning_consumer(
        settings,
        session_store=store,
        queue=queue,
        model_client=model_client,
        audit_store=InMemoryAuditStore(),
        candidate_store=candidates,
        blueprint_corpus=corpus if corpus is not None else InMemoryBlueprintCorpus(),
        user_store=user_store if user_store is not None else InMemoryUserKnowledgeStore(),
        catalog_schema=catalog,
        git_client=git_client,
        semantic_scanner=semantic_scanner,
        # Never sample so a clean blueprint auto-lands deterministically.
        sampler=sampler if sampler is not None else (lambda _env: False),
        summary_loader=_keep_loader(summary),
        triage=lambda _s: KEEP_VERDICT,
    )


def _global_knowledge_raw(*, statement: str):
    return {
        "type": "global_knowledge",
        "confidence": 0.9,
        "evidence": [evidence_item()],
        "rationale": "a reusable business rule worth learning globally",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "payload": {"statement": statement, "knowledge_type": "business_rule"},
    }


def _schema_edit_raw():
    return {
        "type": "schema_edit",
        "confidence": 0.9,
        "evidence": [evidence_item()],
        "rationale": "the catalog is missing the active_employee rule",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "payload": {
            "edit_type": "add_rule",
            "target": {"database": "payroll"},
            "patch": "rules:\n  - id: active_employee\n    predicate: status = 'ACTIVE'",
            "statement": "define active_employee",
        },
    }


# --- clean blueprint: extract → generalize → leakage(pass) → dedup(insert) → land


async def test_clean_blueprint_flows_end_to_end_and_lands_enriched(
    store, queue, settings, seed_session
):
    candidates = InMemoryCandidateStore()
    corpus = InMemoryBlueprintCorpus()
    user_store = InMemoryUserKnowledgeStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue(store, queue, seed_session, "sess-1")

    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, corpus=corpus, user_store=user_store, summary=summary,
    )
    result = await consumer.run_once()
    assert result.done == 1

    stored = candidates.all_candidates()[0]
    # Auto-landed as a retrievable-after-S9 candidate.
    assert stored.status == "candidate"
    # S4 generalization merged, static validation ok (else it would route to review).
    gen = stored.payload["generalization"]
    assert gen["static_validation"]["outcome"] == "ok"
    # S5 settled the preliminary `pending` self-check into an authoritative pass.
    assert LeakageVerdict.is_settled(stored.entity_scan)
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "pass"
    # S6 adjudicated a genuinely-new insert and SEEDED the corpus at hit_count=1.
    assert stored.dedup is not None
    assert stored.dedup.action == "insert"
    assert corpus.seed_calls == [stored.dedup.canonical_key]
    assert corpus.get_sync(stored.dedup.canonical_key).hit_count == 1
    # A clean blueprint never reroutes a user fact.
    assert user_store.all_records() == []


# --- global_knowledge → in_review (human pre-gate) ---------------------------


async def test_global_knowledge_routes_to_in_review_never_auto_retrievable(
    store, queue, settings, seed_session
):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue(store, queue, seed_session, "sess-1")

    consumer = _consumer(
        store, queue, settings,
        model_client=_model([_global_knowledge_raw(statement="The fiscal year starts in April.")]),
        candidates=candidates, summary=summary,
    )
    assert (await consumer.run_once()).done == 1

    stored = candidates.all_candidates()[0]
    assert stored.type == "global_knowledge"
    assert stored.status == "in_review"        # human pre-gate (D58a)
    assert stored.status != "candidate"        # never auto-retrievable
    assert stored.status != "validated"
    # The gate settled a clean pass on the entity-free statement.
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "pass"


# --- entity-leaking global_knowledge → reject terminal -----------------------


async def test_entity_leaking_global_knowledge_is_reject_terminal(
    store, queue, settings, seed_session
):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue(store, queue, seed_session, "sess-1")

    # E12345 trips the employee_code detector; global_knowledge is a HARD-reject type.
    consumer = _consumer(
        store, queue, settings,
        model_client=_model([_global_knowledge_raw(statement="Employee E12345 is exempt.")]),
        candidates=candidates, summary=summary,
    )
    assert (await consumer.run_once()).done == 1

    stored = candidates.all_candidates()[0]
    assert stored.status == "rejected"          # terminal (D58: hard entity in entity-free target)
    verdict = LeakageVerdict.from_doc(stored.entity_scan)
    assert verdict.result == "reject"
    assert verdict.hits  # the leaking entity was recorded on the settled verdict


# --- reroute → per-user fact lands scoped to session user; residual flows on --


async def test_reroute_commits_user_fact_scoped_and_residual_flows_on(
    store, queue, settings, seed_session
):
    candidates = InMemoryCandidateStore()
    user_store = InMemoryUserKnowledgeStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1", user_id="user-42")
    await _enqueue(store, queue, seed_session, "sess-1")

    # The semantic scanner classifies the entity as a legitimate per-user fact.
    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, user_store=user_store,
        semantic_scanner=ScriptedSemanticScanner(classification="user_fact"),
        summary=summary,
    )
    assert (await consumer.run_once()).done == 1

    # The per-user fact landed, SCOPED to the session's authenticated user (never a
    # payload-supplied id) — R6/D17.
    records = user_store.all_records()
    assert len(records) == 1
    assert records[0].user_id == "user-42"
    assert records[0].record_id.startswith("userknow::user-42::")

    # The residual global candidate flowed on and the writer routed the near-miss.
    stored = candidates.all_candidates()[0]
    assert stored.status == "in_review"
    assert LeakageVerdict.from_doc(stored.entity_scan).result == "reroute"


# --- schema_edit gets its PR marker BEFORE the writer ------------------------


async def test_schema_edit_gets_pr_marker_before_writer(store, queue, settings, seed_session):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    git = ScriptedGitClient()
    await _enqueue(store, queue, seed_session, "sess-1")

    consumer = _consumer(
        store, queue, settings, model_client=_model([_schema_edit_raw()]),
        candidates=candidates, git_client=git, summary=summary,
    )
    assert (await consumer.run_once()).done == 1

    stored = candidates.all_candidates()[0]
    assert stored.type == "schema_edit"
    assert stored.status == "in_review"                 # human MERGE is the gate (D18)
    review = stored.payload["schema_edit_review"]        # the PR-stage marker
    assert review["pr_opened"] is True
    assert len(git.specs) == 1                           # the injected git client ran, once


# --- frozen stage order (independent guard, Nit 2) ---------------------------


def test_stages_wired_in_frozen_order(store, queue, settings):
    """The composition root builds the six stages in the D102 frozen order, with the
    two target-specific writers BEFORE the terminal writer."""
    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=InMemoryCandidateStore(),
        summary=make_summary(session_id="s", content_hash="h"),
    )
    assert tuple(s.stage_id for s in consumer._stages) == FROZEN_STAGE_IDS


# --- config gating: all-or-nothing, never a partial pipeline -----------------


def test_no_model_client_builds_stub_with_empty_pipeline(store, queue, settings):
    consumer = build_learning_consumer(
        settings, session_store=store, queue=queue, model_client=None,
    )
    assert consumer._stages == ()      # no stage wired
    assert consumer._extractor is None  # S2 would-extract stub fallback


def test_configured_extraction_missing_collaborator_fails_fast(store, queue, settings):
    with pytest.raises(LearningWiringError) as exc:
        build_learning_consumer(
            settings, session_store=store, queue=queue,
            model_client=_model([blueprint_raw()]),
            audit_store=InMemoryAuditStore(),
            candidate_store=InMemoryCandidateStore(),
            # blueprint_corpus / user_store / catalog_schema deliberately omitted.
        )
    # The error names every missing piece (never a silent partial pipeline).
    msg = str(exc.value)
    assert "blueprint_corpus" in msg
    assert "user_store" in msg
    assert "catalog_schema" in msg


def test_shared_singleton_candidate_store_across_gate_and_consumer(store, queue, settings):
    candidates = InMemoryCandidateStore()
    user_store = InMemoryUserKnowledgeStore()
    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, user_store=user_store,
        summary=make_summary(session_id="s", content_hash="h"),
    )
    # The consumer's candidates and the leakage gate's candidate_store are the SAME
    # instance (a split-brain store would strand candidates).
    leakage = consumer._stages[1]
    assert consumer._candidates is candidates
    assert leakage.candidate_store is candidates
    # The reroute path and the S8 auto-commit share one user store.
    user_commit = consumer._stages[4]
    assert leakage.user_store is user_store
    assert user_commit.store is user_store


# --- the inbox↔scheduler linkage (writer routes to in_review → human approve) -


class _NoOpProbe:
    async def run(self, sql, *, grain_columns, column_scope=()):
        from data_agent.learning.promotion.models import ProbeResult
        return ProbeResult(row_count=0, distinct_grain_count=None, columns=())


class _ZeroHits:
    async def hit_count(self, canonical_key):
        return 0


async def test_promotion_plane_wired_to_scheduler_runs_guarded_approve(
    store, queue, settings, seed_session
):
    """A global_knowledge lands in_review; approving it through the factory-wired
    inbox delegates to the injected scheduler's single guarded path → validated."""
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue(store, queue, seed_session, "sess-1")
    consumer = _consumer(
        store, queue, settings,
        model_client=_model([_global_knowledge_raw(statement="Q4 ends December 31.")]),
        candidates=candidates, summary=summary,
    )
    await consumer.run_once()

    scheduler, inbox = build_promotion_plane(
        settings, candidate_store=candidates, probe=_NoOpProbe(), hit_counts=_ZeroHits(),
    )

    items = await inbox.list()
    assert len(items) == 1
    approved = await inbox.approve(items[0].candidate_id)
    # A non-replayable (knowledge) target approves directly through the guarded path.
    assert approved.status == "validated"


def test_promotion_plane_pins_one_candidate_store(settings):
    """The scheduler + inbox built by `build_promotion_plane` share the SAME store
    instance (the inbox reads it; the scheduler CAS-writes it on approve)."""
    candidates = InMemoryCandidateStore()
    scheduler, inbox = build_promotion_plane(
        settings, candidate_store=candidates, probe=_NoOpProbe(), hit_counts=_ZeroHits(),
    )
    assert scheduler.store is candidates
    assert inbox._store is candidates


def test_a_completer_on_another_store_is_refused_fail_fast(settings):
    """The plane takes the store ONCE, so an inbox/scheduler split cannot be expressed —
    the one split still expressible is a completer holding a different store, and it is
    refused fail-fast (a stale-envelope split-brain: the completer would re-validate the
    envelope the inbox read and write the result where nothing lists it)."""
    with pytest.raises(LearningWiringError, match="ONE candidate store"):
        build_promotion_plane(
            settings,
            candidate_store=InMemoryCandidateStore(),
            probe=_NoOpProbe(),
            hit_counts=_ZeroHits(),
            completer=ParameterizationCompleter(store=InMemoryCandidateStore()),
        )


# --- S1 invariants still hold with the stages wired --------------------------


async def test_kill_switch_halts_no_stage_runs(store, queue, settings, seed_session, monkeypatch):
    candidates = InMemoryCandidateStore()
    doc = await _enqueue(store, queue, seed_session, "sess-1")
    monkeypatch.setenv("LEARNING_ENABLED", "false")

    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, summary=make_summary(session_id="sess-1"),
    )
    result = await consumer.run_once()

    assert result.disabled is True
    assert candidates.all_candidates() == []            # no stage ran, nothing persisted
    assert queue.new_count() == 1                        # work waits in the stream
    assert store._docs["sess-1"].learning_status == LearningStatus.QUEUED
    assert doc is store._docs["sess-1"]


async def test_cas_single_writer_skips_when_peer_took_queued(
    store, queue, settings, seed_session
):
    candidates = InMemoryCandidateStore()
    await _enqueue(store, queue, seed_session, "sess-1")
    # A peer claims processing between the consumer's read and its own CAS.
    _, cas = await store.get_session_with_cas("sess-1")
    await state_machine.transition(
        store, "sess-1", LearningStatus.QUEUED, LearningStatus.PROCESSING, cas
    )

    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, summary=make_summary(session_id="sess-1"),
    )
    result = await consumer.run_once()

    assert result.done == 0
    assert result.skipped == 1
    assert candidates.all_candidates() == []            # the extractor/pipeline never ran
    assert queue.pending_count() == 1                   # left for the owner / reclaim


async def test_redelivery_of_done_session_is_dedup_skip(store, queue, settings, seed_session):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    doc = await _enqueue(store, queue, seed_session, "sess-1")

    consumer = _consumer(
        store, queue, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, summary=summary,
    )
    first = await consumer.run_once()
    assert first.done == 1
    assert store._docs["sess-1"].learning_status == LearningStatus.DONE

    # A genuine redelivery (a fresh stream, as a reclaim/replay would produce) of the
    # same (now `done`) job with the SAME content hash → dedup_skip + ack, no re-work.
    queue2 = InMemoryLearningQueue()
    await queue2.enqueue(LearningJob.from_doc(doc, content_hash=compute_content_hash(doc)))
    consumer2 = _consumer(
        store, queue2, settings, model_client=_model([blueprint_raw()]),
        candidates=candidates, summary=summary,
    )
    second = await consumer2.run_once()
    assert second.dedup_skips == 1
    assert second.done == 0


async def test_dead_letter_after_n_when_extraction_persistently_fails(
    store, queue, settings, clock, seed_session
):
    candidates = InMemoryCandidateStore()
    summary = make_summary(session_id="sess-1", content_hash="hash-1")
    await _enqueue(store, queue, seed_session, "sess-1")
    # 3 malformed turns (max_retries=2) → the extractor raises → the message is
    # never acked → reclaimed until it crosses N=5 → dead-letter.
    model_client = ScriptedModelClient([malformed_turn(), malformed_turn(), malformed_turn()])
    consumer = _consumer(
        store, queue, settings, model_client=model_client, candidates=candidates,
        summary=summary,
    )

    outcome = None
    for _ in range(10):
        outcome = await consumer.run_once()
        clock.advance(10)
        if store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER:
            break

    assert store._docs["sess-1"].learning_status == LearningStatus.DEAD_LETTER
    assert outcome.dead_letters == 1
    assert candidates.all_candidates() == []            # nothing persisted on a failed extraction
