"""factory — the learning-loop COMPOSITION ROOT (Wave 3a, D102 §7.1).

The ONE place the six write-router stages are assembled, in the FROZEN order
(`generalize → leakage → dedup → schema_edit_pr → user_commit → writer`), and injected
into the consumer; every collaborator is passed IN and no infra client is constructed here.
Three invariants: SHARED SINGLETONS (one candidate store, one user store across stages);
ALL-OR-NOTHING (extraction is a unit — configured-but-incomplete raises
`LearningWiringError` rather than stranding candidates in a partial pipeline); DORMANT
(nothing runs until an entrypoint calls `run_forever` and `LEARNING_ENABLED` permits it).
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
from .extractor import ExtractorConfig, LearningExtractor, RuleIndex
from .generalize import GeneralizeStage
from .inbox import ParameterizationCompleter, ReviewInbox
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

    Typed here so a miswired embedder fails static checks rather than silently degrading the
    soft layer to insert-forever.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# The writer's inbox-sampling coin (deterministic in tests).
Sampler = Callable[[CandidateEnvelope], bool]


class LearningWiringError(RuntimeError):
    """Extraction is configured but a full-pipeline collaborator is absent.

    Fails FAST at composition, so a half-wired plane never runs and strands candidates mid-flow.
    """


class _InsertOnlyEmbedder:
    """The DEFAULT dedup embedder when none is injected: its `embed` raises.

    The S6 soft layer then degrades to `insert` (D48/D52 fail-soft) — the race-safe hard
    canonical key still dedups exact duplicates, only the near-miss adjudication is off. A
    SUPPORTED posture, but an INVISIBLE degrade (every candidate looks like a clean `insert`),
    so `build_learning_consumer` logs the fallback loudly at startup.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            "no dedup embedder wired; soft-layer near-miss adjudication is disabled "
            "(hard canonical-key dedup still applies)"
        )


