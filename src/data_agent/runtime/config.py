"""RuntimeSettings — env-var configuration surface for the agent runtime.

Mirrors `clickhouse-api`'s `app/config.py` Settings pattern (pydantic-settings, uppercased
env vars, `.env` file support, `extra="ignore"`). Every field maps 1-to-1 to an environment
variable of the same name, and each field's own comment records how its default was derived.

`max_window_token_spend` exists because `BudgetGuard`'s token ceiling has to be a SPEND
limit, not an occupancy one: borrowing `model_context_window` compared a per-window SUM of
whole replayed requests against a single-request OCCUPANCY limit — quadratic in round count,
ending windows at a fraction of real occupancy. A single request is bounded separately by
`request_token_budget()` at the send seam.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from data_agent.runtime.observability.tracing import DEFAULT_DROP_SPAN_NAMES
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT

# enabled but paycompy is missing.
try:
    from paycompy import vault  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only where paycompy is absent
    vault = None

# Recognized truthy spellings for a kill-switch env var (case-insensitive). Anything
# else (including unset → default) resolves per the rules in the switch's own reader —
# `hydrator_enabled` below, and `learning/config.py::learning_enabled` on the offline
# plane, which imports this set rather than restating it.
#
# ONE set for BOTH switches, and the reason is operator-facing rather than internal:
# they are flipped by the same person from the same deployment config, so an operator
# who finds `on` works for one must find it works for the other. Divergence would not
# raise anywhere — the odd spelling out just reads as FALSE, and a fail-safe kill switch
# reading false silently halts a plane. It is defined on the RUNTIME side because D58c
# forbids `runtime/` importing the offline package, so the shared direction can only run
# inward-to-outward. `_TRUTHY` stays as the module-local spelling.
#
# NOTE for editors: `tests/learning/test_kill_switch.py` enforces D58c by TEXT-scanning
# every file under `runtime/` for the offline package path and that switch's env-var
# name. Neither may be spelled here, in a comment or otherwise. That bluntness is the
# point — do not relax the scan to accommodate prose.
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_TRUTHY = TRUTHY_ENV_VALUES

_logger = logging.getLogger(__name__)

# Headroom multiplier on the request fit budget. The shared chars/4 token
# estimator (context/budget.py::_estimate_tokens) UNDER-counts real tokens on
# punctuation-dense JSON/SQL — the fat trail is exactly that (~3 chars/token), so a
# request "fitted" to the raw window could be ~128-150k REAL tokens and re-trigger
# the front-truncation bug. Applied ONLY at `request_token_budget()` (the request
# fit seam), never to the shared estimator, so trail-compaction math is unchanged.
_REQUEST_BUDGET_HEADROOM = 0.8

_DEFAULT_NEO4J_DATABASE = "neo4j"


def _neo4j_database_or_default(value: str) -> str:
    """Blank/whitespace database name → the explicit default.

    An empty string is NOT the same as unset to the neo4j driver: it means "the
    server's home database", which is a THIRD behaviour an operator never asked for
    and cannot see in the config. `NEO4J_DATABASE=""` in a values file (or a Vault
    key that exists but holds "") reads exactly like "not configured", so it is
    normalised to what "not configured" means everywhere else.
    """
    return value.strip() or _DEFAULT_NEO4J_DATABASE


class _HydratorKillSwitchSettings(BaseSettings):
    """A one-field settings surface for `HYDRATOR_ENABLED` ONLY, constructed FRESH on
    every `hydrator_enabled()` call (never cached, deliberately NOT behind the
    `@lru_cache`d `get_runtime_settings` singleton). Reads BOTH `.env` and the process
    environment so an operator flipping the switch in EITHER place halts the hydrator on
    the next poll cycle with NO restart (mirrors the learning-loop kill-switch)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    hydrator_enabled: str | None = None


