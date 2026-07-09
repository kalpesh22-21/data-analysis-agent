"""Layer-1 unit tests for `knowledge_seed_from_candidate` (UI Slice 2 §1.1 row 1).

The pure knowledge-side mirror of `blueprint_seed_from_candidate`: it projects an
approved `global_knowledge` candidate onto the neo4j-corpus `KnowledgeSeed`, reading
ONLY the entity-free knowledge surfaces the leakage gate scans (`statement`,
`structured`, `related_terms`, `scope`) — never `evidence`, audit spans, or any
entity-bearing payload (D17). Raises on an empty `statement`.
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.generalize.mapping import knowledge_seed_from_candidate
from data_agent.runtime.retrieval.corpus_loader import KnowledgeSeed

_ID = "kn::sha256:overtime-rule"


def _knowledge_candidate(payload: dict) -> CandidateEnvelope:
    """A `global_knowledge` candidate with an ENTITY-BEARING `evidence`/audit surface,
    to prove the mapping reads NONE of it (D17)."""
    return CandidateEnvelope.from_doc(
        {
            "candidate_id": "candidate::hash-gk::0",
            "type": "global_knowledge",
            "status": "in_review",
            "payload": payload,
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


def test_maps_statement_to_text_and_stamps_provenance() -> None:
    env = _knowledge_candidate(
        {
            "statement": "Overtime is paid at 1.5x the base rate beyond 40 hours.",
            "scope": "payroll policy",
            "knowledge_type": "business_rule",
        }
    )

    seed = knowledge_seed_from_candidate(env, id=_ID)

    assert isinstance(seed, KnowledgeSeed)
    assert seed.id == _ID
    assert seed.text.startswith("Overtime is paid at 1.5x the base rate beyond 40 hours.")
    assert seed.title == "payroll policy"  # scope → title
    assert seed.doc_id == env.candidate_id  # doc_id ← candidate_id
    assert seed.status == "validated"
    # Provenance: a loop-landed chunk is distinguishable from a hand-authored seed
    # and carries its originating candidate id (incident-response handle).
    assert seed.created_by == "learning"
    assert seed.source_candidate_id == env.candidate_id


def test_related_terms_and_structured_enrich_the_recall_text() -> None:
    env = _knowledge_candidate(
        {
            "statement": "A pay period is the recurring interval wages are calculated over.",
            "related_terms": ["pay cycle", "pay run"],
            "structured": {"cadence": "bi-weekly"},
            "scope": "glossary",
        }
    )

    seed = knowledge_seed_from_candidate(env, id=_ID)

    # The richer recall text concatenates ONLY the STRING LEAF VALUES the S5 gate
    # scans — the related-term strings and the structured string value.
    assert "pay period" in seed.text
    assert "pay cycle" in seed.text and "pay run" in seed.text
    assert "bi-weekly" in seed.text  # the string leaf VALUE lands
    # The dict KEY is NOT a scanned leaf (S5 `_collect_text` skips keys) — it must
    # NEVER reach the scope-bypassed index text (else an entity-as-key would leak).
    assert "cadence" not in seed.text


def test_structured_key_and_numeric_leaf_never_reach_seed_text() -> None:
    """The HIGH leak guard: the S5 gate + entity strip only cover STRING LEAF VALUES.
    An entity smuggled as a dict KEY, or a numeric/boolean leaf, is never scanned,
    stripped, or caught by the last-gate defense — so it MUST NOT be serialized into
    the seed text that lands in the scope-bypassed `:KnowledgeChunk` index (D17/D58a)."""
    entity_key = "ACME Corp"
    env = _knowledge_candidate(
        {
            "statement": "Top accounts get priority routing.",
            "structured": {
                entity_key: "top account",  # entity as a KEY
                "employee_id": 12345,  # numeric leaf
                "is_flagged": True,  # boolean leaf
                "note": "handled quarterly",  # a real string leaf (scanned) — allowed
            },
            "scope": "policy",
        }
    )

    seed = knowledge_seed_from_candidate(env, id=_ID)

    assert entity_key not in seed.text  # the entity KEY never lands
    assert "12345" not in seed.text  # the numeric leaf never lands
    assert "True" not in seed.text and "true" not in seed.text  # boolean never lands
    assert "handled quarterly" in seed.text  # the scanned string leaf DOES land


def test_empty_statement_raises() -> None:
    env = _knowledge_candidate({"statement": "   ", "scope": "x"})
    with pytest.raises(ValueError):
        knowledge_seed_from_candidate(env, id=_ID)


def test_missing_statement_raises() -> None:
    env = _knowledge_candidate({"scope": "x"})  # no statement at all
    with pytest.raises(ValueError):
        knowledge_seed_from_candidate(env, id=_ID)


def test_reads_no_entity_bearing_or_audit_field() -> None:
    """The mapping must read ONLY the entity-free surfaces — an entity-bearing
    `evidence`/`raw_value` in the payload must NEVER reach the seed (D17)."""
    secret = "ACME-CORP-SECRET-DEPT-42"
    env = _knowledge_candidate(
        {
            "statement": "Overtime is paid at 1.5x the base rate.",
            "scope": "policy",
            # Entity-bearing surfaces the mapping must ignore entirely.
            "evidence": [{"quote": secret}],
            "raw_value": secret,
            "entity_hint": secret,
        }
    )

    seed = knowledge_seed_from_candidate(env, id=_ID)

    haystack = "\n".join([seed.text, seed.title or "", seed.doc_id])
    assert secret not in haystack


def test_title_is_none_when_scope_absent() -> None:
    env = _knowledge_candidate({"statement": "Some entity-free rule."})
    seed = knowledge_seed_from_candidate(env, id=_ID)
    assert seed.title is None
