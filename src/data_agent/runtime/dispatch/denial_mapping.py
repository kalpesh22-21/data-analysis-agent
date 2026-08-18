"""Graceful-denial mapping — ToolError code -> {retryable-by-model, user-facing-message}.

A retryable code (`TABLE_NOT_FOUND`, `CLICKHOUSE_QUERY_ERROR`, …) is fed back as an
ordinary tool error so the model self-corrects; a non-retryable one (a scope violation,
a transient warehouse outage) surfaces a static, PII-safe user message instead. Either
way the call still increments the loop's iteration/token counters — a rejected call is
not free.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DenialInfo:
    """Classification of one `ToolError` code."""

    code: str
    retryable: bool
    user_message: str
    # Is this refusal about the CALL rather than about the WORK?
    #
    # True  — a procedural/protocol gate: the runtime refused because of the ORDER or
    #         the SHAPE of the call (read the blueprint before running it, resolve your
    #         intents before finalizing, put a table in your answer, send a well-formed
    #         ledger update). Nothing substantive was attempted, and nothing the model
    #         asked OF THE DATA was found wanting. Release 1 made these routine: a
    #         healthy session trips them.
    # False — a substantive failure: the query/blueprint the model authored was itself
    #         rejected or came back wrong (bad SQL, out-of-scope columns, a table that
    #         does not exist), or the system underneath it failed. This is the DEFAULT
    #         and the conservative direction — an unclassifiable failure stays a
    #         failure.
    #
    # The line is NOT "did the executor run": `COLUMN_SCOPE_VIOLATION` is refused before
    # ClickHouse is touched and is still substantive, because what was refused is the
    # model's query. It is "was the model's DATA WORK judged, or only its call protocol".
    #
    # It lives HERE, where codes are registered, because the offline learning loop needs
    # the split and had been keeping its own hand-written list of it
    # (`learning/summary/loader.py::ENFORCEMENT_ERROR_CODES`, which now DERIVES from this
    # field). A hand-kept list fails loudly on a rename and silently on an ADDITION: a
    # new gate code counted as substantive friction, putting a permanent negative bias on
    # every session that trips it. Registering a code and classifying it are now the same
    # edit. Default `False` so the field can never be the reason an addition is silent in
    # the OTHER direction — an unclassified new code reads as a real failure, which is
    # what it was before this field existed.
    enforcement: bool = False


# The finalization refusal's code (Release 1, 05 §B.1). It lives HERE, at the
# bottom of the dependency graph, because THREE layers need the same literal and
# two of them cannot import each other: `loop/agent_loop.py` sets it,
# `context/assembly.py` drops the entry carrying it from a LATER turn's replay
# (the entry's `denial_detail` names every pending intent, and its `args` carry
# the refused draft prose), and this table renders its fallback text. A duplicated
# string literal in `context/` would silently stop matching the day the code is
# renamed, and the symptom would be a cross-turn text leak, not a failure.
FINALIZATION_BLOCKED_PENDING_INTENTS_CODE = "FINALIZATION_BLOCKED_PENDING_INTENTS"

# The empty-designation refusal's code (Release 1, 08 §O). It lives HERE for the
# SAME three-layer reason as the code above, and it is the FOURTH instance of the
# model-authored-text-crossing-turns class `context/assembly.py` names (README
# findings 9 and 11 are the first two): its `args` carry the model's refused draft
# answer prose, so `_is_stale_model_text_entry` has to drop it from every turn but
# its own — and it can only do that by matching this literal, since the entry is
# persisted under `answerWithTable`, whose SUCCESSFUL entries must keep replaying.
ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE = "ANSWER_TABLE_NO_TABLE_DESIGNATED"


_DENIAL_TABLE: dict[str, DenialInfo] = {
    # COLUMN_SCOPE_VIOLATION: on the LIVE turn the dispatcher
    # (`dispatch/tool_dispatcher.py`) surfaces the MCP's author-controlled
    # `ColumnScopeError` message instead of this string — that message NAMES the
    # specific out-of-scope column(s) (catalog metadata, not PII / cell values,
    # D25) so the model can self-correct. This generic string is the REPLAY
    # fallback only: `user_message` is not persisted on TrailEntry, so
    # `context/budget.py::_render_entry` re-derives it from `error_code` here,
    # where the specific column name is no longer available.
    "COLUMN_SCOPE_VIOLATION": DenialInfo(
        code="COLUMN_SCOPE_VIOLATION",
        retryable=False,
        # SUBSTANTIVE: the model's query asked for columns this scope does not hold.
        # Refused before ClickHouse ran, and still a judgement ON THE QUERY.
        enforcement=False,
        user_message="This needs access to columns outside your current permissions.",
    ),
    "SCRATCH_SESSION_VIOLATION": DenialInfo(
        code="SCRATCH_SESSION_VIOLATION",
        retryable=False,
        # SUBSTANTIVE: the query referenced scratch data belonging to another session —
        # again a fact about what the SQL named, not about call order.
        enforcement=False,
        user_message="That data isn't available in this session.",
    ),
    # ANSWER_TABLE_BLUEPRINT_NOT_RUN: the model ended its turn with
    # `answerWithTable(blueprint_id=…)` naming a blueprint it never actually ran, so
    # there is no terminal SQL to resolve and the user would have got prose with no
    # table. RETRYABLE and instructional: the fix is one `runBlueprint` call away and
    # the model can make it inside the same turn.
    #
    # It lives HERE, not only on the ToolResult, because `user_message` is not
    # persisted on TrailEntry — `context/budget.py::_render_entry` re-derives it from
    # `error_code` on every later rebuild. An unregistered code renders as the
    # generic "Something went wrong processing that request.", which would strand
    # the model with no idea what to do differently.
    "ANSWER_TABLE_BLUEPRINT_NOT_RUN": DenialInfo(
        code="ANSWER_TABLE_BLUEPRINT_NOT_RUN",
        retryable=True,
        # ENFORCEMENT: an answer-shape gate. The blueprint was never run, so there is no
        # blueprint OUTPUT to have been wrong — only a call made out of order.
        enforcement=True,
        user_message=(
            "You referenced a blueprint you have not run in this turn, so there is no "
            "table to show. Call runBlueprint with that blueprint first, then call "
            "answerWithTable again."
        ),
    ),
    # ANSWER_TABLE_NO_TABLE_DESIGNATED (Release 1, 08 §O): the model called
    # `answerWithTable` and named NO table at all — an empty `tables` array with
    # nothing to fold — on a turn holding multi-row results it never presented. The
    # call carries prose, so without this it SUCCEEDS and terminates the turn
    # through exit #2, which the 05 §J answer-shape gate does not watch: measured
    # live, `status=done`, no table, no event, no log. RETRYABLE and instructional,
    # and bounded by the shape gate's own per-window allowance so one mistake cannot
    # be refused twice.
    #
    # Registered here for the reason the two codes around it are: `user_message` is
    # not persisted on `TrailEntry`, `_render_entry` re-derives it from `error_code`
    # on every rebuild, and an unregistered code renders as the generic "Something
    # went wrong processing that request." Unlike those two this message needs no
    # id to name, so the table's text and the result's `denial_detail` can be — and
    # are — the same string.
    "ANSWER_TABLE_NO_TABLE_DESIGNATED": DenialInfo(
        code="ANSWER_TABLE_NO_TABLE_DESIGNATED",
        retryable=True,
        # ENFORCEMENT: the purest answer-SHAPE refusal in the table — the model named no
        # table at all. Nothing was computed and nothing computed was wrong.
        enforcement=True,
        user_message=(
            "Your answerWithTable named no table, so there is nothing for the user "
            "to look at. Every table goes in `tables`, one entry per part of your "
            'answer: `tables: [{sql: "SELECT …"}]` for a query you wrote, or '
            '`tables: [{blueprint_id: "bp-…"}]` for a blueprint you ran this turn. '
            "Send your answer again with the table in it."
        ),
    ),
    # BLUEPRINT_DEFINITION_NOT_READ (Release 1): the model called `runBlueprint` for
    # a blueprint it never expanded with `getBlueprint` in this turn, so it was
    # routing on the card's authored prose `intent` and had never seen the SQL it
    # was about to execute. Refused before the executor ran. RETRYABLE and
    # instructional — the fix is one `getBlueprint` away, inside the same turn.
    #
    # Registered here for the same reason as the code above: `user_message` is not
    # persisted on `TrailEntry`, so `context/budget.py::_render_entry` re-derives it
    # from `error_code` on every rebuild, and an unregistered code renders as the
    # generic "Something went wrong processing that request." The result's own
    # `denial_detail` NAMES the blueprint; this table cannot, since it sees only the
    # code. The two are kept in step deliberately.
    "BLUEPRINT_DEFINITION_NOT_READ": DenialInfo(
        code="BLUEPRINT_DEFINITION_NOT_READ",
        retryable=True,
        # ENFORCEMENT: a call-ORDER gate (getBlueprint before runBlueprint). The
        # executor never ran, so this is not evidence that the blueprint is wrong —
        # which is exactly what the learning loop would otherwise record.
        enforcement=True,
        user_message=(
            "You have not read that blueprint in this turn, so you do not know what "
            "it measures. Call getBlueprint with that blueprint id first, check what "
            "it actually does, then run it."
        ),
    ),
    # analysisState (Release 1, 03 §C.3). Both codes are set by
    # `composite/analysis_state.py`, never by the MCP, and both ALSO carry a
    # `denial_detail` on the result — that is the channel that names the specific
    # rule that failed, which this table cannot. These entries are the replay
    # fallback: without them `classify_denial` returns "Something went wrong
    # processing that request." and the model learns nothing.
    "ANALYSIS_STATE_INVALID": DenialInfo(
        code="ANALYSIS_STATE_INVALID",
        retryable=True,
        # ENFORCEMENT: bookkeeping hygiene. A mis-shaped ledger update says nothing
        # about the data — `updateAnalysisState` computes nothing in the first place.
        enforcement=True,
        user_message=(
            "That analysis-state update was rejected. Send one updateAnalysisState "
            "call listing each intent by the id you were given and its new status, "
            "and nothing else — tag the call that serves an intent with "
            "serves_intent when you make it, and the runtime finds it for you."
        ),
    ),
    # NOT retryable: the substantive work has already started, so the late-init
    # boundary has passed and no retry of the same call can help.
    "ANALYSIS_STATE_LATE_INIT": DenialInfo(
        code="ANALYSIS_STATE_LATE_INIT",
        retryable=False,
        # ENFORCEMENT: the same ledger, refused on TIMING rather than shape. Not
        # retryable and still not a data failure — the two flags are independent.
        enforcement=True,
        user_message=(
            "The intents for this question can no longer be declared — the analysis is "
            "already under way. Continue and answer everything the user asked."
        ),
    ),
    # Finalization enforcement (Release 1, 05 §B.1). The model called
    # `answerWithTable` — the turn's terminal exit — while intents it declared are
    # still `pending`, so the call is turned into a RETRYABLE refusal and the turn
    # continues. Registered here for the same reason as the two codes above: the
    # specific text (which intents are pending) rides on `denial_detail`, and
    # without a table entry `classify_denial`'s fallback would tell the model
    # "Something went wrong processing that request." — which is neither true nor
    # actionable, and it is the only thing left if the detail is ever absent.
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE: DenialInfo(
        code=FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
        retryable=True,
        # ENFORCEMENT: a finalization-ORDER gate. The turn's work may be perfect; what
        # was refused is ending the turn with the ledger unresolved.
        enforcement=True,
        user_message=(
            "You still have intents that are neither completed nor blocked, so this "
            "cannot be the final answer yet. Resolve each one with "
            "updateAnalysisState — mark it completed if a call answered it, or "
            "blocked if a call for it was refused or came back empty — then answer "
            "again."
        ),
    ),
    "PARSE_FAILED_CLOSED": DenialInfo(
        code="PARSE_FAILED_CLOSED",
        retryable=True,
        # SUBSTANTIVE: the SQL itself could not be parsed/validated. The verdict is on
        # the query the model wrote.
        enforcement=False,
        user_message=(
            "I couldn't validate that query safely — let me try explainQuery first."
        ),
    ),
    "DATABASE_NOT_ALLOWED": DenialInfo(
        code="DATABASE_NOT_ALLOWED",
        retryable=True,
        # SUBSTANTIVE: the query named a database it may not read.
        enforcement=False,
        user_message="That database isn't available. Let me check what's accessible.",
    ),
    "TABLE_NOT_FOUND": DenialInfo(
        code="TABLE_NOT_FOUND",
        retryable=True,
        # SUBSTANTIVE: the query named a table that does not exist — the canonical
        # "a query came back wrong and a later one fixed it" shape.
        enforcement=False,
        user_message="I couldn't find that table. Let me verify the table name.",
    ),
    # CARTESIAN_JOIN_FORBIDDEN: the clickhouse-api guardrail rejects a query that
    # cross-joins two physical base tables without a join condition. On the LIVE
    # turn the dispatcher surfaces the MCP's author-controlled message instead of
    # this string — that message NAMES the two offending base tables (catalog
    # metadata, not PII / cell values, D25) and tells the model to add ON/USING or
    # wrap a constant side in a subquery, so it can self-correct. This generic
    # string is the REPLAY fallback only: `user_message` is not persisted on
    # TrailEntry, so `context/budget.py::_render_entry` re-derives it from
    # `error_code` here, where the specific table names are no longer available.
    "CARTESIAN_JOIN_FORBIDDEN": DenialInfo(
        code="CARTESIAN_JOIN_FORBIDDEN",
        retryable=True,
        # SUBSTANTIVE: a guardrail on the SHAPE OF THE QUERY (a missing join
        # condition), not on the shape of the call. The model's SQL was wrong.
        enforcement=False,
        user_message=(
            "That query cross-joins two tables without a join condition. Add an ON "
            "or USING clause, or wrap a constant side in a subquery."
        ),
    ),
    "CLICKHOUSE_QUERY_ERROR": DenialInfo(
        code="CLICKHOUSE_QUERY_ERROR",
        retryable=True,
        # SUBSTANTIVE: the executor ran it and the warehouse rejected it.
        enforcement=False,
        user_message="That query didn't run correctly. Let me fix it and try again.",
    ),
    "CLICKHOUSE_UNAVAILABLE": DenialInfo(
        code="CLICKHOUSE_UNAVAILABLE",
        retryable=False,
        # SUBSTANTIVE: the system underneath the work failed. Nothing about the call
        # was refused, so counting it as friction is correct — it WAS friction.
        enforcement=False,
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
        # SUBSTANTIVE: the model named a table/column that does not exist — the
        # `TABLE_NOT_FOUND` shape, reached through the composite instead of the MCP.
        enforcement=False,
        user_message=(
            "That table or column isn't available. Check the exact name with "
            "getTableSchema and try again."
        ),
    ),
    "RESOLVE_VALUES_INTERNAL_ERROR": DenialInfo(
        code="RESOLVE_VALUES_INTERNAL_ERROR",
        retryable=False,
        # SUBSTANTIVE: a crash, not a refusal. The call was attempted and failed.
        enforcement=False,
        user_message="Something went wrong resolving those values. Please try again.",
    ),
    "RESOLVE_VALUES_UNAVAILABLE": DenialInfo(
        code="RESOLVE_VALUES_UNAVAILABLE",
        retryable=False,
        # SUBSTANTIVE: a capability outage, the `CLICKHOUSE_UNAVAILABLE` shape.
        enforcement=False,
        user_message="Value resolution is not available right now.",
    ),
    # Read-tools (searchBlueprints/getBlueprint/searchKnowledge, read-tools §6):
    # runtime tools that never come from the MCP — the tool/loop sets these
    # locally, and on replay `context/budget.py::_render_entry` re-derives a
    # PII-safe message from `error_code` via this table (the L5 precedent). A
    # SHARED `RETRIEVAL_TOOL_*` family (three tools, one failure vocabulary) with
    # the tool name riding in the span/message.
    "RETRIEVAL_TOOL_INVALID_ARGS": DenialInfo(
        code="RETRIEVAL_TOOL_INVALID_ARGS",
        retryable=True,
        # ENFORCEMENT: arg-SHAPE validation (`retrieval/tools.py::_require_text` /
        # `_clamp_k`) — a blank `query`, an out-of-range `k`. Refused on the envelope
        # before any search ran, the `ANALYSIS_STATE_INVALID` shape. Unreachable by
        # today's learning readers (no retrieval tool is a data tool); classified for
        # completeness so the split is the whole vocabulary, not the reachable part.
        enforcement=True,
        user_message="That search request was malformed. Check the arguments and try again.",
    ),
    "RETRIEVAL_TOOL_UNAVAILABLE": DenialInfo(
        code="RETRIEVAL_TOOL_UNAVAILABLE",
        retryable=False,
        # SUBSTANTIVE: a capability outage — the request was fine, the dependency was
        # not.
        enforcement=False,
        user_message="Blueprint and knowledge search is not available right now.",
    ),
    "RETRIEVAL_TOOL_INTERNAL_ERROR": DenialInfo(
        code="RETRIEVAL_TOOL_INTERNAL_ERROR",
        retryable=False,
        # SUBSTANTIVE: a crash that slipped every guard. Attempted, then failed.
        enforcement=False,
        user_message="Blueprint/knowledge search hit an internal error. Please try again.",
    ),
    # Registry-seam containment (read-tools §2 hardening): a runtime tool that
    # raised or returned a contract-violating result — set by the loop.
    "RUNTIME_TOOL_INTERNAL_ERROR": DenialInfo(
        code="RUNTIME_TOOL_INTERNAL_ERROR",
        retryable=False,
        # SUBSTANTIVE: the registry-seam containment code — a runtime tool raised or
        # returned a contract-violating result. A real failure of a real attempt, and
        # the conservative reading of "something went wrong and we do not know what".
        enforcement=False,
        user_message="That tool hit an internal error. Please try again.",
    ),
}

KNOWN_DENIAL_CODES = frozenset(_DENIAL_TABLE)

# The enforcement half of the table, DERIVED. The offline learning loop's
# `summary/loader.py::ENFORCEMENT_ERROR_CODES` is built from this (plus the one
# enforcement marker that is not a denial-table entry at all), so the set of codes that
# do not count as analyst friction is a projection of the registrations above rather
# than a second list kept in step by a test. Adding a code to `_DENIAL_TABLE` without
# thinking about `enforcement` now yields the pre-existing behaviour (substantive), and
# `tests/runtime/dispatch/test_denial_mapping.py::_EXPECTED_ENFORCEMENT` fails until the
# author states the classification out loud.
ENFORCEMENT_DENIAL_CODES = frozenset(
    code for code, info in _DENIAL_TABLE.items() if info.enforcement
)


def classify_denial(code: str | None) -> DenialInfo:
    """Classify a `ToolError` code into `{retryable, user_message}`.

        An unrecognized or missing (`None`) code — an unexpected/internal MCP error whose
        `[{CODE}]` prefix could not be parsed — is treated conservatively: not retryable,
        surfaced as a generic failure. This never raises, so the dispatcher can always
        classify whatever `MCPToolError.code` it receives.
    """
    if code is not None and code in _DENIAL_TABLE:
        return _DENIAL_TABLE[code]
    return DenialInfo(
        code=code or "UNKNOWN_ERROR",
        retryable=False,
        user_message="Something went wrong processing that request.",
    )
