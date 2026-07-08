"""retrieval/tools.py — the three model-facing knowledge-plane read tools (D8/D77).

`searchBlueprints`, `getBlueprint`, `searchKnowledge` — runtime-implemented tools
(read-tools-design), intercepted in the agent loop through the `RuntimeTool`
registry exactly like `resolveValues`: each returns an inline `ToolResult`, counts
as exactly one `tool_calls_made`, and never reaches `ToolDispatcher.dispatch`
under its own name. They are the read-path siblings of the data-less tools
(`listDatabases`/`listTables`/`explainQuery`): every result is corpus METADATA
(blueprint intents, a blueprint's `uses` column IDENTIFIERS, knowledge prose),
never out-of-scope warehouse row data — so `ToolResult.provenance` is the
safe-empty `frozenset()` (determined, zero warehouse columns → always kept in
D44 replay), NEVER `None` (§3).

Shape/behaviour (read-tools §1/§3/§6):
  - `searchBlueprints(query, k)` → reranked ThinCards, scope PRE-FILTERED (a card
    the user cannot run is never returned); `degraded=true` on the recall-order
    degrade so the model knows ranking is weaker.
  - `getBlueprint(id)` → the stored D87 projection; out-of-scope AND absent both
    return the identical `{found: false}` (the §3 non-oracle — no scope-probing).
  - `searchKnowledge(query)` → reranked chunks; BYPASSES scope (entity-agnostic,
    leakage-gated at write, D58(a)).
All degrade-not-fail: a wired-but-degraded stack returns `status="ok"` + empty +
`degraded=true`; malformed args fail-closed to `RETRIEVAL_TOOL_INVALID_ARGS`
before any work (`k` is clamped to `[1, max_k]`, never rejected for over-ask).
The unwired case (no pipeline/store) is handled one level up in the loop registry
(`RETRIEVAL_TOOL_UNAVAILABLE`), so these tools always hold live dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolResult,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.redaction import tool_span_args

from . import scope_filter
from .models import Candidate

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials

    from .pipeline import RetrievalPipeline
    from .vector_index import VectorIndex

INVALID_ARGS_CODE = "RETRIEVAL_TOOL_INVALID_ARGS"
UNAVAILABLE_CODE = "RETRIEVAL_TOOL_UNAVAILABLE"

# B4-parity: a last-resort code for an UNEXPECTED crash that slipped every guard
# (the pipeline/store degrade-not-fail, but a programming error must never abort
# the turn or leak `str(exc)`). Reuses the shared invalid-args code family? No —
# an internal crash is distinct and non-retryable.
INTERNAL_ERROR_CODE = "RETRIEVAL_TOOL_INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "Blueprint/knowledge search hit an internal error. Please try again."

_logger = logging.getLogger(__name__)


class _ReadTool:
    """Shared plumbing for the three read tools: one TOOL span (with `query`
    redacted, §5), symmetric `tool_dispatch_start`/`ok`/`error` progress, a
    B4-parity crash guard, and the `provenance = frozenset()` guarantee baked
    into every `ToolResult` these tools ever return."""

    tool_name: str = ""

    def __init__(
        self,
        *,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        self._observer = observer
        self._tracer = tracer
        # Access-controlled TELEMETRY DEBUG switch (RuntimeSettings.
        # otlp_disable_redaction). Default False keeps the D25 span (`query`
        # redacted, §5). When True the span carries the REAL `query` free text —
        # telemetry-only; `_guarded` below always gets the raw model_args, so the
        # search/scope-filter path is unaffected.
        self._disable_redaction = disable_redaction

    async def run(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        self._observer("tool_dispatch_start", {"tool_name": self.tool_name})
        if self._tracer is None:
            result = await self._guarded(model_args, credentials)
        else:
            with tracing.tool_span(
                self._tracer,
                tool_name=self.tool_name,
                args=tool_span_args(
                    self.tool_name, model_args, disable_redaction=self._disable_redaction
                ),
                status="ok",
                error_code=None,
                reveal_complex_args=self._disable_redaction,
            ) as span:
                result = await self._guarded(model_args, credentials)
                span.set_attribute("tool.status", result.status)
                if result.error_code is not None:
                    span.set_attribute("tool.error_code", result.error_code)
        if result.status == "ok":
            self._observer("tool_dispatch_ok", {"tool_name": self.tool_name})
        else:
            self._observer(
                "tool_dispatch_error",
                {"tool_name": self.tool_name, "error_code": result.error_code},
            )
        return result

    async def _guarded(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        try:
            return await self._execute(model_args, credentials)
        except Exception:  # noqa: BLE001 - B4-parity: never abort the turn / leak str(exc)
            _logger.exception(
                "%s internal error (session=%s)", self.tool_name, credentials.session_id
            )
            return self._error(INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE, retryable=False)

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- ToolResult builders --------------------------------------------------

    def _ok(
        self,
        result_full: dict[str, Any],
        *,
        provenance: frozenset[tuple[str, str]] | None = frozenset(),
        preview_row_count: int = 20,
    ) -> ToolResult:
        """*provenance* defaults to the safe-empty `frozenset()` (determined,
        zero warehouse columns → always kept in D44 replay) — correct for
        searchBlueprints/searchKnowledge and the getBlueprint not-found path.
        `getBlueprint`'s FOUND path overrides it with the blueprint's scoped
        `uses` footprint so the entry drops under a later scope narrowing (§3)."""
        return ToolResult(
            status="ok",
            tool_name=self.tool_name,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=provenance,
            result_preview=_build_preview(result_full, preview_row_count),
            result_full=result_full,
        )

    def _error(self, code: str, message: str, *, retryable: bool) -> ToolResult:
        return ToolResult(
            status="error",
            tool_name=self.tool_name,
            error_code=code,
            retryable=retryable,
            user_message=message,
            provenance=frozenset(),
            result_preview=None,
            result_full=None,
        )


def _clamp_k(
    raw: Any, *, default_k: int, max_k: int
) -> tuple[int | None, str | None]:
    """Resolve the model-supplied `k`: absent → *default_k*; a non-integer or a
    value < 1 is malformed (§6, fail-closed); a valid `k` over *max_k* is CLAMPED
    (a lenient over-ask, not a rejection, §9). Returns `(k, None)` or
    `(None, error_message)`."""
    if raw is None:
        # N3: a mis-set config (default_k > max_k) must never leak an
        # over-max default through the tool — clamp the default too.
        return min(default_k, max_k), None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, "'k' must be an integer number of cards."
    if raw < 1:
        return None, "'k' must be a positive integer."
    return min(raw, max_k), None


def _uses_to_provenance(uses: frozenset[str]) -> frozenset[tuple[str, str]] | None:
    """Split a blueprint's scoped `uses` ("database.table.column" keys) into the
    `(db_table, column)` provenance tuples the D44 replay filter consumes — it
    rebuilds each key as `f"{db_table}.{column}"` and checks scope membership
    (`context/scope_filter.is_provenance_in_scope`). This is what makes a
    getBlueprint trail entry DROP under a later scope narrowing (S1/§3): its
    footprint is no longer a subset of the narrowed scope.

    Fail-closed: any malformed key (no dot, empty part) → `None` for the WHOLE
    set (undetermined → dropped from replay), never a partial set that could
    fail-open. An empty `uses` → `frozenset()` (determined, zero columns → kept)."""
    tuples: set[tuple[str, str]] = set()
    for use in uses:
        db_table, sep, column = use.rpartition(".")
        if not sep or not db_table or not column:
            return None
        tuples.add((db_table, column))
    return frozenset(tuples)


def _put_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    """Add *key*→*value* only when the blueprint actually stored the DAG field
    (non-None) — keeps the `getBlueprint` FOUND shape strictly additive so a
    DAG-less D87/D88 blueprint renders byte-identically to before (§1.3)."""
    if value is not None:
        target[key] = value


def _require_text(raw: Any, name: str) -> tuple[str | None, str | None]:
    """A required non-blank free-text arg (`query`/`id`). Fail-closed on
    missing/blank/non-string, naming ONLY the bad arg (no enumeration, §6)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, f"A non-empty '{name}' is required."
    return raw, None


