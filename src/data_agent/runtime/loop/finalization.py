"""Finalization enforcement's DECISION layer — the refusals, the nudges, the answer-shape
counter and the per-round block allowance (Release 1). This is where `analysisState` gets
teeth.

THE INVARIANT IS SCOPED: no intent ends `pending` on any turn that reaches a TERMINAL
outcome (`done` / `stopped_hard_ceiling`). A turn abandoned at an askUser, budget-cap or
blueprint pause, or whose resume loses a CAS race, is a NON-TERMINATED turn and
legitimately leaves its intents `pending` — an unscoped assertion fails against any real
store.

ENFORCEMENT APPLIES ONLY TO THE LIVE STATE. Everything here reads the window-local the
loop loaded through `live_analysis_state` and hands in as an argument, so a state left
behind by an abandoned earlier turn cannot refuse an unrelated later turn.

This owns the STATE and the DECISION; the loop keeps the EFFECTS and the CONTROL FLOW —
the two-gate `if pending / elif shape` precedence, the `refused_finalization` local, the
draft clears, the nudge's one-round-trip set/splice/clear cycle, the `tool_result`
rewrites, `_force_block_pending_intents` and both budget-exhaustion branches. Only the two
BLOCK-CLAIM events move with the claim; `loop_finalization_refused`,
`loop_enforcement_exhausted` and the two answer-shape events stay at their emit sites in
the loop, interleaved with body-owned flag writes.

So this is NOT the stdlib leaf `read_guard.py` is, and it does not need to be — nothing
outside the loop package imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from data_agent.runtime.composite.answer_with_table import TOOL_NAME as ANSWER_TABLE_TOOL_NAME
from data_agent.runtime.dispatch.denial_mapping import (
    ANSWER_JUDGE_REJECTED_CODE,
    ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolObserver, ToolResult
from data_agent.runtime.sanitize import MAX_FIELD_CHARS, sanitize_text
from data_agent.runtime.session.models import (
    AnalysisState,
    FinalizationBlockKind,
    ResultPreview,
    TrackedIntent,
)
from data_agent.runtime.session.store import SessionStore

__all__ = [
    "ANSWER_SHAPE_EXHAUSTED_EVENT",
    "ANSWER_SHAPE_REFUSED_EVENT",
    "DATA_ANSWER_TOOLS",
    "EMPTY_ANSWER_EXHAUSTED_EVENT",
    "EMPTY_ANSWER_FALLBACK_TEXT",
    "EMPTY_ANSWER_REFUSED_EVENT",
    "MAX_NUDGE_DRAFT_CHARS",
    "AnswerShapeCounter",
    "FinalizationGate",
    "answer_judge_rejected",
    "answer_shape_nudge_text",
    "answer_table_no_table_designated",
    "empty_answer_nudge_text",
    "finalization_blocked",
    "finalization_nudge_text",
    "pending_intents",
    "refreshed_analysis_state",
]

_logger = logging.getLogger(__name__)

# How much of the model's refused draft answer is quoted back to it in the nudge.
# Generous: the point is that the model does not have to REGENERATE the answer it
# just wrote (exit #1 persists nothing and D22 discards free text around tool
# calls), so a truncated quote costs a rewrite of the tail only.
MAX_NUDGE_DRAFT_CHARS = 2000

# The tools whose SUCCESSFUL result IS an answer's rows, for the ANSWER-SHAPE gate
# (05 §J). Deliberately just two: `sampleRows`, `getTableSchema` and the listings
# are DISCOVERY — a model that peeks at ten sample rows and then answers a single
# figure in prose is behaving correctly, and counting those would refuse it.
#
# PUBLIC because the ANSWER JUDGE selects the same set (09 §D.2): the results it is
# shown are the ones the answer was written FROM, and discovery reads ground the
# MODEL rather than the answer. Two definitions of "a data-bearing result" would let
# the gate and the judge disagree about which turns hold one, which is the difference
# between a judge that can check a figure and one that cannot see it.
DATA_ANSWER_TOOLS = frozenset({"runBlueprint", "runQuery"})
_DATA_ANSWER_TOOLS = DATA_ANSWER_TOOLS

# The answer-shape gate's two events, NAMED because the `loop_` prefix is
# load-bearing rather than a convention: `observability/tracing.py::
# guardrail_observer` drops every event that lacks it, SILENTLY, so a misnamed
# event fires perfectly in every raw-recorder unit test and reaches production
# telemetry never (06, and the `loop_analysis_state_auto_bound` near-miss that
# shipped that way for a review round). Exported so the span test can assert the
# real observer's output against the same symbol the emit site uses.
ANSWER_SHAPE_REFUSED_EVENT = "loop_answer_shape_refused"
ANSWER_SHAPE_EXHAUSTED_EVENT = "loop_answer_shape_exhausted"

# The EMPTY-ANSWER gate's two events (05 §K), named under the same `loop_` rule as
# the pair above — and for this gate the naming matters more than for any other,
# because a silent finish produces NO other artifact: no persisted assistant
# message, no tool call, no denial. Before these events existed the failure was
# invisible end to end — the trace showed a `Response` span and a `loop_turn_done`,
# and the only witness was a user looking at a blank bubble.
EMPTY_ANSWER_REFUSED_EVENT = "loop_empty_answer_refused"
EMPTY_ANSWER_EXHAUSTED_EVENT = "loop_empty_answer_exhausted"

# What the user is shown when the model finishes silently TWICE — the gate refused
# once, the re-round came back empty as well, and the turn has to end.
#
# THE ALTERNATIVE WAS THE BLANK BUBBLE, which is what shipped: `assistant_text=None`
# rode the `result` event out and the UI rendered `text || ""` beside `status: done`,
# so a total failure was indistinguishable from an answer that had not loaded. This
# says the true thing instead, and says it in the agent's voice because that is the
# channel the user is reading.
#
# IT IS PERSISTED, unlike the empty string it replaces (see the exit-#1 `persist_text`
# comment): a live answer that `/session/history` does not have is the divergence the
# whole scrub-before-persist discipline exists to prevent, and "the turn is simply
# missing from history" is the worst version of it — tomorrow the session reads as
# though the user was never answered at all, with nothing to say why.
#
# NO IDENTIFIER SHAPES IN IT, deliberately: it goes through `scrub_answer_prose` like
# every other answer, and a marker-mangled apology would be a second defect wearing
# the first one's clothes.
EMPTY_ANSWER_FALLBACK_TEXT = (
    "I was not able to produce an answer for that — my last two attempts came back "
    "with nothing at all. Please send the question again; if it covered several "
    "things at once, asking for one of them at a time is more likely to get through."
)


def _is_multi_row_answer_call(
    tool_name: str, status: str, preview: ResultPreview | None
) -> bool:
    """Whether one call is a SUCCESSFUL, data-returning call that produced MORE THAN ONE
        ROW — the fact the answer-shape gate counts.

        `row_count > 1`, not `>= 1`, and the strictness is the whole safety margin: ZERO rows
        is a legitimate prose answer ("no employees match"), and ONE row is a single figure
        ("headcount is 412"). Refusing either would turn a correct turn into an extra
        round-trip and a confusing instruction to table something that is not a table.

        Module-private: `AnswerShapeCounter` holds BOTH call sites (the trail seed and the
        live count), so the two can no longer disagree about what is being counted.
    """
    return (
        tool_name in _DATA_ANSWER_TOOLS
        and status == "ok"
        and preview is not None
        and preview.row_count > 1
    )


def pending_intents(state: AnalysisState | None) -> tuple[TrackedIntent, ...]:
    """Every intent of the LIVE state still `pending`, in declaration order.

        `None` state -> empty tuple, which is the whole fast path: most turns are
        single-intent, so the enforcement check must cost one `is None` test on a local and
        never a store read.
    """
    if state is None:
        return ()
    return tuple(intent for intent in state.intents if intent.status == "pending")


def _describe_pending(pending: Sequence[TrackedIntent]) -> str:
    """`i2 ('attrition by department')` for each pending intent.

        The descriptions are MODEL-authored text re-entering model context, so they go through
        the SAME structural sanitisation the rendered state block uses (`runtime/sanitize.py`)
        — a newline in one could otherwise fabricate an instruction line inside the message it
        lands in. Same turn and same `column_scope` as the state it quotes, so there is no D44
        exposure AT THE POINT OF USE.

        It CAN outlive the turn. On the exit-#2 path this text rides `denial_detail` on a
        PERSISTED `answerWithTable` entry, and `frozenset()` provenance passes
        `is_entry_in_scope` under any scope forever. What actually bounds its lifetime is
        `context/assembly.py::_is_stale_model_text_entry`, which matches on the error code.
    """
    return "; ".join(
        f"{intent.intent_id} ('{sanitize_text(intent.description, MAX_FIELD_CHARS)}')"
        for intent in pending
    )


def finalization_blocked(pending: Sequence[TrackedIntent]) -> ToolResult:
    """The refusal returned in place of a terminal `answerWithTable` while intents are
        still pending.

        Returned BEFORE the trail entry is written, so the persisted entry IS the refusal and
        the model reads it on the next round-trip.

        IN-TURN VISIBILITY COMES FROM THE STATUS GATE, NOT FROM THE PROVENANCE:
        `filter_trail`'s current-turn exemption keeps a `status != "ok"` entry of the CURRENT
        turn whatever its provenance, and that is the only place this entry has to survive.

        `provenance=frozenset()` IS STILL THE RIGHT VALUE, for a different reason: the refusal
        is runtime-authored and reads no warehouse data, and `_compute_turn_provenance_union`
        is fail-closed — a `None` here would collapse the turn's union, tag the turn's own
        final assistant message undetermined, and drop the user's answer from every later
        replay.

        WHAT BOUNDS ITS LIFETIME is `_is_stale_model_text_entry`, which drops this entry from
        any turn other than its own by matching `FINALIZATION_BLOCKED_PENDING_INTENTS_CODE`
        (it is persisted under `answerWithTable`, whose SUCCESSFUL entries must keep replaying,
        so it cannot be matched by tool name). Without that drop, dead intent ids plus the
        refused draft prose in `args` would replay in every later turn under any
        since-narrowed scope.

        `denial_detail` NAMES THE PENDING INTENTS because `context/budget.py::_render_entry`
        builds the model-facing text as `entry.denial_detail or
        classify_denial(entry.error_code).user_message` and NEVER from
        `ToolResult.user_message`, which has no `TrailEntry` field at all.
    """
    detail = (
        f"You cannot finish yet: {len(pending)} intent(s) you are tracking are still "
        f"pending — {_describe_pending(pending)}. Resolve each one with "
        "updateAnalysisState — completed, citing the call that answered it, or "
        "blocked, citing the call that shows it cannot be done — then send this "
        "answer again."
    )
    return ToolResult(
        status="error",
        tool_name=ANSWER_TABLE_TOOL_NAME,
        error_code=FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
        retryable=True,
        user_message=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        denial_detail=detail,
    )


def answer_table_no_table_designated() -> ToolResult:
    """The nudge for an `answerWithTable` that designated NOTHING — no `tables` entry
        carrying a designation and no legacy pair to fold — on a turn that is holding
        multi-row results it has not tabled.

        `{answer: <prose>, tables: []}` otherwise succeeds, carries non-blank prose, and
        TERMINATES the turn through exit #2, which the answer-shape gate does not watch (that
        gate lives on exit #1, a model turn with no tool calls): the turn returns `done` with
        no table, no event and no log line. `tables` being REQUIRED makes that likelier rather
        than rarer — a model that cannot omit a declared key emits `tables: []`.

        BOUNDED BY THE SHAPE GATE'S OWN ALLOWANCE (`kind="answer_shape"`): this is the same
        complaint the shape gate makes, arriving through the other exit, so the two must share
        one bound or a model could be refused twice per window for one mistake. When the grant
        is spent the prose PASSES and the turn ends — the runtime never hard-locks a turn.

        SCOPED TO `multi_row_answer_calls > 0`: a turn holding no multi-row result has nothing
        to table, and a zero-row "none found" answered in prose is CORRECT. Nudging it would
        charge a right answer an extra round-trip.

        AND TO A NON-BLANK `answer`, mirroring the terminal condition exactly. A call that
        would not have ended the turn is not a finalization and must not be refused as one;
        refusing a blank-`answer` call would spend the window's allowance on a habit call and
        leave the real prose finish unrefusable. That half is covered at the other end, by
        `answer_table_succeeded` being set from substance rather than from the call.

        Mirrors `_answer_table_blueprint_not_run` in every mechanical respect: non-`ok` so the
        terminal exit does not fire and the status-gated current-turn exemption keeps it
        visible this same turn; `denial_detail` because that is the channel `_render_entry`
        actually reads; registered in `dispatch/denial_mapping.py` because `classify_denial`
        otherwise degrades to a generic message.
    """
    detail = (
        "Your answerWithTable named no table, so there is nothing for the user to "
        "look at. Every table goes in `tables`, one entry per part of your answer: "
        "`tables: [{sql: \"SELECT …\"}]` for a query you wrote, or "
        "`tables: [{blueprint_id: \"bp-…\"}]` for a blueprint you ran this turn. "
        "Send your answer again with the table in it."
    )
    return ToolResult(
        status="error",
        tool_name=ANSWER_TABLE_TOOL_NAME,
        error_code=ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
        retryable=True,
        user_message=detail,
        # Determined-EMPTY, like every other runtime-authored refusal here: this
        # reads no warehouse data, and `_compute_turn_provenance_union` is
        # fail-closed, so `None` would collapse the turn's union and drop the
        # user's own answer from every later replay.
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        denial_detail=detail,
    )


def answer_judge_rejected(feedback: str) -> ToolResult:
    """The ANSWER JUDGE's refusal at exit #2 (09 §G.2).

        MECHANICALLY IDENTICAL to `finalization_blocked` and `answer_table_no_table_
        designated`, and every one of those mechanics is load-bearing: non-`ok` so the
        terminal exit does not fire and `filter_trail`'s status-gated current-turn exemption
        keeps it visible this same turn; `denial_detail` because that is the only channel
        `context/budget.py::_render_entry` reads (`ToolResult.user_message` has no
        `TrailEntry` field at all); `frozenset()` provenance for the fail-closed-union
        reason above; and registered in `dispatch/denial_mapping.py` +
        `context/assembly.py::_STALE_CROSS_TURN_ERROR_CODES` so `classify_denial` does not
        degrade and the text does not replay into a later turn under a narrowed scope.

        NO DRAFT ECHO, unlike every exit-#1 nudge. Exit #2 preserves the model's prose for
        free — it is in `TrailEntry.args` and `_render_entry` replays it — so quoting it
        back would be a second copy in the same request.

        *feedback* IS ALREADY SANITISED by `answer_judge.py::parse_verdict`, which is where
        that has to happen: this text was composed by a model that had just read tool
        results, and a newline in it could otherwise forge a structural line in the message
        it lands in. Nothing here re-sanitises, because a second, differently-tuned pass is
        how the two silently diverge.
    """
    detail = (
        f"That answer was reviewed against this turn and sent back. {feedback} "
        "Send your answer again with that fixed, the rest of it unchanged — including "
        "the tables you designated, which are still what the user needs to see."
    )
    return ToolResult(
        status="error",
        tool_name=ANSWER_TABLE_TOOL_NAME,
        error_code=ANSWER_JUDGE_REJECTED_CODE,
        retryable=True,
        user_message=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        denial_detail=detail,
    )


def finalization_nudge_text(draft: str | None, pending: Sequence[TrackedIntent]) -> str:
    """The ephemeral `user`-role message injected in place of exit #1's missing error
        channel.

        IT CARRIES THE DRAFT BACK. Exit #2's refusal preserves the model's prose for free — it
        lives in `TrailEntry.args` and is replayed by `_render_entry` — while exit #1 preserves
        NOTHING: the answer is not persisted (by design; a persisted draft would surface in
        `/session/history` as something the user said) and D22 discards free text around tool
        calls, so without this quote the model must regenerate its answer blind.

        The `_text` suffix is not decoration: the loop's own window-local for the built string
        is `finalization_nudge`, and a builder of the same name would be shadowed by it inside
        `_run_loop_body`.
    """
    lines: list[str] = []
    if draft and draft.strip():
        lines.append(f"You drafted: {draft.strip()[:MAX_NUDGE_DRAFT_CHARS]}")
        lines.append("")
    lines.append(
        f"That is not your final answer yet — {len(pending)} intent(s) you are "
        f"tracking are still pending: {_describe_pending(pending)}."
    )
    lines.append(
        "Resolve each one with updateAnalysisState — completed, citing the call that "
        "answered it, or blocked, citing the call that shows it cannot be done — then "
        "re-send your final answer."
    )
    return "\n".join(lines)


def answer_shape_nudge_text(draft: str | None, multi_row_calls: int) -> str:
    """The ephemeral `user`-role message injected when a turn tries to finish in bare prose
        while holding multi-row results it never tabled.

        IT CORRECTS A BELIEF ABOUT TURN MECHANICS, which is why a prompt rule alone was not
        enough: live, the model apologised for being unable to call the tool any more —
        nothing had refused it and nothing had ended, but it believed the turn was over. So
        the first line this message has to say is that it is not, and that the tool is still
        there.

        IT CARRIES THE DRAFT BACK for the same reason `finalization_nudge_text` does, and here
        it is doubly load-bearing because the escape hatch asks the model to send that answer
        again. WHICH IS WHY THE TRUNCATION IS MARKED: this echo is the model's ONLY surviving
        copy of what it wrote, and an unmarked cut plus an instruction to re-send "unchanged"
        loses the tail silently.

        THE ESCAPE HATCH IS NOT DECORATION. The gate reads row counts, not meaning: a turn can
        legitimately run a multi-row query and answer a single figure from it. Offering the
        re-send keeps that turn correct at a cost of one round-trip, instead of forcing a
        table nobody asked for.
    """
    lines: list[str] = []
    if draft and draft.strip():
        stripped = draft.strip()
        echo = stripped[:MAX_NUDGE_DRAFT_CHARS]
        if len(echo) < len(stripped):
            echo += " …[truncated]"
        lines.append(f"You drafted: {echo}")
        lines.append("")
    lines.append(
        f"That answer is not finished. This turn produced {multi_row_calls} multi-row "
        "result(s), and a multi-row answer must be delivered through answerWithTable."
    )
    lines.append(
        "The turn is NOT over and answerWithTable is still available to you: you can "
        "and must call it in your NEXT response."
    )
    lines.append(
        "Pass one table per part you answered — blueprint_id for a result a blueprint "
        "produced, sql otherwise — and keep the prose you just wrote as the answer."
    )
    lines.append(
        "If your answer really is a single figure or an empty result, re-send your "
        "full answer with no tool call and it will be accepted."
    )
    return "\n".join(lines)


def empty_answer_nudge_text(incomplete_reason: str | None = None) -> str:
    """The ephemeral `user`-role message injected when a round-trip ends with NO prose and
        NO tool calls — the model said nothing at all.

        NO DRAFT IS ECHOED, which is the one structural difference from the other two
        nudges: there is nothing to echo. That also removes the "re-send what you wrote"
        escape hatch they rely on, so this message has to be self-sufficient — it states the
        one fact the model needs (its last response was empty and the user saw nothing) and
        names the two acceptable shapes of a next response, rather than asking it to repair
        something it cannot see.

        IT DOES NOT SAY "TRY HARDER". A silent response usually means the model believes it
        has already answered — the same class of belief about turn mechanics the answer-shape
        nudge corrects — so the correction has to be about the CHANNEL: the words did not
        arrive, and text is the only way they can.

        *incomplete_reason* is `ModelTurnResult.incomplete_reason`, and it changes the ask
        rather than decorating it: a completion cut off at the token cap does not need to be
        told it was silent, it needs to be told to be SHORTER. `None` (an ordinary
        completion that carried no words) gets the plain form.
    """
    lines: list[str] = [
        "Your last response was EMPTY — no text and no tool call. The user saw a blank "
        "answer, so nothing you may have intended to say reached them.",
    ]
    if incomplete_reason:
        lines.append(
            f"The provider reported that response as incomplete ({incomplete_reason}), "
            "which usually means the answer was cut off before it began. Keep this one "
            "SHORT: lead with the finding in a sentence or two."
        )
    lines.append(
        "The turn is NOT over and every tool is still available to you. Your next "
        "response must be one of exactly two things: the final answer as TEXT, or a "
        "tool call that gets you closer to it."
    )
    lines.append(
        "If you cannot answer the question, say so in text and say what blocked you — "
        "that is a valid, complete answer. Silence is not."
    )
    return "\n".join(lines)


def refreshed_analysis_state(
    tool_result: ToolResult, turn_index: int
) -> AnalysisState | None:
    """The state a SUCCESSFUL `updateAnalysisState` call just wrote, read back off its own
        result, or `None` when there is nothing to refresh from.

        The state changes mid-turn, so a once-per-window read would be wrong — but a store read
        at each terminal exit would cost a round-trip on EVERY turn, including the
        single-intent ones that never touch this feature. So the loop loads the state ONCE at
        the top and refreshes the local from each state call's result; state calls are
        dispatched before anything else in the batch, so the local is current by the time
        either exit is reached.

        Defensive: a malformed result degrades to "no refresh" (the loaded value stands)
        rather than raising into the dispatch loop.
    """
    if tool_result.status != "ok" or not isinstance(tool_result.result_full, dict):
        return None
    try:
        state = AnalysisState.from_doc(tool_result.result_full)
    except (KeyError, TypeError, ValueError):
        _logger.warning(
            "updateAnalysisState returned a result this loop could not read back as "
            "state; keeping the state loaded at the top of the window"
        )
        return None
    # The A.1 gate again, belt-and-braces: a state for another turn must never
    # become the one this turn enforces on.
    return state if state.turn_index == turn_index else None


class AnswerShapeCounter:
    """The ANSWER-SHAPE GATE's two facts for one budget window: how many SUCCESSFUL
        multi-row `runQuery`/`runBlueprint` calls this TURN has made, and whether any
        `answerWithTable` has actually put a table in front of the user.

        Both are TURN-scoped facts held in a WINDOW-scoped object, so both are seeded — from
        the persisted trail (`observe_prior_entry`) and, on the blueprint approval-resume path,
        from the tables designated before the pause. Without that seeding a budget-cap
        continue or any resume starts a fresh counter, and a gate that forgot the rows the
        model already has goes silent on exactly the long turns that produce several tables.

        THE TRAIL WALK THE CALLER RUNS IS ALREADY TURN-FILTERED, which is also the cross-turn
        replay protection; the `claim_finalization_block` key is `(turn_index, window, kind)`
        for the same reason.

        IT OWNS NO EVENTS: the two answer-shape events are emitted by the loop, interleaved
        with body-owned flag writes and `tool_result` rewrites.
    """

    def __init__(self, seeded_succeeded: bool) -> None:
        self._multi_row_answer_calls = 0
        # *seeded_succeeded* covers the blueprint approval-resume path, whose
        # designation was made before the pause; `observe_prior_entry` covers
        # everything else.
        self._answer_table_succeeded = seeded_succeeded

    @property
    def multi_row_calls(self) -> int:
        """How many multi-row data calls this turn has made — the number the nudge quotes back
                and both refused-event payloads carry.
        """
        return self._multi_row_answer_calls

    @property
    def armed(self) -> bool:
        """Whether a bare-prose finish right now would be refused: the turn is
        holding multi-row results AND has never shown a table.

        The loop keeps the `not result.tool_calls` half of the test, because that
        half is the EXIT it is standing at, not a fact about answer shape."""
        return bool(self._multi_row_answer_calls) and not self._answer_table_succeeded

    def observe_prior_entry(
        self, tool_name: str, status: str, preview: ResultPreview | None
    ) -> None:
        """Seed BOTH facts from ONE persisted trail entry of this turn (the caller's walk is
                already filtered to `turn_index` + `status == "ok"`; the status is passed
                explicitly anyway so the multi-row predicate reads the same at both call sites).

                NAME + STATUS FOR THE SUCCEEDED FLAG, DELIBERATELY ASYMMETRIC with the live site
                (`note_answer_succeeded`), which requires a designation to have actually resolved.
                Re-resolving `args` here would be a third reading of the designation — the
                divergence `resolve_designation` was extracted to prevent — and would need this
                window's `blueprint_runs` (a KV de-reference per blueprint) to answer correctly.

                The asymmetry is safe in the direction that matters: this is the FALSE-NEGATIVE
                side, and it can only leave the gate disarmed on a turn whose `answerWithTable`
                succeeded in an earlier window — while a successful entry that designated nothing
                is now itself refused at the live site. Erring the other way would re-arm the gate
                and refuse turns that already showed their table.
        """
        if _is_multi_row_answer_call(tool_name, status, preview):
            self._multi_row_answer_calls += 1
        if tool_name == ANSWER_TABLE_TOOL_NAME and status == "ok":
            self._answer_table_succeeded = True

    def note_call(
        self, tool_name: str, status: str, preview: ResultPreview | None
    ) -> None:
        """Count one just-dispatched call if it is a successful data-returning call with more
                than one row.

                THE CALLER'S POSITION IS PART OF THE CONTRACT. It reads the same `result_preview`
                that was just persisted on the trail entry, so the in-window count and the trail
                seed can never disagree — and it is called AFTER every finalization or
                blueprint-not-run rewrite of `tool_result`, so a REFUSED call is never counted.
        """
        if _is_multi_row_answer_call(tool_name, status, preview):
            self._multi_row_answer_calls += 1

    def note_answer_succeeded(self) -> None:
        """The turn HAS tabled its answer, so the gate is done for this turn.

                SET FROM SUBSTANCE, NOT FROM THE CALL — which is why the caller keeps the
                condition. Reading `status == "ok"` alone let a mid-turn `{answer: "", tables: []}`
                succeed and disarm the gate with ZERO designations, after which a bare-prose finish
                passed unrefused. The flag means "the user has a grid", so it is set only when one
                exists.

                The substance test stays at the call site because it reads the loop's
                `answer_tables` ACCUMULATOR as well as this call's own resolution: a LATER call
                that designates nothing deliberately leaves an EARLIER good set intact, and reading
                only this call would re-arm the gate on that retry.
        """
        self._answer_table_succeeded = True


class FinalizationGate:
    """The finalization block ALLOWANCE for one budget window: the per-round-trip flag and
        the persisted per-window claim.

        WINDOW-SCOPED BY CONSTRUCTION. `session_id`, `turn_index` and `window_count` are
        constant for the whole of one `_run_loop_body` and together are the claim key, so they
        are taken once here rather than repeated at four call sites where they could drift.

        THE FLAG IS PER ROUND-TRIP, THE CLAIM IS PER WINDOW, and the two must not be confused.
        `begin_round` resets the flag; the claim lives in `SessionDoc.finalization_blocks` and
        survives every resume of this turn (see `SessionStore.claim_finalization_block` for
        why a local counter cannot express "per window" at all).

        IT EMITS THE TWO CLAIM EVENTS ITSELF because nothing observable happens between the
        claim and either of them. The REFUSAL events stay at the loop's four call sites,
        interleaved with the `tool_result` rewrites and draft clears that are the loop's own.
    """

    def __init__(
        self,
        session_store: SessionStore,
        observer: ToolObserver,
        *,
        session_id: str,
        turn_index: int,
        window_count: int,
    ) -> None:
        self._session_store = session_store
        self._observer = observer
        self._session_id = session_id
        self._turn_index = turn_index
        self._window_count = window_count
        self._refused_this_round = False
        self._granted_kinds: set[FinalizationBlockKind] = set()

    def has_spent(self, kind: FinalizationBlockKind) -> bool:
        """Whether THIS GATE has already been granted *kind*'s re-round — a NON-CONSUMING
                read, added for the answer judge (09 §F.1) and useful only to a check that costs
                something to run.

                WHY IT EXISTS AT ALL. Every other gate's predicate is a regex or a counter, so
                asking `may_refuse` and being told `False` is free. The judge's predicate is a
                MODEL CALL, and once the allowance is gone a rejection cannot act: the call would
                buy a verdict nothing is permitted to use. So the judge asks this first and skips
                itself, which is the whole reason the answer-judge kinds do not simply reuse the
                ask-and-be-refused shape.

                IT IS WINDOW-LOCAL, NOT A STORE READ, and the difference is a real one that the
                caller must not paper over. `_run_loop_body` builds a fresh gate on every entry —
                including every resume — while `SessionDoc.finalization_blocks` persists, so this
                reads `False` for a kind an EARLIER invocation of the same window already spent.
                That is not a correctness bug: the caller then runs the judge, `may_refuse`
                returns `False` against the persisted claim, and the answer ships exactly as it
                should. The only cost is one judge call that could have been skipped, on the one
                path that reaches a second finish inside one window without re-entering the loop
                — an askUser resume. A store read to close that gap would put a Couchbase
                round-trip on every terminal exit to save a rare model call, and would mean a new
                `SessionStore` Protocol method for a question the loop can already answer about
                itself.

                THE PRIMARY CASE IS EXACT. A judge rejection hands the round back INSIDE this
                same `_run_loop_body`, so the second finish of that window meets this same gate
                object and is skipped without a call.
        """
        return kind in self._granted_kinds

    @property
    def refused_this_round(self) -> bool:
        """Whether a finalization refusal has already happened in the round-trip in progress.
                Read by the loop after `guard.record_iteration` to tell a budget cap reached during
                a refused round from an ordinary pause.
        """
        return self._refused_this_round

    def begin_round(self) -> None:
        """Start a response batch: clear the per-round-trip flag.

                The window's forced re-round is consumed PER ROUND-TRIP, not per refused call — so
                an `[answerWithTable, answerWithTable]` batch is refused twice and advances the
                persisted counter once.
        """
        self._refused_this_round = False

    async def may_refuse(self, kind: FinalizationBlockKind) -> bool:
        """Whether a finalization refusal may proceed — ONE forced re-round per budget window
                OF THIS TURN PER KIND, CONSUMED PER ROUND-TRIP. On `True` the round is marked
                refused, which is what makes the second and later refusals of one batch free.

                THREE INDEPENDENT ALLOWANCES, SELECTED BY `kind`:

                  `intents`      | the pending-intents refusals, exits #1 and #2
                  `answer_shape` | the untabled-multi-row refusal, exit #1 only
                  `empty_answer` | the no-prose-no-tool-calls refusal, exit #1 only

                They must stay independent. On the multi-intent questions this release exists for,
                the two gates fire in SEQUENCE rather than in competition — finish with intents
                pending, close the ledger, finish again with the tables still untabled — so one
                shared allowance starves the shape gate on exactly the question it was built for.
                Splitting raises the worst case to two extra round-trips per window, still bounded
                by `max_budget_windows`.

                PRECEDENCE IS NOT EXPRESSED HERE: the call site keeps the shape gate as an `elif`
                on the pending-intents branch, so at most one refusal happens per round-trip.

                `turn_index` and `kind` are part of the claim key, not context. `window_count`
                restarts at 1 on every external turn while `SessionDoc.finalization_blocks`
                persists across the session — see `session/models.py::finalization_block_key`.

                THE PER-ROUND GATE IS NOT A NICETY. Exit #2's refusal happens inside the
                per-tool-call loop, which processes up to 8 calls from ONE model response: a model
                emitting `[answerWithTable, answerWithTable]` would otherwise burn both chances in
                a single round-trip, force-block on the second and finalize — having been given NO
                re-round at all, with `ENFORCEMENT_EXHAUSTED` written for intents it was never
                asked twice about. So later refusals in one batch return the same retryable error
                but make no store call, advance no counter and emit no event.

                THE ROUND FLAG STAYS ONE FLAG ACROSS ALL THREE KINDS, and for `empty_answer` the
                flag is what enforces it. `answer_shape` is a later branch of the SAME `if/elif`
                chain the pending-intents branch opens, so the chain excludes it structurally.
                `empty_answer` is deliberately NOT in that chain — it is a post-chain `if` guarded
                on `refused_finalization`, because the chain's branches are entered when their
                complaint QUALIFIES rather than when they refuse, and an `elif` would silence the
                gate on every round where an earlier allowance was already spent (05 §K.4; it is
                §J.3's starvation one gate later). The guard keeps the same invariant the chain
                gives the other two: at most one refusing kind per round-trip, with the more
                specific complaint winning the round it fires in. The batched exit-#2 refusals the
                flag exists for require tool calls and are unaffected.

                DEGRADE-NEVER-FAIL. `claim_finalization_block` is a CAS read-modify-write that can
                raise `CASMismatchError` after lost retries or a transient connection error, and
                either would otherwise propagate out of `_run_loop_body` and abort the turn AT THE
                MOMENT THE MODEL HAS A FINISHED ANSWER. In-memory and scripted doubles cannot fail,
                so this only bites against a real store.

                A FAILED CLAIM IS TREATED AS `False`: the caller force-blocks the surviving intents
                with `ENFORCEMENT_EXHAUSTED` and finalizes, recording a disposition and ending the
                turn. Treating it as `True` would grant a re-round whose consumption was never
                persisted, so the next round-trip would ask again, fail again, and re-round again.
        """
        if self._refused_this_round:
            return True
        try:
            granted = await self._session_store.claim_finalization_block(
                self._session_id, self._turn_index, self._window_count, kind
            )
        except Exception:
            _logger.exception(
                "failed to claim the %s finalization block (session=%s, turn=%d, "
                "window=%d) — treating the re-round as unavailable and finalizing",
                kind,
                self._session_id,
                self._turn_index,
                self._window_count,
            )
            # D25: shape-only. All three keys are on
            # `observability/tracing.py::_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`, so this
            # actually reaches Phoenix rather than being a correctly-named span
            # carrying nothing (README finding 10).
            self._observer(
                "loop_finalization_block_claim_failed",
                {
                    "turn_index": self._turn_index,
                    "window": self._window_count,
                    "reason": "store_error",
                },
            )
            return False
        if granted:
            self._observer(
                "loop_finalization_block_spent", {"window": self._window_count}
            )
            self._refused_this_round = True
            # Recorded ONLY on a real grant, and deliberately not on the
            # `_refused_this_round` short-circuit above: that path returns `True` for a
            # SECOND kind in one batch without claiming anything, so recording there
            # would make `has_spent` report an allowance that is still available.
            self._granted_kinds.add(kind)
        return granted
