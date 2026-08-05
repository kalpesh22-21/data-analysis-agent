"""Governed-corpus SAFETY invariant (Phase 2): a learning-loop-landed seed lands in
the `source='learning'` STAGING tier, NEVER the trusted `source='mcp'` recall canon.

`blueprint_seed_from_candidate` / `knowledge_seed_from_candidate` build the seeds the
landing writer MERGE-upserts. If either inherited the `BlueprintSeed`/`KnowledgeSeed`
DEFAULT (`source="mcp"`, `verified=True`), unvetted learning output would leak straight
into the trusted recall partition (the `source='mcp'` trust gate). These tests pin the
explicit override to `source="learning"`, `verified=False`.
"""

from __future__ import annotations

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.generalize.mapping import (
    blueprint_seed_from_candidate,
    knowledge_seed_from_candidate,
)

from ..promotion.helpers import make_blueprint_candidate

_BP_KEY = "sha256:single-bp"
_KN_ID = "kn::sha256:overtime-rule"


def _knowledge_candidate() -> CandidateEnvelope:
    return CandidateEnvelope.from_doc(
        {
            "candidate_id": "candidate::hash-gk::0",
            "type": "global_knowledge",
            "status": "in_review",
            "payload": {
                "statement": "Overtime is paid at 1.5x the base rate beyond 40 hours.",
                "scope": "payroll policy",
                "knowledge_type": "business_rule",
            },
            "provenance": {
                "source_session": "sess-gk",
                "source_trace": "trace-gk",
                "evidence_ref": ["evidence::sess-gk::e1"],
                "extractor_rationale": "a durable business rule",
            },
            "entity_scan": {"result": "pass", "hits": []},
            "confidence": 0.9,
            "proposed_action": "new",
            "depends_on": [],
            "content_hash": "hash-gk",
        }
    )


def test_landed_blueprint_seed_is_learning_tier_unverified() -> None:
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=_BP_KEY)
    seed = blueprint_seed_from_candidate(env, id=f"bp::{_BP_KEY}")
    # SAFETY: staging tier, not trusted canon → excluded from the source='mcp' recall.
    assert seed.source == "learning"
    assert seed.verified is False


def test_landed_knowledge_seed_is_learning_tier_unverified() -> None:
    env = _knowledge_candidate()
    seed = knowledge_seed_from_candidate(env, id=_KN_ID)
    assert seed.source == "learning"
    assert seed.verified is False
