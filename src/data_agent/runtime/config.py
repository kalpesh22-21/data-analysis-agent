"""RuntimeSettings — env-var configuration surface for the Phase-0 agent runtime.

Mirrors `clickhouse-api`'s `app/config.py` Settings pattern (pydantic-settings,
uppercased env vars, `.env` file support, `extra="ignore"`). Every field maps
1-to-1 to an environment variable of the same name.

Tunable defaults below are LOCKED per the orchestrator's Pass-A brief (not
independently re-derived): session_ttl_seconds=604800 (7 days),
preview_row_count=20, history_token_budget_ratio=0.20, max_loop_iterations=15,
max_wall_clock_seconds=60, max_budget_windows=3. See docs/decisions/
phase0-runtime-design.md §11 (OQ-D/G/H/I) for the provenance of these numbers
— they are explicitly provisional pending real Phase-0 traffic.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """All runtime configuration, read from environment variables (or `.env`)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Adopted MCP (clickhouse-api, D75) ---
    mcp_url: str = Field(
        "http://localhost:18090/mcp",
        description="Streamable-HTTP endpoint of the adopted clickhouse-api MCP.",
    )

    # --- OpenAI model provider (fields only — unused until Pass B, D71) ---
    openai_api_key: str = Field("", description="OpenAI API key (secret). Unused in Pass A.")
    openai_model: str = Field(
        "gpt-4.1", description="Model name for Responses/Chat Completions. Unused in Pass A."
    )
    openai_base_url: str = Field(
        "", description="Optional OpenAI-compatible base URL override. Unused in Pass A."
    )
    model_context_window: int = Field(
        128_000,
        description=(
            "Token budget of the configured model's context window. Used to derive the "
            "absolute history token budget (history_token_budget_ratio * this value)."
        ),
    )

    # --- Couchbase session store (D22/D44/D45) ---
    couchbase_connection_string: str = Field(
        "couchbase://localhost", description="Couchbase cluster connection string."
    )
    couchbase_username: str = Field("", description="Couchbase username (secret).")
    couchbase_password: str = Field("", description="Couchbase password (secret).")
    couchbase_bucket: str = Field("agent_sessions", description="Bucket holding session docs.")
    couchbase_scope: str = Field("_default", description="Scope for the session collection.")
    couchbase_sessions_collection: str = Field(
        "sessions", description="Collection name for session documents."
    )
    couchbase_results_collection: str = Field(
        "session_results",
        description="Collection name for full (non-preview) tool results, keyed by UUID.",
    )

    # --- Observability (D23/D24/D25, Phoenix/OTLP — fields only until Pass B) ---
    otlp_endpoint: str = Field(
        "", description="OTLP collector endpoint (self-hosted Phoenix). Unused in Pass A."
    )
    otlp_service_name: str = Field(
        "data-agent-runtime", description="Service name reported in OTel spans."
    )

    # --- Auth (JWKS verification, D5/D79/D80/D81/D82 — fields only until Pass B) ---
    jwks_url: str = Field(
        "", description="JWKS endpoint used to verify inbound JWTs (mirrors clickhouse-api)."
    )
    jwt_issuer: str = Field("", description="Expected JWT 'iss' claim.")
    jwt_audience: str = Field("", description="Expected JWT 'aud' claim.")

    # --- Session / retention (D22/D44) ---
    session_ttl_seconds: int = Field(
        604_800,  # 7 days
        ge=1,
        description="Single Couchbase TTL applied to both session docs and session_results.",
    )

    # --- Context assembly (D46/D50) ---
    preview_row_count: int = Field(
        20, ge=1, description="Max preview rows (N) rendered into model-visible context (OQ-G)."
    )
    history_token_budget_ratio: float = Field(
        0.20,
        gt=0,
        le=1,
        description="Fraction of model_context_window reserved for replayed history (OQ-G).",
    )

    # --- Agent loop / budget caps (D47/D55, OQ-H) ---
    max_loop_iterations: int = Field(
        15, ge=1, description="Max model<->tool round-trips per budget window."
    )
    max_wall_clock_seconds: int = Field(
        60, ge=1, description="Max wall-clock seconds per budget window."
    )
    max_budget_windows: int = Field(
        3,
        ge=1,
        description="Hard outer ceiling on 'continue' budget-window grants per turn (OQ-D).",
    )
    max_tool_calls_per_iteration: int = Field(
        8,
        ge=1,
        description=(
            "S3 (2026-07-01 hardening fix): cap on how many of one model "
            "response's tool_calls are dispatched per loop iteration, so a "
            "single pathological/adversarial response requesting hundreds of "
            "calls cannot overshoot the budget window unbounded."
        ),
    )

    # --- Embedding client (D71 custom API — resolveValues ranking) ---
    embedding_api_url: str = Field(
        "",
        description=(
            "Full custom embedding API endpoint URL (D71), e.g. "
            "http://localhost:8003/embed. Empty => freq-only degrade."
        ),
    )
    embedding_api_key: str = Field(
        "", description="Optional embedding API bearer key (mock needs none; prod TBD)."
    )
    embedding_model: str = Field(
        "", description="Embedding model id — EMBEDDING span attribute only (not a request param)."
    )
    embedding_timeout_seconds: float = Field(
        10.0, gt=0, description="Per-request embedding timeout (seconds)."
    )

    # --- Reranker client (D71 custom API — retrieval pipeline building block) ---
    # Not wired into app.py yet; consumed by the upcoming retrieval brick.
    reranker_api_url: str = Field(
        "", description="Custom reranker API endpoint (D71). Empty => unconfigured."
    )
    reranker_api_key: str = Field("", description="Reranker API key (secret).")
    reranker_timeout_seconds: float = Field(
        10.0, gt=0, description="Per-request reranker timeout (seconds)."
    )

    # --- resolveValues composite (D77) ---
    resolve_values_query_limit: int = Field(
        200, ge=1, description="LIMIT N on the backing runQuery (candidate pool)."
    )
    resolve_values_top_k: int = Field(
        10, ge=1, description="Max ranked values returned to the model after ranking."
    )
    resolve_values_similarity_weight: float = Field(
        0.7,
        ge=0,
        le=1,
        description="Weight w on cosine similarity vs. normalized log-freq in the score.",
    )

    def history_token_budget(self) -> int:
        """Absolute history token budget derived from the model's context window (OQ-G)."""
        return int(self.model_context_window * self.history_token_budget_ratio)


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    """Return a cached RuntimeSettings singleton. Call this everywhere config is needed."""
    return RuntimeSettings()
