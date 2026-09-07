"""ToolDispatcher — the single choke point for every model-requested tool call.

Per call: credentials are attached at the MCP-call boundary only, the tool is invoked,
an `MCPToolError` maps through `denial_mapping.py`, provenance is captured, and a
PREVIEW-only result is built — the full result rides `ToolResult.result_full` for the
caller to persist, and the dispatcher itself does no session I/O. `askUser` is
intercepted upstream by `AgentLoop` and never reaches here.

A RAW (non-`MCPToolError`) transport exception — a refusal, a timeout, a malformed
response, none of which carry the `[{CODE}]` prefix — is caught by a broad
`except Exception` and degraded to a clean `ToolResult(status="error", ...)` with a
generic, PII-safe `user_message`. The real exception is logged server-side only and
never reaches the client or the model.

D5 (load-bearing): `ToolResult` NEVER carries the JWT or session_id in any field —
`credentials` is consumed only to build the `call_tool(...)` invocation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.mcp.client import MCPClient, MCPToolError
from data_agent.runtime.observability import tracing
from data_agent.runtime.observability.redaction import tool_span_args
from data_agent.runtime.provenance.capture import capture_provenance
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.models import ResultPreview

from .denial_mapping import DenialInfo, classify_denial
from .schema_preview import _estimate_tokens, fit_schema_under_cap

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

ToolObserver = Callable[[str, dict[str, Any]], None]

# A catalog PROVIDER resolves the immutable `CatalogHandle` for THIS turn from its
# credentials (D75 Wave 1b): the runtime no longer holds a fixed startup handle — it
# resolves lazily through the process-wide `CatalogCache`, which authenticates one
# `/catalog/export` fetch with the turn's JWT and then serves every subsequent turn.
# A bare `CatalogHandle` is still accepted (tests + any fixed-catalog caller) and used
# as-is. The credentials are consumed ONLY to authenticate the fetch — never reflected
# into a handle or message (D5).
CatalogProvider = Callable[[RuntimeCredentials], Awaitable["CatalogHandle"]]


async def resolve_catalog(
    catalog: CatalogHandle | CatalogProvider, credentials: RuntimeCredentials
) -> CatalogHandle:
    """Resolve THIS turn's `CatalogHandle` — a fixed handle passes through; a provider is
        awaited with the turn's credentials (D75).

        Lives here, beside the `CatalogProvider` alias it destructures, because BOTH holders
        of a `CatalogHandle | CatalogProvider` have to agree on what the union means: the
        `isinstance` branch IS the contract, so a third holder should call this rather than
        re-derive which arm is which.
    """
    if isinstance(catalog, CatalogHandle):
        return catalog
    return await catalog(credentials)


_logger = logging.getLogger(__name__)

# B4: a raw transport exception (connection refusal, timeout, malformed
# response) never carries a `[{CODE}]` prefix an MCPToolError would — it is
# NOT a tool-level denial the MCP itself classified, so it is not looked up
# in `denial_mapping.py`'s table at all. Generic, PII-safe, never the raw
# exception text.
INTERNAL_TRANSPORT_ERROR_CODE = "INTERNAL_TRANSPORT_ERROR"
_INTERNAL_TRANSPORT_ERROR_MESSAGE = (
    "Something went wrong reaching the data warehouse. Please try again."
)


def _default_observer(event: str, payload: dict[str, Any]) -> None:
    """No-op observer — Pass B replaces this with real spans/progress events."""
    return None


@dataclass(frozen=True)
class ToolPause:
    """A runtime tool's request to PAUSE the turn — the generalized `askUser` seam.

        Carried on `ToolResult.pause`; the loop honors it by writing a `PauseCheckpoint` and
        returning `paused_ask_user`, exactly as for `askUser`. Only RUNTIME tools set it —
        dispatched MCP tools never pause, and the loop ignores it on a dispatched result.
        The `blueprint_*` fields carry the D45 mid-DAG resume state.
    """

    reason: str  # "blueprint_slot" (Slice C: "blueprint_approval" | "blueprint_when_ask")
    pending_question: dict[str, Any]
    blueprint_id: str | None = None
    slot_bindings_json: str | None = None
    completed_nodes_json: str | None = None
    awaiting_node: int | None = None


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one dispatched tool call — never carries credentials.

        `pause` is set ONLY by a runtime tool that needs to pause the turn, and is `None`
        for every dispatched MCP tool and every non-pausing runtime tool.

        `authoritative` is set True ONLY by `runBlueprint` on a SUCCESSFUL, D56-VERIFIED
        result. It is False for a `runQuery`, for any denied/errored/paused outcome, and for
        a blueprint whose verify did not pass — so a `result_preview` shaped identically to
        a raw query result still carries an unambiguous "this is the trusted answer, do not
        re-derive" signal into the model's tool message.
    """

    status: Literal["ok", "denied", "error"]
    tool_name: str
    error_code: str | None
    retryable: bool | None
    user_message: str | None
    provenance: frozenset[tuple[str, str]] | None
    result_preview: ResultPreview | None
    result_full: dict[str, Any] | list[Any] | None
    pause: ToolPause | None = None
    authoritative: bool = False
    # `denial_detail` (additive): the SPECIFIC, model-actionable reason for a
    # non-`ok` outcome, when the runtime has already decided that text is safe to
    # show and to persist. `None` (the default, and the case for every ordinary
    # denial) means "the canned `denial_mapping.py` string is the whole story".
    #
    # It exists because `user_message` NEVER reaches the model: `TrailEntry` has no
    # field for it, and `context/budget.py::_render_entry` — the single producer of
    # every model-facing tool message — regenerates the text from `error_code`
    # alone. So a specific message set on `user_message` is silently dropped, and
    # the model is told only the generic string. `denial_detail` is the persisted
    # channel that actually arrives.
    denial_detail: str | None = None
    # `window_note` (additive, J7): the one-line honesty note for a blueprint whose
    # window is DATA-anchored — it counts back from the latest data on record, not from
    # today's date. Set ONLY by `runBlueprint`, and only for a blueprint that DECLARES
    # `window_anchor: data`; `None` for every other tool, every other blueprint, and
    # every error/pause outcome.
    #
    # A SEPARATE field rather than more text on the `authoritative` note, because the two
    # answer different questions and occur in any combination: may I TRUST this result
    # (`authoritative`), and how do I DESCRIBE the window it covers (this). Like
    # `denial_detail`, it has to be persisted to arrive at all — `TrailEntry` is the only
    # channel to the model, and `context/budget.py::_render_entry` drops anything the
    # entry does not carry.
    window_note: str | None = None
    terminal: bool = False