def hydrator_enabled() -> bool:
    """Read the hydrator master kill-switch `HYDRATOR_ENABLED` FRESH, EVERY call —
    deliberately bypassing the `@lru_cache`d `RuntimeSettings` so a flip (of the env var
    OR `.env`) takes effect on the next poll cycle with NO restart.

    Default (unset/blank) is enabled. Unrecognized values are treated as DISABLED
    (fail-safe: a typo'd override halts the seeder, it does not silently keep running)."""
    raw = _HydratorKillSwitchSettings().hydrator_enabled
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in _TRUTHY


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

    # --- Semantic-catalog source (D75 Wave 1b — MCP as the single source of truth) ---
    # The runtime rebuilds its immutable CatalogHandle/SemanticCatalogHandle from the
    # MCP's `GET /catalog/export` instead of a local `databaseSchemaDocs/` copy.
    #   * "mcp"     (default) — fetch the export from the live MCP host, cached
    #                 process-wide after the first successful fetch (CatalogCache).
    #   * "fixture" — load the frozen `tests/fixtures/catalog_export.json` (or
    #                 `catalog_fixture_path`) from disk; used by the test suite and
    #                 fully-offline runs. No network.
    catalog_source: str = Field(
        "mcp",
        description="Where the semantic catalog comes from: 'mcp' (live export) or 'fixture' (offline JSON).",
    )
    # The `/catalog/export` route rides the SAME MCP host as a plain authenticated
    # GET (not an MCP tool), like the scratch side-channel. When empty, the base is
    # derived from `mcp_url` (host root + `/catalog`), so a deploy that sets only
    # `MCP_URL` wires the catalog surface automatically.
    catalog_api_url: str = Field(
        "",
        description="Base URL of the MCP catalog export (…/catalog). Empty → derived from mcp_url.",
    )
    # Fixture path used when `catalog_source="fixture"`. Empty → the committed
    # `tests/fixtures/catalog_export.json` relative to the repo root.
    catalog_fixture_path: str = Field(
        "",
        description="Path to the offline catalog-export JSON. Empty → repo tests/fixtures/catalog_export.json.",
    )

    # --- Governed corpus source (Phase 2 — the MCP as the single source of trusted
    # recall canon). The runtime projects the MCP's `GET /blueprints/export` +
    # `GET /knowledge/export` into the neo4j recall corpus as `source="mcp"` canon
    # (the trust partition recall serves); the learning loop stages `source="learning"`
    # nodes recall ignores.
    #   * "mcp"     (default) — fetch both exports from the live MCP host and seed the
    #                 trusted corpus partition (one-shot, self-healing, gc=False).
    #   * "fixture" — read the offline `tests/fixtures/corpus/{blueprints,knowledge}.yaml`
    #                 seeds from disk; used by the test suite + fully-offline runs.
    corpus_source: str = Field(
        "mcp",
        description="Where the recall corpus canon comes from: 'mcp' (live exports) or 'fixture' (offline YAML).",
    )
    # The `/blueprints/export` + `/knowledge/export` routes ride the SAME MCP host root
    # as plain authenticated GETs (NOT under `/catalog`). When empty, the base is
    # derived from `mcp_url` (host root), so a deploy that sets only `MCP_URL` wires the
    # corpus surface automatically. `HttpCorpusClient` appends the two route paths.
    corpus_api_url: str = Field(
        "",
        description="Base URL of the MCP corpus exports (host root; /blueprints/export + /knowledge/export appended). Empty → derived from mcp_url.",
    )
    # Fixture paths used when `corpus_source="fixture"`. Empty → the committed
    # `tests/fixtures/corpus/{blueprints,knowledge}.yaml` relative to the repo root.
    corpus_blueprints_fixture_path: str = Field(
        "",
        description="Path to the offline blueprints seed YAML. Empty → repo tests/fixtures/corpus/blueprints.yaml.",
    )
    corpus_knowledge_fixture_path: str = Field(
        "",
        description="Path to the offline knowledge seed YAML. Empty → repo tests/fixtures/corpus/knowledge.yaml.",
    )

    # --- OpenAI model provider (D71) ---
    openai_api_key: str = Field("", description="OpenAI API key (secret).")
    openai_model: str = Field("gpt-4.1", description="Model name for Responses/Chat Completions.")
    openai_base_url: str = Field("", description="Optional OpenAI-compatible base URL override.")
    model_context_window: int = Field(
        128_000,
        description=(
            "Token budget of the configured model's context window. Used to derive the "
            "total-request fit budget (model_context_window - response_token_reserve)."
        ),
    )
    response_token_reserve: int = Field(
        16_000,
        gt=0,
        description=(
            "Tokens reserved for the model's OUTPUT (completion). The total ASSEMBLED "
            "request (every message handed to send_turn, base prompt included) is fit to "
            "`model_context_window - response_token_reserve` before each model call so it "
            "can never front-truncate the leading base prompt out of the window (the "
            "root-cause of the dropped-system-prompt bug). See "
            "loop/agent_loop.py::_build_canonical_messages + context/budget.py::"
            "fit_request_to_budget."
        ),
    )
    max_tool_result_tokens: int = Field(
        4_000,
        gt=0,
        description=(
            "Per-tool-result cap (approx tokens) on the stored preview of a single tool "
            "call. A large non-tabular result (notably a wide getTableSchema with 100+ "
            "columns) is truncated to this cap with a clear marker rather than stored as "
            "one unbounded ~30k-token blob that survives the row-count-only trail budget. "
            "Applies to every tool result EXCEPT a getTableSchema's column list, which "
            "has its own budget below. See dispatch/tool_dispatcher.py::_build_preview."
        ),
    )
    schema_columns_token_budget: int = Field(
        6_000,
        gt=0,
        description=(
            "SCHEMA_COLUMNS_TOKEN_BUDGET — budget (approx tokens) for the COLUMNS SECTION "
            "of a getTableSchema preview, and for that alone. A schema's table-level "
            "sections (description, grain, temporal, primary_key, join_keys, measures, "
            "rules, ambiguities) ride COMPLETE and are never budgeted: they are the "
            "semantics the system prompt tells the model to read, so trading them away "
            "for column detail buys width with correctness. Deliberately larger than "
            "`max_tool_result_tokens` — at 6,000 the real 130-column "
            "dbpcm_warehouse.employee schema shows ~90% of its columns with their full "
            "documentation; column NAMES are never dropped above ~2,000. Raise it to "
            "show more detail, at ~1 token per 4 characters of the model's request "
            "budget per schema in the trail. See dispatch/schema_preview.py."
        ),
    )

    # --- LLM-generated progress summaries (opt-in; D25 relaxation for the
    # progress channel only). When True, the agent loop fires a small/cheap side
    # LLM over each tool CALL (name + args, NOT results) to mint a natural-
    # language, present-tense progress line ("Querying overtime pay by
    # department…") streamed to the UI alongside the instant template label. This
    # DELIBERATELY relaxes the D25 "no cell/slot values in progress" rule for THIS
    # channel — the value-rich line may include concrete parameters drawn from the
    # tool arguments (docs/08-ui.md). Off by default → zero behavior change (no
    # extra LLM call, no D25 relaxation). The summarization is fire-and-forget and
    # never blocks tool dispatch or the turn result (fail-soft everywhere).
    progress_summary_enabled: bool = Field(
        False,
        description=(
            "Opt-in: run a cheap side LLM over each tool call (name+args) to stream a "
            "natural-language, value-rich progress line to the UI. Adds one LLM call per "
            "tool call and relaxes D25 for the progress channel. Off => byte-identical."
        ),
    )
    openai_summary_model: str = Field(
        "gpt-4.1-mini",
        description=(
            "Cheap/small model id used ONLY for progress-line summarization "
            "(progress_summary_enabled). A second OpenAIModelClient is built on this "
            "model, sharing the OpenAI api_key/base_url with the main client."
        ),
    )
    progress_summary_timeout_seconds: float = Field(
        3.0,
        gt=0,
        description=(
            "Per-call timeout for the progress-line summarization LLM call. On timeout "
            "the summary is dropped (fail-soft) and the instant template label stands."
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

    # --- Observability (D23/D24/D25, Phoenix/OTLP — wired in app.py) ---
    # `app.py::create_app` calls `configure_tracing(otlp_endpoint=..., service_name=...,
    # project_name=...)`: when `otlp_endpoint` is set the runtime exports spans to
    # Phoenix; when empty the provider is a no-op (zero infra required). See
    # `runtime/observability/tracing.py`.
    otlp_endpoint: str = Field(
        "",
        description="OTLP collector endpoint (self-hosted Phoenix). Empty => no-op provider (no export).",
    )
    otlp_service_name: str = Field(
        "data-agent-runtime", description="Service name reported in OTel spans (service.name)."
    )
    otlp_project_name: str = Field(
        "data-agent-runtime",
        description=(
            "Phoenix project the runtime's spans land in (openinference.project.name). "
            "Phoenix groups traces by THIS attribute, not service.name — set in code so "
            "a normal turn is findable as a named project without an env hack."
        ),
    )
    otlp_hide_llm_content: bool = Field(
        False,
        description=(
            "D25 amended 2026-07-15 (deliberate operator posture flip): the default is "
            "now False (REVEAL) — the auto-instrumented OpenAI LLM span carries the raw "
            "prompt + completion, so the runtime Phoenix project is ENTITY-BEARING BY "
            "DEFAULT (raw question + query-derived answer land on the span) and MUST be "
            "access-controlled like the audit/session store (D51). Set True to restore "
            "the D25 shape-only telemetry posture (NO raw prompt/completion — only "
            "shape/timing/model-name/token-counts). The redaction MECHANISM is unchanged; "
            "only which posture is the default flipped. Mirrors LEARNING_TRACE_VERBOSE."
        ),
    )
    otlp_drop_span_names: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_DROP_SPAN_NAMES),
        description=(
            "Span NAMES dropped before export so a reviewer sees only the meaningful "
            "spans of a turn (design §7). Applied centrally by wrapping the exporter "
            "(runtime/observability/tracing.py::_NameFilteringSpanExporter) — never at "
            "a span() call site. Default drops the per-turn plumbing spans "
            "(context.assembly, loop_model_call_start, loop_turn_done) while KEEPING "
            "agent.turn, the OpenAI Response/LLM span, every tool.<name> span, "
            "embedding, rerank, retrieval.recall, and loop_repeated_idempotent_read_"
            "guarded. Add the low-frequency status spans (loop_paused_ask_user, "
            "loop_paused_budget_cap, loop_hard_ceiling_stop, "
            "loop_result_withheld_provenance) to also drop those; set EMPTY ([]) to "
            "disable filtering and export every span. Purely about WHICH spans export "
            "— it does NOT change the CONTENT a kept span carries (that is the "
            "separate otlp_hide_llm_content / otlp_disable_redaction posture)."
        ),
    )
    otlp_disable_redaction: bool = Field(
        True,
        description=(
            "MASTER TELEMETRY DEBUG SWITCH — default TRUE since 2026-08-10 (deliberate "
            "operator posture flip; it defaulted False before). PLAIN ENGLISH FIRST, "
            "because the field name is a double negative and flipping its default makes "
            "the config read backwards: AT THE DEFAULT NOTHING IS REDACTED. Set it FALSE "
            "to turn redaction back ON. "
            "CONSEQUENCE, stated so nobody has to derive it from the mechanism: runtime "
            "spans now carry the REAL tool arguments, hence real ENTITY VALUES — the "
            "actual SQL WITH its literals, the real resolveValues concept/period values "
            "— plus the tool RESULT preview (columns + preview rows + "
            "row_count/truncated) attached to the TOOL span. Combined with "
            "otlp_hide_llm_content (revealed by default since the 2026-07-15 D25 "
            "amendment) the OpenAI LLM span also shows content + exception events (the "
            "effective LLM hide is `otlp_hide_llm_content AND NOT "
            "otlp_disable_redaction`). THE OTLP COLLECTOR IS THEREFORE AS SENSITIVE AS "
            "THE SESSION STORE and must be access-controlled to the same standard (D51): "
            "a Phoenix instance anyone on the network can reach is now a data egress. "
            "That is the price of this default, and it is the thing to decide BEFORE "
            "pointing OTLP_ENDPOINT at a shared collector. "
            "Set FALSE to restore the D25 posture: TOOL/AGENT/CHAIN cell values (SQL "
            "literals / result cells / bound slot values) masked byte-for-byte and no "
            "result preview on any span. Even FALSE was never shape-only OVERALL, "
            "because it does not govern the LLM-span content. "
            "TELEMETRY-ONLY: this changes ONLY what Phoenix sees — it does NOT weaken "
            "any actual scope/PII ENFORCEMENT (D5 credential injection + D57 column-"
            "scope live in the MCP/injected-credentials path, not in the span "
            "redactor). NOT everything is revealed even at the default: the askUser "
            "question (dropped by the guardrail-observer allowlist), the "
            "embedding/rerank/recall text (never an attribute), and the raw column_scope "
            "(only its hash is ever emitted) STAY redacted — the LLM-span reveal (see "
            "otlp_hide_llm_content) partially compensates by showing the model's Q/A. "
            "NAME KEPT DELIBERATELY: `otlp_redact_tool_args=False` would read correctly "
            "at this default, but model_config sets extra='ignore', so at a rename every "
            "deployment carrying an explicit OTLP_DISABLE_REDACTION would have that "
            "setting SILENTLY DROPPED — an operator's explicit choice turned into a "
            "no-op with no error, which is worse than an awkward name. Same treatment "
            "otlp_hide_llm_content got when its default flipped in 2026-07."
        ),
    )

    # --- Auth (JWKS verification, D5/D79/D80/D81/D82 — fields only until Pass B) ---
    jwks_url: str = Field(
        "", description="JWKS endpoint used to verify inbound JWTs (mirrors clickhouse-api)."
    )
    jwt_issuer: str = Field("", description="Expected JWT 'iss' claim.")
    jwt_audience: str = Field("", description="Expected JWT 'aud' claim.")
    verify_signature:bool = Field(False, description="Check signature")

    # --- Offline token mint for the S9 golden-replay probe (S9-activation §1.3/§4) ---
    # The S9 promotion scheduler mints a per-blueprint JWT scoped to the blueprint's
    # `uses` to run the golden-replay grain probe through the MCP `runQuery` choke
    # point (D57 reuse). These are the mint credentials; empty ⇒ the scheduler keeps
    # its fail-closed deferred probe (auto-promotion stays dormant).
    token_service_url: str = Field(
        "",
        description="Full token-IdP mint endpoint (POST /token) for the offline replay JWT.",
    )
    token_issuer_api_key: str = Field(
        "", description="Static issuer API key authorizing the offline POST /token mint (secret)."
    )

    # The three warehouse TENANT claims stamped into that offline replay token. The MCP
    # requires them on every call (`clientcode`/`proc_center`/`jti` → the paycom_*
    # ClickHouse settings its row policies read); omitting them is a 403
    # MISSING_TENANT_CLAIM before any tool runs, which the probe converts into a
    # `probe_unavailable` HOLD indistinguishable from a healthy fail-closed run — i.e.
    # a dark verification gate. See `learning/promotion/token_minter.py::TenantClaims`
    # for WHY a background replay may take its tenant from process config (it carries
    # no user's authority) and what a green replay therefore does NOT prove.
    #
    # SAME env var names as `ui/server.py`'s TENANT_* block, deliberately: one knob per
    # tenant fact, configured once, honored by both the request path and the offline
    # plane. Defaults match the seeded local warehouse
    # (docker/clickhouse-init/hr-4tables-snake-migration.sql), as the UI's do.
    tenant_client_code: str = Field(
        "CLIENT_A", description="'clientcode' claim → ClickHouse setting paycom_client_code."
    )
    tenant_proc_center: str = Field(
        "PC01", description="'proc_center' claim → ClickHouse setting paycom_proc_center."
    )
    tenant_jti: str = Field(
        "TESTJTI001",
        description=(
            "'jti' claim → paycom_authenticated_user. The PRINCIPAL the offline replay "
            "runs as: the employee/payroll row policies gate on this."
        ),
    )

    # --- Session / retention (D22/D44) ---
    session_ttl_seconds: int = Field(
        604_800,  # 7 days
        ge=1,
        description="Single Couchbase TTL applied to both session docs and session_results.",
    )

    # --- Context assembly (D46/D50) ---
    # `history_token_budget_ratio` (0.20) + the `history_token_budget()` derivation it
    # fed were DELETED here (L1, cleanup 2026-08): Tier 2 removed `ContextAssembler(
    # history_token_budget=...)`, their only reader, leaving a knob that read like a
    # live tuning lever and changed nothing. Replayed history is now bounded by
    # `request_token_budget()` at the send seam (`context/budget.py::
    # fit_request_to_budget`), not by a fraction reserved up front.
    #
    # MIGRATION POSTURE: `RuntimeSettings` is `extra="ignore"`, so a deployment still
    # exporting `HISTORY_TOKEN_BUDGET_RATIO` keeps booting — the var is now INERT BY
    # ABSENCE (silently dropped, no validation error), exactly as it was already inert
    # by having no reader. Nothing to un-set before rollout; strip it from env files at
    # leisure.
    preview_row_count: int = Field(
        20, ge=1, description="Max preview rows (N) rendered into model-visible context (OQ-G)."
    )

    # --- Base agent system prompt (always-present leading instruction) ---
    # A concise, static role + operating-procedure prompt prepended as the FIRST
    # `role:"system"` message of every assembled model turn (before the retrieval
    # block and history). Static constant => a D45 rebuild/resume re-derives
    # byte-identical messages; inserted after compaction so it is exempt from the
    # history-token-budget trimming (always leads the messages). Disable to run
    # the raw, prompt-less loop, or override the text to experiment.
    agent_system_prompt_enabled: bool = Field(
        True,
        description="When True (default) prepend the base agent system prompt as the first message.",
    )
    agent_system_prompt: str = Field(
        AGENT_SYSTEM_PROMPT,
        description="The base agent system prompt text (used only when agent_system_prompt_enabled).",
    )

    # --- Agent loop / budget caps (D47/D55, OQ-H) ---
    #
    # RAISED 2026-08-12 ON LIVE MEASUREMENT, and the direction of the evidence is
    # what makes the pair of changes coherent: every observed failure was the WALL
    # CLOCK, never iterations and never tokens. Turns capped at ~61-73s with
    # iterations at ~8 of 15. Two things made a multi-intent turn legitimately
    # longer than the 60s this was set to before any traffic existed: the
    # blueprint-definition gate (finding 22) costs a getBlueprint round-trip per
    # blueprint, and intent tracking adds declare/close rounds around the work.
    max_loop_iterations: int = Field(
        25,
        ge=1,
        description=(
            "Max model<->tool round-trips per budget window. Raised 15 -> 25 with "
            "the wall clock below, so the round ceiling does not become the next "
            "binding constraint at 180s."
        ),
    )
    max_wall_clock_seconds: int = Field(
        180,
        ge=1,
        description=(
            "Max wall-clock seconds per budget window. Raised 60 -> 180: live "
            "multi-intent turns capped here (~61-73s) while iterations sat at ~8 "
            "of 15, so the wall clock — not rounds, not tokens — was what ended "
            "them."
        ),
    )
    max_budget_windows: int = Field(
        3,
        ge=1,
        description=(
            "Hard outer ceiling on 'continue' budget-window grants per turn "
            "(OQ-D). UNCHANGED at 3 alongside the 180s window: this bounds how "
            "many times a turn may be granted a FRESH window after a user "
            "'continue', and each grant is now worth 3x what it was, so 3 windows "
            "is a longer leash than it was, not a shorter one."
        ),
    )
    # ADDED 2026-08-12. `BudgetGuard`'s token ceiling used to be fed
    # `model_context_window` — a per-window SUM of whole replayed requests measured
    # against a single-request OCCUPANCY limit, so it went quadratic in round count
    # and ended windows at 6-14 rounds with the real context at 12-31% of the
    # window. Occupancy was never this counter's job (fit_request_to_budget enforces
    # it per request, verified: at a 10.5k-token-per-result payload the last of 25
    # requests measured 89,229 against the 89,600 budget). What the counter really
    # measures is SPEND, so spend gets its own ceiling here.
    #
    # 1,000,000 = `max_loop_iterations` (25) x 40,000 tokens of average request.
    # Measured full-window spend, driving the real loop and real assembly for the
    # full 25 rounds (scratch probe_full_window_spend.py):
    #     per-result   135 tok ->   177,345   (mean request  7.1k)
    #     per-result   765 tok ->   366,345   (mean request 14.7k)
    #     per-result 2,165 tok ->   786,345   (mean request 31.5k)
    #     per-result 4,265 tok -> 1,350,990   (mean request 54.0k)
    #     per-result 7,065 tok -> 1,605,048   (requests plateaued at the fit budget)
    #     per-result 10,565 tok-> 1,856,965   (requests plateaued at the fit budget)
    # The top of the ORDINARY range (2,165 tok/result is already fatter than live
    # traces) spends 786,345 for a full window, so 1M clears it by ~27% and leaves
    # the just-raised iteration/wall-clock caps as the binding constraints on every
    # payload size actually observed. The bottom of the RUNAWAY range spends
    # 1.35M+, where `fit_request_to_budget` is already dropping trail units every
    # round — the extra spend is re-sending context, not doing analysis — so those
    # windows pause at ~round 21 of 25. The ceiling-free maximum the other caps
    # permit is 25 x (89,600 + 16,000) = 2.64M per window, x3 windows per turn.
    #
    # Counts CACHED prompt tokens at full weight (see loop/budget_guard.py): the
    # discount is priced in HERE instead — ~1M counted tokens is ~$0.75-1.00 at
    # gpt-4.1 blended rates precisely because live traces are ~94% cache-read,
    # where 1M of fresh input would be $2.00.
    #
    # Tripping this is a FEATURE, not a failure: it returns `paused_budget_cap` and
    # asks the user "continue, refine, or stop?" — a "continue" grants a fresh
    # window (`max_budget_windows`). Raising `max_loop_iterations` should prompt a
    # look at this number; it is deliberately NOT derived from it, so a round-count
    # change cannot silently multiply spend.
    max_window_token_spend: int = Field(
        1_000_000,
        gt=0,
        description=(
            "Max cumulative token SPEND (sum of prompt + completion over every "
            "model round-trip, cached prompt tokens included at full weight) per "
            "budget window. NOT an occupancy limit — a single request is bounded "
            "by request_token_budget(). Derived as max_loop_iterations x 40k mean "
            "request: measured full-window spend is 786k for the fattest ordinary "
            "payload and 1.35M+ once requests plateau against the fit budget."
        ),
    )
    max_tool_calls_per_iteration: int = Field(
        8,
        ge=1,
        description=(
            "S3 (2026-07-01 hardening fix): cap on how many of one model "
            "response's tool_calls are dispatched per loop iteration, so a "
            "single pathological/adversarial response requesting hundreds of "
            "calls cannot overshoot the budget window unbounded. UNCHANGED at 8: "
            "the largest batch observed live was 3, so there is no evidence this "
            "binds. (updateAnalysisState is exempt from this cap and separately "
            "bounded — see loop/agent_loop.py's partition.)"
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
    embedding_dimension: int | None = Field(
        None,
        gt=0,
        description=(
            "Embedding vector dimension for the neo4j vector-index DDL. None (default) "
            "⇒ INFER it from the live embedder (embed a probe, read len(vector[0])); set "
            "an int to pin it explicitly. A change here (or a changed embedding model) "
            "against an existing index raises DimensionMismatchError; the singleton "
            "hydrator daemon then nukes + rebuilds the graph at the new dimension."
        ),
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

    # --- Emulated-discovery injection (context/discovery_emulation.py) ---
    # Emulate `listDatabases`+`listTables` once per budget window (through the
    # ToolDispatcher, so credentials/scope/denial-mapping/telemetry stay
    # consistent) and inject synthetic assistant/tool pairs as if the model had
    # already made those calls, so it need not spend completion round-trips on
    # pure discovery. Also seeds the repeated-idempotent-read guard so a re-call is
    # served locally. Degrade-not-fail: a sweep failure injects nothing and the
    # model falls back to the tools. `getTableSchema` stays model-driven.
    discovery_emulation_enabled: bool = Field(
        True,
        description=(
            "When True (default) emulate listDatabases+listTables once per turn "
            "and inject synthetic assistant/tool pairs as if the model had already "
            "called them so it need not call those two discovery tools."
        ),
    )
    # The ONE database the agent actually answers questions from. The emulated
    # discovery sweep lists tables for THIS database only — not for every database
    # `listDatabases` returns. The others on a live warehouse are not analysis
    # surface (`dbpcm_warehouse_security` is the access-control side, `scratch` is
    # the D93 per-session materialization area), so sweeping them spent an MCP
    # round-trip each per turn to inject listings the model should never query.
    #
    # `listDatabases` itself is still emulated in full (it is one round-trip, and
    # its injected pair is what seeds the read guard so the model does not spend a
    # live call rediscovering the same list).
    #
    # Matches the two long-standing hardcoded defaults for the same name —
    # `catalog/loader.py::DEFAULT_DATABASE` and `sqlparse/provenance.py::_DEFAULT_DB`
    # — which is why the default is the literal below rather than a required field.
    base_database: str = Field(
        "dbpcm_warehouse",
        min_length=1,
        description=(
            "The database the agent answers from; the emulated discovery sweep "
            "lists tables for this database only."
        ),
    )
    retrieval_recall_k: int = Field(
        30,
        ge=1,
        description="High-recall per-corpus recall fan-out (precision restored by rerank).",
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
    neo4j_database: str = Field(
        _DEFAULT_NEO4J_DATABASE,
        description=(
            "neo4j database name the runtime reads and the hydrator/learning write "
            "(multi-database deployments). The default is the driver-level default "
            "every call site used before this was honoured, so unset == today. Blank "
            "is normalised to it as well (see _neo4j_database_or_default)."
        ),
    )

    @field_validator("neo4j_database")
    @classmethod
    def _default_blank_neo4j_database(cls, value: str) -> str:
        return _neo4j_database_or_default(value)

    neo4j_timeout_seconds: float = Field(
        10.0,
        gt=0,
        description=(
            "neo4j connection-acquisition + per-query timeout (seconds); a "
            "recall exceeding it degrades to empty (D86), matching the "
            "embedding/reranker timeout convention."
        ),
    )

    # --- Singleton hydrator daemon (owns neo4j seed + nuke/rebuild) ---
    # The runtime pods are pure READERS: an INDEPENDENT `replicas:1` hydrator daemon
    # (scripts/run_hydrator.py) seeds neo4j on boot, polls the MCP for changes and
    # re-seeds live, and owns the DESTRUCTIVE nuke/rebuild on a dimension change. It
    # authenticates to the MCP export routes with the STATIC service key below (no user
    # JWT). The runtime's own catalog-handle fetch ALSO uses this service key (full
    # decouple), so no per-request credential ever reaches the MCP export.
    mcp_service_key: str = Field(
        "",
        description=(
            "Static service key for the MCP export routes (sent as X-Service-Key INSTEAD "
            "of a user JWT). Used by the hydrator daemon AND the runtime's decoupled "
            "catalog-handle fetch. Empty ⇒ the export clients fall back to per-request JWT "
            "auth. Secret."
        ),
    )
    hydrator_poll_interval_seconds: int = Field(
        60,
        gt=0,
        description="The hydrator daemon's re-check cadence (seconds between MCP export polls).",
    )
    # HYDRATOR_ENABLED (the daemon's master kill-switch) is deliberately read UNCACHED,
    # per poll cycle, via the module-level `hydrator_enabled()` accessor — mirroring the
    # learning-loop kill-switch — so a flip halts the seeder without a restart. It is not
    # a field here (a field would freeze behind the `@lru_cache`d settings singleton).

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
    # never the full {EARN, EETAX, DDUCT, NETPAYDIST, EEBEN, ERTAX} domain (folding
    # a deduction/tax code such as DDUCT would corrupt an earnings total).
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

    # --- HashiCorp Vault (sensitive-value sourcing) ---
    # When VAULT_ENABLED=true, sensitive values (the ClickHouse connection, the
    # scratch/security writer credentials, the Redis URL) are
    # read from Vault at startup and OVERRIDE anything from the environment.  Each
    # *_PATH points at a KV secret; a path left empty skips that group (its env or
    # default value is kept).  See load_settings_from_vault() for the read logic and
    # fail-closed behaviour.  Defaults keep Vault OFF so existing env-based deploys
    # are entirely unaffected.
    vault_enabled: bool = Field(
        False,
        description="Read sensitive values from HashiCorp Vault at startup (overrides env).",
    )
    neo4j_creds_path: str = Field(
        "application/datascience/iwant_reporting/neo4j",
        description=(
            "Vault KV path holding the Neo4j connection secret"
        ),
    )
    mcp_service_key_vault_path: str = Field(
        "application/datascience/iwant_reporting/admin",
        description=(
            "Vault KV path holding the MCP export service key (key: "
            "MCP_SERVICE_KEY). Empty → use env/default (and, if that is also "
            "empty, the service-key path stays OFF / fail-closed)."
        ),
    )
    couchbase_vault_path: str = Field(
        "application/datascience/iwant_reporting/couchbase",
        description=(
            "Vault KV path holding the Couchbase connection"
            "Empty → use env/default."
        ),
    )
    openai_api_key_path:str = Field(
        "application/datascience/iwant_reporting/llm",
        description=(
            "Vault KV path holding the OpenAI API Key"
        ),
    )
    
    def effective_agent_system_prompt(self) -> str | None:
        """The base system prompt to prepend, or `None` when disabled.

        `None` reproduces the pre-existing prompt-less loop exactly (no leading
        system message from this feature).
        """
        return self.agent_system_prompt if self.agent_system_prompt_enabled else None

    def request_token_budget(self) -> int:
        """Absolute cap on the FULL assembled request (all messages) handed to `send_turn`: the
                model context window minus the reserve held back for the model's own output, times a
                0.8 headroom factor.

                The 0.8 is headroom because the chars/4 estimator under-counts JSON- and SQL-dense
                content (~3 chars/token in practice): a list "fitted" to the raw window could still
                be 20-30% over the REAL window and let the endpoint front-truncate the leading base
                prompt. The margin is applied ONLY at this request-fit seam, so the shared estimator
                and the trail token math are untouched.

                Clamped to at least 1, so a configuration where the reserve meets or exceeds the
                window can never yield a non-positive budget the fit walk would treat as "drop
                everything".
        """
        headroom = (
            self.model_context_window - self.response_token_reserve
        ) * _REQUEST_BUDGET_HEADROOM
        return max(1, int(headroom))

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

    def catalog_api_base(self) -> str:
        """Resolve the MCP catalog-export base URL (…/catalog), no trailing slash.

                Uses `catalog_api_url` when set; otherwise derives it from `mcp_url` by replacing the
                MCP mount path with `/catalog` (the route lives on the same MCP host).
                `HttpCatalogClient` appends `/export`.

                WARNING: the derivation keeps ONLY `mcp_url`'s scheme and netloc and DISCARDS any
                path prefix, so a path-routed ingress like `https://host/prefix/mcp` yields
                `https://host/catalog` — which may point at the wrong host or 404. Set
                `catalog_api_url` to the explicit base to override the derivation in that case.
        """
        if self.catalog_api_url:
            return self.catalog_api_url.rstrip("/")
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.mcp_url)
        return urlunsplit((parts.scheme, parts.netloc, "/catalog", "", "")).rstrip("/")

    def catalog_fixture_file(self) -> Path:
        """Resolve the offline catalog-export fixture path (`catalog_source="fixture"`).

        Uses `catalog_fixture_path` when set; otherwise the committed
        `tests/fixtures/catalog_export.json` relative to the repo root (this file lives
        at `src/data_agent/runtime/config.py`, so the repo root is 3 parents up)."""
        if self.catalog_fixture_path:
            return Path(self.catalog_fixture_path)
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "tests" / "fixtures" / "catalog_export.json"

    def corpus_api_base(self) -> str:
        """Resolve the MCP corpus-export host root (no trailing slash) — governed
        corpus (Phase 2). `HttpCorpusClient` appends `/blueprints/export` and
        `/knowledge/export`.

        Uses `corpus_api_url` when set; otherwise derives the HOST ROOT from `mcp_url`
        (scheme + netloc only — the two corpus routes live at the host root, NOT under
        the `/mcp` mount NOR under `/catalog`). E.g. `http://host:18090/mcp` →
        `http://host:18090`. Same path-prefix caveat as `catalog_api_base`: a
        path-routed ingress must set `corpus_api_url` explicitly."""
        if self.corpus_api_url:
            return self.corpus_api_url.rstrip("/")
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.mcp_url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")

    def corpus_blueprints_fixture_file(self) -> Path:
        """Resolve the offline blueprints seed YAML (`corpus_source="fixture"`).

        Uses `corpus_blueprints_fixture_path` when set; otherwise the committed
        `tests/fixtures/corpus/blueprints.yaml` (repo root is 3 parents up)."""
        if self.corpus_blueprints_fixture_path:
            return Path(self.corpus_blueprints_fixture_path)
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "tests" / "fixtures" / "corpus" / "blueprints.yaml"

    def corpus_knowledge_fixture_file(self) -> Path:
        """Resolve the offline knowledge seed YAML (`corpus_source="fixture"`).

        Uses `corpus_knowledge_fixture_path` when set; otherwise the committed
        `tests/fixtures/corpus/knowledge.yaml` (repo root is 3 parents up)."""
        if self.corpus_knowledge_fixture_path:
            return Path(self.corpus_knowledge_fixture_path)
        repo_root = Path(__file__).resolve().parents[3]
        return repo_root / "tests" / "fixtures" / "corpus" / "knowledge.yaml"


def effective_llm_hide(settings: RuntimeSettings) -> bool:
    """The EFFECTIVE OpenAI-LLM-content hide, resolving the two observability flags.

        Content is hidden ONLY when `otlp_hide_llm_content` is True AND the master telemetry
        debug switch `otlp_disable_redaction` is False. Disabling redaction forces the reveal, so
        a debugging operator sees the LLM Q/A and exception events alongside the real tool calls:

            hide_llm_content  disable_redaction  -> hidden?
            True              False              -> True   (the ONLY hiding combination)
            True              True               -> False  (revealed; disable wins)
            False             False              -> False
            False             True               -> False  (the shipped default)

        Note what that costs: hiding the LLM content takes TWO settings, not one.
        `OTLP_HIDE_LLM_CONTENT=true` alone no longer hides anything, because
        `otlp_disable_redaction` defaults True and overrides it.

        `app.py` passes this single value to BOTH `configure_tracing(hide_llm_content=)` and
        `instrument_openai(hide_content=)`, so the two content channels can never disagree.
    """
    return settings.otlp_hide_llm_content and not settings.otlp_disable_redaction


def _read_vault_secret(vc, key: str, path: str, current):
    """Read one KV field from Vault, returning ``current`` (env/default) on failure.

    A per-key failure is logged and swallowed so one missing/renamed key does not
    take down the whole config load; the caller's fail-closed check still catches a
    genuinely-absent critical secret (see load_settings_from_vault()).
    """
    try:
        return vc.read_kv_secret(key, path).get_secret_value()
    except Exception as exc:  # noqa: BLE001 - surface as a warning, keep booting
        # KEY, PATH and EXCEPTION TYPE only — never the value, and never the exception's
        # repr/str: a client error may embed the HTTP response body, which is the secret
        # material this function exists to move around.
        _logger.warning(
            "Vault read failed for key %s at path %s (%s); using env/default",
            key,
            path,
            type(exc).__name__,
        )
        return current

def load_settings_from_vault() -> RuntimeSettings:
    """Construct Settings, then override sensitive values from Vault when enabled.

    Priority for each sensitive value:

      1. Vault  — when VAULT_ENABLED and the relevant ``*_PATH`` is set
      2. Environment variable / ``.env``
      3. Field default

    Vault runs AFTER construction (it mutates the already-built Settings) because
    pydantic-settings sources env/defaults at construction time; this mirrors the
    llm-router pattern.  When Vault is disabled the env-built Settings is returned
    unchanged, so nothing about the existing env-based deployment path changes.
    """
    settings = RuntimeSettings()

    if not settings.vault_enabled:
        return settings

    if vault is None:
        raise RuntimeError(
            "VAULT_ENABLED=true but the internal 'paycompy' package is not "
            "installed. Install paycompy or set VAULT_ENABLED=false."
        )

    vc = vault.get_client_using_os_environ()

    # --- Neo4j main connection ---
    if settings.neo4j_creds_path:
        p = settings.neo4j_creds_path
        settings.neo4j_url = _read_vault_secret(
            vc, "NEO4J_URL", p, settings.neo4j_url
        )
        settings.neo4j_username = _read_vault_secret(
            vc, "NEO4J_USERNAME", p, settings.neo4j_username
        )
        settings.neo4j_password = _read_vault_secret(
            vc, "NEO4J_PASSWORD", p, settings.neo4j_password
        )
        # Normalised HERE too: Vault writes the field after construction, so the
        # field validator never sees this value, and a KV key that exists but holds
        # "" would reach the driver as "the server's home database".
        settings.neo4j_database = _neo4j_database_or_default(
            _read_vault_secret(vc, "NEO4J_DATABASE", p, settings.neo4j_database)
        )

    if settings.mcp_service_key_vault_path:
        settings.mcp_service_key = _read_vault_secret(
            vc, "OIDC_PUBLIC_KEY", settings.mcp_service_key_vault_path, settings.mcp_service_key
        )

    # --- Employee-access Redis URL ---
    if settings.couchbase_vault_path:
        p = settings.couchbase_vault_path
        settings.couchbase_bucket = _read_vault_secret(
            vc, "COUCHBASE_BUCKET", p, settings.couchbase_bucket
        )
        settings.couchbase_connection_string = _read_vault_secret(
            vc, "COUCHBASE_CONNECTION_STRING", p, settings.couchbase_connection_string
        )
        settings.couchbase_username = _read_vault_secret(
            vc, "COUCHBASE_USERNAME", p, settings.couchbase_username
        )
        settings.couchbase_password = _read_vault_secret(
            vc, "COUCHBASE_PASSWORD", p, settings.couchbase_password
        )
    if settings.openai_api_key_path:
        settings.openai_api_key = _read_vault_secret(
            vc, "OPENAI_API_KEY", settings.openai_api_key_path, settings.openai_api_key
        )

    # --- MCP export service key ---
    if settings.mcp_service_key_vault_path:
        settings.mcp_service_key = _read_vault_secret(
            vc, "API_KEY", settings.mcp_service_key_vault_path, settings.mcp_service_key
        )
    return settings

@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    """Return a cached RuntimeSettings singleton. Call this everywhere config is needed."""
    return load_settings_from_vault()