class _NullGitPullRequestClient:
    """The DEFAULT schema-edit git client: opens NO real PR, returns a marker result.

    The S8 PR stage still stamps its `schema_edit_review` marker and routes the candidate to
    human review — never an auto-commit (D18). Inject a real client to author actual PRs.
    """

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
    rule_index: RuleIndex | None = None,
    tracer: object | None = None,
    summary_loader: SummaryLoader | None = None,
    triage: Triage | None = None,
) -> LearningConsumer:
    """Assemble a fully-wired (or a deliberately stub) `LearningConsumer`.

    Extraction is a UNIT: with `model_client is None` the consumer runs the S2 `would_extract`
    stub with an EMPTY pipeline; with it present, ALL of `audit_store`, `candidate_store`,
    `blueprint_corpus`, `user_store` and `catalog_schema` must be provided or this raises
    `LearningWiringError`. A `{}` catalog is permitted and degraded-safe (every blueprint then
    fails static validation and routes to review). Stage-level fakes default in place, so the
    six stages are always all present or all absent.

    `prior_art` is deliberately NOT part of that unit and is threaded to BOTH the S6 dedup stage
    and the S3 extractor; absent, each behaves exactly as it did pre-slice, because requiring it
    would let a neo4j outage stop learning. It is logged loudly instead. The coverage judge,
    which can DISCARD work, is wired only when its kill-switch, a prior-art index and a durable
    audit store are all present — and is handed THE SAME audit store the consumer snapshots
    evidence into, so every drop's record lands where someone queries it.
    """
    # Only override the consumer's OWN defaults when a loader/triage is supplied
    # (tests inject a scripted loader; production uses the consumer's defaults).
    loader_triage: dict[str, Callable[..., Awaitable] | Triage] = {}
    if summary_loader is not None:
        loader_triage["summary_loader"] = summary_loader
    if triage is not None:
        loader_triage["triage"] = triage

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
        # `known_rule_ids_from_catalog(catalog)` over the MCP export is what fixes
        # this). Safe (a decline routes to review, never a bad landing) but
        # invisible — warn loudly so the entrypoint wires the real rule set.
        _logger.warning(
            "learning consumer built WITH extraction but known_rules is EMPTY — every "
            "rule-role blueprint plan will decline missing_rule (MEDIUM-2). Pass the "
            "grounded catalog rule ids (known_rule_ids_from_catalog()) to enable "
            "rule-role plans."
        )
    elif rule_index is None:
        # Grounded ids but no index: a rule-role plan citing an id that does not exist
        # declines missing_rule TERMINALLY, with no corrective turn, even when the
        # catalog names the same concept under another id (the `earnings_only` /
        # `gross_earnings` case). Safe — the decline reaches a human either way — but
        # invisible, and the whole cost is a correct proposal thrown away over a label.
        # Build it from the SAME catalog as `known_rules`
        # (`rule_index_from_catalog`), which is what keeps the two views consistent.
        _logger.info(
            "learning consumer built with known_rules but NO rule index — an unknown "
            "rule_id declines missing_rule terminally and is never re-asked, even when "
            "the catalog names the concept under a different id. Pass "
            "rule_index=rule_index_from_catalog(catalog) to enable the hinted correction."
        )

    extractor = LearningExtractor(
        model_client,
        config=ExtractorConfig(
            max_retries=settings.learning_extractor_max_retries,
            max_shape_corrections=settings.learning_extractor_max_shape_corrections,
            known_rules=known_rules,
            rule_index=rule_index,
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
    stages = build_write_router_stages(
        settings,
        candidate_store=candidate_store,
        blueprint_corpus=blueprint_corpus,
        catalog_schema=catalog_schema,
        embedder=embedder,
        user_store=user_store,
        prior_art=prior_art,
        judge=judge,
        semantic_scanner=semantic_scanner,
        git_client=git_client,
        checks=checks,
        sampler=sampler,
        tracer=tracer,
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


def build_write_router_stages(
    settings: LearningSettings,
    *,
    candidate_store: CandidateStore,
    blueprint_corpus: BlueprintCorpus,
    catalog_schema: dict[str, dict[str, str]],
    embedder: Embedder,
    user_store: UserKnowledgeStore | None = None,
    prior_art: PriorArtIndex | None = None,
    judge: CoverageJudge | None = None,
    semantic_scanner: SemanticEntityScanner | None = None,
    git_client: GitPullRequestClient | None = None,
    checks: SchemaEditChecks | None = None,
    sampler: Sampler | None = None,
    tracer: object | None = None,
    include_target_specific: bool = True,
) -> tuple[CandidateStage, ...]:
    """The FROZEN write-router order (D102 §7.1), assembled in ONE place.

    Two callers assemble this pipeline — `build_learning_consumer`, and the inbox service for a
    candidate a human finished filling in (`inbox/completion.py`) — and the ORDER is the thing
    that must not fork. `include_target_specific=False` omits `schema_edit_pr` and
    `user_commit`: the completion path only ever re-runs a `blueprint`, so neither would do
    anything except require a git client and a per-user store that plane has no reason to hold.
    """
    stages: list[CandidateStage] = [
        GeneralizeStage(catalog_schema=catalog_schema),
        LeakageGateStage(
            candidate_store=candidate_store,
            semantic_scanner=(
                semantic_scanner
                if semantic_scanner is not None
                else NullSemanticEntityScanner()
            ),
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
    ]
    if include_target_specific:
        if user_store is None:
            raise LearningWiringError(
                "the target-specific stages need a user-knowledge store (the S8 "
                "auto-commit stage commits per-user facts through it); pass one, or "
                "build the blueprint-only pipeline with include_target_specific=False"
            )
        stages.append(
            SchemaEditPRStage(
                git_client=(
                    git_client if git_client is not None else _NullGitPullRequestClient()
                ),
                checks=checks if checks is not None else AllPassChecks(),
            )
        )
        stages.append(UserKnowledgeCommitStage(store=user_store))
    stages.append(WriterStage(sampler=sampler))
    return tuple(stages)


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

    THREE preconditions, each absence logged separately so an operator knows which of three
    things to fix: the kill-switch is off (INFO), there is no prior-art index (INFO — nothing
    to be covered BY), or there is no audit store (WARNING, and DISQUALIFYING: without a
    durable record a drop is invisible, which is exactly the risk the record mitigates). The
    third check is redundant today — `_require_full_pipeline` already refused — and stays so
    the guarantee does not depend on another function's ordering.
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
    """The model id STAMPED ON EVERY VERDICT — it must name the client that will actually ANSWER.

    `JudgeRecord.model` exists so verdicts compared across months are not silently pooled when
    the model changes underneath them. Reading `LEARNING_JUDGE_MODEL` unconditionally would
    mislabel the callers that pass one model client and never consult the setting. So the id
    follows the CLIENT: an injected judge client ⇒ the configured judge model (or an explicit
    `unknown-injected-judge-client` when none is configured — honest rather than plausible); no
    separate client ⇒ the extractor's model, with a configured-but-unused setting warned about.
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
    completer: ParameterizationCompleter | None = None,
) -> tuple[PromotionScheduler, ReviewInbox]:
    """Assemble the S9 promotion plane (scheduler + review inbox) over ONE `candidate_store`.

    The inbox's `approve` reads that store and DELEGATES to the scheduler's single guarded
    `apply_human_decision` CAS-write over the same one, so a split would read a stale envelope
    and CAS-write the wrong one. Prefer this over the two thin builders below — it is the only
    builder for either half, so the split cannot be expressed. *corpus_status* must be the SAME
    object as *hit_counts* for that reason; omitted ⇒ no terminal stamping (pre-slice
    behaviour). *completer* takes the same store too (an inbox without one refuses 503).

    The POLICY is built from *settings* when the caller omits it, so `LEARNING_PROMOTION_*`
    vars take effect without an entrypoint change while an explicit `policy=` still wins. The
    SAME policy object is handed to the inbox: `review_score_cutoff` and the routing threshold
    are two ends of one decision, and reading them from two objects would let a deployment
    route work into a queue its own cutoff then hides.
    """
    extra = {} if clock is None else {"clock": clock}
    scheduler = PromotionScheduler(
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
    if completer is not None and completer.store is not candidate_store:
        raise LearningWiringError(
            "review inbox and its parameterization completer must share ONE candidate "
            "store (the inbox reads it; the completer re-validates and writes back to "
            "it). Build both from one store."
        )
    inbox = ReviewInbox(
        candidate_store,
        scheduler=scheduler,
        policy=scheduler.policy,
        completer=completer,
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
    completer: ParameterizationCompleter | None = None,
) -> tuple[PromotionScheduler, ReviewInbox]:
    """Assemble the FULLY-ACTIVATED S9 promotion WRITE plane (S9-activation Slice 2, §4).

    The scheduler + inbox with the REAL warehouse probe, dependency resolver AND corpus-landing
    writer, `require_landing` flipped ON. The write plane is a UNIT: the caller passes EVERY
    injected infra client (the MCP `runQuery` transport, the offline token minter, the neo4j
    async driver, the embedding client) and this root wraps them into ports, constructing no
    infra itself. With a real writer present, `require_landing=True` no longer holds
    `landing_unavailable` — the scheduler LANDS into neo4j FIRST, then CAS-writes `validated`.
    """
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
        completer=completer,
    )


def _require_full_pipeline(
    *,
    audit_store: AuditStore | None,
    candidate_store: CandidateStore | None,
    blueprint_corpus: BlueprintCorpus | None,
    user_store: UserKnowledgeStore | None,
    catalog_schema: dict[str, dict[str, str]] | None,
) -> None:
    """Fail FAST if extraction is configured but a full-pipeline collaborator is absent (§2).

    A `{}` catalog is intentionally ALLOWED (degraded-safe); only `None` counts as missing.
    """
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


__all__ = [
    "Embedder",
    "LearningWiringError",
    "Sampler",
    "build_learning_consumer",
    "build_promotion_plane",
    "build_promotion_write_plane",
    "build_write_router_stages",
]