# Default per-result token cap for the stored preview (RuntimeSettings.
# max_tool_result_tokens overrides it). The estimator imported above
# (`schema_preview._estimate_tokens`) is intentionally a byte-identical copy of
# context/budget.py::_estimate_tokens (chars/4) — the two modules cannot share the
# import (dispatch/__init__ eagerly imports this module, and budget.py transitively
# imports dispatch, so a direct import cycles). A parity test
# (`tests/runtime/dispatch/test_tool_dispatcher.py`) pins the two implementations
# together so a future tokenizer swap must update both. It LIVES in schema_preview
# (which imports nothing from this package) because the schema fitter has to measure
# its own trial fits with exactly the estimator this cap is expressed in; re-defining
# it here would have made a THIRD copy of the same four characters-per-token rule.
_DEFAULT_MAX_TOOL_RESULT_TOKENS = 4_000

# Default budget for a getTableSchema result's COLUMNS SECTION ALONE
# (RuntimeSettings.schema_columns_token_budget overrides it). Deliberately NOT the
# generic cap above (ISSUES C5b): a schema's table-level sections — rules,
# ambiguities, join_keys, grain — ride COMPLETE and unbudgeted, because they are
# the semantics the prompt tells the model to read, and the column list gets its
# own, larger allowance. At 6,000 the real `dbpcm_warehouse.employee` schema (130
# columns) shows ~90% of its columns with full documentation instead of the ~28%
# a shared 4,000-token cap left room for. Every OTHER tool result — the blueprint
# cards, the generic stringify branch — is still bounded by
# `max_tool_result_tokens`.
_DEFAULT_SCHEMA_COLUMNS_TOKEN_BUDGET = 6_000


