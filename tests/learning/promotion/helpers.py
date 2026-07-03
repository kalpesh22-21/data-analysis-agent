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
from data_agent.learning.promotion.models import ProbeResult

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

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

    async def run(self, sql: str, *, grain_columns: tuple[str, ...]) -> ProbeResult:
        self.calls.append((sql, grain_columns))
        return self._result


class FakeHitCountReader:
    """A fake corpus `hit_count` reader keyed by `canonical_key` (D-OQ1). An
    unknown key reads 0 (a not-yet-landed artifact)."""

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self._counts = dict(counts or {})

    async def hit_count(self, canonical_key: str) -> int:
        return self._counts.get(canonical_key, 0)


class FakeDependencyResolver:
    """A fake `depends_on` resolver. `resolved` is the set of refs that HAVE
    landed; anything else is unresolved (§11.6)."""

    def __init__(self, resolved: set[str] | None = None) -> None:
        self._resolved = set(resolved or ())

    async def is_resolved(self, ref: str) -> bool:
        return ref in self._resolved
