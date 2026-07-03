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

    # --- Observability (D23/D24) ---
    otlp_endpoint: str = Field(
        "", description="OTLP collector endpoint (Phoenix). Empty => no-op provider."
    )
    learning_service_name: str = Field(
        "learning-loop", description="OTel service.name / Phoenix project for both processes."
    )


def get_learning_settings() -> LearningSettings:
    """Return a fresh `LearningSettings`. Deliberately NOT `@lru_cache`d at the
    kill-switch's expense — but the process-static fields (Redis URL, stream
    names, thresholds) are read once at process start by the entrypoints, so a
    plain constructor is sufficient; the ONLY runtime-toggled value is
    `LEARNING_ENABLED`, served fresh by `learning_enabled()`."""
    return LearningSettings()