def _cap_list_under_key(
    raw_result: dict[str, Any], key: str, max_result_tokens: int
) -> tuple[dict[str, Any], int, int]:
    """Keep the HEAD of `raw_result[key]` — as many leading items as fit under
        *max_result_tokens* — and return `(capped_dict, kept_count, total_count)`.

        The head is kept because the caller hands in a RANKED list (the score-ordered
        blueprint cards), so dropping the tail degrades it the way a ranked list should. The
        other top-level keys ride along untouched, so the result stays the SAME SHAPE the
        model expects — a dict with a shorter list under *key*, never a string.

        NOT for column lists: a schema's columns are ordered but NOT ranked, and head-cutting
        them dropped the NAMES of everything past the cut. That branch delegates to
        `schema_preview.fit_schema_under_cap`, which degrades per-column detail instead.

        No minimum-one carve-out: if not even the first item fits, the list comes back empty.
        Forcing an over-cap item back in would reintroduce exactly the unbounded cell this
        cap exists to prevent, and the caller's `_truncated` marker names the drop either way.
    """
    items = raw_result[key]
    base = {k: v for k, v in raw_result.items() if k != key}
    kept: list[Any] = []
    for item in items:
        trial = {**base, key: [*kept, item]}
        if _estimate_tokens(json.dumps(trial, default=str)) > max_result_tokens:
            break
        kept.append(item)
    return {**base, key: kept}, len(kept), len(items)


