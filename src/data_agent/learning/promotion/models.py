"""promotion/models.py — the S9 scheduler's typed value objects + injected ports.

S9 (the separate cron-scanned promotion scheduler, D29/§7.2) is NOT a
`CandidateStage`; it reads `learning_candidates` by `status`, runs golden replay +
the D43 drift probes, and advances `status` + stamps `drift` (Contract E). This
module freezes the scheduler's own data contracts:

  * `PromotionPolicy`  — the tunable promotion knobs (hit-count threshold T,
    drift freshness window). Provisional defaults per the D-OQ1 resolution
    (`blueprint_promotion_hit_threshold = 3`); wired as config, not constants.
  * `ProbeResult`      — what an injected warehouse probe returns for a golden
    replay: `(row_count, distinct_grain_count, columns)` — the exact input the
    reused D56 `verify_result` gate consumes. NEVER the returned value/number
    (D98 — replay verifies STRUCTURE, not values; no value oracle, D17).
  * `CandidateDecision`/`PromotionSweep` — the per-candidate outcome + the
    per-cycle report (observability + Layer-1 assertions).

The three injected PORTS (`WarehouseProbe`, `HitCountReader`, `DependencyResolver`)
are Protocols so Layer-1 fakes and the live stack use the identical path — no real
ClickHouse, no real corpus store in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ..candidate.models import CandidateEnvelope

# Targets S9 NEVER auto-promotes by count (T = ∞ — human-gated, D58a/D18). A
# blueprint auto-promotes at `blueprint_hit_threshold`; `user_knowledge`
# auto-commits in its OWN writer (S8), never through this scheduler.
HUMAN_GATED_TYPES: frozenset[str] = frozenset({"global_knowledge", "schema_edit"})
BLUEPRINT_TYPE = "blueprint"

# A `CandidateDecision.action` — the transition (or non-transition) the scheduler
# applied to one candidate this cycle.
DecisionAction = Literal[
    "promote",  # candidate → validated (guards all passed)
    "hold",  # stays candidate (below threshold / replay-fail / deps unresolved / …)
    "demote",  # validated → candidate (drift suspect / replay-fail / user correction)
    "drift_clean",  # validated stays validated; drift re-stamped clean+fresh
    "approve",  # in_review → validated (human approval)
    "reject",  # in_review → rejected (human reject)
    "retire",  # * → retired (leaked/stale — out of Phase-1 scope; reserved)
    "verify",  # validated stays validated; verified flag flipped true (Phase-3 inbox)
    "promote_emit",  # validated → promoted (Phase-3 inbox — MCP YAML emitted for a PR)
    "skip",  # nothing to do (e.g. a non-replayable validated knowledge artifact)
]


@dataclass(frozen=True)
class PromotionPolicy:
    """Tunable promotion knobs (config, not constants — D-OQ1 posture)."""

    # T for blueprints (D-OQ1 provisional). global_knowledge/schema_edit are T=∞
    # (HUMAN_GATED_TYPES); user_knowledge auto-commits elsewhere (S8).
    blueprint_hit_threshold: int = 3
    # `fresh(last_drift_check_at)` window for the silent-eligibility predicate.
    drift_freshness_seconds: float = 86_400.0  # 24h (provisional)
    # Max candidates scanned per status per cycle (bounds one sweep).
    scan_limit: int = 200
    # run_forever cadence (its OWN knob, mirroring the sweeper's interval).
    promotion_interval_seconds: float = 300.0


@dataclass(frozen=True)
class ProbeResult:
    """What an injected `WarehouseProbe` returns for one golden replay — the exact
    `verify_result` (D56) input triple. `distinct_grain_count` is `None` when the
    grain check is skipped (empty/unverifiable grain). NEVER carries the result
    VALUE/number (D98/D17 — no value oracle)."""

    row_count: int
    distinct_grain_count: int | None
    columns: tuple[str, ...]


@dataclass(frozen=True)
class CandidateDecision:
    """One candidate's outcome this cycle (observability + test assertions)."""

    candidate_id: str
    type: str
    action: DecisionAction
    from_status: str
    to_status: str
    reason: str | None = None  # stable machine tag (e.g. "below_hit_threshold")


