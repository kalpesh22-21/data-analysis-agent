"""QA — ONLY a deterministic hit may DROP a candidate (PriorArt Slice 2, item 5).

`redundant_with_canon` is the loop's single destructive verdict: the candidate is
discarded and no human ever sees it. The design says that verdict may rest ONLY on the
structural key — an exact hash identity over the two inputs both authoring paths have —
and never on a cosine, however high. This module attacks that claim from both ends
rather than asserting it once:

  * SWEEP the soft layer across every trust tier x a similarity grid that spans both
    bands and both boundaries, and assert `control` is never `drop` anywhere in it. A
    single scripted 0.99-canon test proves the one point someone thought of; the sweep
    is what catches a future `if card.is_canon and score >= merge` shortcut.
  * Attack the two SEAMS a drop could leak through that are not the soft layer: the
    structural layer's own tier mapping (only `mcp` drops), and the `PriorArtCard`
    surface a consumer bands on (a card cannot claim canon by any route but `tier`).

Slugs:
  * S6-drop-is-deterministic-only — no soft-layer path emits a drop.
  * S6-canon-drop-needs-the-key   — canon + high cosine + NO structural key ⇒ merge.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.dedup import DedupStage, InMemoryBlueprintCorpus
from data_agent.learning.priorart import (
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    InMemoryPriorArtIndex,
    PriorArtCard,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.summary.models import SessionSummary
from data_agent.learning.triage import TriageVerdict
from data_agent.runtime.blueprint.structural_key import structural_key_from_templates
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

# Every tier a card can carry, including the one that exists only for nodes no writer
# we control produced. All three must behave identically in the SOFT layer.
ALL_TIERS = (TIER_MCP, TIER_LEARNING, TIER_UNSOURCED)

# The similarity grid: both band boundaries, both sides of each, and the extremes. The
# two thresholds are the stage defaults (merge 0.95 / conflict 0.83).
SIMILARITY_GRID = (0.0, 0.5, 0.829, 0.83, 0.831, 0.949, 0.95, 0.951, 0.999, 1.0)


def _ctx() -> StageContext:
    summary = SessionSummary(
        session_id="sess-qa",
        user_id="u1",
        scope_ref="scope-1",
        trace_id="trace-qa",
        content_hash="hash-qa",
        turns=(),
        tool_calls=(),
        blueprint_usages=(),
        askuser_exchanges=(),
        failed_fixed_sql=(),
        accepted_signal="no_correction",
    )
    return StageContext(summary=summary, verdict=TriageVerdict(decision="keep", reason="K1"))


def _envelope() -> CandidateEnvelope:
    doc = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    return CandidateEnvelope.from_doc(doc["single"]["envelope"])


def _candidate_structural_key(env: CandidateEnvelope) -> str:
    gen = env.payload["generalization"]
    return structural_key_from_templates(
        gen.get("result_grain"),
        gen.get("sql_template"),
        [(n["order"], n["sql_template"]) for n in gen.get("node_templates") or []],
    )


def _card(*, tier: str, key: str, intent: str) -> PriorArtCard:
    return PriorArtCard(
        id=f"bp-{tier}",
        kind="blueprint",
        tier=tier,  # type: ignore[arg-type]
        status="validated",
        verified=tier == TIER_MCP,
        drift_status="clean",
        intent=intent,
        result_grain=("department",),
        uses_rules=(),
        structural_key=key,
        embedding_model="all-mpnet-base-v2",
        similarity=0.0,
        model_matched=True,
    )


# --- S6-drop-is-deterministic-only --------------------------------------------


@pytest.mark.parametrize("tier", ALL_TIERS)
@pytest.mark.parametrize("similarity", SIMILARITY_GRID)
async def test_no_soft_layer_similarity_on_any_tier_can_drop_a_candidate(tier, similarity):
    """The sweep. A cosine over intent PROSE is not an identity claim; the loop's most
    consequential decision must not rest on its weakest evidence.

    The card carries a structural key that DOES NOT match the candidate's, so layer 2
    misses and the verdict is produced by the soft layer at exactly *similarity*. No cell
    of the grid may return `drop`, and none may return `redundant_with_canon` — including
    the `mcp` row at 1.0, which is the cell a "the canon obviously has this" shortcut
    would light up."""
    env = _envelope()
    intent = env.payload["intent"]
    card = _card(tier=tier, key="sha256:a-key-that-matches-no-candidate", intent=intent)
    index = InMemoryPriorArtIndex([card], scores={(intent, card.id): similarity})
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    verdict = result.envelope.dedup
    assert verdict is not None
    assert verdict.action != "redundant_with_canon"
    assert verdict.action in {"insert", "conflict", "merge"}
    assert verdict.layer == "soft"
    # The structural layer WAS consulted and missed — otherwise this sweep would be
    # proving nothing about the soft layer at all.
    assert index.key_calls == [_candidate_structural_key(env)]


# --- S6-canon-drop-needs-the-key ----------------------------------------------


async def test_a_perfect_canon_cosine_without_the_key_is_a_merge_not_a_drop():
    """The single most dangerous cell of the grid, called out on its own so its intent
    survives a refactor of the parametrization: an MCP-canon card at cosine 1.0 whose
    structural key does not match. `merge` routes it to a human; `drop` would discard a
    genuinely-new blueprint on the strength of two similar sentences."""
    env = _envelope()
    intent = env.payload["intent"]
    card = _card(tier=TIER_MCP, key="sha256:different", intent=intent)
    index = InMemoryPriorArtIndex([card], scores={(intent, card.id): 1.0})
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "merge"
    assert result.envelope.dedup.layer == "soft"


async def test_the_drop_verdict_is_reachable_only_through_the_structural_layer():
    """The positive control for the sweep above: swap ONLY the card's structural key for
    the candidate's and the same card, same tier, same cosine now drops. Without this the
    sweep could be passing because the drop path is broken rather than because it is
    correctly gated."""
    env = _envelope()
    intent = env.payload["intent"]
    card = _card(tier=TIER_MCP, key=_candidate_structural_key(env), intent=intent)
    index = InMemoryPriorArtIndex([card], scores={(intent, card.id): 0.0})
    stage = DedupStage(InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=index)

    result = await stage.process(env, _ctx())

    assert result.control == "drop"
    assert result.envelope.dedup.action == "redundant_with_canon"
    assert result.envelope.dedup.layer == "structural"
    # A drop is scored 1.0 — the exact-identity confidence — NOT the 0.0 cosine the
    # index was scripted with. The two layers must not share a score.
    assert result.envelope.dedup.similarity == 1.0


@pytest.mark.parametrize("tier", [TIER_LEARNING, TIER_UNSOURCED])
async def test_a_structural_hit_outside_the_canon_never_drops_either(tier):
    """Layer 2 is deterministic, but determinism alone does not license a drop: only
    `mcp` is the tier whose artifacts the agent already recalls. A learning-tier or
    unsourced twin goes to a human."""
    env = _envelope()
    card = _card(tier=tier, key=_candidate_structural_key(env), intent=env.payload["intent"])
    stage = DedupStage(
        InMemoryBlueprintCorpus(), FakeEmbeddingClient(), prior_art=InMemoryPriorArtIndex([card])
    )

    result = await stage.process(env, _ctx())

    assert result.control == "continue"
    assert result.envelope.dedup.action == "merge"


async def test_a_card_cannot_claim_canon_by_any_route_but_its_tier():
    """`is_canon` is what gates the drop, so it must be derivable from ONE field. A card
    stamped `verified=True` with a canon-looking id and a perfect score is still not
    canon unless its `tier` says so — a `verified` learning node (Phase-3 approved, but
    still staging) is exactly that shape and must not become droppable."""
    card = _card(tier=TIER_LEARNING, key="sha256:k", intent="x")
    canon_looking = replace(card, id="bp-total-earnings-by-department", verified=True, similarity=1.0)
    assert not canon_looking.is_canon
    assert replace(canon_looking, tier=TIER_UNSOURCED).is_canon is False
    assert replace(canon_looking, tier=TIER_MCP).is_canon is True