def _cap_nontabular_result(
    raw_result: Any,
    max_result_tokens: int,
    *,
    observer: ToolObserver = _default_observer,
    tool_name: str | None = None,
    question: str | None = None,
    schema_columns_token_budget: int = _DEFAULT_SCHEMA_COLUMNS_TOKEN_BUDGET,
) -> tuple[Any, bool]:
    """Bound a non-tabular tool result stored as ONE preview cell (especially a wide
        `getTableSchema`) so it can never be an unbounded blob that the row-count-only trail
        budget never trims. Returns `(capped_value, truncated)`, always valid and parseable:

          * a `{... "columns": [...]}` dict ALWAYS goes through
            `schema_preview.fit_schema_under_cap`;
          * a `{... "blueprints": [...]}` dict keeps the HEAD of the ranked card list;
          * any other over-cap value is rendered to a string and truncated at the cap with a
            `…[truncated: N of M chars omitted]` marker.

        THE SCHEMA BRANCH RUNS FIRST AND UNCONDITIONALLY: it is the one branch that is not a
        size cap — it also strips the PRE-APPLIED TENANCY COLUMNS, which are not the model's
        to filter on at any size, so a small schema must go through it too. It then fits the
        COLUMNS SECTION ALONE under *schema_columns_token_budget*, with the table-level
        sections riding complete and unbudgeted. A schema whose columns fit is returned
        COMPACTED-STABLE rather than byte-identical. *question*, when the caller has one,
        only ORDERS the columns (D25).

        Only `getTableSchema` reaches that branch: `_build_preview` has already routed every
        tabular result and every bare list elsewhere, and no other non-tabular tool result
        carries a top-level `columns` list.

        The card branch exists because a `searchBlueprints` result carries no top-level
        `columns` key: without it, maximally-slotted cards crossing the cap fell through to
        stringify-and-truncate and the model received a MANGLED JSON STRING ending in a
        truncation marker instead of a card list, on the release's primary route.

        Under the cap a non-schema value is returned unchanged (`truncated=False`).
    """
    if isinstance(raw_result, dict) and isinstance(raw_result.get("columns"), list):
        fitted, report = fit_schema_under_cap(
            raw_result, schema_columns_token_budget, question=question
        )
        # THE TENANCY STRIP HAS ITS OWN CHANNEL, ungated by the budget. It is not a
        # degrade — those columns are pre-applied by the platform and are absent
        # from every schema the four RLS tables already emit — so the model is told
        # nothing (§0) and this is INFO, not a warning. But it happens on every
        # path, including the under-budget one, and until this event existed the
        # count only ever reached an operator when the SAME result also blew its
        # columns budget: a small schema could have `client_code` silently removed
        # with no record anywhere. The count is the whole payload (D25) — no column
        # name, no schema text, no question.
        if report.tenancy_hidden_count:
            observer(
                "tool_dispatch_tenancy_columns_hidden",
                {
                    "tool_name": tool_name,
                    "tenancy_hidden_count": report.tenancy_hidden_count,
                },
            )
            _logger.info(
                "Hid %d pre-applied tenancy column(s) from the %r result shown to "
                "the model — they are bound by the row policy, not by the model",
                report.tenancy_hidden_count,
                tool_name,
            )
        # GATED ON `marker_added`, i.e. on "the model was told something was
        # withheld" — a COLUMN-BUDGET degrade and nothing else. The tenancy count
        # rides along on this payload as context for the degrade; the event above
        # is what reports the strip itself.
        if report.marker_added:
            # Same degrade-not-fail posture as the card branch below: the model is
            # told by the in-fit marker, the operator by this event + the log.
            # COUNTS ONLY (D25) — no column name, no section name, no schema text,
            # no question. Like every other `tool_dispatch_*` event this is NOT
            # forwarded to Phoenix (see the standing note in the card branch below);
            # it is consumed by the SSE progress observer, the log, and any
            # `create_app(extra_observers=…)` sink.
            #
            # `base_dropped_count` WAS here and is GONE (C5b): base sections can no
            # longer be dropped at all, so the field could only ever report 0. It
            # was never in `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` (this event family
            # does not reach Phoenix), so nothing outside this call site changes.
            observer(
                "tool_dispatch_schema_detail_dropped",
                {
                    "tool_name": tool_name,
                    "detailed_count": report.detailed_count,
                    "total_count": report.total_columns,
                    "tenancy_hidden_count": report.tenancy_hidden_count,
                },
            )
            # Every count is emitted unconditionally, including the zeros.
            _logger.warning(
                "Columns budget reduced %d of %d columns of %r to name+type (and "
                "omitted %d entirely, %d tenancy columns hidden; budget=%d tokens) — "
                "raise RuntimeSettings.schema_columns_token_budget to show more",
                report.reduced_count,
                report.total_columns,
                tool_name,
                report.omitted_columns,
                report.tenancy_hidden_count,
                schema_columns_token_budget,
            )
        return fitted, report.marker_added

    rendered = json.dumps(raw_result, default=str)
    if _estimate_tokens(rendered) <= max_result_tokens:
        return raw_result, False

    if isinstance(raw_result, dict) and isinstance(raw_result.get("blueprints"), list):
        capped, kept_count, total_cards = _cap_list_under_key(
            raw_result, "blueprints", max_result_tokens
        )
        omitted = total_cards - kept_count
        if kept_count == 0:
            # The cap is too small for even ONE card (no minimum-one carve-out, see
            # `_cap_list_under_key`). The kept-count phrasing below would read "the 0
            # best matches are shown in full", which is not merely awkward — it
            # describes a list the model can act on when there is none.
            capped["_truncated"] = (
                f"…[truncated: all {total_cards} blueprint cards were omitted — not "
                f"even the highest-scoring one fits the result cap; search again with "
                f"a smaller k, or ask an operator to raise max_tool_result_tokens]"
            )
        else:
            capped["_truncated"] = (
                f"…[truncated: the {omitted} lowest-scoring of {total_cards} blueprint "
                f"cards were omitted to fit — the {kept_count} best matches are shown "
                f"in full; search again with a narrower intent if none of them fits]"
            )
        # RECONCILE THE RIDDEN-ALONG COUNT. `count` is set by
        # `retrieval/tools.py::SearchBlueprintsTool` to the PRE-cap card total and
        # rides through `_cap_list_under_key`'s `base` untouched, so a truncated
        # result told the model `count: 20` beside a 13-item `blueprints` list — a
        # valid shape whose two signals disagree, and the model has no way to tell
        # which one to believe. `count` now describes what is actually present and
        # `count_total` carries what was found, so both questions have an answer.
        # Only rewritten when the key was there to begin with (this branch is keyed
        # on `blueprints`, not on `count`).
        if "count" in capped:
            capped["count_total"] = total_cards
            capped["count"] = kept_count
        # Degrade-not-fail, NEVER SILENTLY: the model is told by `_truncated`, the
        # operator by this event + the server-side log. `dropped_count` /
        # `total_count` are shape-only (D25) — no card text, id, or score.
        #
        # THIS EVENT DELIBERATELY DOES NOT REACH PHOENIX. `observability/tracing.py::
        # guardrail_observer` forwards only `loop_`-prefixed events (AgentLoop
        # stage boundaries), and this is a `tool_dispatch_*` event — the same family
        # as `tool_dispatch_start`/`ok`/`denied`/`error`, none of which is forwarded
        # either; the dispatcher's Phoenix surface is the `tool.<name>` span from
        # `_emit_tool_span`. It is consumed by the SSE progress observer, the
        # server-side log below, and any `create_app(extra_observers=…)` sink. Doc 06
        # says "name anything that should reach Phoenix accordingly" — this one is
        # named for the layer it belongs to, so if it should ever be traced, rename
        # it AND add `dropped_count`/`total_count` to
        # `_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST` (both are plain counts, so D25-safe).
        observer(
            "tool_dispatch_cards_dropped",
            {
                "tool_name": tool_name,
                "dropped_count": omitted,
                "total_count": total_cards,
            },
        )
        _logger.warning(
            "Preview cap dropped %d of %d blueprint cards from %r (cap=%d tokens) — "
            "raise RuntimeSettings.max_tool_result_tokens or lower the search k",
            omitted,
            total_cards,
            tool_name,
            max_result_tokens,
        )
        return capped, True

    # Generic over-cap value: keep a valid, truncated STRING cell with a marker.
    char_cap = max_result_tokens * 4
    truncated_str = (
        rendered[:char_cap]
        + f"…[truncated: {len(rendered) - char_cap} of {len(rendered)} chars omitted]"
    )
    return truncated_str, True


