"""Table-driven tests for dispatch/denial_mapping.py over every registered ToolError
code (Layer 1). The authoritative set is `denial_mapping.KNOWN_DENIAL_CODES`; the
groups below enumerate it by family so a new code must be added here deliberately."""

from __future__ import annotations

import pytest

from data_agent.runtime.dispatch.denial_mapping import (
    ENFORCEMENT_DENIAL_CODES,
    KNOWN_DENIAL_CODES,
    classify_denial,
)

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
    # getBlueprint-before-runBlueprint (Release 1): retryable by construction —
    # the fix is one `getBlueprint` away, inside the same turn.
    "BLUEPRINT_DEFINITION_NOT_READ": True,
    # The empty designation (08 §O): retryable by construction, and bounded by the
    # 05 §J shape-gate allowance rather than by this flag — a non-retryable refusal
    # would end the turn this exists to hand back.
    "ANSWER_TABLE_NO_TABLE_DESIGNATED": True,
}

# `answerWithTable(blueprint_id=…)` naming a blueprint that never ran this turn.
# Registered here — not only on the ToolResult — because `user_message` is not
# persisted on TrailEntry: every later rebuild re-derives it from `error_code`, and
# an unregistered code would render as the generic failure string, stranding the
# model with no idea what to do differently.
#
# ANSWER_TABLE_NO_TABLE_DESIGNATED (08 §O) is the same failure one step earlier —
# the model named NO table rather than an unresolvable one — and it exists because
# the empty call otherwise SUCCEEDS and terminates the turn through exit #2, which
# the 05 §J shape gate does not watch. Measured live as `done` with no table and no
# event at all.
_ANSWER_TABLE_CODES = {
    "ANSWER_TABLE_BLUEPRINT_NOT_RUN",
    "ANSWER_TABLE_NO_TABLE_DESIGNATED",
}

# `updateAnalysisState` rejections. Registered for the same reason: both codes
# ALSO carry a specific `denial_detail`, but `denial_detail` is per-entry while
# this table is what a code renders as when no detail was set.
_ANALYSIS_STATE_CODES = {"ANALYSIS_STATE_INVALID", "ANALYSIS_STATE_LATE_INIT"}

# Finalization enforcement (Release 1, 05 §B.1): the refusal returned in place of a
# terminal `answerWithTable` while declared intents are still pending. RETRYABLE —
# the whole point is that the turn continues and the model gets one more round to
# resolve them.
_FINALIZATION_CODES = {"FINALIZATION_BLOCKED_PENDING_INTENTS"}

# The getBlueprint-before-runBlueprint gate (Release 1): `runBlueprint` for a
# blueprint the model never expanded this turn, so it had never seen the SQL it was
# about to execute. Registered for the same reason as the codes above — the specific
# text (which blueprint) rides on `denial_detail`, and without a table entry
# `classify_denial` renders the generic string on every later rebuild.
_BLUEPRINT_DEFINITION_CODES = {"BLUEPRINT_DEFINITION_NOT_READ"}

