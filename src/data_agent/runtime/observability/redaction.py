"""Redaction — PII/secret redaction for telemetry (D25).

Non-negotiable rules: never log the JWT or the raw `column_scope`, only a stable hash
(`hash_scope` delegates to `context/scope_filter.py`, so telemetry and the D44 replay
filter can never disagree); mask SQL literals in the TELEMETRY COPY only, leaving the
dispatched and UI-visible SQL verbatim; and never emit result rows or cell values
anywhere — `redact_tool_args` must never be handed a `ToolResult`, only call arguments.
"""

from __future__ import annotations

import re
from typing import Any

# Mirrors clickhouse-api's app/security.py `_SINGLE_QUOTED_STRING` exactly
# (handles both `''`-doubled and backslash-escaped quotes).
_SINGLE_QUOTED_STRING = re.compile(r"'(?:[^'\\]|\\.|\\'|'')*'")
_DOUBLE_QUOTED_STRING = re.compile(r'"(?:[^"\\]|\\.|"")*"')
# A standalone numeric literal — not part of an identifier (so `s3`/`col2` are
# left alone; only genuine numeric literal tokens like `20` or `3.14` match).
_NUMERIC_LITERAL = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")

# Tool argument keys that may carry a raw SQL string (only `runQuery`'s `sql`
# in Phase 0 — kept as a set for forward-compatibility with future tools).
_SQL_ARG_KEYS = frozenset({"sql"})

# Tool argument keys that are FREE USER TEXT (highest PII risk) — fully replaced
# with a placeholder in telemetry, distinct from SQL-literal masking (D77/D25,
# resolvevalues-design §6.1). Precedent: `askUser`'s `question` is kept out of
# spans (`tracing.guardrail_observer`); `resolveValues`'s `concept` gets the
# same treatment. The read tools' `query` (searchBlueprints/searchKnowledge) is
# model-authored free text that may quote user PII — same treatment (read-tools
# §5); `id` (a structural blueprint id) is non-PII and kept for debuggability.
_FULLY_REDACTED_ARG_KEYS = frozenset({"concept", "query"})
_REDACTED_PLACEHOLDER = "<redacted>"


def hash_scope(column_scope: frozenset[str]) -> str:
    """Stable hash of *column_scope* — never log the raw scope (D25).

        `compute_scope_hash` is imported INSIDE the function on purpose: an eager import
        creates the cycle `redaction -> context -> dispatch -> redaction`, which makes
        `runtime.observability.tracing` un-importable as an entry point.
    """
    from data_agent.runtime.context.scope_filter import compute_scope_hash

    return compute_scope_hash(column_scope)


def mask_sql(sql: str) -> str:
    """Mask string and numeric literals in *sql* for the telemetry copy only.

        Structurally equivalent output: string literal contents become `''`/`""`, numeric
        literals become `0`. The un-redacted SQL is still what is dispatched to the MCP and
        shown in the UI — only this masked copy may enter a span or progress attribute.
    """
    masked = _SINGLE_QUOTED_STRING.sub("''", sql)
    masked = _DOUBLE_QUOTED_STRING.sub('""', masked)
    masked = _NUMERIC_LITERAL.sub("0", masked)
    return masked


def redact_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a telemetry-safe copy of *args* — SQL literals masked, the rest passed through.

        Non-SQL arguments (`database`, `table`, `limit`, ...) are structural, not PII, and
        are kept as-is for debuggability. Callers must never pass tool RESULTS here.
    """
    redacted: dict[str, Any] = dict(args)
    for key in _SQL_ARG_KEYS:
        value = redacted.get(key)
        if isinstance(value, str):
            redacted[key] = mask_sql(value)
    for key in _FULLY_REDACTED_ARG_KEYS:
        if key in redacted:
            redacted[key] = _REDACTED_PLACEHOLDER
    # `resolveValues`'s structured `period` (D77): keep `period.column` (a
    # structural catalog identifier) but FULLY redact the `start`/`end` bounds.
    # They are unvalidated model-supplied free text (not guaranteed SQL-shaped),
    # so `mask_sql` would pass non-SQL-shaped values straight through (L1) — a
    # placeholder is the safe choice.
    period = redacted.get("period")
    if isinstance(period, dict):
        masked_period = dict(period)
        for bound_key in ("start", "end"):
            if bound_key in masked_period and masked_period[bound_key] is not None:
                masked_period[bound_key] = _REDACTED_PLACEHOLDER
        redacted["period"] = masked_period
    # `runBlueprint`'s `slot_bindings` (runblueprint-design §5.5): the model-
    # authored slot VALUES may quote user PII / entity values and become SQL
    # literals — fully redact every value in telemetry, keeping only the slot
    # NAMES (structural, non-PII) so a span still shows WHICH slots were filled.
    slot_bindings = redacted.get("slot_bindings")
    if isinstance(slot_bindings, dict):
        redacted["slot_bindings"] = {key: _REDACTED_PLACEHOLDER for key in slot_bindings}
    return redacted


def tool_span_args(
    tool_name: str, args: dict[str, Any], *, disable_redaction: bool
) -> dict[str, Any]:
    """The args a TOOL span should carry — the single seam for the access-controlled
        `RuntimeSettings.otlp_disable_redaction` telemetry debug switch.

        Default is the D25 posture (the redacted copy); `disable_redaction=True` records the
        REAL args so a debugging operator sees them in Phoenix. TELEMETRY-ONLY: every caller
        passes the RAW args to the actual tool work regardless, so flipping the flag never
        changes what executes or what scope the MCP enforces. Returns a shallow copy, so
        span code can never mutate the caller's live args dict.
    """
    return dict(args) if disable_redaction else redact_tool_args(tool_name, args)


__all__ = ["hash_scope", "mask_sql", "redact_tool_args", "tool_span_args"]
