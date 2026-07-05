"""factory — the learning-loop COMPOSITION ROOT (Wave 3a, D102 §7.1).

The ONE place the six write-router stages (S4–S8) are assembled, in the FROZEN
order (D102 §7.1 / `stage.py`), and injected into the consumer's frozen `stages`
seam alongside the S3 extractor + the shared candidate/audit stores. Every
collaborator is passed IN (dependency injection) so Layer-1 fakes and the live
Couchbase/Redis stack travel the identical path — this module constructs NO infra
clients itself (that stays in the process entrypoint).

Frozen stage order (honored exactly, D-frozen 2026-07-03):
    generalize (S4) → leakage (S5) → dedup (S6) → schema_edit_pr (S8)
    → user_commit (S8) → writer (S7)

Three load-bearing invariants this root enforces:

  1. **Shared singletons.** The candidate store handed to the leakage gate and the
     consumer's `candidates` are the SAME instance (a split-brain store would strand
     candidates); the user-knowledge store shared by the leakage gate's reroute path
     and the S8 auto-commit stage is likewise one instance. (The terminal `WriterStage`
     takes NO candidate store — the consumer's `_run_stages` owns the persist, so the
     writer's "store" is the consumer's `candidates` by construction.)

  2. **All-or-nothing gating (critical, S3 precedent).** Extraction is a UNIT: the
     model client + a durable audit store + a durable candidate store. When it is not
     configured (no model client), the consumer falls back to the S2 `would_extract`
     stub with an EMPTY pipeline — never a half-wired plane. When it IS configured,
     EVERY collaborator the full pipeline needs must be present, else we FAIL FAST
     (`LearningWiringError`) — never a PARTIAL pipeline that strands candidates
     mid-flow (the exact class of bug S3's config gating fixed). A stage's own fake
     collaborator (null semantic scanner, insert-only embedder, null git client) is a
     fine DELIBERATE default — but the stage itself is always present or none are.

  3. **Dormant by default.** This root only BUILDS the wiring; nothing runs until an
     entrypoint calls `run_forever` AND the D58c kill-switch (`LEARNING_ENABLED`, read
     FRESH per cycle inside `run_once`) permits it. There is no request-path import of
     the learning plane (the D58c no-import invariant); this module lives entirely
     under `learning/`.

The S9 promotion scheduler is NOT a consumer stage (§7.2): it is a separate
cron-scanned process. `build_promotion_scheduler` assembles it, and `build_review_inbox`
wires the writer↔inbox↔scheduler linkage so a human approve runs the ONE guarded
`apply_human_decision` path (R4).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from data_agent.runtime.session.store import SessionStore

from .audit import AuditStore
from .candidate import CandidateEnvelope, CandidateStore
from .config import LearningSettings
from .consumer import LearningConsumer, SummaryLoader, Triage
from .dedup import BlueprintCorpus, DedupStage
from .extractor import ExtractorConfig, LearningExtractor
from .generalize import GeneralizeStage
from .inbox import ReviewInbox
from .leakage import (
    LeakageGateStage,
    NullSemanticEntityScanner,
    SemanticEntityScanner,
)
from .promotion import (
    DependencyResolver,
    HitCountReader,
    PromotionPolicy,
    PromotionScheduler,
    WarehouseProbe,
)
from .queue import LearningQueue
from .schema_edit import AllPassChecks, GitPullRequestClient, SchemaEditChecks
from .schema_edit.models import PullRequestResult, PullRequestSpec
from .schema_edit.pr_stage import SchemaEditPRStage
from .stage import CandidateStage
from .user import UserKnowledgeCommitStage, UserKnowledgeStore
from .writer import WriterStage

_logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """The dedup soft-layer embedding seam (mirrors `dedup/stage.py::_EmbeddingClient`).
    Typed here so a miswired embedder fails static checks rather than silently
    degrading the soft layer to insert-forever."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# The writer's inbox-sampling coin (deterministic in tests).
