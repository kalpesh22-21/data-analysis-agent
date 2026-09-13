"""Graceful-denial mapping — ToolError code -> {retryable-by-model, user-facing-message}.

A retryable code (`TABLE_NOT_FOUND`, `CLICKHOUSE_QUERY_ERROR`, …) is fed back as an
ordinary tool error so the model self-corrects; a non-retryable one (a scope violation,
a transient warehouse outage) surfaces a static, PII-safe user message instead. Either
way the call still increments the loop's iteration/token counters — a rejected call is
not free.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DenialKind(StrEnum):
    """What a denial says about the model's WORK — the three answers a refusal can give.

    H1 shipped this as a boolean (`enforcement`), which forced the third answer to
    borrow the second one's slot: a warehouse outage on a `runBlueprint` read as "the
    model's blueprint was judged and found wanting". The two offline readers want
    DIFFERENT things from an outage (see `learning/summary/loader.py`), and a boolean
    cannot give them different answers.
    """

    GATE = "gate"  # the runtime declined; no verdict was formed on the work
    WORK_JUDGED = "work_judged"  # the model's SQL/blueprint was judged — the DEFAULT
    INFRA_FAILED = "infra_failed"  # the system under the work failed; no verdict on the work


@dataclass(frozen=True)
class DenialInfo:
    """Classification of one `ToolError` code."""

    code: str
    retryable: bool
    user_message: str
    # What does this refusal say about the model's work?
    #
    # GATE         — a procedural/protocol refusal: the runtime refused because of the
    #                ORDER or the SHAPE of the call (read the blueprint before running
    #                it, resolve your intents before finalizing, put a table in your
    #                answer, send a well-formed ledger update), or declined to run at
    #                all (an unsupported blueprint, a user denying an approval gate).
    #                Nothing the model asked OF THE DATA was found wanting. Usually
    #                nothing ran either, but that is a common consequence, NOT the test:
    #                a mid-run `when … on_violation: abort` stops after real nodes have
    #                executed and is still a GATE, because no verdict was formed on the
    #                work. Release 1 made these routine: a healthy session trips them.
    # WORK_JUDGED  — the query/blueprint the model authored was itself rejected or came
    #                back wrong (bad SQL, out-of-scope columns, a table that does not
    #                exist). This is the DEFAULT and the conservative direction — an
    #                unclassifiable failure reads as a real failure of real work, which
    #                is what every code read as before this field existed.
    # INFRA_FAILED — the system UNDERNEATH the work fell over (warehouse outage, a
    #                dependency down, a crash inside a composite). The call was fine and
    #                the SQL may have been perfect; there is simply no verdict on the
    #                work, because nothing judged it.
    #
    # The GATE/WORK_JUDGED line is NOT "did the executor run": `COLUMN_SCOPE_VIOLATION`
    # is refused before ClickHouse is touched and is still WORK_JUDGED, because what was
    # refused is the model's query. It is "was the model's DATA WORK judged, or only its
    # call protocol". The WORK_JUDGED/INFRA_FAILED line is a different question: did
    # anything actually FORM a verdict about the work.
    #
    # It lives HERE, where codes are registered, because the offline learning loop needs
    # the split and had been keeping its own hand-written list of it
    # (`learning/summary/loader.py::ENFORCEMENT_ERROR_CODES`, which now DERIVES from this
    # field). A hand-kept list fails loudly on a rename and silently on an ADDITION: a
    # new gate code counted as substantive friction, putting a permanent negative bias on
    # every session that trips it. Registering a code and classifying it are now the same
    # edit. The default is `WORK_JUDGED` so the field can never be the reason an addition
    # is silent in the OTHER direction — an unclassified new code reads as a real
    # failure, exactly as it did before this field existed.
    kind: DenialKind = DenialKind.WORK_JUDGED

    @property
    def enforcement(self) -> bool:
        """Is this refusal about the CALL rather than about the WORK?

        DERIVED from `kind`, kept as a property so every existing reader of the H1
        boolean sees exactly the shape it always did. An outage is NOT enforcement — it
        never was, and widening this to include `INFRA_FAILED` would tell
        `_failed_fixed_pairs` that a warehouse outage cost the analyst nothing, which is
        false. `INFRA_FAILED` is read through `INFRA_FAILURE_CODES` instead, by the one
        reader that wants it.
        """
        return self.kind is DenialKind.GATE


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

# The exit-#2 code added by the ANSWER JUDGE slice (09 §G.2). It belongs to the SAME
# model-authored-text-crossing-turns class as the two above and for the same reason:
# it is persisted under `answerWithTable`, whose SUCCESSFUL entries must keep
# replaying, so `_is_stale_model_text_entry` can only drop it by matching this
# literal — and it carries the model's refused draft answer in `args`.
ANSWER_JUDGE_REJECTED_CODE = "ANSWER_JUDGE_REJECTED"


UNKNOWN_TOOL_CODE = "UNKNOWN_TOOL"
BLUEPRINT_NOT_SEARCHED_CODE = "BLUEPRINT_NOT_SEARCHED"
BLUEPRINT_NOT_SEARCHED_MESSAGE = (
    "Blueprint-first check: no blueprint has been searched or used this turn. "
    "Call searchBlueprints to look for an expert-authored blueprint first. If nothing "
    "fits or the corpus is unavailable, re-issue runQuery and it will proceed."
)

_DENIAL_TABLE: dict[str, DenialInfo] = {
    UNKNOWN_TOOL_CODE: DenialInfo(
        code=UNKNOWN_TOOL_CODE,
        retryable=True,
        kind=DenialKind.GATE,
        user_message="That tool is not available. Call only an advertised tool by its exact name; never use a tool-call ID as a tool name.",
    ),
    BLUEPRINT_NOT_SEARCHED_CODE: DenialInfo(
        code=BLUEPRINT_NOT_SEARCHED_CODE,
        retryable=True,
        kind=DenialKind.GATE,
        user_message=BLUEPRINT_NOT_SEARCHED_MESSAGE,
    ),
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
        # WORK_JUDGED: the model's query asked for columns this scope does not hold.
        # Refused before ClickHouse ran, and still a judgement ON THE QUERY.
        kind=DenialKind.WORK_JUDGED,
        user_message="This needs access to columns outside your current permissions.",
    ),
    "SCRATCH_SESSION_VIOLATION": DenialInfo(
        code="SCRATCH_SESSION_VIOLATION",
        retryable=False,
        # WORK_JUDGED: the query referenced scratch data belonging to another session —
        # again a fact about what the SQL named, not about call order.
        kind=DenialKind.WORK_JUDGED,
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
        # GATE: an answer-shape gate. The blueprint was never run, so there is no
        # blueprint OUTPUT to have been wrong — only a call made out of order.
        kind=DenialKind.GATE,
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
        # GATE: the purest answer-SHAPE refusal in the table — the model named no
        # table at all. Nothing was computed and nothing computed was wrong.
        kind=DenialKind.GATE,
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
        # GATE: a call-ORDER gate (getBlueprint before runBlueprint). The executor
        # never ran, so this is not evidence that the blueprint is wrong — which is
        # exactly what the learning loop would otherwise record.
        kind=DenialKind.GATE,
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
        # GATE: bookkeeping hygiene. A mis-shaped ledger update says nothing about
        # the data — `updateAnalysisState` computes nothing in the first place.
        kind=DenialKind.GATE,
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
        # GATE: the same ledger, refused on TIMING rather than shape. Not retryable
        # and still not a data failure — `retryable` and `kind` are independent.
        kind=DenialKind.GATE,
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
        # GATE: a finalization-ORDER gate. The turn's work may be perfect; what was
        # refused is ending the turn with the ledger unresolved.
        kind=DenialKind.GATE,
        user_message=(
            "You still have intents that are neither completed nor blocked, so this "
            "cannot be the final answer yet. Resolve each one with "
            "updateAnalysisState — mark it completed if a call answered it, or "
            "blocked if a call for it was refused or came back empty — then answer "
            "again."
        ),
    ),
    ANSWER_JUDGE_REJECTED_CODE: DenialInfo(
        code=ANSWER_JUDGE_REJECTED_CODE,
        retryable=True,
        # GATE for the same reason as the pending-intents refusal above: the turn's
        # WORK may be perfect. What was refused is finishing with this answer.
        kind=DenialKind.GATE,
        # THE REPLAY FALLBACK ONLY. The live refusal always carries `denial_detail`
        # with the judge's own sentence about THIS answer, which is what
        # `_render_entry` reads; this generic string appears only if that detail is
        # ever absent. It deliberately says nothing specific — a fabricated
        # specificity would send the model to fix something the judge never said.
        user_message=(
            "That answer was reviewed against this turn and sent back. Re-read the "
            "question and the results you have, then answer again."
        ),
    ),
    "PARSE_FAILED_CLOSED": DenialInfo(
        code="PARSE_FAILED_CLOSED",
        retryable=True,
        # WORK_JUDGED: the SQL itself could not be parsed/validated. The verdict is on
        # the query the model wrote.
        kind=DenialKind.WORK_JUDGED,
        user_message=("I couldn't validate that query safely — let me try explainQuery first."),
    ),
    "DATABASE_NOT_ALLOWED": DenialInfo(
        code="DATABASE_NOT_ALLOWED",
        retryable=True,
        # WORK_JUDGED: the query named a database it may not read.
        kind=DenialKind.WORK_JUDGED,
        user_message="That database isn't available. Let me check what's accessible.",
    ),
    "TABLE_NOT_FOUND": DenialInfo(
        code="TABLE_NOT_FOUND",
        retryable=True,
        # WORK_JUDGED: the query named a table that does not exist — the canonical
        # "a query came back wrong and a later one fixed it" shape.
        kind=DenialKind.WORK_JUDGED,
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
        # WORK_JUDGED: a guardrail on the SHAPE OF THE QUERY (a missing join
        # condition), not on the shape of the call. The model's SQL was wrong.
        kind=DenialKind.WORK_JUDGED,
        user_message=(
            "That query cross-joins two tables without a join condition. Add an ON "
            "or USING clause, or wrap a constant side in a subquery."
        ),
    ),
    "CLICKHOUSE_QUERY_ERROR": DenialInfo(
        code="CLICKHOUSE_QUERY_ERROR",
        retryable=True,
        # WORK_JUDGED: the executor ran it and the warehouse rejected it.
        kind=DenialKind.WORK_JUDGED,
        user_message="That query didn't run correctly. Let me fix it and try again.",
    ),
    "CLICKHOUSE_UNAVAILABLE": DenialInfo(
        code="CLICKHOUSE_UNAVAILABLE",
        retryable=False,
        # INFRA_FAILED: the warehouse fell over. NO VERDICT ON THE WORK — the SQL may
        # have been perfect and nothing ever ran it, so a `runBlueprint` that hits this
        # must not be recorded as a blueprint that produced a wrong answer. It is still
        # friction the analyst lived through, which is why the two offline readers split
        # here rather than sharing one flag (`learning/summary/loader.py`).
        kind=DenialKind.INFRA_FAILED,
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
        # WORK_JUDGED: the model named a table/column that does not exist — the
        # `TABLE_NOT_FOUND` shape, reached through the composite instead of the MCP.
        kind=DenialKind.WORK_JUDGED,
        user_message=(
            "That table or column isn't available. Check the exact name with "
            "getTableSchema and try again."
        ),
    ),
    "RESOLVE_VALUES_INTERNAL_ERROR": DenialInfo(
        code="RESOLVE_VALUES_INTERNAL_ERROR",
        retryable=False,
        # INFRA_FAILED: a crash inside the composite, not a refusal. Nothing ever
        # judged what the model asked for, so there is no verdict on the work.
        kind=DenialKind.INFRA_FAILED,
        user_message="Something went wrong resolving those values. Please try again.",
    ),
    "RESOLVE_VALUES_UNAVAILABLE": DenialInfo(
        code="RESOLVE_VALUES_UNAVAILABLE",
        retryable=False,
        # INFRA_FAILED: a capability outage, the `CLICKHOUSE_UNAVAILABLE` shape — the
        # request was fine, the dependency was down, and no verdict on the work exists.
        kind=DenialKind.INFRA_FAILED,
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
        # GATE: arg-SHAPE validation (`retrieval/tools.py::_require_text` /
        # `_clamp_k`) — a blank `query`, an out-of-range `k`. Refused on the envelope
        # before any search ran, the `ANALYSIS_STATE_INVALID` shape. Unreachable by
        # today's learning readers (no retrieval tool is a data tool); classified for
        # completeness so the split is the whole vocabulary, not the reachable part.
        kind=DenialKind.GATE,
        user_message="That search request was malformed. Check the arguments and try again.",
    ),
    "RETRIEVAL_TOOL_UNAVAILABLE": DenialInfo(
        code="RETRIEVAL_TOOL_UNAVAILABLE",
        retryable=False,
        # INFRA_FAILED: a capability outage — the request was fine, the dependency
        # was not, and nothing formed a verdict on the work.
        kind=DenialKind.INFRA_FAILED,
        user_message="Blueprint and knowledge search is not available right now.",
    ),
    "RETRIEVAL_TOOL_INTERNAL_ERROR": DenialInfo(
        code="RETRIEVAL_TOOL_INTERNAL_ERROR",
        retryable=False,
        # INFRA_FAILED: a crash that slipped every guard. The system under the call
        # broke; nothing judged what was asked for.
        kind=DenialKind.INFRA_FAILED,
        user_message="Blueprint/knowledge search hit an internal error. Please try again.",
    ),
    # Registry-seam containment (read-tools §2 hardening): a runtime tool that
    # raised or returned a contract-violating result — set by the loop.
    "RUNTIME_TOOL_INTERNAL_ERROR": DenialInfo(
        code="RUNTIME_TOOL_INTERNAL_ERROR",
        retryable=False,
        # INFRA_FAILED: the registry-seam containment code — a runtime tool raised or
        # returned a contract-violating result. The TOOL broke, which is a fact about
        # the runtime and not a judgement of the model's data work.
        kind=DenialKind.INFRA_FAILED,
        user_message="That tool hit an internal error. Please try again.",
    ),
    # runBlueprint executor family (§5.4) — `blueprint/executor.py`, which sets each of
    # these on an `ExecFailed`. Registered LATE (H8 patch 2): they land on real
    # `runBlueprint` trail entries and were never in this table, so they took the
    # unregistered path in BOTH readers of it — the model saw the generic "Something went
    # wrong processing that request." on every replay, and the learning loop's
    # `classify_denial` fallback filed all five as `WORK_JUDGED`, recording a
    # user-declined approval as a blueprint that produced a wrong answer.
    #
    # The codes and the messages are SPELLED here rather than imported. `executor.py`
    # imports `dispatch/tool_dispatcher.py`, which imports this module, so importing the
    # constants back would close a cycle — the same constraint `loader.py` works under
    # for its non-table markers. Each entry names its definition site instead, and the
    # `user_message` is byte-identical to the executor's live one so replay says exactly
    # what the model was told on the turn itself.
    #
    # Retryable flags MATCH the live `ExecFailed(..., retryable=…)` at each site, for the
    # reason the D77 composite codes do: a replay that contradicts the live semantics
    # teaches the model the opposite lesson from the one it learned.
    #
    # `RUN_BLUEPRINT_NOT_FOUND` (`executor.py::NOT_FOUND_CODE`, raised at the scope
    # filter and the load): the model named a blueprint id that is not in its scope.
    "RUN_BLUEPRINT_NOT_FOUND": DenialInfo(
        code="RUN_BLUEPRINT_NOT_FOUND",
        retryable=True,
        # WORK_JUDGED: the model named a blueprint that does not exist for it — the
        # `TABLE_NOT_FOUND` shape one layer up. A verdict on what the model asked for,
        # and the search that follows is a genuine repair of a genuine mistake.
        kind=DenialKind.WORK_JUDGED,
        user_message="That blueprint is not available. Search for one with searchBlueprints.",
    ),
    # `RUN_BLUEPRINT_SLOT_INVALID` (`executor.py::SLOT_INVALID_CODE`): a slot value the
    # model supplied could not be bound — wrong type, failed a rule, resolved to nothing.
    "RUN_BLUEPRINT_SLOT_INVALID": DenialInfo(
        code="RUN_BLUEPRINT_SLOT_INVALID",
        retryable=True,
        # WORK_JUDGED: the ARGUMENTS the model chose were judged and rejected. This is
        # not envelope shape (that would be a gate) — the call was well-formed and the
        # value inside it was wrong, which is the same class as bad SQL.
        kind=DenialKind.WORK_JUDGED,
        user_message="A value for this blueprint could not be used. Please rephrase or retry.",
    ),
    # `RUN_BLUEPRINT_UNSUPPORTED` (`executor.py::UNSUPPORTED_CODE`): the fast path
    # declined — an unsupported node type, a shape the compiler will not emit, a rule it
    # cannot honour. NOT retryable at almost every site (the model must fall back to the
    # raw tools; one passthrough site sets True), so the table takes the dominant and
    # conservative reading: re-issuing the same call cannot help.
    "RUN_BLUEPRINT_UNSUPPORTED": DenialInfo(
        code="RUN_BLUEPRINT_UNSUPPORTED",
        retryable=False,
        # GATE: a CAPABILITY refusal. The executor declined before forming any opinion
        # about the blueprint — it is not that the blueprint is wrong, it is that this
        # path will not run it. Nothing was computed, so there is no output to judge.
        kind=DenialKind.GATE,
        user_message=(
            "This blueprint can't run on the fast path yet — answer it with the raw "
            "tools (getTableSchema / runQuery)."
        ),
    ),
    # `RUN_BLUEPRINT_VERIFY_FAILED` (`executor.py::VERIFY_FAILED_CODE`): the fast path
    # RAN and the D56 verification of its final node rejected the result, so it was never
    # returned. Retryable — the raw loop can still answer the question.
    "RUN_BLUEPRINT_VERIFY_FAILED": DenialInfo(
        code="RUN_BLUEPRINT_VERIFY_FAILED",
        retryable=True,
        # WORK_JUDGED: the ONLY code in this family where the blueprint's own output was
        # examined and found wanting. This is precisely what `outcome="corrected"` is
        # supposed to mean, and it is why the other four had to be classified rather than
        # left to share the fallback with it.
        kind=DenialKind.WORK_JUDGED,
        user_message=(
            "The fast path produced a result that failed verification, so it was not "
            "returned. Answer this from the raw tools (getTableSchema / runQuery) instead."
        ),
    ),
    # `RUN_BLUEPRINT_ABORTED` (`executor.py::ABORTED_CODE`): a `when … on_violation:
    # abort` fired, an approval gate was DENIED by the user, or every node was
    # skipped/gated leaving nothing terminal. Retryable, matching the live sites.
    "RUN_BLUEPRINT_ABORTED": DenialInfo(
        code="RUN_BLUEPRINT_ABORTED",
        retryable=True,
        # GATE: the run stopped cleanly BEFORE producing an answer, most often because a
        # human said no at an approval gate. Recording that as a blueprint that produced
        # a wrong answer would punish the corpus for a user exercising the consent seam
        # the corpus itself asked for.
        kind=DenialKind.GATE,
        user_message=(
            "The fast path stopped before producing an answer — answer this from the "
            "raw tools (getTableSchema / runQuery) instead."
        ),
    ),
    # `INTERNAL_TRANSPORT_ERROR` (`dispatch/tool_dispatcher.py::
    # INTERNAL_TRANSPORT_ERROR_CODE`, the B4 raw-exception path): a connection refusal, a
    # timeout, a malformed response — no `[{CODE}]` prefix, so the dispatcher never looks
    # it up here on the LIVE turn and stamps the marker directly. It is registered anyway
    # because the trail keeps it and every later REPLAY does come through this table.
    # Spelled, not imported: `tool_dispatcher.py` imports this module.
    "INTERNAL_TRANSPORT_ERROR": DenialInfo(
        code="INTERNAL_TRANSPORT_ERROR",
        retryable=False,
        # INFRA_FAILED: the wire broke. `loader.py::INFRA_ERROR_CODES` already unioned
        # this literal in by hand for exactly this reason; registering it makes that
        # union redundant rather than load-bearing, which is the direction H1 pushed
        # every one of these lists.
        kind=DenialKind.INFRA_FAILED,
        user_message="Something went wrong reaching the data warehouse. Please try again.",
    ),
}

KNOWN_DENIAL_CODES = frozenset(_DENIAL_TABLE)

# The enforcement half of the table, DERIVED. The offline learning loop's
# `summary/loader.py::ENFORCEMENT_ERROR_CODES` is built from this (plus the one
# enforcement marker that is not a denial-table entry at all), so the set of codes that
# do not count as analyst friction is a projection of the registrations above rather
# than a second list kept in step by a test. Adding a code to `_DENIAL_TABLE` without
# thinking about `kind` now yields the pre-existing behaviour (`WORK_JUDGED`), and
# `tests/runtime/dispatch/test_denial_mapping.py::_EXPECTED_KIND` fails until the author
# states the classification out loud.
ENFORCEMENT_DENIAL_CODES = frozenset(
    code for code, info in _DENIAL_TABLE.items() if info.enforcement
)

# The INFRA_FAILED third of the table, DERIVED on the same terms. Read by
# `learning/summary/loader.py::INFRA_ERROR_CODES` (plus the one transport marker that is
# not a denial-table entry at all) so an outage stops being recorded as a blueprint that
# produced a wrong answer, while STILL counting as the analyst friction it was.
INFRA_FAILURE_CODES = frozenset(
    code for code, info in _DENIAL_TABLE.items() if info.kind is DenialKind.INFRA_FAILED
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