def _build_preview(
    raw_result: Any,
    preview_row_count: int,
    max_result_tokens: int = _DEFAULT_MAX_TOOL_RESULT_TOKENS,
    *,
    observer: ToolObserver = _default_observer,
    tool_name: str | None = None,
    question: str | None = None,
    schema_columns_token_budget: int = _DEFAULT_SCHEMA_COLUMNS_TOKEN_BUDGET,
) -> ResultPreview:
    """Build the `{columns, row_count, truncated, preview_rows}` preview object.

        Handles the three MCP result shapes the tools return: `{columns, rows, row_count,
        truncated}`, a bare list of dicts, and a small non-tabular dict. The first two
        enforce the N-row preview cap. A non-tabular dict is additionally SIZE-capped to
        `max_result_tokens` — except a `getTableSchema`, whose COLUMNS SECTION is budgeted
        separately by `schema_columns_token_budget` while its table-level sections ride
        complete, and which is reshaped on EVERY path so the pre-applied tenancy columns
        never reach the model (see `_cap_nontabular_result`).

        *observer*/*tool_name* are used ONLY to report a size-cap degrade. *question* is the
        turn's raw user text, supplied ONLY by the agent loop's dispatch site; it reaches the
        schema fitter, where it orders which columns keep their documentation, and is never
        written into any output (D25).
    """
    if isinstance(raw_result, dict) and "rows" in raw_result and "columns" in raw_result:
        rows = raw_result["rows"]
        preview_rows = rows[:preview_row_count]
        truncated = bool(raw_result.get("truncated", False)) or len(rows) > preview_row_count
        return ResultPreview(
            columns=list(raw_result["columns"]),
            row_count=int(raw_result.get("row_count", len(rows))),
            truncated=truncated,
            preview_rows=[list(row) for row in preview_rows],
        )
    if isinstance(raw_result, list):
        preview_rows = raw_result[:preview_row_count]
        return ResultPreview(
            columns=[],
            row_count=len(raw_result),
            truncated=len(raw_result) > preview_row_count,
            preview_rows=[[item] for item in preview_rows],
        )
    # Small non-tabular dict (e.g. getTableSchema) — size-capped, not row-capped.
    capped, truncated = _cap_nontabular_result(
        raw_result,
        max_result_tokens,
        observer=observer,
        tool_name=tool_name,
        question=question,
        schema_columns_token_budget=schema_columns_token_budget,
    )
    return ResultPreview(columns=[], row_count=1, truncated=truncated, preview_rows=[[capped]])


