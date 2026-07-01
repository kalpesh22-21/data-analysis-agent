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

from data_agent.runtime.context.scope_filter import compute_scope_hash

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


def hash_scope(column_scope: frozenset[str]) -> str:
    """Stable hash of *column_scope* — never log the raw scope (D25)."""
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
    return redacted


class Redactor:
    """Bundles the redaction rules above behind one object — the seam
    `observability/tracing.py` and `observability/progress.py` depend on."""

    def hash_scope(self, column_scope: frozenset[str]) -> str:
        return hash_scope(column_scope)

    def mask_sql(self, sql: str) -> str:
        return mask_sql(sql)

    def redact_tool_args(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return redact_tool_args(tool_name, args)


__all__ = ["Redactor", "hash_scope", "mask_sql", "redact_tool_args"]
