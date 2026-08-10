"""S9-silent-eligibility-predicate (contracts-design §9 row 11, D43).

`silent_eligible ⇔ status == validated AND drift.status == clean AND
fresh(last_drift_check_at)`. Every leg is load-bearing: a candidate, a
non-clean drift, or a stale check each disqualifies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.promotion import PromotionPolicy, silent_eligible

from .helpers import make_blueprint_candidate

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
POLICY = PromotionPolicy(drift_freshness_seconds=86_400.0)  # 24h window


def _drift(status: str, *, ago_seconds: float) -> DriftStamp:
    checked = datetime.fromtimestamp(NOW.timestamp() - ago_seconds, tz=UTC)
    return DriftStamp(
        status=status,
        last_drift_check_at=checked.isoformat(),
        probes=("grain_integrity",),
        failed_probe=None if status == "clean" else "grain_integrity",
    )


def test_validated_clean_fresh_is_silent_eligible():
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, drift=_drift("clean", ago_seconds=60)
    )
    assert silent_eligible(env, now=NOW, policy=POLICY) is True


def test_validated_clean_but_stale_is_not_eligible():
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED,
        drift=_drift("clean", ago_seconds=90_000),  # > 24h — stale
    )
    assert silent_eligible(env, now=NOW, policy=POLICY) is False


def test_validated_suspect_fresh_is_not_eligible():
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, drift=_drift("suspect", ago_seconds=60)
    )
    assert silent_eligible(env, now=NOW, policy=POLICY) is False


def test_candidate_clean_fresh_is_not_eligible():
    """Not `validated` ⇒ not silent-eligible, even with a clean fresh drift."""
    env = make_blueprint_candidate(
        status=CandidateStatus.CANDIDATE, drift=_drift("clean", ago_seconds=60)
    )
    assert silent_eligible(env, now=NOW, policy=POLICY) is False


def test_validated_unchecked_default_drift_is_not_eligible():
    """An `unchecked` drift (no `last_drift_check_at`) is never fresh ⇒ not
    silent-eligible (fail-closed)."""
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, drift=DriftStamp()
    )
    assert silent_eligible(env, now=NOW, policy=POLICY) is False


@pytest.mark.parametrize("bad", [1_780_000_000, 12.5, {"at": "now"}, ["now"], True])
def test_a_non_string_drift_timestamp_is_not_fresh_and_does_not_crash(bad):
    """The freshness leg reads a value straight out of `DriftStamp.from_doc`, which is
    a bare `doc.get("last_drift_check_at")` over rehydrated JSON from a store other
    processes (and humans, via cbq) can write.

    `datetime.fromisoformat` raises **TypeError** on an int/dict/list, not the
    ValueError the parse was guarding, so before the type check a hand-edited or
    machine-written non-string stamp was an uncaught crash — on the runtime silent
    path here, and (since the promotion scheduler now consults the same helper to
    decide whether to re-probe) inside the cron scan too. It must read as "no usable
    stamp" and fail closed, exactly like a missing one."""
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED,
        drift=DriftStamp(
            status="clean", last_drift_check_at=bad, probes=("grain_integrity",)
        ),
    )
    assert silent_eligible(env, now=NOW, policy=POLICY) is False
