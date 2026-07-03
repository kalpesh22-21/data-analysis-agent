"""promotion/drift.py — the D43 three drift probes + the silent-eligibility rule.

D43 names three drift probes the scheduler runs over a `validated` artifact:

  1. `grain_integrity`     — LIVE (Phase 1). The result's declared grain still
     holds under a fresh golden replay: `row_count == COUNT(DISTINCT grain)` (the
     reused D56 teeth) + the result_signature column shape. This is exactly the
     `golden_replay` verdict, so `grain_integrity` wraps the full D56 gate.
  2. `catalog_conformance` — STUBBED `unchecked` (Phase 2). Catalog-vs-warehouse
     schema conformance needs a live catalog probe (not wired in Phase 1).
  3. `rule_currency`       — STUBBED `unchecked` (Phase 2). Rule-semantics
     currency needs a live rule-registry probe (not wired in Phase 1).

This mirrors D56's phased note: the D56 grain-integrity gate runs on every result
in Phase 1; the full freshness predicate (catalog + rule probes) is Phase 2. The
`DriftStamp.probes` tuple records WHICH of the three actually ran — in Phase 1 that
is only `("grain_integrity",)`; the two stubbed probes are absent (= not run,
honestly reported, never a silent "clean" claim for a check that did not happen).

`silent_eligible` is the D43 predicate the silent (blueprint fast) path consults:
`status == validated AND drift.status == clean AND fresh(last_drift_check_at)`.
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


def _is_fresh(last_check: str | None, now: datetime, window_seconds: float) -> bool:
    """True iff `last_check` is within `window_seconds` of `now`. A missing or
    unparseable timestamp is NOT fresh (fail-closed — an un-drift-checked artifact
    is never silent-eligible)."""
    if not last_check:
        return False
    try:
        checked = datetime.fromisoformat(last_check)
    except ValueError:
        return False
    if checked.tzinfo is None or now.tzinfo is None:
        return False
    return 0.0 <= (now - checked).total_seconds() <= window_seconds


def silent_eligible(
    env: CandidateEnvelope, *, now: datetime, policy: PromotionPolicy
) -> bool:
    """The D43 silent-eligibility predicate:
    `validated AND drift.status == clean AND fresh(last_drift_check_at)`.

    Any leg false ⇒ not silent-eligible (a candidate, a suspect/stale/unchecked
    drift, or a stale check all disqualify)."""
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
    "silent_eligible",
    "user_correction_stamp",
]
