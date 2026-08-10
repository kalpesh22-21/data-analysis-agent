"""QA — `confidence` cannot be made to disagree with `model_matched` (item 4).

The model-skew discount is only a safety property if it is UNFORGEABLE. If a card can
exist whose `confidence` says "trust me" while its `model_matched` says "this cosine was
computed between two different vector spaces", every downstream band is decided on a
number that carries no information — and the failure would be invisible, because both
fields would look right in isolation.

The existing suite asserts the discount arithmetic and that `replace()` keeps the two in
step. This module attacks the STRUCTURE that makes it true, so a future refactor that
turns `confidence` into a stored field (an obvious "optimization" — it is recomputed on
every sort comparison) fails here rather than in production a model swap later:

  * `confidence` must not be a dataclass field — no constructor argument, no `replace`
    keyword, nothing to persist or rehydrate wrongly.
  * The card must be frozen, so no post-construction assignment can split them.
  * The formula must hold across the FULL cross product of `score_basis` x
    `model_matched` x a similarity grid, not at the two points someone tested.
  * `score_basis` must protect an exact-key identity from the discount — and that
    protection must not leak into the vector basis.
"""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError, replace

import pytest

from data_agent.learning.priorart.models import (
    MODEL_MISMATCH_PENALTY,
    TIER_MCP,
    PriorArtCard,
)

SIMILARITY_GRID = (0.0, 0.1, 0.5, 0.83, 0.95, 0.99, 1.0)


def _card(**overrides) -> PriorArtCard:
    base = {
        "id": "bp-x",
        "kind": "blueprint",
        "tier": TIER_MCP,
        "status": "validated",
        "verified": True,
        "drift_status": "clean",
        "intent": "total earnings by department",
        "result_grain": ("department",),
        "uses_rules": (),
        "structural_key": "sha256:k",
        "embedding_model": "all-mpnet-base-v2",
        "similarity": 0.9,
        "model_matched": True,
    }
    base.update(overrides)
    return PriorArtCard(**base)  # type: ignore[arg-type]


# --- the two fields cannot be stored apart ------------------------------------


def test_confidence_is_not_a_stored_field():
    """A stored `confidence` is a second copy of a derived truth, and every second copy
    eventually disagrees with the first. It must be impossible to persist one, to
    rehydrate one, or to pass one to the constructor."""
    field_names = {f.name for f in dataclasses.fields(PriorArtCard)}
    assert "confidence" not in field_names
    with pytest.raises(TypeError):
        PriorArtCard(confidence=1.0, **{})  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        replace(_card(), confidence=1.0)  # type: ignore[call-arg]


def test_the_card_is_frozen_so_neither_half_can_be_reassigned():
    """Frozen is what stops `card.model_matched = True` "fixing" a flagged card in place
    while leaving the cosine that was never comparable."""
    card = _card()
    for attribute, value in (
        ("model_matched", False),
        ("similarity", 1.0),
        ("score_basis", "structural_key"),
        ("tier", "learning"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(card, attribute, value)


def test_confidence_has_no_setter():
    """The property is read-only, so `card.confidence = 1.0` cannot shadow the
    computation even on a hypothetically-unfrozen card."""
    assert isinstance(PriorArtCard.confidence, property)
    assert PriorArtCard.confidence.fset is None
    assert PriorArtCard.confidence.fdel is None


# --- the formula, over the full cross product ---------------------------------


@pytest.mark.parametrize("similarity", SIMILARITY_GRID)
@pytest.mark.parametrize("model_matched", [True, False])
@pytest.mark.parametrize("score_basis", ["vector", "structural_key"])
def test_confidence_is_exactly_one_formula_everywhere(similarity, model_matched, score_basis):
    """One discount, one place, no exceptions. Written as an independent re-derivation
    rather than a call into the property, so a change to the property that keeps the
    tested points and moves the rest is caught."""
    card = _card(similarity=similarity, model_matched=model_matched, score_basis=score_basis)
    discounted = score_basis == "vector" and not model_matched
    expected = similarity * MODEL_MISMATCH_PENALTY if discounted else similarity
    assert card.confidence == expected


@pytest.mark.parametrize("similarity", SIMILARITY_GRID)
def test_a_flagged_vector_card_is_always_discounted(similarity):
    """No cosine survives the mismatch un-discounted — including 0.0, where the discount
    is a no-op in value but must still be the branch taken."""
    card = _card(similarity=similarity, model_matched=False, score_basis="vector")
    assert card.confidence <= card.similarity
    assert card.confidence == similarity * MODEL_MISMATCH_PENALTY


def test_no_flagged_vector_card_can_reach_either_dedup_band():
    """The property that makes the discount a SAFETY measure rather than a decoration:
    with `similarity` capped at 1.0 by cosine, the discounted ceiling is 0.5 — below the
    0.83 conflict band and far below the 0.95 merge band. So a corpus the hydrator left
    un-re-embedded can never route a genuinely-new blueprint to the inbox as a
    duplicate."""
    ceiling = _card(similarity=1.0, model_matched=False, score_basis="vector").confidence
    assert ceiling == 0.5
    assert ceiling < 0.83  # conflict threshold
    assert ceiling < 0.95  # merge threshold


# --- score_basis protects the exact identity, and only that -------------------


@pytest.mark.parametrize("similarity", SIMILARITY_GRID)
def test_a_structural_key_hit_is_never_discounted_whatever_the_model_says(similarity):
    """A structural-key match is a hash identity that involved NO vector, so the model
    stamp is irrelevant to it. Discounting one would be a bug that only bites after an
    embedding-model swap — i.e. long after it was written — and it would silently turn
    the canon-redundancy DROP into a 0.5-confidence merge."""
    card = _card(similarity=similarity, model_matched=False, score_basis="structural_key")
    assert card.confidence == similarity


def test_the_exemption_does_not_leak_into_the_vector_basis():
    """The exemption is keyed on `score_basis`, so the SAME card flipped back to the
    vector basis is discounted again. Without this, "structural_key hits are exempt"
    could be implemented as "mcp cards are exempt" and pass every other test here."""
    exempt = _card(similarity=1.0, model_matched=False, score_basis="structural_key")
    assert exempt.confidence == 1.0
    assert replace(exempt, score_basis="vector").confidence == 0.5  # type: ignore[arg-type]


def test_score_basis_is_the_only_thing_that_exempts_a_card():
    """Sweep every OTHER field that could plausibly be mistaken for the exemption key —
    tier, verified, status, a non-empty structural key — with the vector basis and a
    mismatched model. Every one stays discounted."""
    for overrides in (
        {"tier": "mcp"},
        {"tier": "learning"},
        {"tier": "unsourced"},
        {"verified": True},
        {"verified": None},
        {"status": "validated"},
        {"structural_key": "sha256:a-real-key"},
        {"structural_key": ""},
    ):
        card = _card(similarity=1.0, model_matched=False, score_basis="vector", **overrides)
        assert card.confidence == 0.5, overrides
