"""Inbox ranking — `novelty × groundedness² × session-quality` (plan §4).

The squared term is not a typo. GROUNDEDNESS is the share of the `parameterization` resolving
to a catalog rule or a known column, as against free-floating `inline` literals — all-hardcoded
filters are a one-off transcription, slots and rules are a reusable template. SESSION-QUALITY
is how the producing session went, from the stamped `SessionSignals`. NOVELTY is distance from
the closest LANDED artifact, from the dedup stage's `NoveltyStamp`.

GROUNDEDNESS ENTERS TWICE, and the asymmetry is the point: a plain product is symmetric in its
first two terms, so a hardcoded one-off nothing resembles would rank identically to a
well-parameterized template of a familiar question. Novelty is therefore GATED — scaled by
groundedness before entering the product — which pushes the ungrounded outlier down an order
of magnitude and leaves the grounded-but-familiar candidate where it was.

Everything degrades to a NUMBER, never an exception: this runs inside a list projection a
human is waiting on, over rehydrated JSON. A missing stamp yields that axis's neutral, and
`components` reports which axes were measured.

THE SCORE IS AN ORDERING, NOT A PERCENTAGE, and does not use the top of its range: cosines
over English prose have a ~0.53 floor, so `novelty = 1 - cosine` occupies roughly
`[0.0, 0.47]` and a perfect candidate scores about 0.26. So `review_score_cutoff` must be set
from MEASURED data — a "half decent" 0.5 would hide the ENTIRE queue, hence the 0.0 default —
and the score must never be shown to a reviewer as a confidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..candidate.models import CandidateEnvelope

# --- session-quality constants -----------------------------------------------
#
# The acceptance bucket. `explicit_confirm`/`thumbs_up` are a human SAYING the answer was
# right; `no_correction` is the absence of a complaint, which is weaker evidence but is
# what the D34 acceptance rule is mostly built on; `None`/unknown means the loader
# detected no acceptance at all, which is the weakest thing a candidate can be built on.
#
# An UNKNOWN spelling falls to the weakest bucket, not to the middle: the value comes off
# a rehydrated doc, and a foreign writer inventing a signal name must not be able to
# promote its candidate up the queue.
_ACCEPTANCE_WEIGHT: dict[str | None, float] = {
    "explicit_confirm": 1.0,
    "thumbs_up": 1.0,
    "no_correction": 0.85,
}
_UNACCEPTED_WEIGHT = 0.6

# How hard each unit of struggle bites. A "unit" is a failed-then-fixed query, a
# clarifying question the agent had to ask, or a turn beyond the first. The factor is
# `1 / (1 + w * units)` — hyperbolic rather than exponential so a genuinely long session
# is discounted heavily but never annihilated: a ten-turn session that ended in a correct,
# accepted answer is still a real blueprint, just a less clean template than a one-shot.
_STRUGGLE_WEIGHT = 0.25

# A session in which the agent ran an EXISTING blueprint and the human then corrected it.
# Halved, because it is a negative statement about the corpus that this candidate is
# being minted from: whatever the analyst ended up accepting, the session's own evidence
# is that the blueprint layer got it wrong once already.
_CORRECTED_PENALTY = 0.5

# --- neutrals for an unmeasured axis -----------------------------------------
#
# All three are 1.0. A CONSTANT preserves the relative order of whatever else WAS
# measured, whereas any other value would silently re-rank on the strength of a fact
# nobody established.
#
# **This constant is only comparable WITHIN the unmeasured partition, and the sort key is
# what enforces that.** An earlier version of this comment claimed a constant "preserves
# the ordering of the axes that were measured" and stopped there. That is true within one
# candidate and FALSE ACROSS candidates — which is the only place a ranking exists. The
# arithmetic: novelty's measured ceiling against a real corpus is ~0.47 (see the
# measurement table above), so a perfect MEASURED candidate scores ~0.26, while an
# unmeasured one scores up to `1.0 × 1.0² × quality`. Writer-routed knowledge items have
# neither a dedup verdict nor a parameterization, so BOTH their axes are unmeasured — they
# would have sat permanently above every measured blueprint, for ever, and a non-zero
# cutoff would then have hidden the honest rows and kept the blind ones.
#
# `rank_key` therefore PARTITIONS on `RankedScore.measured` before it compares any score,
# and `ReviewInbox.list` exempts unmeasured rows from the cutoff. Neither the order nor
# the filter can trade a measured row for an unmeasured one.
_UNMEASURED = 1.0


@dataclass(frozen=True)
class RankedScore:
    """One candidate's review score plus every input that produced it.

    The components travel to the review UI so a reviewer can see WHY a row is where it is. A bare
    float would make the ordering unfalsifiable — the single most common way a ranking quietly
    stops working is that nobody can tell it has.
    """

    score: float
    novelty: float
    groundedness: float
    session_quality: float
    # Which axes rest on a real measurement. A candidate with `novelty_measured=False` is
    # not "maximally novel"; it is one nobody could compare, and its position in the queue
    # carries no information about novelty at all.
    novelty_measured: bool
    quality_measured: bool
    groundedness_measured: bool

    @property
    def measured(self) -> bool:
        """True iff every axis rests on a real measurement."""
        return (
            self.novelty_measured
            and self.quality_measured
            and self.groundedness_measured
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "novelty": self.novelty,
            "groundedness": self.groundedness,
            "session_quality": self.session_quality,
            "novelty_measured": self.novelty_measured,
            "quality_measured": self.quality_measured,
            "groundedness_measured": self.groundedness_measured,
            # The conjunction, on the wire rather than left for the client to recompute.
            # It is the SORT PARTITION (`rank_key`) and the cutoff exemption, so a UI that
            # derived it differently would render groupings the server does not have.
            "measured": self.measured,
        }


def groundedness(env: CandidateEnvelope) -> tuple[float, bool]:
    """The share of this candidate's parameterization that is GROUNDED.

    An entry is grounded when it resolves to something the catalog knows: `role == "rule"` with a
    non-empty `rule_id`; `role == "slot"` whose `binds_to` is a member of the S4 `uses`
    footprint; or `role == "slot"` with `binds_to is None`, a WINDOWED slot type that the
    runtime's `SlotSpec.parse` REFUSES a `binds_to` for because it consumes no column domain. The
    last predicate is `is None`, not falsiness — `""` is a value `SlotSpec.parse` refuses — since
    mirroring the operator rather than the sentence is what keeps the two from drifting.
    Everything else is ungrounded.

    An EMPTY parameterization is fully grounded (1.0), not undefined: the measure is what share
    of this template's literals are still hardcoded, and a query with no literal predicates has
    none. Returns `(share, measured)`, where `measured` is False only when the candidate carries
    no parameterization FIELD at all — which is different from an empty list.
    """
    payload = env.payload
    if not isinstance(payload, Mapping):
        return _UNMEASURED, False
    raw = payload.get("parameterization")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return _UNMEASURED, False

    uses = _uses_footprint(env)
    total = 0
    grounded = 0
    for entry in raw:
        if not isinstance(entry, Mapping):
            # Untrusted rehydrated JSON: a non-mapping member is counted as a literal we
            # cannot vouch for, never skipped. Skipping would let a corrupted entry
            # RAISE the score by shrinking the denominator.
            total += 1
            continue
        total += 1
        if _entry_is_grounded(entry, uses):
            grounded += 1
    if total == 0:
        return 1.0, True
    return grounded / total, True


def _entry_is_grounded(entry: Mapping[str, Any], uses: frozenset[str]) -> bool:
    role = entry.get("role")
    if role == "rule":
        rule_id = entry.get("rule_id")
        return isinstance(rule_id, str) and bool(rule_id.strip())
    if role == "slot":
        slot = entry.get("slot")
        if not isinstance(slot, Mapping):
            return False
        binds_to = slot.get("binds_to")
        if binds_to is None:
            return True  # a windowed slot consumes no column domain — see the docstring
        return isinstance(binds_to, str) and binds_to in uses
    return False  # `inline`, or an unrecognized role


def _uses_footprint(env: CandidateEnvelope) -> frozenset[str]:
    """The S4-declared `database.table.column` scope keys, as a membership set.

    Read off `payload["generalization"]["uses"]` rather than reconstructed, because that is the
    authored list the loader validates and the golden-replay token is minted from. A missing or
    malformed generalization yields an EMPTY set, which makes every slot ungrounded — the
    fail-closed direction, where an unrankable candidate sinks rather than floats.
    """
    gen = env.payload.get("generalization") if isinstance(env.payload, Mapping) else None
    if not isinstance(gen, Mapping):
        return frozenset()
    uses = gen.get("uses")
    if not isinstance(uses, Sequence) or isinstance(uses, (str, bytes)):
        return frozenset()
    return frozenset(u for u in uses if isinstance(u, str))


def session_quality(env: CandidateEnvelope) -> tuple[float, bool]:
    """How clean the session that produced this candidate was.

    `acceptance × struggle × corrected`, all in `(0, 1]`. ACCEPTANCE ranks an explicit
    confirmation above the mere absence of a complaint, which outranks no detected acceptance at
    all. STRUGGLE charges one unit per failed-then-fixed query, per clarifying question and per
    turn beyond the first, discounted hyperbolically, so a single-shot accepted session scores
    exactly 1.0. CORRECTED halves the score when the session had to CORRECT an existing
    blueprint — the only signal here about the corpus rather than the analyst. Unmeasured
    (neutral 1.0) for a candidate extracted before `SessionSignals` existed, whose summary is long
    gone and cannot be re-read.
    """
    signals = env.session_signals
    if signals is None:
        return _UNMEASURED, False
    acceptance = _ACCEPTANCE_WEIGHT.get(signals.accepted_signal, _UNACCEPTED_WEIGHT)
    units = (
        signals.failed_fixed_count
        + signals.askuser_count
        + max(0, signals.turn_count - 1)
    )
    struggle = 1.0 / (1.0 + _STRUGGLE_WEIGHT * units)
    corrected = _CORRECTED_PENALTY if signals.corrected_blueprint else 1.0
    return acceptance * struggle * corrected, True


def novelty(env: CandidateEnvelope) -> tuple[float, bool]:
    """Distance from the closest LANDED artifact, as stamped by S6 dedup.

    Not recomputed here on purpose: recomputing would cost an embed plus an ANN query per row per
    page view and — worse — would answer a different question than the one routing was decided
    on, because the graph moves. The stamp is the novelty AT THE MOMENT we decided to ask a
    human, which is the honest thing to rank on. Unmeasured (neutral 1.0) when no stamp exists.
    """
    stamp = env.novelty
    if stamp is None or not stamp.measured:
        return _UNMEASURED, False
    return stamp.novelty, True


def review_score(env: CandidateEnvelope) -> RankedScore:
    """The composite review score for one candidate — `[0.0, 1.0]`, higher first.

    `gated_novelty × groundedness × quality`, where `gated_novelty = novelty × groundedness`. See
    the module docstring for why groundedness appears twice.
    """
    nov, nov_measured = novelty(env)
    ground, ground_measured = groundedness(env)
    quality, quality_measured = session_quality(env)
    gated_novelty = nov * ground
    return RankedScore(
        score=gated_novelty * ground * quality,
        novelty=nov,
        groundedness=ground,
        session_quality=quality,
        novelty_measured=nov_measured,
        quality_measured=quality_measured,
        groundedness_measured=ground_measured,
    )


def rank_key(env: CandidateEnvelope) -> tuple[int, float, str]:
    """Sort key for the review queue: MEASURED FIRST, then score descending, then `created_at`.

    The partition comes BEFORE the score, and it fixes an inversion a plain score-sort has: an
    unmeasured axis contributes the neutral 1.0 while novelty's measured ceiling is ~0.47, so a
    candidate nobody could measure would outrank every candidate we actually know something
    about, permanently. The realistic population makes that the DEFAULT rather than an edge case —
    a writer-routed `global_knowledge` item has two of its three axes neutral by construction.
    Unmeasured rows are ordered among THEMSELVES by the same score, which is meaningful within
    the block and meaningless across it.

    The final tiebreak is load-bearing and is NOT the candidate id: at the shipped weights many
    candidates score identically, and arrival order preserves the FIFO drain the inbox promised
    before ranking existed. `created_at` is COERCED because of the OPERATION, not the field —
    `from_doc` reads it with a bare `doc.get`, and inside `sorted` it is compared ONLY when the
    score ties, so a non-str would be a tie-dependent `TypeError` appearing the day two rows
    happen to score the same and never before.
    """
    created = env.created_at if isinstance(env.created_at, str) else ""
    scored = review_score(env)
    return (0 if scored.measured else 1, -scored.score, created)


__all__ = [
    "RankedScore",
    "groundedness",
    "novelty",
    "rank_key",
    "review_score",
    "session_quality",
]
