"""S7 inbox — the in_review projection + human transitions (Slice 7, Contract D).

Slug:
  * S7-reject-is-negative-signal — reject ⇒ status=rejected (a NEGATIVE signal),
    NOT a delete: the row is retained in the store for the S9 learner (D29).

Also covers the in_review projection, reason labelling, approve→validated with
entity-span stripping (D17), retract→retired, and transition guards. Built against
`envelopes_each_reason.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.inbox import InboxTransitionError, ReviewInbox

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
