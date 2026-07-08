"""resolve_values.py — the `resolveValues` runtime composite tool (D77, design §1).

Model interface (fixed contract):
    resolveValues(table, column, concept, period?) -> [{value, description, score, freq}]

Under the hood the runtime issues **one ordinary `runQuery`**
(`SELECT <column>, <descCol>, count() AS freq FROM <table> [WHERE <period>]
GROUP BY … ORDER BY freq DESC LIMIT N`) through the existing
`ToolDispatcher.dispatch("runQuery", …)` — so D5 credential injection, D57/D5
column-scope enforcement, provenance capture, denial mapping, preview building,
and the inner TOOL span all come for FREE and unchanged — then ranks the rows
by semantic similarity of `concept` (embedding client, D71) blended with `freq`.

Key invariants (design §§1-8):
  - `concept` NEVER touches SQL (D10) — it is only ever embedded + compared.
  - `table`/`column`/`period.column` are catalog-allowlisted before any SQL is
    built (`sql_builder.resolve_target`); unknown/ambiguous -> fail-closed
    `RESOLVE_VALUES_UNKNOWN_TARGET` (retryable), never reaching the MCP.
  - Enforcement is the inner `runQuery`'s (D57/D5) — every returned value is
    provably in-scope regardless of ranking.
  - Embedding failure -> degrade to frequency-only ranking, `degraded=True`
    (design §3.2) — it is NOT a tool-call failure.
  - Inner denials/errors pass through `classify_denial` verbatim with
    `tool_name="resolveValues"` (design §7); empty result -> ok + empty list.
  - The returned `ToolResult` carries the INNER runQuery's provenance (design §8).

Two entry points (design §1.4): `run()` (model tool-call path -> `ToolResult`)
is a thin adapter over `resolve()` (typed in, typed out -> `ResolveOutcome`),
which D67's rule expander can call directly later without a model round-trip.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite import ranking, sql_builder
from data_agent.runtime.composite.ranking import ResolvedValue, RowValue
from data_agent.runtime.composite.sql_builder import Period, TargetValidationError
from data_agent.runtime.dispatch.denial_mapping import DenialInfo, classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    ToolResult,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.model.embedding_client import EmbeddingClient, EmbeddingError
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.redaction import tool_span_args
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

TOOL_NAME = "resolveValues"
UNKNOWN_TARGET_CODE = "RESOLVE_VALUES_UNKNOWN_TARGET"
# B4-parity: a last-resort code for an UNEXPECTED crash anywhere in the
# composite pipeline (malformed backing data that slipped every guard, a
# programming error). Never carries raw exception text (D5/D25).
INTERNAL_ERROR_CODE = "RESOLVE_VALUES_INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "Something went wrong resolving those values. Please try again."

# The inner query's freq column alias (must match sql_builder.build_sql).
_FREQ_COLUMN = "freq"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolveOutcome:
    """Typed result of a `resolve()` call — the D67 programmatic surface (design §1.4)."""

    status: Literal["ok", "denied", "error"]
    values: list[ResolvedValue] = field(default_factory=list)
    provenance: frozenset[tuple[str, str]] | None = None
    denial: DenialInfo | None = None
    degraded: bool = False
    error_code: str | None = None
    user_message: str | None = None
    retryable: bool | None = None
    top_margin: float | None = None


def parse_period(raw: Any) -> Period | None:
    """Parse the model's optional structured `period` arg into a `Period`.

    Fail-closed (design §2.4): a present-but-malformed `period` (not an object,
    or missing/blank `column`, or non-string `start`/`end`) raises
    `TargetValidationError` rather than being silently dropped.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TargetValidationError("The 'period' argument must be an object.")
    column = raw.get("column")
    if not isinstance(column, str) or not column:
        raise TargetValidationError("A 'period' requires a 'column' to filter on.")
    start = raw.get("start")
    end = raw.get("end")
    for label, value in (("start", start), ("end", end)):
        if value is not None and not isinstance(value, str):
            raise TargetValidationError(f"'period.{label}' must be an ISO date string.")
    return Period(column=column, start=start, end=end)


