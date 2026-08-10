"""InMemoryPriorArtIndex — the Layer-1 fake behind the `PriorArtIndex` port.

The fake exists so the unit suite stays hermetic, which only works if a caller cannot
tell it from the real reader. These tests pin the behaviours a caller depends on and
that the two implementations must therefore share:

  * terminal artifacts are excluded (a filter present in one and absent in the other is
    a bug no unit test would catch);
  * canon wins a cross-tier structural-key tie;
  * an empty key is a MISS, never a match-everything;
  * failure RAISES `PriorArtUnavailableError` rather than returning `[]`.

Slug: PA-fake-matches-the-real-contract.
"""

from __future__ import annotations

import pytest

from data_agent.learning.priorart import (
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    InMemoryPriorArtIndex,
    PriorArtCard,
    PriorArtUnavailableError,
)


def _card(id_: str, *, intent: str = "", tier: str = TIER_MCP, status: str = "validated",
          key: str = "", kind: str = "blueprint") -> PriorArtCard:
    return PriorArtCard(
        id=id_,
        kind=kind,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        status=status,
        verified=True,
        drift_status="clean",
        intent=intent or id_,
        result_grain=(),
        uses_rules=(),
        structural_key=key,
        embedding_model="all-mpnet-base-v2",
        similarity=0.0,
        model_matched=True,
    )


async def test_search_ranks_by_overlap_and_respects_the_limit():
    index = InMemoryPriorArtIndex(
        [
            _card("bp-near", intent="total earnings by department for a year"),
            _card("bp-far", intent="headcount as of a date"),
        ]
    )
    hits = await index.search("total earnings by department", limit=2)
    assert [c.id for c in hits] == ["bp-near", "bp-far"]
    assert hits[0].similarity > hits[1].similarity
    assert await index.search("total earnings by department", limit=1) == hits[:1]


async def test_scripted_scores_pin_a_band_boundary_without_a_fake_vector():
    index = InMemoryPriorArtIndex(
        [_card("bp-x", intent="anything at all")],
        scores={("some query", "bp-x"): 0.96},
    )
    (hit,) = await index.search("some query")
    assert hit.similarity == 0.96


async def test_search_excludes_terminal_artifacts():
    """A candidate a human explicitly REJECTED must not come back as live prior art —
    the whole point of stamping terminal status onto the artifact."""
    index = InMemoryPriorArtIndex(
        [
            _card("bp-rejected", intent="total earnings by department", status="rejected"),
            _card("bp-retired", intent="total earnings by department", status="retired"),
            _card("bp-live", intent="total earnings by department"),
        ]
    )
    assert [c.id for c in await index.search("total earnings by department")] == ["bp-live"]


async def test_search_filters_by_kind():
    index = InMemoryPriorArtIndex(
        [
            _card("bp-1", intent="overtime rules", kind="blueprint"),
            _card("kn-1", intent="overtime rules", kind="knowledge"),
        ]
    )
    assert [c.id for c in await index.search("overtime rules", kinds=("blueprint",))] == ["bp-1"]
    assert [c.id for c in await index.search("overtime rules", kinds=("knowledge",))] == ["kn-1"]
    both = await index.search("overtime rules", kinds=("blueprint", "knowledge"))
    assert {c.id for c in both} == {"bp-1", "kn-1"}


async def test_structural_key_lookup_prefers_the_canon_on_a_cross_tier_tie():
    """The collision the loose key EXISTS to detect: the canon and a learning node that
    re-derived it carry the same structural key. `mcp` must win — "the canon already has
    this" is the stronger answer, and the only one that justifies dropping a candidate."""
    index = InMemoryPriorArtIndex(
        [
            _card("bp-learning-twin", tier=TIER_LEARNING, key="sha256:same"),
            _card("bp-canon", tier=TIER_MCP, key="sha256:same"),
            _card("bp-unsourced-twin", tier=TIER_UNSOURCED, key="sha256:same"),
        ]
    )
    hit = await index.get_by_structural_key("sha256:same")
    assert hit is not None
    assert hit.id == "bp-canon"
    assert hit.is_canon
    # An exact key identity, not a cosine — so it is undiscounted and self-describing.
    assert hit.score_basis == "structural_key"
    assert hit.confidence == 1.0


async def test_an_empty_structural_key_is_a_miss_not_a_wildcard():
    """`structural_key_from_templates` returns "" when a template does not normalize, and
    the graph stores no key for such a blueprint. Matching those against each other would
    be the worst false positive available: every un-keyable candidate "already exists"."""
    index = InMemoryPriorArtIndex([_card("bp-keyless", key="")])
    assert await index.get_by_structural_key("") is None


async def test_structural_key_lookup_excludes_terminal_artifacts():
    index = InMemoryPriorArtIndex([_card("bp-dead", status="rejected", key="sha256:k")])
    assert await index.get_by_structural_key("sha256:k") is None


async def test_failure_raises_rather_than_returning_empty():
    """THE inversion of `VectorIndex`'s degrade-to-`[]` contract. Here `[]` is a factual
    claim — "nothing like this exists" — that the dedup stage acts on by minting a new
    candidate. Returning it for an unreachable store would make the loop record a
    duplicate as novel, which is the exact failure this port was built to fix."""
    index = InMemoryPriorArtIndex([_card("bp-x")], fail=True)
    with pytest.raises(PriorArtUnavailableError):
        await index.search("anything")
    with pytest.raises(PriorArtUnavailableError):
        await index.get_by_structural_key("sha256:k")


async def test_calls_are_recorded_for_assertions():
    index = InMemoryPriorArtIndex()
    await index.search("q", kinds=("blueprint",), limit=3)
    await index.get_by_structural_key("sha256:k")
    assert index.search_calls == [("q", ("blueprint",), 3)]
    assert index.key_calls == ["sha256:k"]
