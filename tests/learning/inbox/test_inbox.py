"""S7 inbox — the in_review projection + human transitions (Slice 7, Contract D).

Slug:
  * S7-reject-is-negative-signal — reject ⇒ status=rejected (a NEGATIVE signal),
    NOT a delete: the row is retained in the store for the S9 learner (D29).

Also covers the in_review projection, reason labelling, approve→validated with
entity-span stripping (D17), retract→retired, and transition guards. Built against
`envelopes_each_reason.json`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.inbox import InboxTransitionError, ReviewInbox
from data_agent.learning.inbox.models import InboxItem

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _load() -> dict:
    return json.loads((FIXTURES / "envelopes_each_reason.json").read_text())


async def _store_with_all_reasons() -> InMemoryCandidateStore:
    store = InMemoryCandidateStore()
    for doc in _load().values():
        await store.put(CandidateEnvelope.from_doc(doc))  # all fixtures are in_review
    return store


# --- the in_review projection -------------------------------------------------


async def test_inbox_lists_all_in_review_candidates_with_reasons():
    store = await _store_with_all_reasons()
    inbox = ReviewInbox(store)
    items = await inbox.list()
    assert len(items) == 6  # one per reason
    by_id = {it.candidate_id: it for it in items}
    reasons = {it.candidate_id: it.reason for it in items}
    # Each fixture's reason is re-derived correctly from its envelope.
    assert reasons["candidate::reason-knowledge_pre_gate::0"] == "knowledge_pre_gate"
    assert reasons["candidate::reason-schema_edit::0"] == "schema_edit"
    assert reasons["candidate::reason-leakage_near_miss::0"] == "leakage_near_miss"
    assert reasons["candidate::reason-blueprint_sampled::0"] == "blueprint_sampled"
    assert reasons["candidate::reason-dedup_conflict::0"] == "dedup_conflict"
    assert reasons["candidate::reason-fail_to_review::0"] == "fail_to_review"
    # The dedup_conflict item carries its collision verdict for the reviewer.
    conflict = by_id["candidate::reason-dedup_conflict::0"]
    assert conflict.dedup is not None and conflict.dedup.action == "conflict"


async def test_inbox_item_summary_is_entity_free_one_liner():
    store = await _store_with_all_reasons()
    items = {it.candidate_id: it for it in await ReviewInbox(store).list()}
    kn = items["candidate::reason-knowledge_pre_gate::0"]
    assert kn.summary == "the fiscal year starts in April"  # the statement
    bp = items["candidate::reason-blueprint_sampled::0"]
    assert bp.summary == "reason=blueprint_sampled example"  # the intent


# --- status round-trips onto the projection (ui-inbox-type-archive contract) --


async def test_inbox_item_status_round_trips_in_review():
    """An `in_review` envelope projects `status='in_review'` (the review-queue default —
    the badge driver the archive view flips)."""
    store = await _store_with_all_reasons()  # every fixture is in_review
    items = await ReviewInbox(store).list()
    assert items, "the seeded store must project at least one in_review item"
    assert all(it.status == CandidateStatus.IN_REVIEW for it in items)
    assert all(it.status == "in_review" for it in items)


async def test_inbox_item_status_round_trips_from_rejected_envelope():
    """A REJECTED envelope projects `status='rejected'` verbatim onto the `InboxItem`
    (drives the archive REJECTED badge); the rest of the projection still derives (the
    reason is re-derived, the summary stays entity-free)."""
    doc = copy.deepcopy(_load()["knowledge_pre_gate"])
    doc["status"] = CandidateStatus.REJECTED
    env = CandidateEnvelope.from_doc(doc)

    item = InboxItem.from_envelope(env)
    assert item.status == CandidateStatus.REJECTED
    assert item.status == "rejected"
    # Nothing else silently changed: id/type carry through and the reason re-derives.
    assert item.candidate_id == env.candidate_id
    assert item.type == "global_knowledge"
    assert item.reason == "knowledge_pre_gate"


async def test_list_status_rejected_returns_only_the_archive():
    """`list(status='rejected')` is the durable archive projection — after a reject it
    returns ONLY the rejected row, while the default `list()` (in_review) no longer
    shows it. The rejected row is retained, not deleted (D29)."""
    store = await _store_with_all_reasons()  # all in_review
    inbox = ReviewInbox(store)
    cid = "candidate::reason-knowledge_pre_gate::0"

    await inbox.reject(cid)  # in_review → rejected (retained)

    review = await inbox.list()  # default = in_review
    assert cid not in {it.candidate_id for it in review}
    assert all(it.status == "in_review" for it in review)

    archive = await inbox.list(status=CandidateStatus.REJECTED)
    assert {it.candidate_id for it in archive} == {cid}
    assert all(it.status == "rejected" for it in archive)


async def test_archive_list_is_newest_first_review_queue_stays_oldest_first():
    """The archive (`order='desc'`) lists newest-first so a LIMIT trims OLD history,
    not present rejects; the review queue keeps its oldest-first FIFO order."""
    from dataclasses import replace

    store = InMemoryCandidateStore()
    base = CandidateEnvelope.from_doc(_load()["knowledge_pre_gate"])
    # Three rejects at increasing timestamps, seeded out of order.
    for cid, created_at in [
        ("candidate::arch::0", "2026-07-03T00:00:00+00:00"),
        ("candidate::arch::2", "2026-07-03T00:00:02+00:00"),
        ("candidate::arch::1", "2026-07-03T00:00:01+00:00"),
    ]:
        await store.put(replace(base, candidate_id=cid,
                                status=CandidateStatus.REJECTED, created_at=created_at))
    # Two review-queue rows to prove ASC is untouched.
    for cid, created_at in [
        ("candidate::rev::1", "2026-07-03T01:00:01+00:00"),
        ("candidate::rev::0", "2026-07-03T01:00:00+00:00"),
    ]:
        await store.put(replace(base, candidate_id=cid,
                                status=CandidateStatus.IN_REVIEW, created_at=created_at))

    inbox = ReviewInbox(store)
    archive = await inbox.list(status=CandidateStatus.REJECTED, order="desc")
    assert [it.candidate_id for it in archive] == [
        "candidate::arch::2", "candidate::arch::1", "candidate::arch::0"
    ]
    review = await inbox.list()  # default in_review, order=asc
    assert [it.candidate_id for it in review] == [
        "candidate::rev::0", "candidate::rev::1"
    ]


# --- S7-reject-is-negative-signal ---------------------------------------------


async def test_reject_marks_rejected_not_deleted():
    """S7-reject-is-negative-signal: reject moves in_review → rejected and the row
    STAYS in the store (a negative training signal, not a delete — D29)."""
    store = await _store_with_all_reasons()
    inbox = ReviewInbox(store)
    cid = "candidate::reason-knowledge_pre_gate::0"

    returned = await inbox.reject(cid)
    assert returned.status == CandidateStatus.REJECTED

    # NOT deleted: still fetchable, now at status=rejected.
    still_there = await store.get(cid)
    assert still_there is not None
    assert still_there.status == CandidateStatus.REJECTED

    # And it has left the inbox projection (no longer in_review).
    remaining = {it.candidate_id for it in await inbox.list()}
    assert cid not in remaining
    # It surfaces as a rejected row for the learner.
    rejected = await store.list_by_status(CandidateStatus.REJECTED)
    assert any(e.candidate_id == cid for e in rejected)


# --- approve / retract + span stripping ---------------------------------------


async def test_approve_promotes_to_validated_and_strips_entity_spans():
    """approve moves in_review → validated; entity-bearing leakage spans are
    stripped before the promotion (D17)."""
    store = await _store_with_all_reasons()
    inbox = ReviewInbox(store)
    cid = "candidate::reason-leakage_near_miss::0"  # carries an employee_code span

    before = await store.get(cid)
    assert LeakageVerdict.from_doc(before.entity_scan).hits[0].span == "E12345"

    promoted = await inbox.approve(cid)
    assert promoted.status == CandidateStatus.VALIDATED
    # The span is blanked; the field/kind (reviewer context) survive.
    hit = LeakageVerdict.from_doc(promoted.entity_scan).hits[0]
    assert hit.span == ""
    assert hit.kind == "employee_code"
    # Persisted stripped.
    assert LeakageVerdict.from_doc((await store.get(cid)).entity_scan).hits[0].span == ""


async def test_retract_moves_validated_to_retired():
    store = await _store_with_all_reasons()
    inbox = ReviewInbox(store)
    cid = "candidate::reason-blueprint_sampled::0"
    await inbox.approve(cid)  # in_review → validated
    retired = await inbox.retract(cid)  # validated → retired
    assert retired.status == CandidateStatus.RETIRED


# --- transition guards --------------------------------------------------------


async def test_approve_requires_in_review():
    store = await _store_with_all_reasons()
    inbox = ReviewInbox(store)
    cid = "candidate::reason-blueprint_sampled::0"
    await inbox.approve(cid)  # now validated
    with pytest.raises(InboxTransitionError):
        await inbox.approve(cid)  # can't approve a validated candidate


async def test_retract_requires_validated():
    store = await _store_with_all_reasons()
    inbox = ReviewInbox(store)
    with pytest.raises(InboxTransitionError):
        await inbox.retract("candidate::reason-blueprint_sampled::0")  # still in_review


async def test_transition_on_missing_candidate_raises():
    inbox = ReviewInbox(InMemoryCandidateStore())
    with pytest.raises(InboxTransitionError):
        await inbox.reject("candidate::nope::0")