_ALL_KNOWN_CODES = (
    _ALL_SEVEN_CODES
    | _GUARDRAIL_CODES
    | _COMPOSITE_CODES
    | _READ_TOOL_CODES
    | _ANSWER_TABLE_CODES
    | _ANALYSIS_STATE_CODES
    | _FINALIZATION_CODES
    | _BLUEPRINT_DEFINITION_CODES
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


# --- enforcement: is the refusal about the CALL or about the WORK? --------------
#
# `DenialInfo.enforcement` is READ by the offline learning loop
# (`learning/summary/loader.py::ENFORCEMENT_ERROR_CODES` is derived from it): an
# enforcement denial is NOT counted as analyst friction — no `failed_fixed` pair, no
# `corrected` blueprint usage. So a wrong classification here is a silent measurement
# bug two packages away, in either direction: True on a real failure hides friction that
# happened, False on a gate invents friction that did not.
#
# This map is DELIBERATELY hand-written, one line of rationale per code, and the test
# below fails if a registered code is missing from it. Mirroring `_DENIAL_TABLE` with a
# comprehension would assert nothing; the value of this map is that adding a code to the
# table cannot go green until a human has said, in words, which side it falls on.
#
# The line is not "did the executor run" — `COLUMN_SCOPE_VIOLATION` is refused before
# ClickHouse is touched and is still substantive. It is "was the model's DATA WORK
# judged (its SQL, its blueprint, the warehouse under them), or only its CALL PROTOCOL
# (the order of calls, the shape of the envelope)".
_EXPECTED_ENFORCEMENT = {
    # --- protocol / gate refusals: nothing was computed, nothing computed was wrong ---
    # Call ORDER: getBlueprint must precede runBlueprint. The executor never ran, so
    # this is not evidence that the blueprint is wrong.
    "BLUEPRINT_DEFINITION_NOT_READ": True,
    # Answer SHAPE: a blueprint named in the answer that was never run this turn.
    "ANSWER_TABLE_BLUEPRINT_NOT_RUN": True,
    # Answer SHAPE: no table designated at all (08 §O).
    "ANSWER_TABLE_NO_TABLE_DESIGNATED": True,
    # Finalization ORDER: the turn's work may be perfect; ending the turn with intents
    # still pending is what was refused.
    "FINALIZATION_BLOCKED_PENDING_INTENTS": True,
    # Bookkeeping hygiene: a mis-shaped intent ledger update. `updateAnalysisState`
    # computes nothing, so it can fail in no data-bearing way.
    "ANALYSIS_STATE_INVALID": True,
    # The same ledger refused on TIMING rather than shape. Non-retryable AND
    # enforcement — the two flags are independent.
    "ANALYSIS_STATE_LATE_INIT": True,
    # Envelope validation on the read tools (`_require_text` / `_clamp_k`): a blank
    # `query`, an out-of-range `k`. Refused before any search ran — the
    # ANALYSIS_STATE_INVALID shape, not the TABLE_NOT_FOUND one. Unreachable by today's
    # learning readers (no retrieval tool is a data tool), classified for completeness.
    "RETRIEVAL_TOOL_INVALID_ARGS": True,
    # --- substantive failures: the work was judged, or the system under it failed -----
    # The query asked for columns this scope does not hold — a verdict on the SQL.
    "COLUMN_SCOPE_VIOLATION": False,
    # The query referenced another session's scratch data — again, what the SQL named.
    "SCRATCH_SESSION_VIOLATION": False,
    # The SQL could not be parsed/validated.
    "PARSE_FAILED_CLOSED": False,
    # The query named a database it may not read.
    "DATABASE_NOT_ALLOWED": False,
    # The query named a table that does not exist — the canonical failed→fixed shape.
    "TABLE_NOT_FOUND": False,
    # A guardrail on the shape of the QUERY (a missing join condition), not of the call.
    "CARTESIAN_JOIN_FORBIDDEN": False,
    # The warehouse ran it and rejected it.
    "CLICKHOUSE_QUERY_ERROR": False,
    # The system underneath the work failed. That IS friction, and counting it is right.
    "CLICKHOUSE_UNAVAILABLE": False,
    # The model named a table/column that does not exist — TABLE_NOT_FOUND reached
    # through the composite instead of the MCP.
    "RESOLVE_VALUES_UNKNOWN_TARGET": False,
    # A crash, not a refusal: attempted, then failed.
    "RESOLVE_VALUES_INTERNAL_ERROR": False,
    # A capability outage — the CLICKHOUSE_UNAVAILABLE shape.
    "RESOLVE_VALUES_UNAVAILABLE": False,
    # A capability outage: the request was fine, the dependency was not.
    "RETRIEVAL_TOOL_UNAVAILABLE": False,
    # A crash that slipped every guard.
    "RETRIEVAL_TOOL_INTERNAL_ERROR": False,
    # Registry-seam containment: a runtime tool raised or broke its contract. A real
    # failure of a real attempt, and the conservative reading of "we do not know what
    # went wrong".
    "RUNTIME_TOOL_INTERNAL_ERROR": False,
}


def test_every_registered_code_has_a_stated_enforcement_classification() -> None:
    """The guard H1 exists for. A code added to `_DENIAL_TABLE` defaults to
    `enforcement=False` (substantive, the pre-existing behaviour), which is safe but
    SILENT — the author may have meant to register a gate. This fails until the new
    code is written down here with a reason, which is the only moment anyone is
    thinking about the learning loop's friction counters."""
    assert KNOWN_DENIAL_CODES == frozenset(_EXPECTED_ENFORCEMENT)


@pytest.mark.parametrize("code", sorted(_EXPECTED_ENFORCEMENT))
def test_classify_denial_enforcement(code: str) -> None:
    assert classify_denial(code).enforcement is _EXPECTED_ENFORCEMENT[code]


def test_enforcement_denial_codes_is_the_true_half_of_the_table() -> None:
    """The derived export the learning loop actually imports."""
    assert ENFORCEMENT_DENIAL_CODES == frozenset(
        code for code, is_enforcement in _EXPECTED_ENFORCEMENT.items() if is_enforcement
    )


def test_an_unknown_code_is_not_enforcement() -> None:
    """Conservative direction, matching `retryable`: an unclassifiable failure stays a
    failure, so the learning loop keeps counting it as friction rather than quietly
    forgiving it."""
    assert classify_denial("SOME_NEW_UNMAPPED_CODE").enforcement is False
    assert classify_denial(None).enforcement is False
