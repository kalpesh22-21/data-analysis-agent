"""Layer-1 fakes + builders for the S9 promotion-scheduler tests.

Infra-free: an in-memory candidate store (reused from the candidate module), a fake
warehouse probe returning the `(row_count, distinct_grain_count, columns)` triple
(NO real ClickHouse), a fake corpus `hit_count` reader, and a fake `depends_on`
resolver. All build from the FROZEN S4 fixture (`s4_enriched_blueprint.json`) so the
tests replay the exact S4 output shape S9 depends on.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import DedupVerdict, DriftStamp
from data_agent.learning.config import LearningSettings
from data_agent.learning.promotion.models import (
    ProbeResult,
    PromotionPolicy,
    policy_from_settings,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def promotion_policy(**overrides: Any) -> PromotionPolicy:
    """THE policy every promotion test builds on — PRODUCTION's, not a literal.

    Roughly fourteen test files used to spell `PromotionPolicy(blueprint_hit_threshold=3)`
    inline. That was safe only while 3 was also the shipped value; the moment plan §4
    moved the shipped threshold to 1 those literals became fourteen assertions about a
    gate production no longer has — all still green, all describing a system that no
    longer exists. Editing the literals would have re-created the same trap at the next
    change.

    So the builder resolves the SHIPPED configuration (`policy_from_settings` over
    `LearningSettings` defaults) and lets a test override only the knob it is actually
    about. A test that pins `blueprint_hit_threshold=` here is making a deliberate
    statement about the corroboration gate itself; every other test gets whatever
    production gets, and fails loudly when that changes.

    `_env_file=None` keeps the unit suite HERMETIC: without it, a developer's `.env` (or
    a CI secret file) would feed real thresholds into these assertions and the suite
    would pass or fail depending on the machine.
    """
    return replace(policy_from_settings(LearningSettings(_env_file=None)), **overrides)

# The frozen S4 single-blueprint result signature column shape → the replay's
# expected columns. A passing replay's probe must return exactly these columns.
SINGLE_COLUMNS = ("total_earnings",)


def _load_single_doc() -> dict[str, Any]:
    data = json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())
    return copy.deepcopy(data["single"]["envelope"])


def make_blueprint_candidate(
    *,
    status: str = CandidateStatus.CANDIDATE,
    canonical_key: str | None = "sha256:single-bp",
    depends_on: tuple[str, ...] = (),
    grain_verifiable: bool = False,
    static_ok: bool = True,
    drift: DriftStamp | None = None,
) -> CandidateEnvelope:
    """Build a single-blueprint candidate from the frozen S4 fixture.

    `canonical_key` seeds the S6 dedup verdict so the scheduler can read hit_count
    (None ⇒ no dedup ⇒ count 0). `grain_verifiable=True` promotes the declared grain
    to a VERIFIABLE grain (columns=["department"]) so the live grain_integrity probe
    actually runs (the fixture's default grain is unverifiable → teeth skipped)."""
    doc = _load_single_doc()
    doc["status"] = status
    doc["depends_on"] = list(depends_on)

    gen = doc["payload"]["generalization"]
    if grain_verifiable:
        gen["result_grain"] = {"columns": ["department"], "verifiable": True}
    if not static_ok:
        gen["static_validation"]["read_only_select"] = False
        gen["static_validation"]["outcome"] = "fail_to_review"
        gen["static_validation"]["reason"] = "not_read_only_select"

    if canonical_key is not None:
        doc["dedup"] = DedupVerdict(
            canonical_key=canonical_key,
            matched_id=None,
            similarity=1.0,
            action="increment",
            layer="hard",
        ).to_doc()
    else:
        doc["dedup"] = None

    if drift is not None:
        doc["drift"] = drift.to_doc()

    return CandidateEnvelope.from_doc(doc)


def with_type(env: CandidateEnvelope, type_: str) -> CandidateEnvelope:
    """Re-type a candidate (e.g. to `global_knowledge`) keeping the rest frozen."""
    return replace(env, type=type_)


class FakeWarehouseProbe:
    """A structure-only warehouse probe (D98). Returns a fixed `ProbeResult` and
    records every replay SQL it was handed (so a test can assert a synthetic —
    never a stored-entity — value was bound, D17)."""

    def __init__(
        self,
        *,
        row_count: int = 1,
        distinct_grain_count: int | None = None,
        columns: tuple[str, ...] = SINGLE_COLUMNS,
    ) -> None:
        self._result = ProbeResult(
            row_count=row_count,
            distinct_grain_count=distinct_grain_count,
            columns=columns,
        )
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        # Every minted column_scope (the blueprint `uses`) handed to the probe, so a
        # test can assert the replay was scoped to the declared footprint (S9 §1.3).
        self.column_scopes: list[tuple[str, ...]] = []

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
        self.calls.append((sql, grain_columns))
        self.column_scopes.append(tuple(column_scope))
        return self._result


class FakeHitCountReader:
    """A fake corpus `hit_count` reader keyed by `canonical_key` (D-OQ1). An
    unknown key reads 0 (a not-yet-landed artifact).

    Also duck-types `RecurrenceCountReader` (plan §4) — the SAME shape the production
    `CouchbaseBlueprintCorpus` has, where one object serves both ports so the two counts
    can never address different artifact sets. *recurrences* defaults to empty, so a test
    that says nothing about the soft counter reads 0 for it, which at the shipped
    `recurrence_weight = 0.0` is what production does anyway."""

    def __init__(
        self,
        counts: dict[str, int] | None = None,
        recurrences: dict[str, int] | None = None,
    ) -> None:
        self._counts = dict(counts or {})
        self._recurrences = dict(recurrences or {})

    async def hit_count(self, canonical_key: str) -> int:
        return self._counts.get(canonical_key, 0)

    async def recurrence_count(self, canonical_key: str) -> int:
        return self._recurrences.get(canonical_key, 0)


class FakeDependencyResolver:
    """A fake `depends_on` resolver. `resolved` is the set of refs that HAVE
    landed; anything else is unresolved (§11.6)."""

    def __init__(self, resolved: set[str] | None = None) -> None:
        self._resolved = set(resolved or ())

    async def is_resolved(self, ref: str) -> bool:
        return ref in self._resolved


class FakeLandingWriter:
    """A Layer-1 `LandingWriter` double (no real neo4j). Records every landed
    candidate envelope (so a test can assert land-BEFORE-status ordering + an
    idempotent re-land), and can be scripted to RAISE on the Nth call (a landing
    failure) so the scheduler HOLDS `landing_failed`."""

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        fail_times: int = 0,
        update_fail: Exception | None = None,
        update_fail_times: int = 0,
        verify_fail: Exception | None = None,
        verify_fail_times: int = 0,
        verify_stamped: bool = True,
    ) -> None:
        self._fail = fail
        self._fail_times = fail_times
        self._update_fail = update_fail
        self._update_fail_times = update_fail_times
        self._verify_fail = verify_fail
        self._verify_fail_times = verify_fail_times
        self._verify_stamped = verify_stamped
        self.landed: list[CandidateEnvelope] = []
        # Every `forbidden_spans` tuple handed to the writer (so a test can assert the
        # scheduler captured the PRE-strip entity spans, D17 last gate).
        self.forbidden_spans: list[tuple[str, ...]] = []
        # Every `verified` flag handed to `land` (Phase-3: auto=False, human-approve=True).
        self.verified_flags: list[bool] = []
        # Every candidate whose landed node was `mark_verified`'d (Phase-3 VERIFY action).
        self.verified: list[CandidateEnvelope] = []
        self.verify_calls = 0
        self.calls = 0
        # Every retraction write-back (S9-activation Slice 3): the (candidate, status,
        # drift_status) the scheduler stamped, so a test can assert the demote/reject/
        # user-correction edges + the clean-rescan re-assert wrote the node back.
        self.status_updates: list[tuple[CandidateEnvelope, str, str]] = []
        self.update_calls = 0

    async def land(
        self,
        env: CandidateEnvelope,
        *,
        forbidden_spans: tuple[str, ...] = (),
        verified: bool = False,
    ) -> None:
        self.calls += 1
        self.forbidden_spans.append(tuple(forbidden_spans))
        self.verified_flags.append(verified)
        if self._fail is not None and self.calls <= self._fail_times:
            raise self._fail
        self.landed.append(env)

    async def update_status(
        self, env: CandidateEnvelope, *, status: str, drift_status: str
    ) -> bool:
        self.update_calls += 1
        if self._update_fail is not None and self.update_calls <= self._update_fail_times:
            raise self._update_fail
        self.status_updates.append((env, status, drift_status))
        return True

    async def mark_verified(self, env: CandidateEnvelope) -> bool:
        self.verify_calls += 1
        if self._verify_fail is not None and self.verify_calls <= self._verify_fail_times:
            raise self._verify_fail
        self.verified.append(env)
        # `verify_stamped=False` models a MATCH-by-id miss (the node was never landed):
        # `mark_verified` runs but stamps nothing, so `node_stamped` reports False.
        return self._verify_stamped