class ResolveValuesComposite:
    """The `resolveValues` composite (design §1) — intercepted in the agent loop."""

    def __init__(
        self,
        *,
        tool_dispatcher: ToolDispatcher,
        catalog: CatalogHandle,
        embedding_client: EmbeddingClient | None = None,
        query_limit: int = 200,
        top_k: int = 10,
        similarity_weight: float = 0.7,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        self._tool_dispatcher = tool_dispatcher
        self._catalog = catalog
        self._embedding_client = embedding_client
        self._query_limit = query_limit
        self._top_k = top_k
        self._similarity_weight = similarity_weight
        self._preview_row_count = preview_row_count
        self._observer = observer
        self._tracer = tracer
        # Access-controlled TELEMETRY DEBUG switch (RuntimeSettings.
        # otlp_disable_redaction). Default False keeps the D25 span (concept
        # redacted, period literals masked). When True the span carries the REAL
        # concept/period values — telemetry-only; `_safe_run_inner` below always
        # gets the raw model_args, so resolution/enforcement is unaffected.
        self._disable_redaction = disable_redaction

    # -- model tool-call path -------------------------------------------------

    async def run(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        """Model tool-call path: validate args, resolve, wrap as a `ToolResult`.

        Emits one `TOOL` span for `resolveValues` (with `concept` redacted and
        `period` literals masked, design §6.1 — UNLESS the access-controlled
        `otlp_disable_redaction` debug switch is on, which reveals the real
        concept/period values on the span, telemetry-only); the inner `runQuery` TOOL span
        nests inside it via the ambient OTel context. The whole pipeline is
        wrapped in a B4-parity guard (`_safe_run_inner`): an UNEXPECTED crash
        never propagates out to abort the turn or leak `str(exc)` — it degrades
        to a clean `status="error"` `ToolResult`, logged server-side only.
        """
        if self._tracer is None:
            outcome = await self._safe_run_inner(model_args, credentials)
        else:
            with tracing.tool_span(
                self._tracer,
                tool_name=TOOL_NAME,
                args=tool_span_args(
                    TOOL_NAME, model_args, disable_redaction=self._disable_redaction
                ),
                status="ok",
                error_code=None,
                reveal_complex_args=self._disable_redaction,
            ) as span:
                outcome = await self._safe_run_inner(model_args, credentials)
                span.set_attribute("tool.status", outcome.status)
                if outcome.error_code is not None:
                    span.set_attribute("tool.error_code", outcome.error_code)

        return self._outcome_to_tool_result(outcome)

    async def _safe_run_inner(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ResolveOutcome:
        try:
            return await self._run_inner(model_args, credentials)
        except Exception:
            # B4-parity: the composite has no equivalent of ToolDispatcher's
            # transport-exception guard, so an unguarded crash here (a malformed
            # backing result that slipped every guard, etc.) would abort the
            # whole turn. Log the real exception server-side ONLY; the
            # model/user only ever sees the generic canned message (D5/D25).
            _logger.exception(
                "resolveValues internal error (session=%s)", credentials.session_id
            )
            self._observer(
                "tool_dispatch_error",
                {"tool_name": TOOL_NAME, "error_code": INTERNAL_ERROR_CODE},
            )
            return ResolveOutcome(
                status="error",
                error_code=INTERNAL_ERROR_CODE,
                user_message=_INTERNAL_ERROR_MESSAGE,
                retryable=False,
            )

    async def _run_inner(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
    ) -> ResolveOutcome:
        table = model_args.get("table")
        column = model_args.get("column")
        concept = model_args.get("concept")
        # L3: missing/mistyped-arg errors emit the same start+error observer
        # pair the catalog-validation path does, so progress events are
        # symmetric regardless of WHERE validation fails.
        if (
            not isinstance(table, str)
            or not table
            or not isinstance(column, str)
            or not column
            or not isinstance(concept, str)
            or not concept
        ):
            self._observer("tool_dispatch_start", {"tool_name": TOOL_NAME})
            self._observer(
                "tool_dispatch_error",
                {"tool_name": TOOL_NAME, "error_code": UNKNOWN_TARGET_CODE},
            )
            missing = (
                "table"
                if not isinstance(table, str) or not table
                else "column"
                if not isinstance(column, str) or not column
                else "concept"
            )
            return self._target_error(f"A '{missing}' is required.")
        try:
            period = parse_period(model_args.get("period"))
        except TargetValidationError as exc:
            self._observer("tool_dispatch_start", {"tool_name": TOOL_NAME})
            self._observer(
                "tool_dispatch_error",
                {"tool_name": TOOL_NAME, "error_code": UNKNOWN_TARGET_CODE},
            )
            return self._target_error(exc.message)

        return await self.resolve(
            table=table,
            column=column,
            concept=concept,
            period=period,
            credentials=credentials,
        )

    # -- programmatic path (D67 hook, design §1.4) ----------------------------

    async def resolve(
        self,
        *,
        table: str,
        column: str,
        concept: str,
        period: Period | None,
        credentials: RuntimeCredentials,
    ) -> ResolveOutcome:
        """Typed in, typed out — no `ToolResult` wrapping (design §1.4).

        L3: emits `tool_dispatch_start` at the top so every terminal event
        (ok/denied/error) has a matching start, on BOTH the model tool-call
        path and the D67 programmatic path.
        """
        self._observer("tool_dispatch_start", {"tool_name": TOOL_NAME})
        try:
            target = sql_builder.resolve_target(
                self._catalog,
                table=table,
                column=column,
                period=period,
                column_scope=credentials.column_scope,
            )
        except TargetValidationError as exc:
            self._observer(
                "tool_dispatch_error",
                {"tool_name": TOOL_NAME, "error_code": UNKNOWN_TARGET_CODE},
            )
            return self._target_error(exc.message)

        sql = sql_builder.build_sql(target, period=period, limit=self._query_limit)
        inner = await self._tool_dispatcher.dispatch(
            "runQuery", {"sql": sql, "limit": None}, credentials
        )

        if inner.status != "ok":
            # Pass the inner denial/error through verbatim (design §7).
            denial = (
                classify_denial(inner.error_code) if inner.status == "denied" else None
            )
            self._observer(
                f"tool_dispatch_{'denied' if inner.status == 'denied' else 'error'}",
                {"tool_name": TOOL_NAME, "error_code": inner.error_code},
            )
            return ResolveOutcome(
                status=inner.status,
                values=[],
                provenance=inner.provenance,
                denial=denial,
                degraded=False,
                error_code=inner.error_code,
                user_message=inner.user_message,
                retryable=inner.retryable,
            )

        rows = _extract_rows(inner.result_full, target)
        if not rows:
            self._observer("tool_dispatch_ok", {"tool_name": TOOL_NAME})
            return ResolveOutcome(
                status="ok", values=[], provenance=inner.provenance, degraded=False
            )

        ranked, degraded = await self._rank(concept, rows)
        self._observer("tool_dispatch_ok", {"tool_name": TOOL_NAME})
        return ResolveOutcome(
            status="ok",
            values=ranked,
            provenance=inner.provenance,
            degraded=degraded,
            top_margin=ranking.top_margin(ranked),
        )

    async def _rank(
        self, concept: str, rows: list[RowValue]
    ) -> tuple[list[ResolvedValue], bool]:
        concept_vec: list[float] | None = None
        row_vecs: list[list[float]] | None = None
        if self._embedding_client is not None:
            texts = [concept] + [row.embedding_text() for row in rows]
            try:
                vectors = await self._embedding_client.embed(texts)
                validated = _validate_embed_shape(vectors, expected=len(rows) + 1)
                if validated is not None:
                    concept_vec = validated[0]
                    row_vecs = validated[1:]
            except EmbeddingError:
                # Degrade to freq-only ranking — NOT a tool failure (design
                # §3.2). Enforcement already happened on the inner query.
                pass
            except Exception:  # noqa: BLE001 - a protocol-violating client degrades, never crashes
                _logger.debug("resolveValues embedding client raised; degrading", exc_info=True)
        degraded = concept_vec is None or row_vecs is None
        ranked = ranking.rank(
            rows=rows,
            row_vectors=None if degraded else row_vecs,
            concept_vector=None if degraded else concept_vec,
            similarity_weight=self._similarity_weight,
            top_k=self._top_k,
        )
        return ranked, degraded

    # -- helpers --------------------------------------------------------------

    def _target_error(self, message: str) -> ResolveOutcome:
        return ResolveOutcome(
            status="error",
            values=[],
            provenance=None,
            denial=None,
            degraded=False,
            error_code=UNKNOWN_TARGET_CODE,
            user_message=message,
            retryable=True,
        )

    def _outcome_to_tool_result(self, outcome: ResolveOutcome) -> ToolResult:
        if outcome.status != "ok":
            return ToolResult(
                status=outcome.status,
                tool_name=TOOL_NAME,
                error_code=outcome.error_code,
                retryable=outcome.retryable,
                user_message=outcome.user_message,
                provenance=outcome.provenance,
                result_preview=None,
                result_full=None,
            )

        # H3: wrap the ranked list with the ranking-quality metadata so the
        # MODEL (which sees only `result_preview`, never `result_full`) learns
        # when ranking degraded to frequency-only. In freq-only mode the top
        # `score` is a normalized-log-freq artifact (up to 1.0), NOT a semantic
        # match — surfacing `degraded`/`ranking` lets the model apply D66(c)
        # (prefer askUser) instead of trusting a spurious "perfect match".
        result_full: dict[str, Any] = {
            "degraded": outcome.degraded,
            "ranking": "freq_only" if outcome.degraded else "semantic+freq",
            "top_margin": outcome.top_margin,
            "values": [value.to_dict() for value in outcome.values],
        }
        # `_build_preview`'s non-tabular-dict branch carries the whole wrapper
        # (degraded flag + values) into the single preview cell the model reads.
        preview = _build_preview(result_full, self._preview_row_count)
        return ToolResult(
            status="ok",
            tool_name=TOOL_NAME,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=outcome.provenance,
            result_preview=preview,
            result_full=result_full,
        )


def _validate_embed_shape(vectors: Any, *, expected: int) -> list[list[float]] | None:
    """Return *vectors* iff it is a well-shaped batch of `expected` equal-length
    numeric vectors; else `None` (a signal to degrade — M2 / H1).

    Guards against a protocol-violating embedding client (wrong count, ragged
    or non-list vectors, non-numeric elements) crashing `ranking.cosine`
    (`strict=True`) or `vectors[0]`. Ranking quality degradation is always safe
    — enforcement already happened on the inner query.
    """
    if not isinstance(vectors, list) or len(vectors) != expected:
        return None
    dim: int | None = None
    for vec in vectors:
        if not isinstance(vec, list) or len(vec) == 0:
            return None
        if any(not isinstance(x, int | float) or isinstance(x, bool) for x in vec):
            return None
        if any(not math.isfinite(x) for x in vec):
            return None
        if dim is None:
            dim = len(vec)
        elif len(vec) != dim:
            return None
    return vectors


def _extract_rows(raw_result: Any, target: sql_builder.ResolvedTarget) -> list[RowValue]:
    """Map the backing `runQuery` `{columns, rows, ...}` result into `RowValue`s.

    B4-parity hardening (H1): a MALFORMED backing result never crashes the turn
    — every structural surprise (missing keys, wrong types, short rows, dict
    rows, non-numeric freq) is skipped/defaulted so the call degrades to a
    valid (possibly empty) result rather than raising. The composite issues the
    inner SQL itself, so a well-formed MCP response is expected; this is purely
    defense-in-depth against a protocol-violating/faulty backend.
    """
    if not isinstance(raw_result, dict):
        return []
    columns = raw_result.get("columns")
    rows = raw_result.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return []
    index = {name: pos for pos, name in enumerate(columns)}
    value_idx = index.get(target.column)
    if value_idx is None:
        return []
    desc_idx = index.get(target.description_col) if target.description_col else None
    freq_idx = index.get(_FREQ_COLUMN)

    result: list[RowValue] = []
    for row in rows:
        # Only positional (list/tuple) rows are expected; a dict/scalar row (a
        # protocol violation) is skipped rather than raising KeyError/TypeError.
        if not isinstance(row, list | tuple):
            continue
        # value_idx is guarded exactly like desc_idx/freq_idx (H1 BUG-2): a row
        # shorter than the value column's position is skipped, not an IndexError.
        if value_idx >= len(row):
            continue
        raw_value = row[value_idx]
        if raw_value is None:
            continue
        description = None
        if desc_idx is not None and desc_idx < len(row):
            desc_value = row[desc_idx]
            description = None if desc_value is None else str(desc_value)
        freq = 0
        if freq_idx is not None and freq_idx < len(row) and row[freq_idx] is not None:
            # H1 BUG-1: a non-numeric freq must not raise — default to 0.
            try:
                freq = int(row[freq_idx])
            except (TypeError, ValueError):
                freq = 0
        result.append(RowValue(value=str(raw_value), description=description, freq=freq))
    return result


__all__ = [
    "TOOL_NAME",
    "UNKNOWN_TARGET_CODE",
    "Period",
    "ResolveOutcome",
    "ResolveValuesComposite",
    "ResolvedValue",
    "parse_period",
]