class SearchBlueprintsTool(_ReadTool):
    """`searchBlueprints(query, k)` — reranked, scope-pre-filtered thin cards."""

    tool_name = "searchBlueprints"

    def __init__(
        self,
        *,
        pipeline: RetrievalPipeline,
        default_k: int,
        max_k: int,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        super().__init__(observer=observer, tracer=tracer, disable_redaction=disable_redaction)
        self._pipeline = pipeline
        self._default_k = default_k
        self._max_k = max_k
        self._preview_row_count = preview_row_count

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        query, err = _require_text(model_args.get("query"), "query")
        if err is not None:
            return self._error(INVALID_ARGS_CODE, err, retryable=True)
        k, k_err = _clamp_k(model_args.get("k"), default_k=self._default_k, max_k=self._max_k)
        if k_err is not None:
            return self._error(INVALID_ARGS_CODE, k_err, retryable=True)

        cards, reranked = await self._pipeline.search_blueprints(
            question=query,  # type: ignore[arg-type]
            column_scope=credentials.column_scope,
            k=k,  # type: ignore[arg-type]
        )
        result_full: dict[str, Any] = {
            "count": len(cards),
            "degraded": not reranked,
            "blueprints": [
                {
                    "id": card.id,
                    "intent": card.intent,
                    "slots_summary": card.slots_summary,
                    "score": card.score,
                }
                for card in cards
            ],
        }
        return self._ok(result_full, preview_row_count=self._preview_row_count)


class SearchKnowledgeTool(_ReadTool):
    """`searchKnowledge(query)` — reranked knowledge chunks, scope-bypassed."""

    tool_name = "searchKnowledge"

    def __init__(
        self,
        *,
        pipeline: RetrievalPipeline,
        knowledge_k: int,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        super().__init__(observer=observer, tracer=tracer, disable_redaction=disable_redaction)
        self._pipeline = pipeline
        self._knowledge_k = knowledge_k
        self._preview_row_count = preview_row_count

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        query, err = _require_text(model_args.get("query"), "query")
        if err is not None:
            return self._error(INVALID_ARGS_CODE, err, retryable=True)

        hits, reranked = await self._pipeline.search_knowledge(
            question=query,  # type: ignore[arg-type]
            k=self._knowledge_k,
        )
        result_full: dict[str, Any] = {
            "count": len(hits),
            "degraded": not reranked,
            "knowledge": [
                {"id": hit.id, "text": hit.text, "score": hit.score, "title": hit.title}
                for hit in hits
            ],
        }
        return self._ok(result_full, preview_row_count=self._preview_row_count)


class GetBlueprintTool(_ReadTool):
    """`getBlueprint(id)` — keyed fetch of the stored projection; out-of-scope OR
    absent → the identical `{found: false}` (the §3 non-oracle)."""

    tool_name = "getBlueprint"

    def __init__(
        self,
        *,
        vector_index: VectorIndex,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        super().__init__(observer=observer, tracer=tracer, disable_redaction=disable_redaction)
        self._vector_index = vector_index
        self._preview_row_count = preview_row_count

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        blueprint_id, err = _require_text(model_args.get("id"), "id")
        if err is not None:
            return self._error(INVALID_ARGS_CODE, err, retryable=True)

        detail = await self._vector_index.get_blueprint(blueprint_id)  # type: ignore[arg-type]
        # A store failure returns None too (degrade), rendered identically to a
        # real miss. The scope check reuses the CANONICAL blueprint predicate:
        # `uses is None` (undetermined) → fail-closed → not-found, never fail-open.
        if detail is None or not scope_filter.is_blueprint_in_scope(
            Candidate(id=detail.id, kind="blueprint", text=detail.intent, uses=detail.uses),
            credentials.column_scope,
        ):
            return self._ok({"found": False}, preview_row_count=self._preview_row_count)

        # Determined at the scope check above: `detail.uses` is not None.
        uses = detail.uses if detail.uses is not None else frozenset()
        result_full: dict[str, Any] = {
            "found": True,
            "id": detail.id,
            "intent": detail.intent,
            "slots_summary": detail.slots_summary,
            # A sorted list is deterministic; `uses` is a subset of scope here
            # (the check above passed), and is column IDENTIFIERS, never data.
            "uses": sorted(uses),
            "status": detail.status,
            "drift_status": detail.drift_status,
            "hit_count": detail.hit_count,
            "catalog_sha": detail.catalog_sha,
        }
        # Additive full-DAG expansion (runblueprint-design §1.3) — the D8
        # progressive-disclosure "expand" step is now complete: the model sees the
        # typed `slots` (to fill), `resolves`, `uses_rules`, `result_grain`, and
        # SQL. Rendered ONLY when the blueprint stored them (a DAG-less D87/D88
        # blueprint carries `None` → the FOUND shape is byte-identical to before,
        # keeping the extension strictly additive). The non-oracle {found:false}
        # posture (D88(b)) above is unchanged.
        _put_if_present(result_full, "resolves", detail.resolves)
        _put_if_present(result_full, "slots", detail.slots)
        _put_if_present(result_full, "uses_rules", detail.uses_rules)
        _put_if_present(result_full, "sql_template", detail.sql_template)
        _put_if_present(result_full, "composes", detail.composes)
        _put_if_present(result_full, "result_grain", detail.result_grain)
        # S1: provenance is the blueprint's SCOPED uses footprint (NOT the
        # safe-empty frozenset()) — this is the same class of info getTableSchema
        # exposes (column identifiers) and, like it, must drop from D44 replay
        # under a later scope narrowing so a now-forbidden blueprint's existence +
        # footprint is not re-surfaced (06-security §scope table, §3).
        return self._ok(
            result_full,
            provenance=_uses_to_provenance(uses),
            preview_row_count=self._preview_row_count,
        )


__all__ = [
    "GetBlueprintTool",
    "SearchBlueprintsTool",
    "SearchKnowledgeTool",
]
