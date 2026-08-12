"""Table-driven tests for dispatch/denial_mapping.py over every registered ToolError
code (Layer 1). The authoritative set is `denial_mapping.KNOWN_DENIAL_CODES`; the
groups below enumerate it by family so a new code must be added here deliberately."""

from __future__ import annotations

import pytest

from data_agent.runtime.dispatch.denial_mapping import KNOWN_DENIAL_CODES, classify_denial

_ALL_SEVEN_CODES = {
    "COLUMN_SCOPE_VIOLATION",
    "SCRATCH_SESSION_VIOLATION",
    "PARSE_FAILED_CLOSED",
    "DATABASE_NOT_ALLOWED",
    "TABLE_NOT_FOUND",
    "CLICKHOUSE_QUERY_ERROR",
    "CLICKHOUSE_UNAVAILABLE",
}

# runQuery guardrail codes surfaced by the clickhouse-api service layer (not part
# of the original seven): the cartesian-join block. Retryable — the model can
# self-correct by adding an ON/USING condition or wrapping a constant side.
_GUARDRAIL_CODES = {
    "CARTESIAN_JOIN_FORBIDDEN",
}

# D77 resolveValues composite codes (L5): set locally by the composite, but
# re-derived from the denial table on replay (user_message is not persisted).
_COMPOSITE_CODES = {
    "RESOLVE_VALUES_UNKNOWN_TARGET",
    "RESOLVE_VALUES_INTERNAL_ERROR",
    "RESOLVE_VALUES_UNAVAILABLE",
}

# Read-tools codes (read-tools §6): shared RETRIEVAL_TOOL_* family + the
# registry-seam containment code, all rendered from the denial table on replay.
_READ_TOOL_CODES = {
    "RETRIEVAL_TOOL_INVALID_ARGS",
    "RETRIEVAL_TOOL_UNAVAILABLE",
    "RETRIEVAL_TOOL_INTERNAL_ERROR",
    "RUNTIME_TOOL_INTERNAL_ERROR",
}

_EXPECTED_RETRYABLE = {
    # RETRYABLE on purpose: the fix is one runBlueprint call away and the model can
    # make it inside the same turn.
    "ANSWER_TABLE_BLUEPRINT_NOT_RUN": True,
    "COLUMN_SCOPE_VIOLATION": False,
    "SCRATCH_SESSION_VIOLATION": False,
    "PARSE_FAILED_CLOSED": True,
    "DATABASE_NOT_ALLOWED": True,
    "TABLE_NOT_FOUND": True,
    "CLICKHOUSE_QUERY_ERROR": True,
    "CLICKHOUSE_UNAVAILABLE": False,
    "CARTESIAN_JOIN_FORBIDDEN": True,
    # Retryable flags MUST match what the composite itself sets so replay is
    # consistent with the live-call semantics (composite/resolve_values.py).
    "RESOLVE_VALUES_UNKNOWN_TARGET": True,
    "RESOLVE_VALUES_INTERNAL_ERROR": False,
    "RESOLVE_VALUES_UNAVAILABLE": False,
    "RETRIEVAL_TOOL_INVALID_ARGS": True,
    "RETRIEVAL_TOOL_UNAVAILABLE": False,
    "RETRIEVAL_TOOL_INTERNAL_ERROR": False,
    "RUNTIME_TOOL_INTERNAL_ERROR": False,
    # analysisState (Release 1, 03 §C.3). The split is load-bearing: a rejected
    # UPDATE is fixable inside the same turn, but a LATE INIT is not — the
    # substantive work has already started, so the boundary has passed and no
    # retry of the same call can help.
    "ANALYSIS_STATE_INVALID": True,
    "ANALYSIS_STATE_LATE_INIT": False,
    # Finalization enforcement (05 §B.1): retryable BY CONSTRUCTION — a
    # non-retryable refusal would end the turn it exists to keep alive.
    "FINALIZATION_BLOCKED_PENDING_INTENTS": True,
}

# `answerWithTable(blueprint_id=…)` naming a blueprint that never ran this turn.
# Registered here — not only on the ToolResult — because `user_message` is not
# persisted on TrailEntry: every later rebuild re-derives it from `error_code`, and
# an unregistered code would render as the generic failure string, stranding the
# model with no idea what to do differently.
_ANSWER_TABLE_CODES = {"ANSWER_TABLE_BLUEPRINT_NOT_RUN"}

# `updateAnalysisState` rejections. Registered for the same reason: both codes
# ALSO carry a specific `denial_detail`, but `denial_detail` is per-entry while
# this table is what a code renders as when no detail was set.
_ANALYSIS_STATE_CODES = {"ANALYSIS_STATE_INVALID", "ANALYSIS_STATE_LATE_INIT"}

# Finalization enforcement (Release 1, 05 §B.1): the refusal returned in place of a
# terminal `answerWithTable` while declared intents are still pending. RETRYABLE —
# the whole point is that the turn continues and the model gets one more round to
# resolve them.
_FINALIZATION_CODES = {"FINALIZATION_BLOCKED_PENDING_INTENTS"}

_ALL_KNOWN_CODES = (
    _ALL_SEVEN_CODES
    | _GUARDRAIL_CODES
    | _COMPOSITE_CODES
    | _READ_TOOL_CODES
    | _ANSWER_TABLE_CODES
    | _ANALYSIS_STATE_CODES
    | _FINALIZATION_CODES
)


def test_all_known_codes_are_registered() -> None:
    assert KNOWN_DENIAL_CODES == frozenset(_ALL_KNOWN_CODES)


@pytest.mark.parametrize("code", sorted(_ALL_KNOWN_CODES))
def test_classify_denial_retryability(code: str) -> None:
    info = classify_denial(code)
    assert info.code == code
    assert info.retryable is _EXPECTED_RETRYABLE[code]
    assert info.user_message  # non-empty, user-facing text


@pytest.mark.parametrize("code", sorted(_ALL_KNOWN_CODES))
def test_classify_denial_user_message_is_non_technical(code: str) -> None:
    info = classify_denial(code)
    # Never leak the raw MCP error-code string into the user-facing message.
    assert code not in info.user_message


def test_unknown_code_is_not_retryable() -> None:
    info = classify_denial("SOME_NEW_UNMAPPED_CODE")
    assert info.retryable is False
    assert info.user_message


def test_none_code_is_handled() -> None:
    info = classify_denial(None)
    assert info.retryable is False
    assert info.code == "UNKNOWN_ERROR"