Sampler = Callable[[CandidateEnvelope], bool]


class LearningWiringError(RuntimeError):
    """Raised when extraction is configured but a full-pipeline collaborator is
    absent. Fails FAST at composition so a half-wired plane never runs and strands
    candidates mid-flow (the all-or-nothing invariant, §2)."""


class _InsertOnlyEmbedder:
    """The DEFAULT dedup embedder when none is injected: its `embed` raises, so the
    S6 soft layer degrades to `insert` (D48/D52 fail-soft) — the race-safe hard
    canonical key still dedups exact duplicates; only the soft near-miss
    adjudication is disabled. Inject a real embedder to enable it."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            "no dedup embedder wired; soft-layer near-miss adjudication is disabled "
            "(hard canonical-key dedup still applies)"
        )


class _NullGitPullRequestClient:
    """The DEFAULT schema-edit git client when none is injected: opens NO real PR
    and returns a marker result, so the S8 PR stage still stamps its
    `schema_edit_review` marker and routes the candidate to human review (never an
    auto-commit — D18). Inject a real client to author actual PRs (deferred, D53)."""

    async def open_pull_request(self, spec: PullRequestSpec) -> PullRequestResult:
        return PullRequestResult(url="", number=0, branch=spec.branch)


def build_learning_consumer(
    settings: LearningSettings,
    *,
    session_store: SessionStore,
    queue: LearningQueue,
    model_client: object | None = None,
    audit_store: AuditStore | None = None,
    candidate_store: CandidateStore | None = None,
    blueprint_corpus: BlueprintCorpus | None = None,
    user_store: UserKnowledgeStore | None = None,
    catalog_schema: dict[str, dict[str, str]] | None = None,
    embedder: Embedder | None = None,
    git_client: GitPullRequestClient | None = None,
    semantic_scanner: SemanticEntityScanner | None = None,
    checks: SchemaEditChecks | None = None,
    sampler: Sampler | None = None,
    known_rules: frozenset[str] = frozenset(),
    tracer: object | None = None,
    summary_loader: SummaryLoader | None = None,
    triage: Triage | None = None,
) -> LearningConsumer:
    """Assemble a fully-wired (or a deliberately stub) `LearningConsumer`.

    Extraction is a UNIT (`model_client` + durable `audit_store` + durable
    `candidate_store`, §2). When `model_client is None` the consumer runs the S2
    `would_extract` stub with an EMPTY pipeline (the safe fallback). When it is
    present, ALL of `audit_store`, `candidate_store`, `blueprint_corpus`,
    `user_store`, and `catalog_schema` MUST be provided (a `{}` catalog is a
    permitted, degraded-but-safe choice — every blueprint then fails static
    validation and routes to review, never auto-lands); a missing one raises
    `LearningWiringError` rather than building a partial pipeline.

    Stage-level fakes default in place (null semantic scanner → regex-only gate,
    insert-only embedder → hard-key-only dedup, null git client → PR-marker-only
    schema-edit) so the SIX stages are always all present or all absent.
    """
    loader_triage = _loader_triage_kwargs(summary_loader, triage)

    if model_client is None:
        # Unconfigured extraction ⇒ the S2 stub with an EMPTY pipeline (dormant,
        # S3-parity). No stage is built — never a partial plane.
        _logger.info(
            "learning consumer built WITHOUT extraction (no model client wired); "
            "KEEP path uses the would-extract stub, pipeline is empty"
        )
        return LearningConsumer(
            session_store,
            queue,
            settings,
            tracer=tracer,
            audit=audit_store,
            candidates=candidate_store,
            extractor=None,
            stages=(),
            **loader_triage,
        )

    _require_full_pipeline(
        audit_store=audit_store,
        candidate_store=candidate_store,
        blueprint_corpus=blueprint_corpus,
        user_store=user_store,
        catalog_schema=catalog_schema,
    )

    # Deliberate stage-level defaults (a fake is fine; the stage must be present).
    embedder = embedder if embedder is not None else _InsertOnlyEmbedder()
    git_client = git_client if git_client is not None else _NullGitPullRequestClient()
    semantic_scanner = (
        semantic_scanner if semantic_scanner is not None else NullSemanticEntityScanner()
    )
    checks = checks if checks is not None else AllPassChecks()

    if not known_rules:
        # MEDIUM-2 regression guard: with no grounded catalog rule ids, EVERY
        # rule-role blueprint plan declines `missing_rule` (the entrypoint's
        # `load_known_rule_ids()` is what fixes this). Safe (a decline routes to
        # review, never a bad landing) but invisible — warn loudly so the
        # entrypoint-adoption slice wires the real rule set.
        _logger.warning(
            "learning consumer built WITH extraction but known_rules is EMPTY — every "
            "rule-role blueprint plan will decline missing_rule (MEDIUM-2). Pass the "
            "grounded catalog rule ids (load_known_rule_ids()) to enable rule-role plans."
        )

    extractor = LearningExtractor(
        model_client,
        config=ExtractorConfig(
            max_retries=settings.learning_extractor_max_retries,
            known_rules=known_rules,
        ),
    )

    # The FROZEN write-router order (D102 §7.1). The SAME `candidate_store` /
    # `user_store` instances thread through the stages that need them and the
    # consumer — a split-brain store would strand candidates (§1).
    stages: tuple[CandidateStage, ...] = (
        GeneralizeStage(catalog_schema=catalog_schema),
        LeakageGateStage(
            candidate_store=candidate_store,
            semantic_scanner=semantic_scanner,
            user_store=user_store,
            tracer=tracer,
        ),
        DedupStage(blueprint_corpus, embedder),
        SchemaEditPRStage(git_client=git_client, checks=checks),
        UserKnowledgeCommitStage(store=user_store),
        WriterStage(sampler=sampler),
    )

    _logger.info(
        "learning consumer built WITH extraction + the %d-stage write-router "
        "pipeline (%s)",
        len(stages),
        " -> ".join(s.stage_id for s in stages),
    )
    return LearningConsumer(
        session_store,
        queue,
        settings,
        tracer=tracer,
        audit=audit_store,
        candidates=candidate_store,
        extractor=extractor,
        stages=stages,
        **loader_triage,
    )


def build_promotion_plane(
    settings: LearningSettings,
    *,
    candidate_store: CandidateStore,
    probe: WarehouseProbe,
    hit_counts: HitCountReader,
    dependency_resolver: DependencyResolver | None = None,
    landing_writer: object | None = None,
    require_landing: bool = False,
    policy: PromotionPolicy | None = None,
    clock: Callable[[], str] | None = None,
) -> tuple[PromotionScheduler, ReviewInbox]:
    """Assemble the S9 promotion plane — the scheduler + the review inbox — from ONE
    `candidate_store` instance. The inbox's `approve` reads that store and DELEGATES
    to the scheduler's single guarded `apply_human_decision` CAS-write over the SAME
    store, so taking the store once here is what pins the shared singleton: a split
    (inbox on one store, scheduler on another) would read a stale envelope and
    CAS-write the wrong one. Prefer this over the two thin builders below when wiring
    both — it makes the split impossible to express."""
    scheduler = build_promotion_scheduler(
        settings,
        candidate_store=candidate_store,
        probe=probe,
        hit_counts=hit_counts,
        dependency_resolver=dependency_resolver,
        landing_writer=landing_writer,
        require_landing=require_landing,
        policy=policy,
        clock=clock,
    )
    inbox = build_review_inbox(candidate_store, scheduler=scheduler)
    return scheduler, inbox


def build_promotion_scheduler(
    settings: LearningSettings,
    *,
    candidate_store: CandidateStore,
    probe: WarehouseProbe,
    hit_counts: HitCountReader,
    dependency_resolver: DependencyResolver | None = None,
    landing_writer: object | None = None,
    require_landing: bool = False,
    policy: PromotionPolicy | None = None,
    clock: Callable[[], str] | None = None,
) -> PromotionScheduler:
    """Assemble the S9 promotion scheduler alone — the SEPARATE cron-scanned process
    (§7.2), NOT a consumer stage. Its warehouse probe, hit-count reader, and
    dependency resolver are injected (Layer-1 fakes = live path). Launched by its own
    entrypoint via `run_forever(sleep=asyncio.sleep)` (or `run_once` from a cron).
    When wiring the inbox too, prefer `build_promotion_plane` (pins the shared store)."""
    extra = {} if clock is None else {"clock": clock}
    return PromotionScheduler(
        candidate_store,
        probe=probe,
        hit_counts=hit_counts,
        policy=policy,
        dependency_resolver=dependency_resolver,
        landing_writer=landing_writer,
        require_landing=require_landing,
        **extra,
    )


def build_review_inbox(
    candidate_store: CandidateStore, *, scheduler: PromotionScheduler
) -> ReviewInbox:
    """Wire the writer↔inbox↔scheduler linkage: the S7 writer routes to `in_review`,
    the inbox projects those rows, and a human `approve` DELEGATES to the injected
    scheduler's single `apply_human_decision` guarded path (R4) — so every approve
    enforces the identical strip + deps + static/replay guards.

    FAIL-FAST on a split store: the inbox reads `candidate_store` while `approve`
    CAS-writes through `scheduler` — if those are different instances the approve
    reads a stale envelope from one store and validates it in another. Assert the
    scheduler was built from the SAME store (its read-only `store` property)."""
    if candidate_store is not scheduler.store:
        raise LearningWiringError(
            "review inbox and its promotion scheduler must share ONE candidate store "
            "(the inbox reads it; the scheduler CAS-writes it on approve). Build both "
            "from one store — prefer build_promotion_plane(...)."
        )
    return ReviewInbox(candidate_store, scheduler=scheduler)


def _require_full_pipeline(
    *,
    audit_store: AuditStore | None,
    candidate_store: CandidateStore | None,
    blueprint_corpus: BlueprintCorpus | None,
    user_store: UserKnowledgeStore | None,
    catalog_schema: dict[str, dict[str, str]] | None,
) -> None:
    """Fail FAST if extraction is configured but a full-pipeline collaborator is
    absent (§2). A `{}` catalog is intentionally ALLOWED (degraded-safe); only
    `None` is missing."""
    required = {
        "audit_store": audit_store,
        "candidate_store": candidate_store,
        "blueprint_corpus": blueprint_corpus,
        "user_store": user_store,
        "catalog_schema": catalog_schema,
    }
    missing = sorted(name for name, val in required.items() if val is None)
    if missing:
        raise LearningWiringError(
            "extraction is configured (model client present) but the full write-router "
            f"pipeline is missing required collaborators: {missing}. Provide them, or "
            "omit the model client to fall back to the unwired would-extract stub — a "
            "PARTIAL pipeline would strand candidates mid-flow and is refused."
        )


def _loader_triage_kwargs(
    summary_loader: SummaryLoader | None, triage: Triage | None
) -> dict[str, Callable[..., Awaitable] | Triage]:
    """Only override the consumer's own defaults when a loader/triage is supplied
    (tests inject a scripted loader; production uses the consumer's defaults)."""
    kwargs: dict[str, object] = {}
    if summary_loader is not None:
        kwargs["summary_loader"] = summary_loader
    if triage is not None:
        kwargs["triage"] = triage
    return kwargs  # type: ignore[return-value]


__all__ = [
    "Embedder",
    "LearningWiringError",
    "Sampler",
    "build_learning_consumer",
    "build_promotion_plane",
    "build_promotion_scheduler",
    "build_review_inbox",
]
