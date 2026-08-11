"""Inbox ranking — `novelty × groundedness² × session-quality` (plan §4).

The squared term is not a typo and not an implementation detail: the plan words the axes
as "novelty × groundedness × session-quality", and implementing that literally is the one
way to get this wrong. See "Groundedness enters TWICE" below.

**Why a ranking exists at all.** Until this slice the promotion gate required three
sessions to converge on a byte-identical normalized AST, which has never happened, so
nothing reached the inbox by the auto path. With that gate at 1 the queue becomes the
place work accumulates, and the question changes from *"is this correct enough to
trust?"* — which the human verify/promote step answers, and which every correctness
guard still enforces — to *"is this worth thirty seconds of a human's time?"*

The three axes, and what each is actually computed from:

  * **groundedness** — the share of the candidate's `parameterization` that resolves to
    a catalog RULE or to a KNOWN COLUMN (a slot whose `binds_to` is inside the
    blueprint's declared `uses` footprint), as against free-floating `inline` literals.
    A blueprint whose filters are all hardcoded values is a one-off transcription; one
    whose filters are all slots and rules is a reusable template. Computable today, from
    fields S3 and S4 already write.
  * **session-quality** — how the session that produced it went, from the
    `SessionSignals` stamped at extraction: a clean single-shot acceptance versus a long
    struggle, with a CORRECTED blueprint as an explicit negative.
  * **novelty** — how far the intent sits from the closest LANDED artifact, from the
    `NoveltyStamp` the dedup stage writes. Measured against landed artifacts only, never
    against sibling candidates; see `candidate/signals.py::NoveltyStamp`.

**Groundedness enters TWICE, and the asymmetry is the whole point.** A plain product
`novelty × groundedness × quality` is symmetric in its first two terms, so a hardcoded
one-off that nothing resembles (novelty 1.0, groundedness 0.1) would rank identically to
a well-parameterized template of a familiar question (0.1, 1.0). The plan is explicit
that the second is the better use of a reviewer's time and the first is usually just
idiosyncratic. So novelty is GATED: it is scaled by groundedness before it enters the
product, which makes the score `novelty × groundedness² × quality` and pushes the
ungrounded outlier down by an order of magnitude while leaving the grounded-but-familiar
candidate where it was.

**Everything degrades to a NUMBER, never to an exception.** This runs inside a list
projection a human is waiting on, over rehydrated JSON from a store other things write.
A missing stamp yields the documented neutral for that axis, and `components` reports
which axes were actually measured so an operator can tell a ranked queue from an
arbitrary one.

**THE SCORE IS AN ORDERING, NOT A PERCENTAGE — and it does not use the top of its range.**
Measured against the live dev corpus (10 canon blueprints, `all-mpnet-base-v2`, via the
real `Neo4jPriorArtIndex`):

    query                                    best cosine   novelty
    an exact re-derivation of a landed intent   0.9998       0.0002
    a plausible NEW HR question                 0.7411       0.2589
    a genuinely unrelated question              0.5869       0.4131
    total nonsense                              0.5342       0.4658

Sentence-embedding cosines over English prose have a high floor — two texts about nothing
in common still score ~0.53 — so `novelty = 1 - cosine` occupies roughly `[0.0, 0.47]`,
not `[0.0, 1.0]`. A perfectly grounded, cleanly accepted, genuinely novel candidate
therefore scores about **0.26**, and the ceiling of the whole scale in practice is under
0.5.

Two consequences, both easy to get wrong:

  * **`review_score_cutoff` must be set from measured data, never from intuition.** A
    "half decent" cutoff of 0.5 would hide the ENTIRE queue — which is why the shipped
    default is 0.0 and why the knob is applied to a listing rather than to routing, where
    the same mistake would silently discard work.
  * **Do not render the score to a reviewer as a confidence or a percentage.** It ranks;
    it does not measure. The `*_measured` flags are what a UI should surface.
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

    The components travel to the review UI so a reviewer can see WHY a row is where it
    is. A bare float would make the ordering unfalsifiable — the single most common way a
    ranking quietly stops working is that nobody can tell it has."""

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
    """The share of this candidate's parameterization that is GROUNDED, and whether it
    could be computed at all.

    A parameterization entry is grounded when it resolves to something the catalog knows:

      * `role == "rule"` with a non-empty `rule_id` — a catalog rule the blueprint
        declares and the loader re-validates;
      * `role == "slot"` whose `binds_to` is a member of the S4 `uses` footprint — a real
        `database.table.column` the blueprint declares it reads;
      * `role == "slot"` with `binds_to is None` — a WINDOWED slot type
        (`relative_window`), which the runtime's `SlotSpec.parse` REFUSES a `binds_to`
        for because it consumes no column domain. Absent is the CORRECT value there, so
        treating it as ungrounded would penalise the one slot type that is right by
        construction. Note the predicate is `is None`, not falsiness: `""` is a value
        `SlotSpec.parse` refuses, so it is ungrounded, and mirroring the operator rather
        than the sentence is what keeps the two from drifting.

    Everything else is ungrounded: an `inline` literal (a value baked into the template),
    a `rule` with no id, a slot pointing outside `uses`, or a malformed entry.

    **An EMPTY parameterization is fully grounded (1.0), not undefined.** The measure is
    "what share of this template's literals are still hardcoded"; a query with no literal
    predicates at all (`SELECT count(*) FROM employees`) has none hardcoded. Returning a
    low score there would push the most general blueprints to the bottom of the queue.

    Returns `(share, measured)`. `measured` is False only when the candidate carries no
    parameterization FIELD at all — a non-blueprint, or a payload shape this function has
    no opinion about — which is different from an empty list.
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

    Read off `payload["generalization"]["uses"]` rather than reconstructed, because that
    is the authored list the loader validates and the golden-replay token is minted from
    — the same list the runtime treats as this blueprint's footprint. A missing or
    malformed generalization yields an EMPTY set, which makes every slot ungrounded; that
    is the fail-closed direction (an unrankable candidate sinks rather than floats)."""
    gen = env.payload.get("generalization") if isinstance(env.payload, Mapping) else None
    if not isinstance(gen, Mapping):
        return frozenset()
    uses = gen.get("uses")
    if not isinstance(uses, Sequence) or isinstance(uses, (str, bytes)):
        return frozenset()
    return frozenset(u for u in uses if isinstance(u, str))


