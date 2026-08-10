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


def _is_fresh(last_check: object, now: datetime, window_seconds: float) -> bool:
    """True iff `last_check` is within `window_seconds` of `now`. A missing,
    unparseable, or WRONG-TYPED timestamp is NOT fresh (fail-closed — an
    un-drift-checked artifact is never silent-eligible, and an unreadable stamp is
    treated as no stamp at all).

    The `isinstance` guard is not defensive padding, it is the actual boundary:
    `last_check` reaches here straight out of `DriftStamp.from_doc`, which is a plain
    `doc.get("last_drift_check_at")` over rehydrated JSON from a store that humans and
    other processes can write. `datetime.fromisoformat(123)` and
    `fromisoformat({"a": 1})` raise **TypeError**, not the ValueError caught below, so
    a non-string stamp would not be a wrong answer — it would be an uncaught crash
    inside the cron scan and inside the silent-path predicate. Type-check first, then
    parse."""
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
    """The stored golden-replay verdict, IF it is still within `window_seconds` and
    therefore safe to reuse instead of paying for a fresh replay.

    Returns `True` (last replay passed), `False` (last replay failed), or `None`
    meaning "no reusable verdict — run the real replay". `None` is the fail-safe
    default: every ambiguous case degrades to today's behaviour (probe the warehouse),
    never to a fabricated pass.

    The `GRAIN_INTEGRITY in drift.probes` test is what keeps this honest. A `suspect`
    stamp is NOT proof that a replay ran and failed — `user_correction_stamp` writes
    `suspect` with `probes=()` because a human said the answer was wrong, which says
    nothing about whether the template still executes. Reusing that as "the replay
    failed" would suppress the real structural check for a whole window. Only a stamp
    that NAMES the live `grain_integrity` probe is a replay verdict.

    That membership test is also why `probes` is read with `in` and not, say, indexed:
    `DriftStamp.from_doc` does `tuple(doc.get("probes", []) or [])`, so a doc holding
    the string `"grain_integrity"` rehydrates as a tuple of 15 single CHARACTERS. `in`
    on that is simply False → `None` → the replay runs. A malformed stamp costs one
    unnecessary probe; it can never fabricate a verdict."""
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
    "reusable_replay_verdict",
    "silent_eligible",
    "user_correction_stamp",
]
