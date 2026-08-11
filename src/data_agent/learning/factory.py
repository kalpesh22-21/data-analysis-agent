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
from typing import TYPE_CHECKING, Protocol

from data_agent.runtime.session.store import SessionStore

from .audit import AuditStore
from .candidate import CandidateEnvelope, CandidateStore
from .config import LearningSettings
from .consumer import LearningConsumer, SummaryLoader, Triage
from .dedup import BlueprintCorpus, DedupStage
from .extractor import ExtractorConfig, LearningExtractor
from .generalize import GeneralizeStage
from .inbox import ReviewInbox
from .judge import CoverageJudge, JudgeConfig
from .leakage import (
    LeakageGateStage,
    NullSemanticEntityScanner,
    SemanticEntityScanner,
)
from .priorart import PriorArtIndex
from .promotion import (
    CandidateStoreDependencyResolver,
    CorpusLandingWriter,
    CorpusStatusWriter,
    DependencyResolver,
    HitCountReader,
    LandingWriter,
    MCPWarehouseProbe,
    PromotionPolicy,
    PromotionScheduler,
    RecurrenceCountReader,
    WarehouseProbe,
    policy_from_settings,
)
from .queue import LearningQueue
from .schema_edit import AllPassChecks, GitPullRequestClient, SchemaEditChecks
from .schema_edit.models import PullRequestResult, PullRequestSpec
from .schema_edit.pr_stage import SchemaEditPRStage
from .stage import CandidateStage
from .user import UserKnowledgeCommitStage, UserKnowledgeStore
from .writer import WriterStage

