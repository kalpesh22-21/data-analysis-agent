"""PriorArtCard — the entity-free projection + the embedding-skew discount.

Slugs:
  * PA-card-model-skew-discount  — a cross-embedding-space cosine is discounted, and
                                   the discount cannot reach any automatic band.
  * PA-card-exact-key-undiscounted — a structural-key identity is NOT discounted (it
                                   involved no vector).
  * PA-card-tri-state-verified   — `verified=None` ("the node does not say") is a
                                   distinct state from `False`.
"""

from __future__ import annotations

from dataclasses import replace

from data_agent.learning.priorart import (
    MODEL_MISMATCH_PENALTY,
    TERMINAL_STATUSES,
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    PriorArtCard,
)

_MERGE_BAND = 0.95
_CONFLICT_BAND = 0.83


def _card(**overrides) -> PriorArtCard:
    base = {
        "id": "bp-total-earnings-by-department",
        "kind": "blueprint",
        "tier": TIER_MCP,
        "status": "validated",
        "verified": True,
        "drift_status": "clean",
        "intent": "total earnings by department for a year",
        "result_grain": ("department",),
        "uses_rules": ("rule-overtime-multiplier",),
        "structural_key": "sha256:abc",
        "embedding_model": "all-mpnet-base-v2",
        "similarity": 0.9,
        "model_matched": True,
    }
    base.update(overrides)
    return PriorArtCard(**base)  # type: ignore[arg-type]


# --- PA-card-model-skew-discount ---------------------------------------------


def test_a_matched_model_confidence_is_the_raw_cosine():
    card = _card(similarity=0.97, model_matched=True)
    assert card.confidence == 0.97


def test_a_mismatched_model_cosine_is_discounted():
    """The hydrator preserves `source='learning'` nodes across an embedding-model swap
    WITHOUT re-embedding them, and a cross-tier search cannot filter on the model (that
    would drop the whole learning tier). So the cosine is between two DIFFERENT vector
    spaces and carries no information — it must not be believed at face value."""
    card = _card(similarity=0.97, model_matched=False)
    assert card.confidence == 0.97 * MODEL_MISMATCH_PENALTY


def test_no_mismatched_cosine_can_reach_an_automatic_band():
    """The property that makes the discount SAFE rather than merely cosmetic: a perfect
    1.0 cosine measured across two embedding spaces still lands below BOTH dedup bands,
    so a model-skewed corpus can never auto-route a genuinely-new blueprint to the inbox
    as a duplicate. If the penalty is ever retuned upward, this is the test that fails."""
    perfect_but_skewed = _card(similarity=1.0, model_matched=False)
    assert perfect_but_skewed.confidence < _CONFLICT_BAND
    assert perfect_but_skewed.confidence < _MERGE_BAND


def test_an_unstamped_embedding_model_is_not_a_match():
    """`model_matched` is set by the mapper with BARE equality — an empty stored model
    is "the node does not say", not parity. Pinned on the card too so a hand-built card
    in a future test cannot express the fail-open combination by accident."""
    card = _card(embedding_model="", model_matched=False, similarity=0.99)
    assert card.confidence < _CONFLICT_BAND


# --- PA-card-exact-key-undiscounted ------------------------------------------


def test_a_structural_key_hit_is_never_discounted_by_model_skew():
    """A structural-key match is an exact hash identity that involved no vector at all.
    Discounting it for an embedding-model mismatch would be a straightforward bug — and
    one that only bites AFTER a model swap, i.e. long after it was written."""
    card = _card(similarity=1.0, model_matched=False, score_basis="structural_key")
    assert card.confidence == 1.0


# --- PA-card-tri-state-verified ----------------------------------------------


def test_verified_is_tri_state():
    assert _card(verified=True).verified is True
    assert _card(verified=False).verified is False
    # NOT coerced to False: an un-stamped node means a writer we do not control touched
    # the graph, which is a different and more alarming fact than "not yet verified".
    assert _card(verified=None).verified is None


# --- tiers + terminal ---------------------------------------------------------


def test_origin_defaults_to_graph_and_names_the_store_the_card_came_from():
    """Added when the soft layer became a UNION of two stores. Not bookkeeping: a match
    on a landed graph node and a match on an un-landed `learning_corpus` sibling are
    different facts (the second is a concurrency signal), and only `graph` cards can
    reach `get_by_structural_key` and therefore the canon-redundancy drop."""
    assert _card().origin == "graph"
    assert replace(_card(), origin="corpus").origin == "corpus"


def test_only_the_mcp_tier_is_canon():
    assert _card(tier=TIER_MCP).is_canon
    assert not _card(tier=TIER_LEARNING).is_canon
    # An unsourced node is NEVER canon, so it can never trigger the drop verdict.
    assert not _card(tier=TIER_UNSOURCED).is_canon


def test_terminal_statuses_are_the_two_human_kill_states():
    assert TERMINAL_STATUSES == frozenset({"rejected", "retired"})
    for status in TERMINAL_STATUSES:
        assert _card(status=status).is_terminal
    for status in ("extracted", "candidate", "in_review", "validated", "promoted", ""):
        assert not _card(status=status).is_terminal


def test_the_card_carries_no_free_text_beyond_the_gate_scanned_intent():
    """Cards only, never payloads. Two live reasons (see `priorart/models.py`): an
    `extracted` candidate has not passed the leakage gate (S5 is stage 2), and
    `extractor_rationale` is never touched by `strip_entity_bearing`. So the card must
    have no field that could carry model prose or SQL — this asserts the SHAPE, which is
    the thing a future field addition would quietly break."""
    fields = set(_card().__dataclass_fields__)
    forbidden = {
        "payload",
        "sql_template",
        "sql",
        "extractor_rationale",
        "rationale",
        "evidence",
        "evidence_refs",
        "resolves",
        "slots",
        "notes",
    }
    assert fields & forbidden == set()


def test_replace_keeps_confidence_consistent_with_the_flag():
    """`confidence` is a property, not a stored field, precisely so no `replace(...)`
    can produce a card whose stored confidence disagrees with its stored model flag."""
    card = _card(similarity=0.9, model_matched=True)
    assert replace(card, model_matched=False).confidence == 0.9 * MODEL_MISMATCH_PENALTY
