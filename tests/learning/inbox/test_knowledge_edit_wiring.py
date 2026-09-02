"""The seams the knowledge edit path depends on (design §B step 1, §E).

TWO SEAMS, and both are the same idea in different places: a check must have exactly one
implementation, or the path that skipped it is the path nobody tested.

  * `validate_payload` is the EXTRACTOR's intake reader, made reachable from outside
    `to_candidate`. These tests pin that the two agree — if they could ever disagree, the closed
    key set would hold on the model path and not on the human one, which is precisely backwards:
    a human's payload is the one a human is about to approve.
  * `build_promotion_plane` threads the three new collaborators and refuses a split store. The
    editor's `guarded_put` re-reads the id it is about to write; against a different store that
    re-read finds nothing and every single edit reports a race the reviewer can never clear.
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.extractor.models import Decline
from data_agent.learning.extractor.validation import (
    _PAYLOAD_READERS,
    CANDIDATE_TYPES,
    to_candidate,
    validate_payload,
)
from data_agent.learning.factory import LearningWiringError, build_promotion_plane
from data_agent.learning.inbox.knowledge_edit import KnowledgeEditor
from data_agent.learning.promotion.models import ProbeResult

from ..extractor.helpers import make_summary

GOOD = {
    "statement": "leave accrues at 1.5 days per month for full-time staff",
    "knowledge_type": "business_rule",
    "related_terms": ["leave", "accrual"],
    "structured": {"rate": "1.5"},
    "scope": "leave accrual",
}


def _raw(payload: dict) -> dict:
    """The envelope shape `to_candidate` reads, wrapped around *payload*."""
    return {
        "type": "global_knowledge",
        "confidence": 0.8,
        "evidence": [{"turn_ref": 0, "tool_call_ref": "tc1", "quote": "q"}],
        "rationale": "r",
        "proposed_action": "add",
        "entity_self_check": {"contains_entities": False, "found": []},
        "payload": payload,
    }


# --- one reader, two callers ------------------------------------------------


def test_a_good_payload_passes_both_readers() -> None:
    assert validate_payload("global_knowledge", GOOD) is None
    assert not isinstance(
        to_candidate(_raw(GOOD), make_summary(), known_rules=frozenset()), Decline
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"statement": "a fact", "definition": "off-contract"},
        {"statement": "a fact", "user_id": "u1"},
        {"statement": ""},
        {},
        {"statement": "a fact", "related_terms": "not a list"},
        {"statement": "a fact", "structured": ["not", "an", "object"]},
        {"statement": 42},
    ],
)
def test_the_two_readers_decline_the_same_payloads_with_the_same_sentence(
    payload: dict,
) -> None:
    """⚠ DERIVED, NOT DUPLICATED. `to_candidate` DELEGATES to `validate_payload`, so this is not
    a coincidence being asserted — it is the delegation being pinned, because the tempting
    "small" refactor is to give the human path its own friendlier check, and that check is
    exactly the one that would stop enforcing the closed key set."""
    direct = validate_payload("global_knowledge", payload)
    through = to_candidate(_raw(payload), make_summary(), known_rules=frozenset())

    assert direct is not None
    assert isinstance(through, Decline)
    assert direct.reason == through.reason
    assert direct.detail == through.detail


def test_a_non_object_payload_declines_rather_than_raising() -> None:
    """The human path can hand this anything a JSON body can hold. `validate_payload` is
    documented never to raise, so a list has to become a legible decline rather than a 500."""
    decline = validate_payload("global_knowledge", ["not", "an", "object"])
    assert isinstance(decline, Decline)
    assert "object" in decline.detail


def test_an_unknown_type_has_no_opinion_rather_than_a_key_error() -> None:
    """`.get`, not `[...]`: a candidate type added without a reader must degrade to the old
    accept-any-object behaviour, never to a KeyError out of a function documented never to
    raise. The parity test below is what makes that gap loud at build time instead."""
    assert validate_payload("not_a_type", {"anything": 1}) is None


def test_every_non_blueprint_type_is_still_reachable_through_the_public_reader() -> None:
    """The same parity `test_every_non_blueprint_type_has_a_payload_reader` asserts, restated
    against the PUBLIC entry — so a fifth target added with a reader but not reachable here
    would fail loudly rather than silently skipping the human path's check."""
    for candidate_type in set(CANDIDATE_TYPES) - {"blueprint"}:
        assert candidate_type in _PAYLOAD_READERS
        assert validate_payload(candidate_type, {}) is not None


# --- the composition root ---------------------------------------------------


class _NoOpProbe:
    async def run(self, sql, *, grain_columns, column_scope=()):
        return ProbeResult(row_count=0, distinct_grain_count=None, columns=())


class _ZeroHitCounts:
    async def hit_count(self, canonical_key: str) -> int:
        return 0


def _plane(**over):
    store = over.pop("candidate_store", None) or InMemoryCandidateStore()
    return build_promotion_plane(
        LearningSettings(_env_file=None),
        candidate_store=store,
        probe=_NoOpProbe(),
        hit_counts=_ZeroHitCounts(),
        **over,
    )


def test_the_three_collaborators_default_absent() -> None:
    """Absent by default, so an existing caller behaves exactly as it did — and refuses loudly
    on use rather than silently doing less."""
    _scheduler, inbox = _plane()
    assert inbox._knowledge_editor is None
    assert inbox._knowledge_reviser is None
    assert inbox._user_store is None


def test_the_three_collaborators_thread_through() -> None:
    store = InMemoryCandidateStore()
    editor = KnowledgeEditor(store=store)
    _scheduler, inbox = _plane(
        candidate_store=store,
        knowledge_editor=editor,
        knowledge_reviser="a-reviser",
        user_store="a-store",
    )
    assert inbox._knowledge_editor is editor
    assert inbox._knowledge_reviser == "a-reviser"
    assert inbox._user_store == "a-store"


def test_a_knowledge_editor_over_a_different_store_is_refused_at_composition() -> None:
    """⚠ FAIL FAST, because the runtime symptom is unreadable. The inbox reads a row and hands
    the envelope to the editor, whose `guarded_put` re-reads THAT id — against a different store
    the re-read finds nothing, so every edit reports a race the reviewer cannot clear and
    nothing in the message points at the wiring."""
    with pytest.raises(LearningWiringError, match="knowledge editor"):
        _plane(knowledge_editor=KnowledgeEditor(store=InMemoryCandidateStore()))
