"""promotion/models.py — the S9 scheduler's typed value objects + injected ports.

`PromotionPolicy` holds the tunable knobs (config, not constants). `ProbeResult` is what an
injected warehouse probe returns for a golden replay — `(row_count, distinct_grain_count,
columns)`, the exact D56 `verify_result` input, NEVER the returned value (D98/D17).
`CandidateDecision`/`PromotionSweep` are the per-candidate and per-cycle outcomes. The
injected PORTS are Protocols, so Layer-1 fakes and the live stack use the identical path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ..config import LearningSettings

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
    "route",  # candidate → in_review (guards all passed; a human decides — plan §4)
    # candidate → validated. RETIRED FROM THE AUTO PATH by plan §4 and kept in the
    # vocabulary deliberately: `PromotionSweep.promoted` and any dashboard over it now
    # read 0 permanently, and a reader who found the action missing entirely would have
    # no way to tell a retired edge from a broken counter. Nothing emits it today; the
    # `→ validated` edge is `approve`.
    "promote",
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
    """Tunable promotion knobs (config, not constants — D-OQ1 posture).

    Built from `LearningSettings` by `policy_from_settings`, which the three promotion factories
    call by default. Anything added to this class must be reachable from an env var, or it is
    not a knob.
    """

    # The CORROBORATION threshold. Named `blueprint_hit_threshold` for its origin (D-OQ1's
    # hard hit count) and read from `LEARNING_PROMOTION_ROUTING_THRESHOLD`, whose default
    # is 1. global_knowledge/schema_edit are T=∞ (HUMAN_GATED_TYPES) and never reach it;
    # user_knowledge auto-commits elsewhere (S8).
    #
    # It was 3 and it was unreachable. The hard key is a SHA-256 over
    # `(resolves, uses_rules, result_grain, canonical_ast_norm)` — exact normalized-AST
    # equality — so three sessions had to produce a byte-identical AST for a candidate to
    # clear it. Two analysts asking the same business question through slightly different
    # SQL mint different keys and never corroborate each other. No candidate has ever
    # cleared it, so nothing has ever reached a human.
    #
    # Lowering it is only safe BECAUSE the edge it gates changed at the same time: the
    # auto path now ends at `in_review`, not `validated`, so what this threshold rations
    # is a human's attention, not corpus trust. Every correctness guard (leakage, static
    # validation, `depends_on`, golden replay) is unchanged and still runs.
    blueprint_hit_threshold: int = 1
    # Weight applied to the SOFT (intent-similarity) recurrence count when computing
    # corroboration. `hit_count + recurrence_weight * recurrence_count` is compared
    # against the threshold above.
    #
    # DORMANT at 0.0, deliberately and by default. At <20 sessions/day the threshold is 1
    # and every candidate clears it on its own first sighting, so a second corroboration
    # signal changes nothing. At ~7000/day the threshold rises and this becomes the thing
    # that lets a paraphrase corroborate — which is the whole reason the hard count
    # failed. The COUNTER is accrued now regardless of the weight (see
    # `CorpusArtifact.recurrence_count`), because a counter switched on with no history
    # behind it reads as "this never recurs" for its first month.
    #
    # **KNOWN BEFORE YOU RAISE THIS: the counter has no per-sighting idempotency.**
    # `hit_count` is bumped once per hard-key HIT and the candidate is then dropped, so a
    # redelivery of the same session collapses to one increment. The soft counter is
    # bumped by `DedupStage._bump_recurrence` for every near artifact on every pass, and
    # a candidate that survives dedup (`merge`, `conflict`, `insert`) can be re-processed
    # — a queue redelivery, a re-enqueue, a peer race, a pipeline re-run — and will
    # re-bump every one of them. So the stored count is "sightings PLUS redelivery noise",
    # biased upward and unbounded by session count. Harmless at weight 0.0; at a non-zero
    # weight it means a flapping session can corroborate itself. Deduplicating it needs a
    # per-(artifact, content_hash) marker, which is a store change, not a knob change.
    recurrence_weight: float = 0.0
    # `fresh(last_drift_check_at)` window for the silent-eligibility predicate — how
    # STALE a drift verdict may be and still be TRUSTED on the blueprint fast path.
    drift_freshness_seconds: float = 86_400.0  # 24h (provisional)
    # How often the scheduler is willing to PAY for a golden replay (a JWT mint + two
    # live warehouse queries) on one candidate. Inside this window the stored D43
    # verdict is REUSED instead of re-probed; outside it the replay runs again.
    #
    # A SEPARATE knob from `drift_freshness_seconds`, deliberately, because the two
    # answer different questions — "how often do I spend money" vs "how stale a verdict
    # will I trust" — and because setting them EQUAL guarantees a periodic gap: the
    # stamp would expire at exactly the moment the re-check becomes due, so every
    # validated blueprint would fall out of `silent_eligible` for however long the scan
    # lag is, every single window, through no fault of its own. Keeping the re-check
    # interval strictly SHORTER refreshes the stamp before it can expire (the ordinary
    # refresh-before-expiry pattern), so the fast path never flickers.
    #
    # INVARIANT (enforced at the point of use in the scheduler, not silently trusted):
    # `replay_recheck_interval_seconds <= drift_freshness_seconds`. A verdict must never
    # be reused for longer than the trust window says it may be believed — reusing it
    # longer would be the scheduler acting on evidence its own policy calls stale.
    replay_recheck_interval_seconds: float = 43_200.0  # 12h (half the trust window)
    # Max candidates scanned per status per cycle (bounds one sweep). NOT a fairness
    # hazard any more: the scan is ordered by `last_scanned_at`, so the window rotates
    # (see `CandidateStore.list_by_status`) instead of pinning the same oldest rows.
    scan_limit: int = 200
    # run_forever cadence (its OWN knob, mirroring the sweeper's interval).
    promotion_interval_seconds: float = 300.0
    # Minimum `review_score` (plan §4) an `in_review` candidate must carry to appear in
    # the default inbox listing. 0.0 = show everything, which is the shipped posture: at
    # <20 sessions/day a human skims the whole list in minutes and a cutoff would only
    # hide work.
    #
    # Applied at LIST time, never at routing time, and that placement is the safety
    # property. A routing-time cutoff would be a silent terminal state — a candidate
    # discarded for a score nobody recorded a decision about. A list filter hides rows
    # that are still there, still queryable by status, and reappear the moment the knob
    # moves.
    review_score_cutoff: float = 0.0


@dataclass(frozen=True)
class ProbeResult:
    """What an injected `WarehouseProbe` returns for one golden replay — the D56 input triple.

    `distinct_grain_count` is `None` when the grain check is skipped (an empty or unverifiable
    grain). NEVER carries the result VALUE (D98/D17 — no value oracle).
    """

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
        """Count of the retired `candidate → validated` auto edge — permanently 0 since plan §4.

        Kept so an existing reader gets a truthful zero rather than an AttributeError; `routed` is
        the counter that moved.
        """
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

    @property
    def routed(self) -> int:
        """How many candidates this cycle were routed to the human review queue.

        Its OWN counter rather than a rename of `promoted`: a dashboard that kept reading `promoted`
        would report zero forever while the loop worked perfectly.
        """
        return self._count("route")


# --- injected ports (Protocols; Layer-1 fakes = live path) --------------------


class WarehouseProbe(Protocol):
    """Runs a golden-replay SQL and returns the D56 probe triple. A STRUCTURE oracle only.

    Injected/fake in tests — no real ClickHouse — and it never returns the result value (D98).
    `column_scope` is the blueprint's declared `uses` footprint (D87): the real probe mints a
    JWT scoped to EXACTLY it, so the replay reads only within the declared footprint and the
    MCP's D57 teeth reject anything outside it.
    """

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...],
    ) -> ProbeResult: ...


class HitCountReader(Protocol):
    """Reads the cross-session `hit_count` from the LANDED corpus artifact, not the envelope.

    The neo4j blueprint node keyed by `canonical_key` (D-OQ1). S6 seeds it at 1 on insert and
    increments on a hard-key hit; S9 reads it for the promotion guard.
    """

    async def hit_count(self, canonical_key: str) -> int: ...


class RecurrenceCountReader(Protocol):
    """The SOFT (intent-similarity) recurrence count — the paraphrase sibling of `HitCountReader`.

    Its OWN port rather than a second method on that one, because every existing fake and
    production double implements it structurally: widening it would turn a Protocol change into
    a runtime `AttributeError` at the one call site that matters, in a cron nobody watches.
    `CouchbaseBlueprintCorpus` duck-types BOTH and the composition roots pass the SAME object as
    both — the two counts must address the same artifacts or the corroboration sum is nonsense.
    OPTIONAL: absent ⇒ the term reads 0, arithmetically identical at the shipped weight of 0.0.
    """

    async def recurrence_count(self, canonical_key: str) -> int: ...


class CorpusStatusWriter(Protocol):
    """Stamps a corpus artifact's lifecycle `status` (PriorArtIndex Slice 2).

    The write-side sibling of `HitCountReader` over the SAME `learning_corpus` artifacts, kept
    as its own narrow port because the two have opposite risk profiles: a stale read costs a
    delayed promotion, a bad write corrupts the cross-session counter the promotion guard
    depends on. ONLY the scheduler's TERMINAL transitions call it (reject, retract) — an
    intermediate state lives on the ENVELOPE, and mirroring it onto the artifact would create a
    second, divergent lifecycle for the same thing. Fail-open at the call site: the store
    transition is source of truth, so a corpus write failure never blocks a human's reject.
    """

    async def set_status(self, canonical_key: str, status: str) -> None: ...


class DependencyResolver(Protocol):
    """Resolves a `depends_on` artifact ref (§11.6). A blueprint depending on a
    not-yet-landed `schema_edit(add_rule)` (D35) stays `candidate` until every ref
    resolves — the S9 `depends_on` guard."""

    async def is_resolved(self, ref: str) -> bool: ...


class LandingWriter(Protocol):
    """Materializes a validated candidate into the neo4j retrieval corpus (S9 §3).

    `land` MERGE-upserts by a deterministic id (idempotent re-land) and RAISES on any failure —
    a model-parity violation, an entity leaking into the seed, a payload that cannot be mapped
    onto a seed, or a neo4j write error — so the scheduler HOLDS and NEVER writes `validated`.
    WHICH exception it raises decides the hold's reason, and therefore what the inbox tells the
    reviewer: see `scheduler._land_and_promote` for the deterministic/entity-leak/infra
    taxonomy. `forbidden_spans` are the
    entity spans S5 identified, captured BEFORE `strip_entity_bearing` blanks them, so the
    last-gate defense can fire even though a validated candidate's own `entity_scan` is blanked.

    `update_status` is the RETRACTION write-back (§8.6): it stamps the landed node's
    `status`/`drift_status` by the same deterministic id, is an idempotent no-op when the node
    was never landed, and RAISES on a driver failure — the scheduler catches that and FAILS
    OPEN, with the recall filter and the periodic re-assert as backstops.
    """

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


def policy_from_settings(settings: LearningSettings) -> PromotionPolicy:
    """Build the promotion policy from env-var configuration (plan §4).

    Before this existed, `PromotionPolicy` was accepted by all three promotion factories and
    passed by no entrypoint, so every knob was hardcoded in a dataclass default — documentation,
    not configuration. Deliberately NOT here: the coverage-judge thresholds, which live on
    `LearningSettings` because a knob that can silently cancel an extraction had to be live from
    its first deploy. Two homes for one threshold is strictly worse than one awkward home.
    """
    return PromotionPolicy(
        blueprint_hit_threshold=settings.learning_promotion_routing_threshold,
        recurrence_weight=settings.learning_promotion_recurrence_weight,
        drift_freshness_seconds=settings.learning_drift_freshness_seconds,
        replay_recheck_interval_seconds=settings.learning_replay_recheck_interval_seconds,
        scan_limit=settings.learning_promotion_scan_limit,
        promotion_interval_seconds=settings.learning_promotion_interval_seconds,
        review_score_cutoff=settings.learning_review_score_cutoff,
    )


__all__ = [
    "BLUEPRINT_TYPE",
    "HUMAN_GATED_TYPES",
    "CandidateDecision",
    "CorpusStatusWriter",
    "DecisionAction",
    "DependencyResolver",
    "HitCountReader",
    "LandingWriter",
    "ProbeResult",
    "PromotionPolicy",
    "PromotionSweep",
    "RecurrenceCountReader",
    "WarehouseProbe",
    "policy_from_settings",
]
