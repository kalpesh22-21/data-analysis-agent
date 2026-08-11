"""DEFECT (failing on purpose): `prior_art._score` is not total over a numeric card.

## What this file is

Seven of these nine tests FAIL against the tree as of plan §3a. They are written as
plain assertions rather than `xfail(strict=True)` because they are not a known
limitation someone decided to live with — they are a live break of the property the
whole slice is built on, and the repo's xfail convention is for the former.

## The property being broken

`prior_art.py` states its posture twice, and both statements are load-bearing:

  * module docstring / `lookup_prior_art` — "Never raises. ... an uncaught raise here
    escapes `LearningExtractor.extract`, which the consumer does not guard, so the
    message goes un-acked -> reclaim -> dead-letter and the WHOLE session's learning is
    lost because a read that only ever improves the prompt failed."
  * `_score` — "a mis-typed card scores bottom, never crashes".

Both are false. `_score` was derived from the right question ("what does the RENDER do
with this value?") but stopped one operation short. The chain is:

    card.confidence  ->  isinstance(value, (int, float))  ->  float(value)  ->  f"{x:.2f}"
    [guarded]            [guarded: bool + non-numeric]        [UNGUARDED]      [UNGUARDED]

`float()` raises `OverflowError` on an unbounded Python `int` — which is a perfectly
ordinary JSON number, and `int` passes the isinstance gate the guard does have. And
`f"{x:.2f}"` happily renders `inf`/`nan`, which are numbers but are not scores.

## Why it matters, in the two directions the module already cares about

  * FAIL-OPEN, broken. The raise happens in `_build_messages` on the MANDATORY
    pre-fetch path — before the model is ever called — so a single poisoned card costs
    the entire session's learning, which is the exact outcome `lookup_prior_art`'s
    blanket `except Exception` exists to prevent. The guard is one layer too shallow:
    `lookup_prior_art` protects the CALL to the port, nothing protects the RENDER of
    what it returned.
  * RANKING, silently poisoned. `match=inf` on a `tier=unsourced` card - a node the
    tier system exists to say "a hand edit or a foreign writer touched this" - reads to
    the model as the most similar artifact in the corpus, outranking every genuine hit.
    `_score` already refuses `True` for precisely this reason ("a `True` scoring 1.00 is
    a perfect false positive"); `inf` is the same false positive with no ceiling.

## Realism

`Neo4jPriorArtIndex._card_from_record` coerces via `_float`, and a neo4j vector score is
a finite cosine, so this is not reachable through today's single production reader —
`_float` has the identical missing guard, but a 64-bit driver integer cannot overflow a
float. The claim under test is not "neo4j will do this". It is the one `prior_art.py`
makes for itself: "The port is a PROTOCOL. `_card_from_record` guarantees nothing about
a card from another implementation ... So the renderer is total over any field type
rather than trusting one implementation's mapper." A renderer that raises is not total.

Slug: PA-card-score-totality.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from data_agent.learning.extractor.prior_art import (
    PriorArtLookup,
    _card_line,
    _score,
    render_prior_art_block,
)

from .helpers import KEEP_VERDICT, blueprint_raw, emit_extractor, make_summary, prior_art_card


class _IndexReturning:
    """A port implementation that is not `Neo4jPriorArtIndex`. Legal by the protocol."""

    def __init__(self, *cards):
        self._cards = list(cards)

    async def search(self, text, *, kinds=(), limit=5):
        return self._cards

    async def get_by_structural_key(self, key):  # pragma: no cover - unused here
        return None


def _card(similarity):
    return replace(prior_art_card("bp-x"), similarity=similarity)  # type: ignore[arg-type]


# --- the crash ------------------------------------------------------------------


def test_an_unbounded_int_similarity_does_not_raise_out_of_the_renderer():
    """FAILS TODAY (OverflowError). `float(10**400)` raises, and `int` is exactly the
    type `_score`'s isinstance gate admits. A JSON body carrying a big integer is not
    exotic; a renderer that raises on it is."""
    _card_line(_card(10**400))


async def test_a_poisoned_card_does_not_cost_the_whole_session_extraction():
    """FAILS TODAY (OverflowError out of `extract`). This is the failure mode
    `lookup_prior_art`'s blanket `except Exception` is documented to prevent, occurring
    one layer inside it: the port returned cleanly and the RENDER is what blew up.

    Traceback today:
        extract -> _call_model_with_retry -> _build_messages
                -> render_prior_art_block -> _card_line -> _score -> float()
    """
    extractor = emit_extractor(
        [blueprint_raw()], prior_art=_IndexReturning(_card(10**400))
    )
    result = await extractor.extract(make_summary(), KEEP_VERDICT)
    assert len(result.candidates) == 1  # fail-open: prior art only ever IMPROVES the prompt


# --- the silent poisoning -------------------------------------------------------


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_similarity_scores_bottom_like_every_other_unusable_value(value):
    """FAILS TODAY for `inf`/`-inf`/`nan`. `_score`'s contract is "a mis-typed card
    scores bottom"; a non-finite float is numerically well-typed and semantically
    unusable, which is the same category as the `bool` the guard already rejects."""
    assert _score(_card(value)) == 0.0


def test_an_infinite_card_cannot_outrank_a_real_one_in_the_rendered_block():
    """The consequence, stated as the model would read it. `tier=unsourced` exists to
    say a foreign writer touched the node, so "the top hit is a node of unknown
    provenance claiming a perfect score" is the concrete threat, not a hypothetical."""
    poisoned = replace(
        prior_art_card("bp-forged", tier="unsourced"), similarity=float("inf")
    )  # type: ignore[arg-type]
    genuine = prior_art_card("bp-real", similarity=0.91)
    block = render_prior_art_block(
        PriorArtLookup(query="q", cards=(poisoned, genuine))
    )
    assert "match=inf" not in block
    assert "match=nan" not in block


def test_an_enormous_finite_similarity_does_not_paste_310_digits_into_the_prompt():
    """FAILS TODAY (renders ~310 characters). Every other field in `_card_line` is
    length-capped because the block is prepended to a request that already carries the
    whole session; the score is the one that is formatted, not sanitized."""
    line = _card_line(_card(1e308))
    match = line.split("match=")[1].split(";")[0].split("]")[0]
    assert len(match) <= 8, f"score rendered {len(match)} chars: {match[:40]}..."


# --- what already holds, pinned so a fix cannot regress it ----------------------


def test_a_finite_score_is_unaffected():
    assert _score(_card(0.42)) == pytest.approx(0.42)
    assert "match=0.42" in _card_line(_card(0.42))


def test_the_existing_guards_still_hold():
    assert _score(_card("0.99")) == 0.0  # non-numeric -> bottom
    assert _score(_card(True)) == 0.0  # bool is an int subclass
    assert not math.isnan(_score(_card(0.0)))