def session_quality(env: CandidateEnvelope) -> tuple[float, bool]:
    """How clean the session that produced this candidate was.

    `acceptance × struggle × corrected`, all in `(0, 1]`:

      * ACCEPTANCE — an explicit confirmation outranks the mere absence of a complaint,
        which outranks no detected acceptance at all.
      * STRUGGLE — one unit per failed-then-fixed query, per clarifying question, and per
        turn beyond the first, discounted hyperbolically. A single-shot accepted session
        scores exactly 1.0 here, which is the reference point the plan names.
      * CORRECTED — halved when the session had to CORRECT an existing blueprint. The
        plan calls this out as a negative specifically, and it is the only signal here
        that is about the corpus rather than about the analyst.

    Returns `(quality, measured)`. Unmeasured (neutral 1.0) for a candidate extracted
    before `SessionSignals` existed — the summary it was derived from is long gone and
    cannot be re-read."""
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

    Not recomputed here on purpose. Recomputing would mean an embed plus an ANN query per
    row per page view, and — worse — it would answer a different question than the one
    routing was decided on, because the graph moves. The stamp is the novelty AT THE
    MOMENT we decided to ask a human, which is the honest thing to rank on.

    Unmeasured (neutral 1.0) when no stamp exists: no prior-art index wired, the graph was
    unreachable, or the candidate never reached the soft layer."""
    stamp = env.novelty
    if stamp is None or not stamp.measured:
        return _UNMEASURED, False
    return stamp.novelty, True


def review_score(env: CandidateEnvelope) -> RankedScore:
    """The composite review score for one candidate — `[0.0, 1.0]`, higher first.

    `gated_novelty × groundedness × quality`, where `gated_novelty = novelty ×
    groundedness`. See the module docstring for why groundedness appears twice."""
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
    """Sort key for the review queue: MEASURED FIRST, then score descending, then
    `created_at` ascending.

    **The partition comes before the score, and it is the fix for an inversion a plain
    score-sort has.** An unmeasured axis contributes the neutral 1.0, and novelty's
    measured ceiling against a real corpus is ~0.47 — so a candidate nobody could measure
    outranks every candidate we actually know something about, permanently. The realistic
    population makes that the DEFAULT rather than an edge case: a writer-routed
    `global_knowledge` item has no dedup verdict and no parameterization, so two of its
    three axes are neutral by construction. Comparing the two groups on one number
    compares a measurement against a placeholder; putting the placeholders in a second
    block does not.

    Unmeasured rows are ordered among THEMSELVES by the same score, which is meaningful
    within the block (they share the same neutrals) and meaningless across it.

    The final tiebreak is load-bearing and is NOT the candidate id. At the shipped weights
    a great many candidates score identically, so without a meaningful tiebreak the
    queue's order would be whatever the store happened to return — and a FIFO drain is
    what the inbox promised before ranking existed. Falling back to arrival order
    preserves that for every tie.

    **`created_at` is coerced, and the reason is the operation, not the field.**
    `CandidateEnvelope.from_doc` reads it with a bare `doc.get(...)` and no type check, so
    a hand-edited or foreign-written document can put anything there. Inside `sorted` the
    second element is compared ONLY when the first ties — so a non-str would be a
    TIE-DEPENDENT `TypeError`, i.e. a crash that appears the day two candidates happen to
    score the same and never before. That is the same latent load-dependent shape a
    previous slice found in `_best_card`; a non-str sorts as `""` (first) instead."""
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
