"""Redactor — PII/secret redaction for telemetry (D25, design §7).

Non-negotiable redaction rules (D25 / docs/10-observability.md):
  - Never log the JWT or the raw `column_scope` list — only a stable hash
    (`hash_scope`, delegating to `context/scope_filter.py`'s own
    `compute_scope_hash` so telemetry and the D44 replay-filter can never
    disagree on what a given scope hashes to).
  - Mask string/numeric SQL literals in the *telemetry copy* of any SQL
    (`mask_sql`, mirroring `clickhouse-api`'s own `_mask_string_literals`
    convention in `app/security.py`) — the dispatched/UI copy of the SQL is
    untouched (08-ui.md's transparency principle: users see their own SQL
    verbatim; only the trace copy is masked).
  - Never emit result rows/cell values, anywhere. `redact_tool_args` only
    ever touches recognized SQL-bearing argument keys; it never sees
    `ToolResult.result_full`/`.result_preview` — callers must not pass those
    to this module at all (spans/progress carry shape, not values).
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

    `compute_scope_hash` is imported lazily (inside the function) rather than at
    module top: the eager import created an import CYCLE
    (`redaction → context → dispatch → redaction`) that made
    `runtime.observability.tracing` un-importable as an entry point. Deferring it
    to call time — telemetry is emitted only at runtime, well after the module
    graph is built — breaks the cycle while returning byte-identical hashes, so
    the `learning` package (and any future consumer) can reuse `tracing` without
    depending on a fragile import order."""
    from data_agent.runtime.context.scope_filter import compute_scope_hash

    return compute_scope_hash(column_scope)


def mask_sql(sql: str) -> str:
    """Mask string and numeric literals in *sql* for the telemetry copy only.

    Structurally equivalent output (keywords/identifiers/punctuation kept);
    string literal *contents* become `''`/`\"\"`, numeric literals become `0`.
    The un-redacted SQL is still what is dispatched to the MCP and shown in
    the UI — only this masked copy may be written into a span/progress
    attribute.
    """
    masked = _SINGLE_QUOTED_STRING.sub("''", sql)
    masked = _DOUBLE_QUOTED_STRING.sub('""', masked)
    masked = _NUMERIC_LITERAL.sub("0", masked)
    return masked


def redact_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a telemetry-safe copy of *args* — SQL literals masked, everything else passed through.

    Non-SQL arguments (`database`, `table`, `limit`, ...) are structural, not
    PII, and are kept as-is for debuggability; only recognized SQL-bearing
    keys are masked. Callers must never pass tool *results* to this function
    — only the model-supplied call arguments.
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
    `RuntimeSettings.otlp_disable_redaction` master telemetry debug switch.

    DEFAULT (`disable_redaction=False`, the D25 posture): the redacted,
    telemetry-safe copy (`redact_tool_args` — SQL literals masked, `concept`/
    `query` fully redacted, `period`/`slot_bindings` values masked).
    DEBUG (`disable_redaction=True`): the REAL args, so a debugging operator sees
    the actual SQL/concept/query/slot/period values on the span in Phoenix.

    TELEMETRY-ONLY: this governs ONLY what a span records. Every caller passes the
    RAW `args` to the actual tool work regardless of this flag, so flipping it
    never changes what the tool executes or what scope the MCP enforces — it only
    changes what Phoenix sees (which becomes entity-bearing when on, hence the
    access-control requirement). Returns a shallow copy so the span code can never
    mutate the caller's live args dict.
    """
    return dict(args) if disable_redaction else redact_tool_args(tool_name, args)


class Redactor:
    """Bundles the redaction rules above behind one object — the seam
    `observability/tracing.py` and `observability/progress.py` depend on."""

    def hash_scope(self, column_scope: frozenset[str]) -> str:
        return hash_scope(column_scope)

    def mask_sql(self, sql: str) -> str:
        return mask_sql(sql)

    def redact_tool_args(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return redact_tool_args(tool_name, args)


__all__ = ["Redactor", "hash_scope", "mask_sql", "redact_tool_args", "tool_span_args"]
