"""Durable, entity-free ranking inputs stamped onto a `CandidateEnvelope`.

Both exist because the inbox ranking needs facts knowable only at a moment the inbox no
longer has access to. `SessionSignals` is derived from the `SessionSummary`, which lives in
memory for one extraction and is then dropped, so a struggle signal not stamped here does not
exist. `NoveltyStamp` is measured at S6 dedup time, the one place that has already paid for
an embed and an ANN query.

BOTH ARE SCALARS ONLY — a hard constraint: these travel to the review UI through `InboxItem`,
the surface the D17 redaction protects, and a count, a bool, an enum member and a float carry
no entity. BOTH ARE OPTIONAL and every reader must tolerate absence, because `None` and a
zero-valued stamp are DIFFERENT claims: `turn_count=0` says the loader saw a session with no
turns, `None` says nobody looked; `novelty=0.0, measured=False` says we could not look,
`measured=True` says an identical artifact is already landed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-cycle avoidance only
    from ..summary.models import SessionSummary


def _count(raw: Any) -> int:
    """A rehydrated count as a non-negative `int`, or 0.

    NOT `int(raw)`: this doc comes back out of Couchbase, where a hand edit can put anything in
    it, and `int("12")`/`int(1.9)` would silently invent a value while `int({})` raises inside a
    queue worker. `bool` is excluded because it is an `int` subclass and `True` is not a count.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw >= 0 else 0


def _unit(raw: Any) -> float:
    """A rehydrated score clamped into `[0.0, 1.0]`, or 0.0 for anything unusable.

    Derived from what the reader DOES with it: `ranking.review_score` multiplies these together
    and the product is a sort key compared against a cutoff. A `nan` would make the sort order
    depend on the comparison direction, and an out-of-range value would let one candidate
    dominate the queue.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    if value != value:  # nan — the one float that breaks a sort key
        return 0.0
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class SessionSignals:
    """How the session that produced this candidate went — the session-quality axis.

    Every field is a shape fact about the transcript, never its content. Stamped once, at
    `build_envelope`, from the `SessionSummary` the consumer already holds.
    """

    # The S2 loader's acceptance verdict verbatim (`no_correction` | `explicit_confirm` |
    # `thumbs_up`), or `None` when no acceptance was detected. Carried as a bare `str`
    # rather than the `AcceptedSignal` Literal because it round-trips through JSON and a
    # foreign value must not raise on rehydrate — `ranking` treats an unknown spelling as
    # the weakest bucket.
    accepted_signal: str | None = None
    turn_count: int = 0
    # Sessions where a query failed and a later one succeeded (`SessionSummary.
    # failed_fixed_sql`) — the clearest machine-readable "the analyst had to fight it".
    failed_fixed_count: int = 0
    # Clarifying questions the agent had to ask. Not a failure, but not a single shot.
    askuser_count: int = 0
    # A `BlueprintUsage.outcome == "corrected"`: the agent ran an EXISTING blueprint and
    # the human then corrected it. That is a negative signal about the corpus, and the
    # plan calls it out specifically — a session that had to correct a blueprint is a
    # poor template for minting another one.
    corrected_blueprint: bool = False

    @classmethod
    def from_summary(cls, summary: SessionSummary) -> SessionSignals:
        """Derive the stamp from the in-memory summary (the only place it exists).

        Reads only lengths, a bool and one enum member — no `user_nl`, no `assistant_text`, no
        `args`, no SQL. That is what makes the result safe to put on an envelope the review UI
        renders.
        """
        return cls(
            accepted_signal=summary.accepted_signal,
            turn_count=len(summary.turns),
            failed_fixed_count=len(summary.failed_fixed_sql),
            askuser_count=len(summary.askuser_exchanges),
            corrected_blueprint=any(
                usage.outcome == "corrected" for usage in summary.blueprint_usages
            ),
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "accepted_signal": self.accepted_signal,
            "turn_count": self.turn_count,
            "failed_fixed_count": self.failed_fixed_count,
            "askuser_count": self.askuser_count,
            "corrected_blueprint": self.corrected_blueprint,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> SessionSignals:
        raw_signal = doc.get("accepted_signal")
        return cls(
            accepted_signal=raw_signal if isinstance(raw_signal, str) else None,
            turn_count=_count(doc.get("turn_count")),
            failed_fixed_count=_count(doc.get("failed_fixed_count")),
            askuser_count=_count(doc.get("askuser_count")),
            corrected_blueprint=bool(doc.get("corrected_blueprint")),
        )


@dataclass(frozen=True)
class NoveltyStamp:
    """How far this candidate's intent sits from the closest LANDED artifact.

    LANDED, not sibling: `novelty` comes exclusively from the GRAPH half of the S6 soft union.
    The `learning_corpus` half is deliberately excluded even though dedup has it in hand, because
    including it would score the FIRST sighting of an idea as novel and each of its
    corroborations as redundant — order-dependent and backwards, since corroboration is evidence
    FOR an idea. `measured=False` means the question was not answered, NOT that nothing was
    found.
    """

    novelty: float = 0.0
    measured: bool = False
    # How many landed artifacts the measurement considered. Zero with `measured=True` is a
    # real and common answer on an empty graph ("nothing has landed, so everything is
    # novel"), and it is worth being able to tell that apart from a crowded corpus in
    # which the candidate happened to be far from everything.
    compared_against: int = 0

    @classmethod
    def from_best_similarity(cls, best: float, *, compared_against: int) -> NoveltyStamp:
        """Novelty as the complement of the closest landed artifact's confidence."""
        return cls(
            novelty=_unit(1.0 - _unit(best)),
            measured=True,
            compared_against=max(0, compared_against),
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "novelty": self.novelty,
            "measured": self.measured,
            "compared_against": self.compared_against,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> NoveltyStamp:
        return cls(
            novelty=_unit(doc.get("novelty")),
            measured=bool(doc.get("measured")),
            compared_against=_count(doc.get("compared_against")),
        )


__all__ = ["NoveltyStamp", "SessionSignals"]
