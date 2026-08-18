"""promotion/drift.py — the D43 three drift probes + the silent-eligibility rule.

Only `grain_integrity` is LIVE (Phase 1): it wraps the full D56 gate via `golden_replay`.
`catalog_conformance` and `rule_currency` are STUBBED `unchecked` pending live catalog and
rule-registry probes. `DriftStamp.probes` records WHICH probes actually ran, so a stubbed
one is ABSENT — never a silent "clean" claim for a check that did not happen.
`silent_eligible` is the D43 predicate the blueprint fast path consults.
"""

from __future__ import annotations

from datetime import datetime

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.verdicts import DriftStamp
from .models import PromotionPolicy
from .replay import ReplayOutcome

# D43 probe ids.
GRAIN_INTEGRITY = "grain_integrity"
CATALOG_CONFORMANCE = "catalog_conformance"
RULE_CURRENCY = "rule_currency"

# Which probes are LIVE vs STUBBED in Phase 1 (documented, not silently assumed).
LIVE_PROBES: tuple[str, ...] = (GRAIN_INTEGRITY,)
STUBBED_PROBES: tuple[str, ...] = (CATALOG_CONFORMANCE, RULE_CURRENCY)

# The demotion cause stamped when a live user correction (not a probe) demotes a
# validated artifact — it is NOT one of the three probes, so `failed_probe` stays
# `None` and the review flag is carried by `status=suspect` + `candidate` status.
USER_CORRECTION = "user_correction"


def drift_from_replay(replay: ReplayOutcome, *, now: str) -> DriftStamp:
    """Build the `DriftStamp` from a golden replay (the live `grain_integrity`
    probe). A passing replay ⇒ `clean`; a failing one ⇒ `suspect` naming
    `grain_integrity` as the failed probe. The two Phase-2 probes are stubbed
    (absent from `probes`), so `probes == ("grain_integrity",)` either way."""
    if replay.passed:
        return DriftStamp(
            status="clean",
            last_drift_check_at=now,
            probes=(GRAIN_INTEGRITY,),
            failed_probe=None,
        )
    return DriftStamp(
        status="suspect",
        last_drift_check_at=now,
        probes=(GRAIN_INTEGRITY,),
        failed_probe=GRAIN_INTEGRITY,
    )


def user_correction_stamp(*, now: str) -> DriftStamp:
    """The drift stamp for a user-correction demotion (a negative signal, not a
    probe). `suspect` carries the review flag; `failed_probe` is `None` because no
    probe fired (the cause is an out-of-band correction, D43)."""
    return DriftStamp(
        status="suspect",
        last_drift_check_at=now,
        probes=(),
        failed_probe=None,
    )


def _is_fresh(last_check: object, now: datetime, window_seconds: float) -> bool:
    """True iff `last_check` is within `window_seconds` of `now`.

    A missing, unparseable or WRONG-TYPED timestamp is NOT fresh (fail-closed). The `isinstance`
    guard is the actual boundary, not defensive padding: `last_check` arrives straight out of a
    plain `doc.get` over rehydrated JSON that humans and other processes can write, and
    `datetime.fromisoformat(123)` raises TypeError rather than the ValueError caught below — an
    uncaught crash inside the cron scan. Type-check first, then parse.
    """
    if not isinstance(last_check, str) or not last_check:
        return False
    try:
        checked = datetime.fromisoformat(last_check)
    except ValueError:
        return False
    if checked.tzinfo is None or now.tzinfo is None:
        return False
    return 0.0 <= (now - checked).total_seconds() <= window_seconds


def reusable_replay_verdict(
    drift: DriftStamp, *, now: datetime, window_seconds: float
) -> bool | None:
    """The stored golden-replay verdict, IF it is still fresh enough to reuse.

    `True`/`False` for the last replay's result, or `None` meaning "run the real replay" — the
    fail-safe default every ambiguous case degrades to, never a fabricated pass. The
    `GRAIN_INTEGRITY in drift.probes` test is what keeps it honest: `user_correction_stamp`
    writes `suspect` with `probes=()` because a human said the answer was wrong, which says
    nothing about whether the template still executes, so reusing that as "the replay failed"
    would suppress the real structural check for a whole window. It is read with `in` rather
    than indexed because a doc holding the bare string rehydrates as a tuple of single
    CHARACTERS — simply False, costing one unnecessary probe and never a fabricated verdict.
    """
    if GRAIN_INTEGRITY not in drift.probes:
        return None
    if not _is_fresh(drift.last_drift_check_at, now, window_seconds):
        return None
    if drift.status == "clean":
        return True
    if drift.status == "suspect":
        return False
    return None


def silent_eligible(
    env: CandidateEnvelope, *, now: datetime, policy: PromotionPolicy
) -> bool:
    """The D43 silent-eligibility predicate.

    `validated AND drift.status == clean AND fresh(last_drift_check_at)` — any leg false ⇒ not
    silent-eligible.
    """
    if env.status != CandidateStatus.VALIDATED:
        return False
    if env.drift.status != "clean":
        return False
    return _is_fresh(
        env.drift.last_drift_check_at, now, policy.drift_freshness_seconds
    )


__all__ = [
    "CATALOG_CONFORMANCE",
    "GRAIN_INTEGRITY",
    "LIVE_PROBES",
    "RULE_CURRENCY",
    "STUBBED_PROBES",
    "USER_CORRECTION",
    "drift_from_replay",
    "reusable_replay_verdict",
    "silent_eligible",
    "user_correction_stamp",
]