class ToolDispatcher:
    """Executes one model-requested tool call end-to-end (design §3.3)."""

    def __init__(
        self,
        mcp_client: MCPClient,
        catalog: CatalogHandle | CatalogProvider,
        *,
        preview_row_count: int = 20,
        max_tool_result_tokens: int = _DEFAULT_MAX_TOOL_RESULT_TOKENS,
        schema_columns_token_budget: int = _DEFAULT_SCHEMA_COLUMNS_TOKEN_BUDGET,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        self._mcp_client = mcp_client
        # `catalog` is EITHER a fixed `CatalogHandle` (tests / fixed-catalog callers)
        # OR an async provider resolving the handle from this turn's credentials via
        # the process-wide `CatalogCache` (D75 Wave 1b). Resolved per-dispatch just
        # before `capture_provenance`; the cache guarantees one fetch, so warm turns
        # are cheap.
        self._catalog = catalog
        self._preview_row_count = preview_row_count
        # Per-result preview SIZE cap (tokens): bounds a single stored tool result
        # (esp. a wide getTableSchema) so it cannot balloon the trail. See
        # `_cap_nontabular_result`.
        self._max_tool_result_tokens = max_tool_result_tokens
        # Budget (tokens) for a getTableSchema result's COLUMNS SECTION alone
        # (C5b). Separate from the cap above because a schema's table-level
        # sections ride complete: see `_DEFAULT_SCHEMA_COLUMNS_TOKEN_BUDGET`.
        self._schema_columns_token_budget = schema_columns_token_budget
        self._observer = observer
        # B5: optional — when a real tracer is wired (app.py's composition
        # root), dispatch() emits one TOOL span per call, SQL-literal-masked
        # via observability/redaction.py::redact_tool_args (D25). `None` here
        # (Layer-1 tests, no Phoenix) means spans are simply never created —
        # no behavioral difference otherwise.
        self._tracer = tracer
        # Access-controlled TELEMETRY DEBUG switch (RuntimeSettings.
        # otlp_disable_redaction, wired by app.py). Default False keeps the D25
        # shape-only span byte-identical. When True the TOOL span carries the REAL
        # args (SQL WITH literals) AND the result preview — see `_emit_tool_span`.
        # TELEMETRY-ONLY: this ONLY changes what the span records; the dispatched
        # `call_tool` below always receives the raw `model_args` regardless (the
        # redactor never touched the dispatch path), and no scope/PII ENFORCEMENT
        # (D5/D57, enforced in the MCP + injected credentials) depends on it.
        self._disable_redaction = disable_redaction

    def _emit_tool_span(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        *,
        status: str,
        error_code: str | None,
        result_preview: ResultPreview | None = None,
    ) -> None:
        if self._tracer is None:
            return
        # DEFAULT (D25): SQL literals masked, NO result on the span. DEBUG
        # (self._disable_redaction): the REAL args + the result preview, so the
        # whole tool call is visible in Phoenix. Purely a telemetry choice — the
        # dispatched call and enforced scope are identical either way.
        span_result = result_preview if self._disable_redaction else None
        with tracing.tool_span(
            self._tracer,
            tool_name=tool_name,
            args=tool_span_args(tool_name, model_args, disable_redaction=self._disable_redaction),
            status=status,
            error_code=error_code,
            result_preview=span_result,
            reveal_complex_args=self._disable_redaction,
        ):
            pass

    async def _resolve_catalog(self, credentials: RuntimeCredentials) -> CatalogHandle:
        """This dispatcher's catalog for THIS turn — see `resolve_catalog` above."""
        return await resolve_catalog(self._catalog, credentials)

    async def capture_sql_provenance(
        self, sql: str, credentials: RuntimeCredentials
    ) -> frozenset[tuple[str, str]] | None:
        """The D44 USES set of a query that is NOT being dispatched.

                `answerWithTable` may designate a query the agent never ran — that is the
                documented contract, because the executed query usually carries a LIMIT the agent
                chose for its own reading and paging needs the un-capped shape. Such a query
                appears in no trail entry's provenance, so the turn union does not cover it and
                the read path cannot otherwise tell whether the table it offers is still in scope.

                SAME extractor, SAME catalog, SAME `capture_provenance` entry point as a real
                `runQuery` dispatch, so the two can never disagree about one query. Reads NOTHING
                and dispatches NOTHING: it parses a string. Degrades to `None` (undetermined)
                rather than raising.
        """
        try:
            catalog = await self._resolve_catalog(credentials)
            return await capture_provenance(
                "runQuery", {"sql": sql}, catalog, session_id=credentials.session_id
            )
        except Exception:
            _logger.warning(
                "could not capture provenance for a designated answer table — "
                "treating it as undetermined",
                exc_info=True,
            )
            return None

    async def dispatch(
        self,
        tool_name: str,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        *,
        tool_call_id: str | None = None,
        emit_progress: bool = True,
        question: str | None = None,
    ) -> ToolResult:
        """Dispatch one tool call. `emit_progress=False` silences the
                `tool_dispatch_start`/`ok`/`denied`/`error` OBSERVER events for THIS call and
                nothing else.

                `blueprint/executor.py` runs every node of a composed blueprint through this same
                choke point, and those events are UI progress labels — so a single `runBlueprint`
                otherwise painted the user a "running runQuery…" line per internal node, exposing
                that the answer is assembled from queries over internal tables. The OUTER
                `runBlueprint` dispatch from the agent loop still emits normally, so the turn is
                never silent.

                SCOPE, deliberately narrow — this gates the OBSERVER channel only. The
                Phoenix/OTel span is UNAFFECTED (an operator debugging a blueprint needs every
                inner node's span), `tool_dispatch_cards_dropped` is UNAFFECTED (an operator
                degrade signal that never reached the UI), and control flow is UNAFFECTED.

                `question` is the turn's raw user text, supplied ONLY by the agent loop's own
                dispatch site. Its ONE use is to order which columns of an over-cap schema keep
                their documentation; it is never persisted, never spanned, never echoed into a
                result (D25).
        """
        # One local, resolved once: the four progress events below route through it,
        # so a future event added to this method cannot silently escape the gate by
        # forgetting to check the flag — it has to pick an observer.
        emit = self._observer if emit_progress else _default_observer
        emit("tool_dispatch_start", {"tool_name": tool_name, "tool_call_id": tool_call_id})

        try:
            raw_result = await self._mcp_client.call_tool(
                tool_name,
                model_args,
                jwt=credentials.jwt,
                session_id=credentials.session_id,
            )
        except MCPToolError as exc:
            denial: DenialInfo = classify_denial(exc.code)
            # B4/D25 posture: the model-facing message is normally the GENERIC
            # canned string from `denial_mapping.py` — raw backend / transport text
            # is NEVER surfaced. The single narrow exception is
            # COLUMN_SCOPE_VIOLATION: its `exc.message` is an author-CONTROLLED
            # `ColumnScopeError` string that NAMES the out-of-scope column(s)
            # (catalog metadata only — not PII / cell values), so showing it lets
            # the model self-correct by seeing WHICH columns it lacks instead of
            # retrying blind. All other codes (and the transport path below) stay
            # canned.
            #
            # It rides `denial_detail`, NOT `user_message`. This carve-out used to
            # set only `user_message` and claim the specific text "already reached
            # the model on the live turn" — it never did, on the live turn or any
            # other: `TrailEntry` has no `user_message` field, so the string was
            # dropped at persistence and `_render_entry` regenerated the generic one
            # from `error_code`. The model has always been told "columns outside your
            # current permissions" with no column named. `denial_detail` IS
            # persisted, so the specific text now actually arrives.
            #
            # Replay safety: a denial carries `provenance=None`, and
            # `scope_filter.filter_trail` exempts a non-`ok` entry only for the
            # CURRENT turn — a prior-turn denial is dropped outright. So this detail
            # can only ever render inside the turn whose scope produced it, and can
            # never leak a column name into a later, narrower-scoped turn.
            denial_detail: str | None = None
            if denial.code == "COLUMN_SCOPE_VIOLATION" and exc.message:
                denial_detail = exc.message
            user_message = denial_detail or denial.user_message
            emit(
                "tool_dispatch_denied",
                {"tool_name": tool_name, "error_code": denial.code, "tool_call_id": tool_call_id},
            )
            self._emit_tool_span(tool_name, model_args, status="denied", error_code=denial.code)
            return ToolResult(
                status="denied",
                tool_name=tool_name,
                error_code=denial.code,
                retryable=denial.retryable,
                user_message=user_message,
                provenance=None,
                result_preview=None,
                result_full=None,
                denial_detail=denial_detail,
            )
        except Exception:
            # B4: a RAW (non-MCPToolError) transport exception — connection
            # refusal, timeout, malformed response — must never crash the
            # turn or leak str(exc) to the client. Log the real exception
            # server-side ONLY; the client/model only ever sees the generic,
            # canned user_message below (D5/D25 — never raw backend detail).
            _logger.exception(
                "Transport exception dispatching tool %r (session=%s)",
                tool_name,
                credentials.session_id,
            )
            emit(
                "tool_dispatch_error",
                {
                    "tool_name": tool_name,
                    "error_code": INTERNAL_TRANSPORT_ERROR_CODE,
                    "tool_call_id": tool_call_id,
                },
            )
            self._emit_tool_span(
                tool_name, model_args, status="error", error_code=INTERNAL_TRANSPORT_ERROR_CODE
            )
            return ToolResult(
                status="error",
                tool_name=tool_name,
                error_code=INTERNAL_TRANSPORT_ERROR_CODE,
                retryable=False,
                user_message=_INTERNAL_TRANSPORT_ERROR_MESSAGE,
                provenance=None,
                result_preview=None,
                result_full=None,
            )

        catalog = await self._resolve_catalog(credentials)
        provenance = await capture_provenance(
            tool_name, model_args, catalog, session_id=credentials.session_id
        )
        preview = _build_preview(
            raw_result,
            self._preview_row_count,
            self._max_tool_result_tokens,
            observer=self._observer,
            tool_name=tool_name,
            question=question,
            schema_columns_token_budget=self._schema_columns_token_budget,
        )

        emit("tool_dispatch_ok", {"tool_name": tool_name, "tool_call_id": tool_call_id})
        self._emit_tool_span(
            tool_name, model_args, status="ok", error_code=None, result_preview=preview
        )
        return ToolResult(
            status="ok",
            tool_name=tool_name,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=provenance,
            result_preview=preview,
            result_full=raw_result,
        )
