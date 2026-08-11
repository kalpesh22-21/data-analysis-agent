"""LearningSettings — env-var config for the offline learning loop (D96 §11).

Mirrors `runtime/config.py`'s `RuntimeSettings` pattern (pydantic-settings,
uppercased env vars, `.env`, `extra="ignore"`), but is a SEPARATE settings
surface: the two learning processes (sweeper, consumer) are distinct entrypoints
(D96 §g) with their own configuration, and — critically — the D58c kill-switch
must NOT be frozen behind an `@lru_cache`d settings singleton (see
`learning_enabled()` below).
"""

from __future__ import annotations

import os
import socket

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Recognized truthy spellings for the kill-switch (case-insensitive). Anything
# else (including unset → default) resolves per the rules in `learning_enabled`.
_TRUTHY = {"1", "true", "yes", "on"}


class _KillSwitchSettings(BaseSettings):
    """A one-field settings surface for `LEARNING_ENABLED` ONLY, constructed
    FRESH on every `learning_enabled()` call (never cached). Reads BOTH `.env`
    and the process environment — with the process env taking precedence
    (pydantic-settings source order) — so an operator flipping the switch in
    EITHER place is honored (MEDIUM-2: reading only `os.environ` silently
    ignored a `.env` override, which fails DANGEROUS)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    learning_enabled: str | None = None


def learning_enabled() -> bool:
    """Read the D58c master kill-switch `LEARNING_ENABLED` FRESH, EVERY call —
    deliberately bypassing the `@lru_cache`d `RuntimeSettings` so a flip (of the
    env var OR `.env`) takes effect on the next cycle with NO restart/deploy
    (D96 §e / design §7).

    Default (unset/blank) is enabled. Unrecognized values are treated as
    disabled (fail-safe: a typo'd override halts, it does not silently run).
    """
    raw = _KillSwitchSettings().learning_enabled
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in _TRUTHY


def _default_consumer_name() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


class LearningSettings(BaseSettings):
    """All learning-loop configuration, read from environment variables (or `.env`).

    `LEARNING_ENABLED` is deliberately ABSENT from this model: it is read
    uncached, per cycle, via the module-level `learning_enabled()` accessor so
    the toggle is never frozen by the settings cache.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Redis Streams transport (D30) ---
    learning_redis_url: str = Field(
        "redis://localhost:6379/0", description="Redis Streams endpoint for the learning queue."
    )
    learning_jobs_stream: str = Field(
        "learning:jobs", description="Work stream — one entry per claimed session."
    )
    learning_dead_letter_stream: str = Field(
        "learning:jobs:dead", description="Terminal parking stream for poison jobs."
    )
    learning_consumer_group: str = Field(
        "learning-workers", description="Redis consumer group name."
    )
    learning_consumer_name: str = Field(
        default_factory=_default_consumer_name,
        description="Per-replica consumer identity for the PEL (default worker-<host>-<pid>).",
    )

    # --- Sweeper / consumer cadence + thresholds ---
    learning_sweep_interval_seconds: float = Field(
        60.0, gt=0, description="Sweep cadence (seconds between idle scans)."
    )
    learning_idle_threshold_seconds: int = Field(
        1800,
        ge=1,
        description=(
            "Idle age past which a session is 'closed' and eligible for learning. "
            "Must keep the §6 TTL invariant: SESSION_TTL > this + P95(dwell+processing)."
        ),
    )
    learning_max_deliveries: int = Field(
        5,
        ge=1,
        description="Dead-letter threshold N: past N deliveries a job → dead-letter stream.",
    )
    learning_reclaim_min_idle_seconds: int = Field(
        300, ge=1, description="XAUTOCLAIM min-idle for reclaiming stuck PEL entries."
    )
    learning_batch_size: int = Field(10, ge=1, description="XREADGROUP COUNT per consume batch.")
    learning_block_ms: int = Field(5000, ge=0, description="XREADGROUP BLOCK milliseconds.")
    learning_consumer_idle_sleep_seconds: float = Field(
        5.0,
        gt=0,
        description=(
            "How long the consumer sleeps before re-checking the kill-switch when "
            "disabled (its OWN knob — not the sweeper's sweep interval)."
        ),
    )
    learning_scan_limit: int = Field(
        200, ge=1, description="Max idle sessions scanned/claimed per sweep cycle."
    )

    # --- Audit / provenance store (D95, §4) — READ in Slice 2. The dedicated
    # `learning_audit` bucket + its own `learning_audit_writer` RBAC user back the
    # CouchbaseAuditStore. S2 provisions + tests the client but writes NO evidence
    # (the first snapshot is S3's, §4.3).
    learning_audit_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the learning_audit bucket (may be the same cluster).",
    )
    learning_audit_bucket: str = Field(
        "learning_audit", description="Dedicated audit bucket (D95) — separate retention/RBAC clock."
    )
    learning_audit_username: str = Field(
        "", description="RBAC user scoped to learning_audit ONLY (learning_audit_writer)."
    )
    learning_audit_password: str = Field(
        "", description="Password for the learning_audit_writer RBAC user (secret)."
    )
    learning_audit_ttl_seconds: int = Field(
        7_776_000,  # 90 days
        ge=1,
        description="Audit retention floor (D95): audit_TTL ≥ max_candidate_lifetime. Set fresh per write.",
    )

    # --- Candidate holding store (D101, Slice 3) — a dedicated, access-controlled
    # bucket sibling to learning_audit; holds extracted candidate envelopes at
    # status=extracted, queryable by status. Its own RBAC user scoped to it only.
    learning_candidates_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the learning_candidates bucket.",
    )
    learning_candidates_bucket: str = Field(
        "learning_candidates", description="Dedicated candidate-holding bucket (D101)."
    )
    learning_candidates_username: str = Field(
        "", description="RBAC user scoped to learning_candidates ONLY (learning_candidates_writer)."
    )
    learning_candidates_password: str = Field(
        "", description="Password for the learning_candidates_writer RBAC user (secret)."
    )
    learning_candidates_ttl_seconds: int = Field(
        7_776_000,  # 90 days — a candidate must outlive the review-inbox dwell (D101/D95).
        ge=1,
        description="Candidate retention (≥ review-inbox dwell). Set fresh per write.",
    )

    # --- Blueprint corpus (Wave 3b-(i), D48) — the DURABLE landed-artifact store the
    # S6 dedup hard key looks up + the S9 promotion `hit_count` reader. A dedicated,
    # access-controlled bucket sibling to learning_candidates/learning_audit, with its
    # OWN RBAC user scoped to it only. Holds landed blueprint artifacts keyed by
    # `canonical_key`; `hit_count` is bumped ATOMICALLY server-side (sub-document
    # counter), never read-modify-write (the D48 "one create + one increment"
    # invariant). Durable by design (no TTL default — a landed artifact + its
    # cross-session hit_count must outlive any candidate/session).
    learning_corpus_connection_string: str = Field(
        "couchbase://localhost",
        description="Couchbase connection string for the learning_corpus bucket.",
    )
    learning_corpus_bucket: str = Field(
        "learning_corpus", description="Dedicated landed-artifact corpus bucket (D48)."
    )
    learning_corpus_username: str = Field(
        "", description="RBAC user scoped to learning_corpus ONLY (learning_corpus_writer)."
    )
    learning_corpus_password: str = Field(
        "", description="Password for the learning_corpus_writer RBAC user (secret)."
    )
    learning_corpus_ttl_seconds: int = Field(
        0,
        ge=0,
        description=(
            "Corpus retention (0 = no expiry — a landed artifact + its cross-session "
            "hit_count is durable and must outlive candidates/sessions)."
        ),
    )

    # --- S6 dedup soft layer (D48 §11) — the embedding near-miss bands. Previously
    # hardcoded in `dedup/stage.py`; surfaced here so an operator can tune them without
    # a code change. They are ORDERED bands over cosine similarity on `intent`:
    # `>= merge` ⇒ a mergeable variant, `>= conflict` ⇒ a partial-overlap conflict,
    # below ⇒ `insert`. NEITHER auto-appends: both route to the review inbox (D48 §3),
    # so a mis-tuned band costs review noise, never a bad landing.
    learning_dedup_merge_threshold: float = Field(
        0.95,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity at/above which an intent near-match is stamped `merge` "
            "(a mergeable blueprint variant, routed to review — never auto-appended)."
        ),
    )
    learning_dedup_conflict_threshold: float = Field(
        0.83,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity at/above which an intent near-match is stamped `conflict` "
            "(partial overlap, routed to review). Must be <= the merge threshold; below "
            "it the candidate is a genuinely-new `insert`."
        ),
    )

    # --- Extractor (Slice 3, D31/D34) — the LLM structured-output model + retry. ---
    learning_extractor_model: str = Field(
        "claude-opus-4-8",
        description="Model id for the extractor's structured-output call (via the runtime ModelClient).",
    )
    learning_extractor_max_retries: int = Field(
        2, ge=0, description="Retries on a malformed (non-tool-call) extractor response (D31)."
    )
    learning_extractor_api_key: str = Field(
        "", description="API key for the extractor model client (secret). Empty ⇒ extractor dormant."
    )
    learning_extractor_base_url: str = Field(
        "", description="Optional OpenAI-compatible base URL for the extractor model client."
    )

    # --- Coverage judge (plan §3b) — the model call that cancels an extraction. ---
    #
    # Every threshold here is a knob because the plan requires it ("everything
    # threshold-shaped must be config, not a constant") and because one of them can
    # DISCARD an analyst's session. They live on `LearningSettings` rather than on
    # `PromotionPolicy` deliberately: `PromotionPolicy` is accepted by all three
    # promotion factories and passed by NO entrypoint, so a knob added there today is a
    # knob that does not exist in production. Wiring that is slice 4's job; a knob that
    # silently cancels work must be live from the first deploy.
    learning_judge_enabled: bool = Field(
        True,
        description=(
            "Master switch for the coverage judge. ON: a session whose work the corpus "
            "already carries is DROPPED before extraction (a durable record is written "
            "to learning_audit for every drop). OFF: the judge is never built and the "
            "loop behaves exactly as it did before plan §3b. Requires a prior-art index "
            "— with no index there is nothing to be covered BY, so no judge is built "
            "regardless of this flag."
        ),
    )
    learning_judge_shadow_mode: bool = Field(
        False,
        description=(
            "SHADOW MODE — the safe rollout. The judge runs, is asked, and records "
            "EVERY verdict to learning_audit exactly as it would in anger, but the drop "
            "is forced to False and logged: nothing is ever discarded. The row carries "
            "`would_drop=true` and `shadow=true`, so "
            "`SELECT count(*) ... WHERE would_drop = true` answers 'what would we have "
            "thrown away last week?' before anything is thrown away. This is NOT the "
            "same as a very high confidence bar (a bar cannot exceed 1.0, and at 1.0 a "
            "model asserting perfect certainty still drops) and NOT the same as "
            "LEARNING_JUDGE_ENABLED=false (which records nothing at all). Run this for "
            "a period, read the distribution, then turn it off."
        ),
    )
    learning_judge_record_ttl_seconds: int = Field(
        94_608_000,  # 3 years
        ge=1,
        description=(
            "Retention for judge verdict records — its OWN clock, deliberately much "
            "longer than LEARNING_AUDIT_TTL_SECONDS. An evidence quote is "
            "entity-bearing and should expire on the D95 90-day floor; a verdict row is "
            "scalars plus one capped reason, and the questions it exists to answer "
            "('how many drops last quarter', 'do verdicts skew to existing-plus-delta') "
            "accumulate over months. Inheriting the evidence retention would erase the "
            "dataset about as fast as its signal accrues."
        ),
    )
    learning_judge_model: str = Field(
        "",
        description=(
            "Model id for the judge's structured-output call. EMPTY => reuse the "
            "extractor's model client. The economics favour a smaller/cheaper model "
            "here: the judge sees a bounded brief plus five summary cards, while the "
            "call it cancels carries the whole session transcript."
        ),
    )
    learning_judge_pre_drop_confidence: float = Field(
        0.90,
        ge=0.0,
        le=1.0,
        description=(
            "PRE-extraction drop bar: a `duplicate` verdict at or above this cancels "
            "extraction entirely. Deliberately HIGHER than the post-extraction bar — "
            "the pre-extraction judge sees raw SQL with literals but no generalization, "
            "no parameterization and no result grain, so it is the cheapest place to "
            "drop and the least-informed one."
        ),
    )
    learning_judge_post_drop_confidence: float = Field(
        0.75,
        ge=0.0,
        le=1.0,
        description=(
            "POST-extraction drop bar: a `duplicate` verdict at or above this discards "
            "an extracted candidate. Lower than the pre-extraction bar because the "
            "judge is shown the generalized template, the grain and the rule ids."
        ),
    )
    learning_judge_band_low: float = Field(
        0.70,
        ge=0.0,
        le=1.0,
        description=(
            "Bottom of the ambiguous prior-art band. Below it nothing in the corpus is "
            "close enough to be worth a model call, at either stage."
        ),
    )
    learning_judge_band_high: float = Field(
        0.97,
        ge=0.0,
        le=1.0,
        description=(
            "Top of the ambiguous band, applied to the POST-extraction judge only. "
            "Above it the deterministic dedup layers and the merge routing already have "
            "an opinion. Not applied pre-extraction: there the score compares a "
            "session's raw text against entity-free corpus intents, which is far "
            "noisier than the post-extraction intent-against-intent comparison, so a "
            "high score is not a settled answer. Must be >= the band low."
        ),
    )
    learning_judge_timeout_seconds: float = Field(
        30.0,
        gt=0.0,
        description=(
            "Hard ceiling on one judge model call. On expiry the judge fails OPEN and "
            "extraction proceeds — it is an optimization and must never be the reason a "
            "session takes longer than it used to."
        ),
    )

    # --- Observability (D23/D24) ---
    otlp_endpoint: str = Field(
        "", description="OTLP collector endpoint (Phoenix). Empty => no-op provider."
    )
    learning_service_name: str = Field(
        "learning-loop", description="OTel service.name / Phoenix project for both processes."
    )
    learning_trace_verbose: bool = Field(
        True,
        description=(
            "D25 GATE, amended 2026-07-15 (deliberate operator posture flip): the default "
            "is now TRUE (verbose). The triage/consume/extract/promote spans AND the "
            "learning-sessions projection carry human-readable content (the user question, "
            "accepted SQL, learned intent/slots, resolves, rationale, evidence quotes, "
            "blueprint id) BY DEFAULT — so the `learning-loop` AND `learning-sessions` "
            "Phoenix projects are ENTITY-BEARING BY DEFAULT and MUST be access-controlled "
            "like the audit/session store (D51 in-boundary PII posture). Set FALSE to "
            "restore the D25 shape-only telemetry posture (counters/labels/session.id only — "
            "no transcript, SQL, question, or intent). The gate MECHANISM is unchanged; only "
            "the default posture flipped."
        ),
    )


def get_learning_settings() -> LearningSettings:
    """Return a fresh `LearningSettings`. Deliberately NOT `@lru_cache`d at the
    kill-switch's expense — but the process-static fields (Redis URL, stream
    names, thresholds) are read once at process start by the entrypoints, so a
    plain constructor is sufficient; the ONLY runtime-toggled value is
    `LEARNING_ENABLED`, served fresh by `learning_enabled()`."""
    return LearningSettings()
