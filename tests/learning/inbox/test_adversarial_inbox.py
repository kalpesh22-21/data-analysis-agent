"""Adversarial QA on the S7 human-gate (invariant #4 — nothing global reaches a
retrievable state without human approval + the D17 entity strip on promotion).

We attack the entity-strip-on-approve and the inbox payload_view: the modules
DOCUMENT that entity-bearing data is stripped before a candidate is stamped
`validated` / shown for review, but the implementation strips only the audit
`entity_scan` spans — the PAYLOAD (which is what a physical promoter reads and what
the reviewer sees) keeps the raw entity. strict-xfail = real hole; passing = pinned
gate.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.models import InboxItem
from data_agent.learning.writer.routing import route_candidate

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _in_review_with_entity_in_payload() -> CandidateEnvelope:
    """A blueprint that reached `in_review` as a leakage near-miss: its S5 verdict
    flagged an employee code AND the entity genuinely lives in the payload intent
    (the state Contract D's `leakage_near_miss` describes). This is the payload the
    physical promoter would read on approve."""
    base_doc = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "leakage_near_miss"
    ]
    env = CandidateEnvelope.from_doc(base_doc)
    settled = LeakageVerdict(
        result="quarantine",
        hits=(EntityHit(field="intent", kind="employee_code", span="E12345"),),
        scanned_fields=("intent",),
        scanner="regex+ner+llm",
    )
    return replace(
        env,
        status=CandidateStatus.IN_REVIEW,
        payload={**env.payload, "intent": "total earnings for employee E12345"},
        entity_scan=settled.to_doc(),
    )


def _clean_global_knowledge() -> CandidateEnvelope:
    base_doc = json.loads((FIXTURES / "s3_candidates_mixed.json").read_text())["clean"]
    env = CandidateEnvelope.from_doc(base_doc)
    passed = LeakageVerdict(result="pass", scanned_fields=("statement",), scanner="regex+ner+llm")
    return replace(
        env,
        candidate_id="candidate::gk-clean::0",
        type="global_knowledge",
        payload={"statement": "the standard headcount rule applies", "knowledge_type": "business_rule"},
        entity_scan=passed.to_doc(),
    )


# =============================================================================
# STRICT-XFAIL — the entity strip does not reach the payload
# =============================================================================


async def test_approve_strips_entity_from_payload_before_validated():
    env = _in_review_with_entity_in_payload()
    store = InMemoryCandidateStore()
    await store.put(env)
    inbox = ReviewInbox(store)

    promoted = await inbox.approve(env.candidate_id)

    assert promoted.status == CandidateStatus.VALIDATED
    # SECURE: the entity is gone from the promotable payload. Currently FAILS
    # (payload.intent still contains 'E12345') -> strict xfail.
    assert "E12345" not in json.dumps(promoted.payload)


async def test_inbox_payload_view_is_entity_stripped():
    item = InboxItem.from_envelope(_in_review_with_entity_in_payload())
    # SECURE: the reviewer view does not re-expose the raw entity value. Currently
    # FAILS -> strict xfail.
    assert "E12345" not in json.dumps(item.payload_view)


# =============================================================================
# PASSING HARDENING — the human gate + routing that HOLDS (pin it)
# =============================================================================


async def test_reject_archives_as_negative_signal_not_deleted():
    """S7-reject-is-negative-signal: reject → status=rejected and the row is RETAINED
    in the store (a negative training signal for S9, D29) — never deleted."""
    env = _in_review_with_entity_in_payload()
    store = InMemoryCandidateStore()
    await store.put(env)
    inbox = ReviewInbox(store)

    rejected = await inbox.reject(env.candidate_id)

    assert rejected.status == CandidateStatus.REJECTED
    still_there = await store.get(env.candidate_id)
    assert still_there is not None and still_there.status == CandidateStatus.REJECTED


async def test_clean_global_knowledge_still_forced_to_human_review():
    """Even a gate-`pass` (clean) global_knowledge is routed to in_review — the D58a
    human pre-gate is unconditional, never auto-retrievable by a passing scan."""
    decision = route_candidate(_clean_global_knowledge(), sampled_for_inbox=False)
    assert decision.status == CandidateStatus.IN_REVIEW
    assert decision.control == "route_inbox"
    assert decision.reason == "knowledge_pre_gate"


async def test_approve_does_blank_the_audit_entity_spans():
    """The partial protection that DOES work is pinned: approve blanks the
    entity_scan hit spans so the audit surface stops carrying the raw value (even
    though the payload gap above remains)."""
    env = _in_review_with_entity_in_payload()
    store = InMemoryCandidateStore()
    await store.put(env)
    inbox = ReviewInbox(store)

    promoted = await inbox.approve(env.candidate_id)
    verdict = LeakageVerdict.from_doc(promoted.entity_scan)
    assert verdict.hits  # the hit record is retained (field/kind), for audit
    assert all(h.span == "" for h in verdict.hits)  # but the entity value is blanked