@dataclass(frozen=True)
class PromotionSweep:
    """The outcome of one `run_once` cycle — the per-candidate decisions + a
    `disabled` flag (kill-switch, mirroring `SweepResult`)."""

    decisions: tuple[CandidateDecision, ...] = ()
    disabled: bool = False

    def _count(self, action: DecisionAction) -> int:
        return sum(1 for d in self.decisions if d.action == action)

    @property
    def promoted(self) -> int:
        return self._count("promote")

    @property
    def demoted(self) -> int:
        return self._count("demote")

    @property
    def held(self) -> int:
        return self._count("hold")

    @property
    def drift_clean(self) -> int:
        return self._count("drift_clean")


# --- injected ports (Protocols; Layer-1 fakes = live path) --------------------


class WarehouseProbe(Protocol):
    """Runs a golden-replay SQL against the warehouse and returns the D56 probe
    triple. Injected/fake in tests — NO real ClickHouse. The probe is a STRUCTURE
    oracle only (row/distinct/columns); it never returns the result value (D98).

    `column_scope` is the blueprint's declared `uses` footprint (D87) — the real
    probe mints a JWT scoped to EXACTLY it (S9-design §1.3) so the replay reads only
    within the declared footprint and the MCP's D57 teeth reject anything outside it.
    """

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...],
    ) -> ProbeResult: ...


class HitCountReader(Protocol):
    """Reads the cross-session `hit_count` from the LANDED corpus artifact (the
    neo4j blueprint node keyed by `canonical_key`, D-OQ1) — NOT the envelope. S6
    seeds it at 1 on insert and increments on a hard-key hit; S9 reads it for the
    promotion guard."""

    async def hit_count(self, canonical_key: str) -> int: ...


class DependencyResolver(Protocol):
    """Resolves a `depends_on` artifact ref (§11.6). A blueprint depending on a
    not-yet-landed `schema_edit(add_rule)` (D35) stays `candidate` until every ref
    resolves — the S9 `depends_on` guard."""

    async def is_resolved(self, ref: str) -> bool: ...


class LandingWriter(Protocol):
    """Materializes a validated candidate into the neo4j retrieval corpus so it
    becomes RECALLABLE (S9-activation Slice 2, §3). Injected/fake in Layer-1 (no real
    neo4j). `land` MERGE-upserts the blueprint by a deterministic id (idempotent
    re-land) and RAISES on any failure — a model-parity violation, an entity leaking
    into the seed (the last-gate D17 defense), or a neo4j write error — so the
    scheduler HOLDS `landing_failed` and NEVER writes `validated` (not landed ⇒ not
    validated, §3.1). The concrete impl is `promotion/landing.py::CorpusLandingWriter`.

    `forbidden_spans` are the entity spans S5 identified, captured by the scheduler
    BEFORE `strip_entity_bearing` blanks them, so the last-gate defense can fire even
    though a validated candidate's own `entity_scan` is blanked (§3.3).

    `update_status` is the RETRACTION write-back (S9-activation Slice 3, §8.6): a
    demote / reject / user-correction stamps the landed node's `status`/`drift_status`
    (keyed by the SAME deterministic landing id) so the recall filter excludes it, and
    the clean validated-rescan re-asserts `validated`/`clean` (the self-heal). It is an
    idempotent no-op when the node was never landed. It RAISES on a driver failure — the
    scheduler catches it and FAILS OPEN (a corpus-write failure must never block the
    store transition; the recall filter + the periodic re-assert are the backstops)."""

    async def land(
        self,
        env: CandidateEnvelope,
        *,
        forbidden_spans: tuple[str, ...] = (),
        verified: bool = False,
    ) -> None: ...

    async def update_status(
        self, env: CandidateEnvelope, *, status: str, drift_status: str
    ) -> bool: ...

    async def mark_verified(self, env: CandidateEnvelope) -> bool: ...


__all__ = [
    "BLUEPRINT_TYPE",
    "HUMAN_GATED_TYPES",
    "CandidateDecision",
    "DecisionAction",
    "DependencyResolver",
    "HitCountReader",
    "LandingWriter",
    "ProbeResult",
    "PromotionPolicy",
    "PromotionSweep",
    "WarehouseProbe",
]
