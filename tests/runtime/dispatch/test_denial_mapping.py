"""Table-driven tests for dispatch/denial_mapping.py over every registered ToolError
code (Layer 1). The authoritative set is `denial_mapping.KNOWN_DENIAL_CODES`; the
groups below enumerate it by family so a new code must be added here deliberately."""

from __future__ import annotations

import pytest

from data_agent.runtime.dispatch.denial_mapping import (
    ENFORCEMENT_DENIAL_CODES,
    INFRA_FAILURE_CODES,
    KNOWN_DENIAL_CODES,
    DenialKind,
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

# The runBlueprint executor family (§5.4, `blueprint/executor.py`) + the B4 raw-transport
# marker (`dispatch/tool_dispatcher.py`). These reach real trail entries and were absent
# from the table until H8 patch 2 — so on replay they all rendered the generic "Something
# went wrong processing that request.", and the learning loop filed every one of them
# under the unregistered default.
_RUN_BLUEPRINT_CODES = {
    "RUN_BLUEPRINT_NOT_FOUND",
    "RUN_BLUEPRINT_SLOT_INVALID",
    "RUN_BLUEPRINT_UNSUPPORTED",
    "RUN_BLUEPRINT_VERIFY_FAILED",
    "RUN_BLUEPRINT_ABORTED",
}

_TRANSPORT_CODES = {"INTERNAL_TRANSPORT_ERROR"}

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
    # runBlueprint executor family: each flag MATCHES the live
    # `ExecFailed(..., retryable=…)` at its production site, for the same reason the
    # composite codes above do — a replay that contradicts the live semantics teaches the
    # model the opposite lesson from the one it learned on the turn.
    "RUN_BLUEPRINT_NOT_FOUND": True,
    "RUN_BLUEPRINT_SLOT_INVALID": True,
    # NOT retryable: the fast path declined the blueprint as a CAPABILITY matter, so
    # re-issuing the identical call cannot help — the model must fall back to raw tools.
    # (One passthrough site in `executor.py` sets True; the table takes the dominant and
    # conservative reading.)
    "RUN_BLUEPRINT_UNSUPPORTED": False,
    "RUN_BLUEPRINT_VERIFY_FAILED": True,
    "RUN_BLUEPRINT_ABORTED": True,
    # B4 raw transport blowup: the dispatcher itself sets retryable=False.
    "INTERNAL_TRANSPORT_ERROR": False,
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
    | _RUN_BLUEPRINT_CODES
    | _TRANSPORT_CODES
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


# --- kind: what does the refusal say about the model's WORK? --------------------
#
# `DenialInfo.kind` is READ by the offline learning loop (both
# `learning/summary/loader.py::ENFORCEMENT_ERROR_CODES` and `::INFRA_ERROR_CODES` are
# derived from it): an enforcement denial is NOT counted as analyst friction and emits no
# blueprint usage, and an INFRA failure emits no blueprint usage while STILL counting as
# friction. So a wrong classification here is a silent measurement bug two packages away,
# in every direction: GATE on a real failure hides friction that happened, WORK_JUDGED on
# a gate invents friction that did not, and WORK_JUDGED on an outage records "this
# blueprint was wrong" about a warehouse that was down.
#
# This map is DELIBERATELY hand-written, one line of rationale per code, and the test
# below fails if a registered code is missing from it. Mirroring `_DENIAL_TABLE` with a
# comprehension would assert nothing; the value of this map is that adding a code to the
# table cannot go green until a human has said, in words, which side it falls on.
#
# The GATE/WORK_JUDGED line is not "did the executor run" — `COLUMN_SCOPE_VIOLATION` is
# refused before ClickHouse is touched and is still WORK_JUDGED. It is "was the model's
# DATA WORK judged (its SQL, its blueprint), or only its CALL PROTOCOL (the order of
# calls, the shape of the envelope)". The WORK_JUDGED/INFRA_FAILED line is the H8 one:
# did anything actually form a verdict on the work, or did the floor give way under it.
_EXPECTED_KIND = {
    # --- GATE: protocol refusals — nothing was computed, nothing computed was wrong ---
    # Call ORDER: getBlueprint must precede runBlueprint. The executor never ran, so
    # this is not evidence that the blueprint is wrong.
    "BLUEPRINT_DEFINITION_NOT_READ": DenialKind.GATE,
    # Answer SHAPE: a blueprint named in the answer that was never run this turn.
    "ANSWER_TABLE_BLUEPRINT_NOT_RUN": DenialKind.GATE,
    # Answer SHAPE: no table designated at all (08 §O).
    "ANSWER_TABLE_NO_TABLE_DESIGNATED": DenialKind.GATE,
    # Finalization ORDER: the turn's work may be perfect; ending the turn with intents
    # still pending is what was refused.
    "FINALIZATION_BLOCKED_PENDING_INTENTS": DenialKind.GATE,
    # Bookkeeping hygiene: a mis-shaped intent ledger update. `updateAnalysisState`
    # computes nothing, so it can fail in no data-bearing way.
    "ANALYSIS_STATE_INVALID": DenialKind.GATE,
    # The same ledger refused on TIMING rather than shape. Non-retryable AND a gate —
    # `retryable` and `kind` are independent.
    "ANALYSIS_STATE_LATE_INIT": DenialKind.GATE,
    # Envelope validation on the read tools (`_require_text` / `_clamp_k`): a blank
    # `query`, an out-of-range `k`. Refused before any search ran — the
    # ANALYSIS_STATE_INVALID shape, not the TABLE_NOT_FOUND one. Unreachable by today's
    # learning readers (no retrieval tool is a data tool), classified for completeness.
    "RETRIEVAL_TOOL_INVALID_ARGS": DenialKind.GATE,
    # --- WORK_JUDGED: the model's own query or blueprint was judged and found wanting -
    # The query asked for columns this scope does not hold — a verdict on the SQL.
    "COLUMN_SCOPE_VIOLATION": DenialKind.WORK_JUDGED,
    # The query referenced another session's scratch data — again, what the SQL named.
    "SCRATCH_SESSION_VIOLATION": DenialKind.WORK_JUDGED,
    # The SQL could not be parsed/validated.
    "PARSE_FAILED_CLOSED": DenialKind.WORK_JUDGED,
    # The query named a database it may not read.
    "DATABASE_NOT_ALLOWED": DenialKind.WORK_JUDGED,
    # The query named a table that does not exist — the canonical failed→fixed shape.
    "TABLE_NOT_FOUND": DenialKind.WORK_JUDGED,
    # A guardrail on the shape of the QUERY (a missing join condition), not of the call.
    "CARTESIAN_JOIN_FORBIDDEN": DenialKind.WORK_JUDGED,
    # The warehouse ran it and rejected it.
    "CLICKHOUSE_QUERY_ERROR": DenialKind.WORK_JUDGED,
    # The model named a table/column that does not exist — TABLE_NOT_FOUND reached
    # through the composite instead of the MCP.
    "RESOLVE_VALUES_UNKNOWN_TARGET": DenialKind.WORK_JUDGED,
    # --- INFRA_FAILED: the floor gave way; nothing formed a verdict on the work -------
    # The warehouse was down. The SPLIT VERDICT this kind exists for (H8): friction YES —
    # the analyst hit a wall and had to re-run, so `_failed_fixed_pairs` still pairs it —
    # but `corrected` NO, because the blueprint's SQL was never judged by anything. As a
    # boolean this had to pick one, and it picked the wrong one for the second reader:
    # a `runBlueprint` during an outage was recorded as a blueprint that produced a wrong
    # answer, penalising the corpus for a warehouse being unreachable.
    "CLICKHOUSE_UNAVAILABLE": DenialKind.INFRA_FAILED,
    # A crash inside the composite: attempted, then the code under it broke.
    "RESOLVE_VALUES_INTERNAL_ERROR": DenialKind.INFRA_FAILED,
    # A capability outage — the CLICKHOUSE_UNAVAILABLE shape.
    "RESOLVE_VALUES_UNAVAILABLE": DenialKind.INFRA_FAILED,
    # A capability outage: the request was fine, the dependency was not.
    "RETRIEVAL_TOOL_UNAVAILABLE": DenialKind.INFRA_FAILED,
    # A crash that slipped every guard.
    "RETRIEVAL_TOOL_INTERNAL_ERROR": DenialKind.INFRA_FAILED,
    # Registry-seam containment: a runtime tool raised or broke its contract. The TOOL
    # broke — a fact about the runtime, not a judgement of the model's data work.
    "RUNTIME_TOOL_INTERNAL_ERROR": DenialKind.INFRA_FAILED,
    # --- the runBlueprint executor family (H8 patch 2), which the three kinds split ----
    # The model named a blueprint id that is not in its scope — the TABLE_NOT_FOUND shape
    # one layer up, and the searchBlueprints that follows is a real repair.
    "RUN_BLUEPRINT_NOT_FOUND": DenialKind.WORK_JUDGED,
    # A slot VALUE the model chose could not be bound. The call was well-formed and what
    # was inside it was wrong, which is the bad-SQL class, not the envelope-shape class.
    "RUN_BLUEPRINT_SLOT_INVALID": DenialKind.WORK_JUDGED,
    # The fast path DECLINED (unsupported node/shape/rule) before forming any opinion of
    # the blueprint. A capability refusal: nothing ran, so nothing can have been wrong.
    "RUN_BLUEPRINT_UNSUPPORTED": DenialKind.GATE,
    # The run STOPPED cleanly before an answer — `when…on_violation: abort`, a user
    # DENYING an approval gate, or every node skipped. Filing a human saying "no" as a
    # blueprint that produced a wrong answer punishes the corpus for its own consent seam.
    "RUN_BLUEPRINT_ABORTED": DenialKind.GATE,
    # The one in this family where the blueprint's OUTPUT was examined and rejected (D56
    # verification of the terminal node). This is what `outcome="corrected"` should mean,
    # and it is why the other four could not be left sharing the unregistered fallback.
    "RUN_BLUEPRINT_VERIFY_FAILED": DenialKind.WORK_JUDGED,
    # B4: a connection refusal / timeout / malformed response. The wire broke.
    "INTERNAL_TRANSPORT_ERROR": DenialKind.INFRA_FAILED,
}


def test_every_registered_code_has_a_stated_kind_classification() -> None:
    """The guard H1 exists for, widened by H8. A code added to `_DENIAL_TABLE` defaults
    to `kind=WORK_JUDGED` (substantive, the pre-existing behaviour), which is safe but
    SILENT — the author may have meant to register a gate, or an outage. This fails until
    the new code is written down here with a reason, which is the only moment anyone is
    thinking about the learning loop's friction and corpus-quality counters."""
    assert KNOWN_DENIAL_CODES == frozenset(_EXPECTED_KIND)


@pytest.mark.parametrize("code", sorted(_EXPECTED_KIND))
def test_classify_denial_kind(code: str) -> None:
    assert classify_denial(code).kind is _EXPECTED_KIND[code]


def test_the_three_kinds_partition_the_table() -> None:
    """Exhaustive AND pairwise disjoint over every registered code. `DenialKind` is
    one-of by construction, so this cannot fail on today's shape — it is here to fail
    LOUDLY the day someone reaches for a fourth answer (a set, a bitmask, a `None`) and
    the two derived exports below silently stop covering the vocabulary between them."""
    by_kind = {
        kind: frozenset(c for c in KNOWN_DENIAL_CODES if classify_denial(c).kind is kind)
        for kind in DenialKind
    }
    assert frozenset().union(*by_kind.values()) == KNOWN_DENIAL_CODES
    kinds = list(DenialKind)
    for i, a in enumerate(kinds):
        for b in kinds[i + 1 :]:
            assert by_kind[a].isdisjoint(by_kind[b]), (a, b)
    # Every kind is actually USED — a partition with an empty part means a class of
    # failure nobody has classified yet, not a clean taxonomy.
    assert all(by_kind[kind] for kind in DenialKind)


def test_enforcement_denial_codes_is_the_gate_third_of_the_table() -> None:
    """The derived export the learning loop actually imports."""
    assert ENFORCEMENT_DENIAL_CODES == frozenset(
        code for code, kind in _EXPECTED_KIND.items() if kind is DenialKind.GATE
    )


def test_infra_failure_codes_is_the_infra_third_of_the_table() -> None:
    """The H8 twin of the export above: `loader.py::INFRA_ERROR_CODES` is built from it,
    and it is what stops a warehouse outage being recorded as a wrong blueprint."""
    assert INFRA_FAILURE_CODES == frozenset(
        code for code, kind in _EXPECTED_KIND.items() if kind is DenialKind.INFRA_FAILED
    )


@pytest.mark.parametrize("code", sorted(_EXPECTED_KIND))
def test_enforcement_property_is_exactly_the_gate_kind(code: str) -> None:
    """`DenialInfo.enforcement` survives as a DERIVED property so no H1-era reader
    changed shape. Pinned per code, both directions: an INFRA failure must NOT read as
    enforcement — `_failed_fixed_pairs` skips enforcement denials, and forgiving an
    outage there would erase real analyst friction."""
    info = classify_denial(code)
    assert info.enforcement is (info.kind is DenialKind.GATE)


def test_an_unknown_code_is_work_judged() -> None:
    """Conservative direction, matching `retryable`: an unclassifiable failure stays a
    failure of real work, so the learning loop keeps counting it as friction and keeps
    reading a denied blueprint as corrected, rather than quietly forgiving either."""
    assert classify_denial("SOME_NEW_UNMAPPED_CODE").kind is DenialKind.WORK_JUDGED
    assert classify_denial(None).kind is DenialKind.WORK_JUDGED
    assert classify_denial("SOME_NEW_UNMAPPED_CODE").enforcement is False
    assert classify_denial(None).enforcement is False


# --- the late registrations must not drift from their production sites ----------


def test_late_registered_codes_render_their_live_message_on_replay() -> None:
    """H8 patch 2. These six codes are SPELLED in `denial_mapping.py`, not imported:
    `blueprint/executor.py` imports `dispatch/tool_dispatcher.py`, which imports the
    denial table, so importing the constants back would close a cycle.

    A test CAN import both sides, which is where the duplication is made safe. Every
    entry must be byte-identical to what the live turn told the model — the whole reason
    `user_message` is re-derived here at all is that it is not persisted on `TrailEntry`,
    so a drift would have the replay contradict the turn rather than fail."""
    from data_agent.runtime.blueprint import executor as _ex
    from data_agent.runtime.dispatch import tool_dispatcher as _td

    live = {
        _ex.NOT_FOUND_CODE: _ex._NOT_FOUND_MESSAGE,
        _ex.SLOT_INVALID_CODE: _ex._SLOT_INVALID_MESSAGE,
        _ex.UNSUPPORTED_CODE: _ex._UNSUPPORTED_MESSAGE,
        _ex.VERIFY_FAILED_CODE: _ex._VERIFY_FAILED_MESSAGE,
        _ex.ABORTED_CODE: _ex._ABORTED_MESSAGE,
        _td.INTERNAL_TRANSPORT_ERROR_CODE: _td._INTERNAL_TRANSPORT_ERROR_MESSAGE,
    }
    assert frozenset(live) == _RUN_BLUEPRINT_CODES | _TRANSPORT_CODES
    for code, message in live.items():
        assert classify_denial(code).user_message == message
        assert classify_denial(code).user_message != "Something went wrong processing that request."
