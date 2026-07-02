"""Graceful-denial mapping — ToolError code -> {retryable-by-model, user-facing-message} (design §3.4).

| Code | Model retry makes sense? | User-facing surfacing |
|---|---|---|
| `COLUMN_SCOPE_VIOLATION` | No | "This needs access to columns outside your current permissions." |
| `SCRATCH_SESSION_VIOLATION` | No | "That data isn't available in this session." |
| `PARSE_FAILED_CLOSED` | Sometimes (rephrase) | Nudges the model toward `explainQuery` first. |
| `DATABASE_NOT_ALLOWED` | Yes | Fed back as a normal tool error; the model self-corrects. |
| `TABLE_NOT_FOUND` | Yes | Fed back as a normal tool error; the model self-corrects. |
| `CLICKHOUSE_QUERY_ERROR` | Yes | Fed back as a normal tool error; the model self-corrects. |
| `CLICKHOUSE_UNAVAILABLE` | No (transient) | "The data warehouse is temporarily unavailable." |

All seven code paths increment the loop's iteration/token counters (Pass B's
`loop/budget_guard.py`) — a rejected call is not free.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DenialInfo:
    """Classification of one `ToolError` code."""

    code: str
    retryable: bool
    user_message: str


_DENIAL_TABLE: dict[str, DenialInfo] = {
    "COLUMN_SCOPE_VIOLATION": DenialInfo(
        code="COLUMN_SCOPE_VIOLATION",
        retryable=False,
        user_message="This needs access to columns outside your current permissions.",
    ),
    "SCRATCH_SESSION_VIOLATION": DenialInfo(
        code="SCRATCH_SESSION_VIOLATION",
        retryable=False,
        user_message="That data isn't available in this session.",
    ),
    "PARSE_FAILED_CLOSED": DenialInfo(
        code="PARSE_FAILED_CLOSED",
        retryable=True,
        user_message=(
            "I couldn't validate that query safely — let me try explainQuery first."
        ),
    ),
    "DATABASE_NOT_ALLOWED": DenialInfo(
        code="DATABASE_NOT_ALLOWED",
        retryable=True,
        user_message="That database isn't available. Let me check what's accessible.",
    ),
    "TABLE_NOT_FOUND": DenialInfo(
        code="TABLE_NOT_FOUND",
        retryable=True,
        user_message="I couldn't find that table. Let me verify the table name.",
    ),
    "CLICKHOUSE_QUERY_ERROR": DenialInfo(
        code="CLICKHOUSE_QUERY_ERROR",
        retryable=True,
        user_message="That query didn't run correctly. Let me fix it and try again.",
    ),
    "CLICKHOUSE_UNAVAILABLE": DenialInfo(
        code="CLICKHOUSE_UNAVAILABLE",
        retryable=False,
        user_message="The data warehouse is temporarily unavailable.",
    ),
    # D77 resolveValues composite codes (L5): these never come from the MCP —
    # the composite sets them locally with a crafted, target-specific
    # `user_message` (e.g. "No column 'X' on table 'Y' is available."). But
    # `user_message` is NOT persisted on `TrailEntry`, so on replay
    # `context/budget.py::_render_entry` re-derives it from `error_code` via
    # this table. The table cannot know the specific column name, so these are
    # generic-but-actionable and match the composite's retryable semantics.
    "RESOLVE_VALUES_UNKNOWN_TARGET": DenialInfo(
        code="RESOLVE_VALUES_UNKNOWN_TARGET",
        retryable=True,
        user_message=(
            "That table or column isn't available. Check the exact name with "
            "getTableSchema and try again."
        ),
    ),
    "RESOLVE_VALUES_INTERNAL_ERROR": DenialInfo(
        code="RESOLVE_VALUES_INTERNAL_ERROR",
        retryable=False,
        user_message="Something went wrong resolving those values. Please try again.",
    ),
    "RESOLVE_VALUES_UNAVAILABLE": DenialInfo(
        code="RESOLVE_VALUES_UNAVAILABLE",
        retryable=False,
        user_message="Value resolution is not available right now.",
    ),
}

KNOWN_DENIAL_CODES = frozenset(_DENIAL_TABLE)


def classify_denial(code: str | None) -> DenialInfo:
    """Classify a `ToolError` code into `{retryable, user_message}`.

    An unrecognized (or missing, `None`) code — e.g. an unexpected/internal
    MCP error whose `[{CODE}]` prefix could not be parsed — is treated
    conservatively: not retryable, and surfaced as a generic failure. This
    never raises, so `dispatch/tool_dispatcher.py` can always classify
    whatever `MCPToolError.code` it receives.
    """
    if code is not None and code in _DENIAL_TABLE:
        return _DENIAL_TABLE[code]
    return DenialInfo(
        code=code or "UNKNOWN_ERROR",
        retryable=False,
        user_message="Something went wrong processing that request.",
    )