if TYPE_CHECKING:
    from neo4j import AsyncDriver

    from data_agent.runtime.mcp.client import MCPClient
    from data_agent.runtime.model.embedding_client import EmbeddingClient

    from .promotion import TokenMinter

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
    adjudication is disabled. Inject a real embedder to enable it.

    A deployment with no embedding endpoint configured is a SUPPORTED posture, not a
    misconfiguration: it must keep running on hard-key-only dedup rather than crash. It
    is, however, an INVISIBLE degrade (every candidate looks like a clean `insert`), so
    the factory logs the fallback loudly at startup — see `build_learning_consumer`."""

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
    judge_model_client: object | None = None,
    audit_store: AuditStore | None = None,
    candidate_store: CandidateStore | None = None,
    blueprint_corpus: BlueprintCorpus | None = None,
    prior_art: PriorArtIndex | None = None,
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

    `prior_art` is deliberately NOT part of the all-or-nothing unit (PriorArt Slice 2),
    and it is threaded to TWO collaborators: the S6 dedup stage and — since plan §3a —
    the S3 extractor, which pre-fetches a PRIOR ART block and gains a `searchCorpus`
    tool. Absent, BOTH behave exactly as they did before their respective slices: dedup
    on hard key plus a brute-force corpus-bucket scan, and the extractor on a single
    forced `emit_candidates` call with no block and no search tool. Making it required
    would mean a neo4j outage stops learning, and the whole point of the fail-open
    posture is that it must not. It is logged loudly instead, because the symptom of
    running without it (duplicate candidates for blueprints the canon already carries)
    points nowhere near the cause.

    **The coverage judge (plan §3b) is built here, and it can DISCARD work.** It is
    wired only when all three of its preconditions hold — the kill-switch
    (`LEARNING_JUDGE_ENABLED`) is on, a prior-art index is present, and a durable audit
    store is present — and it is handed THE SAME audit store instance the consumer
    snapshots evidence into. That sharing is not incidental: every drop's durable record
    goes to that store, and a split would put the records somewhere nobody queries,
    leaving the mitigation present in code and absent in practice. Absent any
    precondition, no judge is built and the loop behaves exactly as it did before the
    slice. `judge_model_client` overrides which model answers (the economics favour a
    smaller one); omitted, the extractor's client is reused.
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
    if embedder is None:
        # SUPPORTED but INVISIBLE degrade: with no embedding endpoint configured the S6
        # soft layer cannot run at all, so every near-duplicate that misses the hard key
        # is adjudicated `insert` and the loop happily re-proposes variants of artifacts
        # it already holds. Never a crash (an unconfigured deployment must keep draining
        # the queue) — but never silent either, because the symptom (duplicate
        # candidates) points nowhere near the cause.
        _logger.warning(
            "no dedup embedder wired — the S6 soft near-miss layer is DISABLED and every "
            "hard-key miss will be adjudicated `insert` (hard canonical-key dedup still "
            "applies). Set EMBEDDING_API_URL so the entrypoint builds an "
            "HttpEmbeddingClient, or inject one explicitly."
        )
        embedder = _InsertOnlyEmbedder()
    else:
        _logger.info(
            "dedup soft layer ENABLED via %s (merge>=%.3f, conflict>=%.3f)",
            type(embedder).__name__,
            settings.learning_dedup_merge_threshold,
            settings.learning_dedup_conflict_threshold,
        )
    if prior_art is None:
        # SUPPORTED but INVISIBLE degrade (PriorArt Slice 2). Without the cross-tier
        # index the dedup stage can only see the `learning_corpus` bucket — which is
        # seeded solely by the dedup stage itself, so it contains ONLY what this loop
        # already minted. The MCP canon the agent recalls and the landed learning tier
        # are both invisible, and a session re-deriving a blueprint we already own
        # produces a duplicate that nothing notices. Never a crash (a deployment with no
        # graph must keep draining the queue) — but never silent.
        _logger.warning(
            "no prior-art index wired — the S6 cross-tier layer AND the S3 extractor's "
            "PRIOR ART block are DISABLED: dedup can see ONLY the learning_corpus "
            "bucket (which this loop seeds itself), so the MCP canon and the landed "
            "learning tier are INVISIBLE, the extractor is never shown what already "
            "exists (no PRIOR ART block, no searchCorpus tool), and already-owned "
            "blueprints will be re-proposed as new. The soft layer also falls back to "
            "the O(corpus) brute-force scan (N+1 embeddings per candidate). Set "
            "NEO4J_URL + EMBEDDING_API_URL so the entrypoint builds a "
            "Neo4jPriorArtIndex, or inject one explicitly."
        )
    else:
        _logger.info(
            "cross-tier prior-art layer ENABLED via %s (S6 dedup + the S3 extractor's "
            "PRIOR ART pre-fetch and searchCorpus tool)", type(prior_art).__name__
        )
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
            max_shape_corrections=settings.learning_extractor_max_shape_corrections,
            known_rules=known_rules,
        ),
        # THE SAME index instance the dedup stage gets (plan §3a). One object, two
        # readers: the extractor asks "has this been proposed before?" BEFORE the LLM
        # call, dedup asks "is what came back a duplicate?" after it. Splitting them
        # would let the two stages disagree about what the corpus contains within a
        # single candidate's lifetime — and would double the neo4j pools this process
        # opens for no benefit.
        prior_art=prior_art,
    )

    judge = _build_judge(
        settings,
        model_client=judge_model_client if judge_model_client is not None else model_client,
        # The recorded model id must describe the client that will actually answer, not
        # the one an operator configured. See `_build_judge`.
        judge_client_injected=judge_model_client is not None,
        audit_store=audit_store,
        prior_art=prior_art,
        tracer=tracer,
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
        DedupStage(
            blueprint_corpus,
            embedder,
            prior_art=prior_art,
            merge_threshold=settings.learning_dedup_merge_threshold,
            conflict_threshold=settings.learning_dedup_conflict_threshold,
            recurrence_threshold=settings.learning_recurrence_similarity_threshold,
            # THE SAME judge instance the consumer screens sessions with — one object,
            # two stages. A split would let the two halves of one candidate's lifetime
            # run under different thresholds and write to different audit stores.
            judge=judge,
            tracer=tracer,
        ),
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
        judge=judge,
        **loader_triage,
    )


def _build_judge(
    settings: LearningSettings,
    *,
    model_client: object | None,
    judge_client_injected: bool,
    audit_store: AuditStore | None,
    prior_art: PriorArtIndex | None,
    tracer: object | None,
) -> CoverageJudge | None:
    """Build the coverage judge, or `None` when any precondition is missing.

    THREE preconditions, and each absence is a different fact so each gets its own log
    line — a single "judge not wired" message would leave an operator guessing which of
    three things to fix:

      * the kill-switch is off — a deliberate operator choice, logged at INFO;
      * no prior-art index — there is nothing to be covered BY, so a judge could only
        ever answer `new` at the price of a model call. Already warned about loudly by
        the caller (the same absence disables two other surfaces), so this one is INFO
        too;
      * no audit store — the DISQUALIFYING one, and it is a WARNING. Without a durable
        record a drop is invisible, and an invisible drop is precisely the risk the
        record was agreed as the mitigation for. Building a judge that cannot write is
        not a degraded judge, it is the failure mode; so we build none.

    Note the third check is redundant TODAY — `_require_full_pipeline` has already
    refused a missing audit store by the time this runs — and it stays because the
    guarantee it encodes ("no record store, no judge") must not depend on another
    function's ordering. That coupling is the shape this codebase keeps getting bitten
    by (see `writer/routing.py::_AUTO_LAND_DEDUP_ACTIONS`).
    """
    if not settings.learning_judge_enabled:
        _logger.info(
            "coverage judge DISABLED by LEARNING_JUDGE_ENABLED — every KEEP-triaged "
            "session pays a full extraction, including the ones that re-derive "
            "something the corpus already carries (pre-plan-§3b behaviour)"
        )
        return None
    if prior_art is None:
        _logger.info(
            "coverage judge NOT wired: no prior-art index, so there is nothing for a "
            "session to be covered BY and the judge could only ever answer `new`"
        )
        return None
    if audit_store is None:
        _logger.warning(
            "coverage judge NOT wired: no durable audit store. The judge DROPS work, "
            "and every drop must leave a queryable record — a judge that cannot write "
            "one would make discarded sessions invisible."
        )
        return None
    if model_client is None:  # unreachable via build_learning_consumer; see the docstring
        return None
    config = JudgeConfig(
        pre_drop_confidence=settings.learning_judge_pre_drop_confidence,
        post_drop_confidence=settings.learning_judge_post_drop_confidence,
        band_low=settings.learning_judge_band_low,
        band_high=settings.learning_judge_band_high,
        timeout_seconds=settings.learning_judge_timeout_seconds,
        shadow=settings.learning_judge_shadow_mode,
        model_id=_judge_model_id(settings, injected=judge_client_injected),
    )
    if config.shadow:
        _logger.warning(
            "coverage judge ENABLED in SHADOW MODE (model=%s): every verdict is "
            "recorded to learning_audit with would_drop/shadow set, and NOTHING is "
            "discarded. Query `WHERE record_type='judge_verdict' AND would_drop=true` "
            "to see what it would have thrown away, then set "
            "LEARNING_JUDGE_SHADOW_MODE=false.",
            config.model_id,
        )
    else:
        _logger.warning(
            "coverage judge ENABLED (model=%s): a KEEP-triaged session whose work the "
            "corpus already carries is DROPPED before extraction at confidence >= %.2f, "
            "and an extracted candidate is discarded at >= %.2f inside the %.2f-%.2f "
            "band. Every drop writes a durable record to the learning_audit bucket "
            "(record_type='judge_verdict'); nothing else records it. Set "
            "LEARNING_JUDGE_SHADOW_MODE=true to record without discarding, or "
            "LEARNING_JUDGE_ENABLED=false to turn it off entirely.",
            config.model_id,
            config.pre_drop_confidence,
            config.post_drop_confidence,
            config.band_low,
            config.band_high,
        )
    return CoverageJudge(
        model_client,  # type: ignore[arg-type]
        audit_store,
        prior_art=prior_art,
        config=config,
        tracer=tracer,
        # The SAME D25 gate the consumer's triage/extract spans read, from the SAME
        # setting. Read here rather than defaulted inside `CoverageJudge` so a session's
        # spans cannot end up half entity-bearing: one switch, one posture, decided at
        # the composition root.
        trace_verbose=settings.learning_trace_verbose,
    )


def _judge_model_id(settings: LearningSettings, *, injected: bool) -> str:
    """The model id STAMPED ON EVERY VERDICT — which must describe the client that will
    actually answer, not the one an operator configured.

    `JudgeRecord.model` exists for exactly one purpose: verdicts are compared across
    months, the model changes underneath them, and without this field the dataset
    silently pools two judges. Reading it off `LEARNING_JUDGE_MODEL` unconditionally
    would defeat that at the first opportunity — the shipped entrypoint only builds a
    separate client when that setting names a DIFFERENT model, but `build_learning_
    consumer` is called from other places (both demo scripts) that pass one model client
    and never look at the setting. Those runs would have recorded the configured id
    while the extractor's model answered: the exact mislabel the field exists to
    prevent, asserted with a straight face.

    So the id follows the CLIENT:
      * a judge-specific client was injected ⇒ the configured judge model (or an
        explicit `unknown-injected-judge-client` when the caller injected a client and
        configured no id — honest rather than plausible);
      * no separate client ⇒ the extractor's model, because that is what will answer,
        and a configured-but-unused `LEARNING_JUDGE_MODEL` is warned about loudly rather
        than believed.
    """
    if injected:
        return settings.learning_judge_model or "unknown-injected-judge-client"
    if settings.learning_judge_model and (
        settings.learning_judge_model != settings.learning_extractor_model
    ):
        _logger.warning(
            "LEARNING_JUDGE_MODEL=%r is set but no separate judge model client was "
            "passed, so the EXTRACTOR's client (%r) will answer every judgement. "
            "Recording %r on the verdicts, not %r — the model field exists so the "
            "coverage dataset never silently pools two judges. Pass "
            "judge_model_client=... to actually use the configured model.",
            settings.learning_judge_model,
            settings.learning_extractor_model,
            settings.learning_extractor_model,
            settings.learning_judge_model,
        )
    return settings.learning_extractor_model


def build_promotion_plane(
    settings: LearningSettings,
    *,
    candidate_store: CandidateStore,
    probe: WarehouseProbe,
    hit_counts: HitCountReader,
    recurrence_counts: RecurrenceCountReader | None = None,
    dependency_resolver: DependencyResolver | None = None,
    landing_writer: LandingWriter | None = None,
    require_landing: bool = False,
    corpus_status: CorpusStatusWriter | None = None,
    policy: PromotionPolicy | None = None,
    clock: Callable[[], str] | None = None,
    tracer: object | None = None,
) -> tuple[PromotionScheduler, ReviewInbox]:
    """Assemble the S9 promotion plane — the scheduler + the review inbox — from ONE
    `candidate_store` instance. The inbox's `approve` reads that store and DELEGATES
    to the scheduler's single guarded `apply_human_decision` CAS-write over the SAME
    store, so taking the store once here is what pins the shared singleton: a split
    (inbox on one store, scheduler on another) would read a stale envelope and
    CAS-write the wrong one. Prefer this over the two thin builders below when wiring
    both — it makes the split impossible to express.

    *corpus_status* (PriorArt Slice 2) is the `learning_corpus` write-side port the
    TERMINAL transitions stamp so a rejected/retired artifact stops surfacing as live
    prior art. Pass the SAME object as `hit_counts` — `CouchbaseBlueprintCorpus`
    duck-types both ports, and a split would let a reject stamp one store while the
    promotion guard reads the count from another. Omitted ⇒ no stamping (the exact
    pre-slice behaviour), which is why every existing caller is unaffected.

    *tracer* (optional) wires the scheduler's promote/land span seam; `trace_verbose`
    is read off `settings.learning_trace_verbose` (D25 gate).

    *policy* is optional and is built from *settings* when omitted (plan §4) — see
    `build_promotion_scheduler`. The SAME policy object is handed to the inbox, because
    `review_score_cutoff` and the routing threshold are two ends of one decision about how
    much a reviewer is asked to look at, and reading them from two objects would let a
    deployment route work into a queue its own cutoff then hides."""
    scheduler = build_promotion_scheduler(
        settings,
        candidate_store=candidate_store,
        probe=probe,
        hit_counts=hit_counts,
        recurrence_counts=recurrence_counts,
        dependency_resolver=dependency_resolver,
        landing_writer=landing_writer,
        require_landing=require_landing,
        corpus_status=corpus_status,
        policy=policy,
        clock=clock,
        tracer=tracer,
    )
    inbox = build_review_inbox(
        candidate_store, scheduler=scheduler, policy=scheduler.policy
    )
    return scheduler, inbox


def build_promotion_write_plane(
    settings: LearningSettings,
    *,
    candidate_store: CandidateStore,
    hit_counts: HitCountReader,
    mcp_client: MCPClient,
    token_minter: TokenMinter,
    neo4j_driver: AsyncDriver,
    embedding_client: EmbeddingClient,
    model_id: str,
    neo4j_database: str = "neo4j",
    corpus_status: CorpusStatusWriter | None = None,
    policy: PromotionPolicy | None = None,
    clock: Callable[[], str] | None = None,
    tracer: object | None = None,
) -> tuple[PromotionScheduler, ReviewInbox]:
    """Assemble the FULLY-ACTIVATED S9 promotion WRITE plane (S9-activation Slice 2,
    §4) — the scheduler + inbox with the REAL warehouse probe, dependency resolver,
    AND corpus-landing writer, `require_landing` flipped ON.

    The write plane is a UNIT (§4, mirroring the consumer factory's all-or-nothing
    rule): the caller passes EVERY injected infra client — the MCP `runQuery`
    transport, the offline token minter, the neo4j async driver, and the embedding
    client — and this root wraps them into the three ports (probe / resolver / landing
    writer). With a real writer present, `require_landing=True` no longer HOLDS
    `landing_unavailable`; the scheduler LANDS a validated blueprint into neo4j FIRST,
    then CAS-writes `validated` (`_land_and_promote`, §3.1). This module constructs NO
    infra clients itself (that stays in the process entrypoint) — it only wires ports."""
    probe = MCPWarehouseProbe(mcp_client=mcp_client, token_minter=token_minter)
    resolver = CandidateStoreDependencyResolver(candidate_store)
    landing_writer = CorpusLandingWriter(
        neo4j_driver, embedding_client, model_id=model_id, database=neo4j_database
    )
    return build_promotion_plane(
        settings,
        candidate_store=candidate_store,
        probe=probe,
        hit_counts=hit_counts,
        # The SAME object as `hit_counts` when the caller passed a corpus store, which is
        # what every real caller does (`CouchbaseBlueprintCorpus` duck-types both ports).
        # Checked structurally rather than assumed: `hit_counts` is a Protocol parameter
        # and a caller may legitimately pass a narrow reader, in which case the dormant
        # soft count simply reads 0 — the shipped weight is 0.0, so nothing changes.
        recurrence_counts=(
            hit_counts if hasattr(hit_counts, "recurrence_count") else None  # type: ignore[arg-type]
        ),
        dependency_resolver=resolver,
        landing_writer=landing_writer,
        require_landing=True,
        corpus_status=corpus_status,
        policy=policy,
        clock=clock,
        tracer=tracer,
    )


def build_promotion_scheduler(
    settings: LearningSettings,
    *,
    candidate_store: CandidateStore,
    probe: WarehouseProbe,
    hit_counts: HitCountReader,
    recurrence_counts: RecurrenceCountReader | None = None,
    dependency_resolver: DependencyResolver | None = None,
    landing_writer: LandingWriter | None = None,
    require_landing: bool = False,
    corpus_status: CorpusStatusWriter | None = None,
    policy: PromotionPolicy | None = None,
    clock: Callable[[], str] | None = None,
    tracer: object | None = None,
) -> PromotionScheduler:
    """Assemble the S9 promotion scheduler alone — the SEPARATE cron-scanned process
    (§7.2), NOT a consumer stage. Its warehouse probe, hit-count reader, and
    dependency resolver are injected (Layer-1 fakes = live path). Launched by its own
    entrypoint via `run_forever(sleep=asyncio.sleep)` (or `run_once` from a cron).
    When wiring the inbox too, prefer `build_promotion_plane` (pins the shared store).

    **The POLICY is built from *settings* when the caller omits it (plan §4).** Before
    this slice `policy=None` fell through to `PromotionScheduler`'s own
    `PromotionPolicy()` default, and since no entrypoint ever passed one, every knob on
    that class was a hardcoded constant wearing a config's clothes. Defaulting HERE — at
    the composition root, which is the only place that legitimately reads settings —
    means an operator's `LEARNING_PROMOTION_*` vars take effect without any entrypoint
    change, while an explicit `policy=` (tests, demos) still wins.

    *tracer* (optional) wires the promote/land span seam; `trace_verbose` is read off
    `settings.learning_trace_verbose` (the D25 gate)."""
    extra = {} if clock is None else {"clock": clock}
    return PromotionScheduler(
        candidate_store,
        probe=probe,
        hit_counts=hit_counts,
        recurrence_counts=recurrence_counts,
        policy=policy if policy is not None else policy_from_settings(settings),
        dependency_resolver=dependency_resolver,
        landing_writer=landing_writer,
        require_landing=require_landing,
        corpus_status=corpus_status,
        tracer=tracer,
        trace_verbose=settings.learning_trace_verbose,
        **extra,
    )


def build_review_inbox(
    candidate_store: CandidateStore,
    *,
    scheduler: PromotionScheduler,
    policy: PromotionPolicy | None = None,
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
    # Default to the SCHEDULER's policy rather than to a fresh `PromotionPolicy()`: the
    # routing threshold and the review cutoff are two ends of one decision, and a fresh
    # default here would silently apply a cutoff of 0.0 to a deployment that configured
    # one — a knob turned in the env and ignored at the surface it governs.
    return ReviewInbox(
        candidate_store,
        scheduler=scheduler,
        policy=policy if policy is not None else scheduler.policy,
    )


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
    "build_promotion_write_plane",
    "build_review_inbox",
]
