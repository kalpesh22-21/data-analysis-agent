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

    # --- Scratch-write side-channel (table-intermediate Slice 2, D93/D90) ---
    # The privileged NON-TOOL materialize/drop routes ride the SAME MCP host as a
    # plain REST POST (not an MCP JSON-RPC tool). When empty, the base is derived
    # from `mcp_url` (host root + `/scratch/v1`), so a Layer-2 deploy that sets
    # only `MCP_URL` wires the scratch surface automatically. Set explicitly to
    # point the runtime at a different scratch host.
    scratch_api_url: str = Field(
        "",
        description="Base URL of the MCP scratch side-channel (…/scratch/v1). Empty → derived from mcp_url.",
    )
    scratch_enabled: bool = Field(
        True,
        description=(
            "Wire the runtime ScratchClient so table-intermediate blueprints run the "
            "materialize-and-join fast path. False → table intermediates stay UNSUPPORTED "
            "(clean raw-loop degrade)."
        ),
    )
    scratch_max_rows: int = Field(
        10_000,
        ge=1,
        description=(
            "Runtime row cap for a materializable table intermediate (OQ-C). An "
            "intermediate over this cap fails closed to the raw loop rather than "
            "POSTing a runaway body (the MCP re-enforces its own SCRATCH_MAX_ROWS)."
        ),
    )
    scratch_max_columns: int = Field(
        256,
        ge=1,
        description="Runtime column cap for a materializable table intermediate (fail-closed over-cap).",
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
        "all-mpnet-base-v2",
        description=(
            "Embedding model id. It is BOTH the EMBEDDING span attribute AND the "
            "load-bearing read-path parity key (D60/D86): `Neo4jVectorIndex` "
            "recall filters `WHERE embedding_model = <this>`, so it MUST match the "
            "id stamped on the corpus at seed time (scripts/seed_neo4j_corpus.py "
            "defaults to the same value) or recall returns an empty corpus."
        ),
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
    reranker_model: str = Field(
        "", description="Reranker model id — RERANKER span attribute only (not a request param)."
    )
    reranker_timeout_seconds: float = Field(
        10.0, gt=0, description="Per-request reranker timeout (seconds)."
    )

    # --- Retrieval pipeline (D7/D8, design §3.4) ---
    # Slice-2 note: the pipeline's real vector store is neo4j (D60). `app.py`
    # now wires `Neo4jVectorIndex` + `RetrievalPipeline` when `neo4j_url` AND an
    # embedder are configured; absent either it leaves retrieval=None (Phase-0
    # parity). `retrieval_enabled=False` is a hard master switch to force empty
    # retrieval even once the store exists.
    retrieval_enabled: bool = Field(
        True, description="Master switch; False => empty RetrievedContext (Phase-0 parity)."
    )
    retrieval_recall_k: int = Field(
        30, ge=1, description="High-recall per-corpus recall fan-out (precision restored by rerank)."
    )
    retrieval_top_k_blueprints: int = Field(
        3, ge=1, description="Blueprint thin cards pre-injected (03 fixes 3)."
    )
    retrieval_top_k_knowledge: int = Field(
        3, ge=1, description="Global-knowledge hits pre-injected."
    )
    retrieval_knowledge_min_score: float | None = Field(
        None,
        description=(
            "Optional knowledge score floor; off by default (ms-marco logits are "
            "uncalibrated — a floor drops good hits more than it catches junk, OQ-R2)."
        ),
    )
    # Model-facing read tools (read-tools-design §1, OQ-T3 — provisional, tune
    # on traffic). `searchBlueprints(query, k)`: absent `k` → default; a given
    # `k` is clamped to `[1, max_k]` (over-ask is clamped, never rejected).
    # `searchKnowledge` has no `k` — it always cuts to `knowledge_k`.
    retrieval_search_default_k: int = Field(
        5, ge=1, description="searchBlueprints default card count when the model omits k."
    )
    retrieval_search_max_k: int = Field(
        20, ge=1, description="searchBlueprints upper clamp on the model-supplied k."
    )
    retrieval_search_knowledge_k: int = Field(
        5, ge=1, description="searchKnowledge fixed cut (a touch above the pre-inject top-3)."
    )
    neo4j_url: str = Field(
        "",
        description=(
            "neo4j bolt URL (D60). Empty => vector index unavailable => retrieval "
            "stays unwired (Phase-0 parity). Wired in Slice 2 when set alongside "
            "an embedder."
        ),
    )
    neo4j_username: str = Field("", description="neo4j username (secret).")
    neo4j_password: str = Field("", description="neo4j password (secret).")
    neo4j_timeout_seconds: float = Field(
        10.0,
        gt=0,
        description=(
            "neo4j connection-acquisition + per-query timeout (seconds); a "
            "recall exceeding it degrades to empty (D86), matching the "
            "embedding/reranker timeout convention."
        ),
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
    # --- D67 resolve_via concept-subset selection (blueprint/rules.py) ---
    # The dynamic `resolve_via` rule expander binds the SUBSET of ranked codes the
    # concept actually names, not the whole domain. `earnings` must bind {EARN},
    # never {EARN, DEDUCTION} (the latter nets deductions into an earnings total).
    resolve_via_gap_threshold: float = Field(
        0.15,
        ge=0,
        le=1,
        description=(
            "Significance threshold on the ranked-score gap: the expander binds "
            "the top prefix up to the FIRST `score[i]-score[i+1] > this` gap. "
            "0.15 sits below the ~0.3-0.4 gap a single-code concept opens (the "
            "0.7·Δcosine term dominates) yet above intra-cluster jitter for a "
            "genuinely multi-code concept. Provisional; tune on Phase-0 traffic."
        ),
    )
    resolve_via_min_confidence: float = Field(
        0.3,
        ge=0,
        le=1,
        description=(
            "Floor on the TOP ranked score: below it the concept confidently "
            "matches no code, so the fast path falls back to the raw loop rather "
            "than guessing a filter. 0.3 ≈ a weak cosine even at full freq weight. "
            "Provisional; tune on Phase-0 traffic."
        ),
    )

    def history_token_budget(self) -> int:
        """Absolute history token budget derived from the model's context window (OQ-G)."""
        return int(self.model_context_window * self.history_token_budget_ratio)

    def scratch_api_base(self) -> str:
        """Resolve the scratch side-channel base URL (…/scratch/v1), no trailing slash.

        Uses `scratch_api_url` when set; otherwise derives it from `mcp_url` by
        replacing the MCP mount path with `/scratch/v1` (the routes live on the
        same MCP host, contract §Q6). E.g. `http://host:18090/mcp` →
        `http://host:18090/scratch/v1`.
        """
        if self.scratch_api_url:
            return self.scratch_api_url.rstrip("/")
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.mcp_url)
        return urlunsplit((parts.scheme, parts.netloc, "/scratch/v1", "", "")).rstrip("/")


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    """Return a cached RuntimeSettings singleton. Call this everywhere config is needed."""
    return RuntimeSettings()
