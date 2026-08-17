"""Finalization enforcement's DECISION layer — the refusals, the nudges, the
answer-shape counter and the per-round block allowance (Release 1,
`docs/decisions/release-1/05-finalization-enforcement.md`). This is where
`analysisState` gets teeth.

THE INVARIANT IS SCOPED (05 §F.1): no intent ends `pending` on any turn that
reaches a TERMINAL outcome (`done` / `stopped_hard_ceiling`). A turn abandoned at
an `askUser`/budget-cap/blueprint pause, or whose resume loses a CAS race, is a
NON-TERMINATED turn and legitimately leaves its intents `pending` — an unscoped
assertion would fail against any real store.

ENFORCEMENT APPLIES ONLY TO THE LIVE STATE (05 §A). Everything here reads the
window-local the loop loaded through `live_analysis_state` and hands in as an
argument, so a state left behind by an abandoned earlier turn cannot refuse an
unrelated later turn (and cannot have `ENFORCEMENT_EXHAUSTED` written onto its
record by one).

WHAT MOVED HERE AND WHAT DELIBERATELY DID NOT. Extracted from
`_run_loop_body` for the reason `read_guard.py` and `blueprint_gate.py` were: the
state, the decision and the events belong together, and they were spread across a
4000-line function where the load-bearing distinctions — the trail seed's
asymmetry, the per-ROUND (not per-CALL) allowance, the position of the live count
relative to the tool_result rewrites — were visible only to a reader holding every
site in their head at once. The loop keeps the EFFECTS and the CONTROL FLOW: the
two-gate `if pending / elif shape` precedence, the `refused_finalization` local,
the draft clears, the nudge's one-round-trip set/splice/clear cycle, the
`tool_result` rewrites, `_force_block_pending_intents` (five callers, one of
them outside the body — `resume()`'s `USER_STOPPED` path) and both
budget-exhaustion branches.

Only the two BLOCK-CLAIM events move with the claim (`FinalizationGate` owns the
whole claim decision and nothing observable happens between the claim and its
event). `loop_finalization_refused`, `loop_enforcement_exhausted` and the two
answer-shape events stay at their emit sites in the loop, interleaved with
body-owned flag writes and `tool_result` rewrites.

SEVEN IMPORTS, each unavoidable and each one-directional (nothing here imports
`agent_loop`, and `composite.answer_with_table`'s own edge back to it is
`TYPE_CHECKING`-only, so no cycle exists):

  - `ToolResult` — two of the three refusals below ARE one; the loop writes each
    into the trail entry in place of the call's real result.
  - `ToolObserver` — the observer alias the loop already passes.
  - `ANSWER_TABLE_TOOL_NAME` — both refusals are persisted under the tool they
    refuse, and the counter's trail seed keys on the same name. Re-spelling the
    literal here is the divergence the shared constant exists to prevent.
  - `ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE` / `FINALIZATION_BLOCKED_PENDING_
    INTENTS_CODE` — the two error codes, from `dispatch/denial_mapping.py` (their
    canonical home, which is also what `classify_denial` reads).
  - `sanitize_text` + `MAX_FIELD_CHARS` — model-authored intent descriptions
    re-enter model context through `_describe_pending`; see it for why.
  - `AnalysisState` / `TrackedIntent` / `ResultPreview` / `FinalizationBlockKind`
    — the state read, the intents described, the row count counted, the allowance
    keyed.
  - `SessionStore` — `FinalizationGate` makes the persisted `claim_finalization_
    block` call itself, because a claim whose consumption is not persisted is an
    unbounded re-round (see `may_refuse`).

So this is NOT the stdlib leaf `read_guard.py` is, and it does not need to be —
nothing outside the loop package imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from data_agent.runtime.composite.answer_with_table import TOOL_NAME as ANSWER_TABLE_TOOL_NAME
from data_agent.runtime.dispatch.denial_mapping import (
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
    "AnswerShapeCounter",
    "FinalizationGate",
    "answer_shape_nudge_text",
    "answer_table_no_table_designated",
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
_MAX_NUDGE_DRAFT_CHARS = 2000

# The tools whose SUCCESSFUL result IS an answer's rows, for the ANSWER-SHAPE gate
# (05 §J). Deliberately just two: `sampleRows`, `getTableSchema` and the listings
# are DISCOVERY — a model that peeks at ten sample rows and then answers a single
# figure in prose is behaving correctly, and counting those would refuse it.
_DATA_ANSWER_TOOLS = frozenset({"runBlueprint", "runQuery"})

# The answer-shape gate's two events, NAMED because the `loop_` prefix is
# load-bearing rather than a convention: `observability/tracing.py::
# guardrail_observer` drops every event that lacks it, SILENTLY, so a misnamed
# event fires perfectly in every raw-recorder unit test and reaches production
# telemetry never (06, and the `loop_analysis_state_auto_bound` near-miss that
# shipped that way for a review round). Exported so the span test can assert the
# real observer's output against the same symbol the emit site uses.
ANSWER_SHAPE_REFUSED_EVENT = "loop_answer_shape_refused"
ANSWER_SHAPE_EXHAUSTED_EVENT = "loop_answer_shape_exhausted"


def _is_multi_row_answer_call(
    tool_name: str, status: str, preview: ResultPreview | None
) -> bool:
    """Whether one call is a SUCCESSFUL, data-returning call that produced MORE
    THAN ONE ROW — the fact the answer-shape gate (05 §J) counts.

    `row_count > 1`, not `>= 1`, and the strictness is the whole safety margin:

      - **zero rows** is a legitimate prose answer ("no employees match"), and
        04 §B.4 already treats an empty result as an answer rather than a failure;
      - **one row** is a single figure ("headcount is 412"), which the prompt does
        not ask to be tabled and which live q6 answers correctly in prose.

    Refusing either would turn a correct turn into an extra round-trip and a
    confusing instruction to table something that is not a table.

    Module-private: `AnswerShapeCounter` holds BOTH call sites (the trail seed and
    the live count), which is the point — the two can no longer disagree about
    what is being counted because there is one predicate behind one object.
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
    single-intent, so the enforcement check must cost one `is None` test on a
    local and never a store read (05 §E)."""
    if state is None:
        return ()
    return tuple(intent for intent in state.intents if intent.status == "pending")


def _describe_pending(pending: Sequence[TrackedIntent]) -> str:
    """`i2 ('attrition by department')` for each pending intent.

    The descriptions are MODEL-authored text derived from the user's question,
    re-entering model context — so they go through the SAME structural
    sanitisation the rendered state block uses (`runtime/sanitize.py`), or a
    newline in one could fabricate an instruction line inside the message it lands
    in. Same turn and same `column_scope` as the state it quotes, so there is no
    D44 exposure AT THE POINT OF USE.

    IT DOES NOT FOLLOW THAT IT CANNOT OUTLIVE THE TURN, and an earlier version of
    this docstring claimed exactly that. On the exit-#1 path the text is ephemeral,
    so it is true there. On the EXIT-#2 path it rides `denial_detail` on a PERSISTED
    `answerWithTable` trail entry: `filter_trail`'s status-gated exemption keeps a
    non-`ok` entry for its OWN turn, but nothing in `filter_trail` drops it later —
    `frozenset()` provenance passes `is_entry_in_scope` under any scope, forever, and
    `_render_entry` has no turn awareness. The cross-turn drop is
    `context/assembly.py::_is_stale_model_text_entry`, which matches this entry by
    its error code; that is what actually bounds the lifetime."""
    return "; ".join(
        f"{intent.intent_id} ('{sanitize_text(intent.description, MAX_FIELD_CHARS)}')"
        for intent in pending
    )


def finalization_blocked(pending: Sequence[TrackedIntent]) -> ToolResult:
    """The refusal returned in place of a terminal `answerWithTable` while intents
    are still pending (05 §B.1) — modelled on `_answer_table_blueprint_not_run`.

    Returned BEFORE the trail entry is written, so the persisted entry IS the
    refusal and the model reads it on the next round-trip.

    IN-TURN VISIBILITY COMES FROM THE STATUS GATE, NOT FROM THE PROVENANCE.
    `filter_trail`'s current-turn exemption keeps a `status != "ok"` entry of the
    CURRENT turn whatever its provenance, and that is the only place this entry has
    to survive. (05 §B.1 originally recorded the opposite — "the status gate is
    belt-and-braces; provenance is binding" — which is inverted: `frozenset()` is
    load-bearing only in contexts that pass no `current_turn_index`, i.e. precisely
    the LATER turns where this entry must NOT survive. Corrected in 05 §B.1/§I.)

    `provenance=frozenset()` IS STILL THE RIGHT VALUE, for a different reason: this
    refusal is runtime-authored and reads no warehouse data, and
    `_compute_turn_provenance_union` is fail-closed — a `None` here would collapse
    the whole turn's union and tag the turn's own final assistant message
    undetermined, dropping the user's answer from every later turn's replay.

    WHAT BOUNDS ITS LIFETIME IS `context/assembly.py::_is_stale_model_text_entry`,
    which drops this entry from any turn other than its own, matching on
    `FINALIZATION_BLOCKED_PENDING_INTENTS_CODE` (the entry is persisted under
    `answerWithTable`, whose SUCCESSFUL entries must keep replaying, so it cannot be
    matched by tool name). Without that drop the detail below — dead intent ids and
    an imperative to call `updateAnalysisState` against a state that no longer
    exists — plus the refused draft prose in `args` would replay in every later turn
    of the session, under any since-narrowed scope.

    `denial_detail` NAMES THE PENDING INTENTS. `context/budget.py::_render_entry`
    builds the model-facing text as `entry.denial_detail or
    classify_denial(entry.error_code).user_message` and NEVER from
    `ToolResult.user_message`, which has no `TrailEntry` field at all — a specific
    message set only there is silently dropped.
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
    """The nudge for an `answerWithTable` that designated NOTHING — no `tables`
    entry carrying a designation and no legacy pair to fold — on a turn that is
    holding multi-row results it has not tabled.

    WHAT THIS CLOSES, measured on two live-loop probes. `{answer: <prose>,
    tables: []}` succeeded, carried non-blank prose, and therefore TERMINATED the
    turn through the exit the 05 §J answer-shape gate does not watch — the gate
    lives on exit #1 (a model turn with no tool calls) and this is exit #2. The
    turn returned `done` with no table, no event and no log line: the user asked
    for a breakdown, held six rows of it, and got prose. Silent in the strongest
    sense — nothing anywhere reported it.

    That is the SAME failure `_answer_table_blueprint_not_run` exists to stop, one
    step earlier. There, the model named a table the runtime could not resolve;
    here it named none at all. 08 §O made the second case likelier rather than
    rarer: `tables` is now REQUIRED, so a model with nothing to put there must
    still emit the key, and `tables: []` is exactly what a model that cannot omit a
    declared key produces.

    WHY IT IS BOUNDED BY THE SHAPE GATE'S OWN ALLOWANCE (`kind="answer_shape"`).
    This is the same complaint the shape gate makes — *you are finishing without
    presenting a table you are holding* — arriving through the other exit, so the
    two must share one bound or a model could be refused twice per window for one
    mistake. When the grant is spent the prose PASSES and the turn ends: the
    runtime records what it can and never hard-locks a turn, the posture
    `ENFORCEMENT_EXHAUSTED` takes for intents.

    WHY IT IS SCOPED TO `multi_row_answer_calls > 0`. A turn holding no multi-row
    result has nothing to table, and an `answerWithTable` with no table on such a
    turn is odd but harmless — a zero-row "none found" answered in prose is
    CORRECT, and live q6 is that case. Nudging it would charge a right answer an
    extra round-trip and tell the model to grid a number, which is the
    false-positive half 05 §J is most exposed to.

    AND TO A NON-BLANK `answer`, mirroring the terminal condition exactly. A call
    that would not have ended the turn is not a finalization and must not be refused
    as one — the rule the pending-intents refusal already follows at this exit. A
    blank-`answer` empty call terminates nothing, so nothing is silently lost;
    refusing it would spend this window's allowance on a habit call and leave the
    real prose finish that follows unrefusable. THAT case is covered at the other
    end, by `answer_table_succeeded` being set from substance rather than from the
    call — the two fixes are halves of one defect and neither is sufficient alone.

    Mirrors `_answer_table_blueprint_not_run` in every mechanical respect: non-`ok`
    so the terminal exit does not fire and `filter_trail`'s status-gated
    current-turn exemption keeps it visible this same turn; `denial_detail` because
    that is the channel `context/budget.py::_render_entry` actually reads;
    registered in `dispatch/denial_mapping.py` because `classify_denial` otherwise
    degrades to "Something went wrong processing that request."
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


def finalization_nudge_text(draft: str | None, pending: Sequence[TrackedIntent]) -> str:
    """The ephemeral `user`-role message injected in place of exit #1's missing
    error channel (05 §B.2/§B.3).

    IT CARRIES THE DRAFT BACK. Exit #2's refusal preserves the model's prose for
    free — it lives in `tool_call.arguments`, is persisted as `TrailEntry.args`,
    and is replayed by `_render_entry`. Exit #1 preserves NOTHING: the answer is
    not persisted (by design — a persisted nudge or draft would surface in
    `/session/history` as something the user said) and D22 discards free text
    around tool calls, so without this quote the model has no record it just wrote
    a final answer and must regenerate it blind.

    The `_text` suffix (on this and `answer_shape_nudge_text`) is not decoration:
    the loop's own window-local for the built string is `finalization_nudge`, and a
    builder of the same name would be shadowed by it inside `_run_loop_body`.
    """
    lines: list[str] = []
    if draft and draft.strip():
        lines.append(f"You drafted: {draft.strip()[:_MAX_NUDGE_DRAFT_CHARS]}")
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
    """The ephemeral `user`-role message injected when a turn tries to finish in
    bare prose while holding multi-row results it never tabled (05 §J).

    IT CORRECTS A BELIEF ABOUT TURN MECHANICS, which is the only thing the runtime
    can correct here and the reason a prompt rule alone was not enough. Live, the
    worst failure mode was not the model deciding prose was better — it was the
    model APOLOGISING for being unable to call the tool any more: *"the requested
    results are multi-row tables and must be returned through the table-rendering
    path, but that final table call was not made before the tool session ended."*
    Nothing had refused it and nothing had ended; it believed the turn was over. So
    the first line this message has to say is that it is not, and that the tool is
    still there.

    IT CARRIES THE DRAFT BACK for the same reason `finalization_nudge_text` does —
    exit #1 persists nothing and D22 discards free text around tool calls — and
    here the draft is doubly load-bearing, because the escape hatch below asks the
    model to send that answer again if the gate was wrong about its shape.

    WHICH IS WHY THE TRUNCATION IS MARKED. This echo is the model's ONLY surviving
    copy of what it wrote, and the slice at `_MAX_NUDGE_DRAFT_CHARS` is invisible
    from the inside: an unmarked cut plus an instruction to re-send "unchanged"
    reads as "re-send exactly this", and the tail of a long answer is lost silently.
    The marker plus "your FULL answer" tells the model the echo is a reminder, not
    the artefact. (The pending-intents nudge has the same slice and the same
    exposure; it is left alone here because its instruction is to resolve intents
    and re-send, not to reproduce a quoted string, and this section does not touch
    its wording.)

    THE ESCAPE HATCH IS NOT DECORATION. The gate reads row counts, not meaning: a
    turn can legitimately run a multi-row query and answer a single figure from it
    (a count over a grouped read, a "yes, three of them" narrative). Offering the
    re-send is what keeps that turn correct at a cost of one round-trip, instead of
    forcing a table nobody asked for.
    """
    lines: list[str] = []
    if draft and draft.strip():
        stripped = draft.strip()
        echo = stripped[:_MAX_NUDGE_DRAFT_CHARS]
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


def refreshed_analysis_state(
    tool_result: ToolResult, turn_index: int
) -> AnalysisState | None:
    """The state a SUCCESSFUL `updateAnalysisState` call just wrote, read back off
    its own result (05 §E), or `None` when there is nothing to refresh from.

    The state changes mid-turn, so a once-per-window read would be wrong — but a
    store read at each terminal exit would cost a round-trip on EVERY turn,
    including the single-intent ones that never touch this feature. So the loop
    loads the state ONCE at the top and refreshes the local from each state call's
    result. 03 §E.2's partition guarantees state calls are dispatched before
    anything else in the batch, so the local is current by the time either exit is
    reached.

    Defensive: a malformed result degrades to "no refresh" (the loaded value
    stands) rather than raising into the dispatch loop.
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
    """The ANSWER-SHAPE GATE's two facts for one budget window (05 §J): how many
    SUCCESSFUL multi-row `runQuery`/`runBlueprint` calls this TURN has made, and
    whether any `answerWithTable` has actually put a table in front of the user.

    Both are TURN-scoped facts held in a WINDOW-scoped object, so both are seeded —
    from the persisted trail (`observe_prior_entry`) and, on the blueprint
    approval-resume path, from the tables designated before the pause
    (*seeded_succeeded*). The seeding is the whole reason a window-local can be
    right: a budget-cap `continue`, an `askUser` resume and a mid-DAG blueprint
    resume each start a fresh `_run_loop_body` with an empty counter, and a gate
    that forgot the rows the model already has would go silent on exactly the long
    turns that produce several tables.

    THE TRAIL WALK THE CALLER RUNS IS ALREADY TURN-FILTERED, which is also the
    cross-turn replay protection: a multi-row query from turn 3 cannot make turn 4's
    prose answer a defect, and the `claim_finalization_block` key is
    `(turn_index, window, kind)` too, so a stale refusal cannot be replayed onto a
    later turn.

    IT OWNS NO EVENTS. `ANSWER_SHAPE_REFUSED_EVENT`/`ANSWER_SHAPE_EXHAUSTED_EVENT`
    are emitted by the loop at three sites interleaved with body-owned flag writes
    and `tool_result` rewrites; this object only answers `armed` and
    `multi_row_calls`.
    """

    def __init__(self, seeded_succeeded: bool) -> None:
        self._multi_row_answer_calls = 0
        # *seeded_succeeded* covers the blueprint approval-resume path, whose
        # designation was made before the pause; `observe_prior_entry` covers
        # everything else.
        self._answer_table_succeeded = seeded_succeeded

    @property
    def multi_row_calls(self) -> int:
        """How many multi-row data calls this turn has made — the number the nudge
        quotes back and the `multi_row_calls` key both refused-event payloads
        carry."""
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
        """Seed BOTH facts from ONE persisted trail entry of this turn (the caller's
        walk is already filtered to `turn_index` + `status == "ok"`; the status is
        passed explicitly anyway so the multi-row predicate reads the same at both
        of its call sites).

        NAME + STATUS FOR THE SUCCEEDED FLAG, DELIBERATELY ASYMMETRIC with the live
        site (`note_answer_succeeded`), which since 08 §O requires a designation to
        have actually resolved.

        This walk reads PERSISTED entries and would have to re-resolve `args` to
        know whether one designated anything — a second reading of the designation
        in a third place, which is the divergence `resolve_designation` was
        extracted to prevent, and it would need this window's `blueprint_runs` (a
        D46 KV de-reference per blueprint) to answer correctly for the blueprint
        form. The cheap wrong answer would be to treat an unresolvable id as "no
        table" and re-arm the gate on a turn that HAD one.

        The asymmetry is safe in the direction that matters. This is the
        FALSE-NEGATIVE side: it can only leave the gate disarmed on a turn whose
        `answerWithTable` succeeded in an earlier window, and a successful entry
        that designated nothing is now itself refused at the live site, so it never
        becomes a persisted `ok` entry in the first place. Pre-§O entries all
        carried a designation in practice. Erring the other way — re-arming — would
        refuse turns that already showed their table, which is the false positive
        05 §J is most exposed to."""
        if _is_multi_row_answer_call(tool_name, status, preview):
            self._multi_row_answer_calls += 1
        if tool_name == ANSWER_TABLE_TOOL_NAME and status == "ok":
            self._answer_table_succeeded = True

    def note_call(
        self, tool_name: str, status: str, preview: ResultPreview | None
    ) -> None:
        """Count one just-dispatched call if it is a successful data-returning call
        with more than one row.

        THE CALLER'S POSITION IS PART OF THE CONTRACT. It reads the same
        `result_preview` that was just persisted on the trail entry, so the
        in-window count and the trail seed can never disagree about what happened —
        and it is called AFTER every finalization/blueprint-not-run rewrite of
        `tool_result`, so a REFUSED call (now non-`ok`) is never counted."""
        if _is_multi_row_answer_call(tool_name, status, preview):
            self._multi_row_answer_calls += 1

    def note_answer_succeeded(self) -> None:
        """The turn HAS tabled its answer, so the gate is done for this turn.

        SET FROM SUBSTANCE, NOT FROM THE CALL (08 §O) — which is why the caller
        keeps the condition. It used to read `status == "ok"` alone, and a live
        probe showed what that bought: a mid-turn `{answer: "", tables: []}`
        succeeds (the tool is stateless and refuses nothing), disarmed the gate with
        ZERO designations, and the model's later bare-prose finish then passed
        unrefused. The flag is supposed to mean "the user has a grid", so it is set
        only when one exists.

        The substance test stays at the call site because it reads the loop's
        `answer_tables` ACCUMULATOR (folded moments earlier) as well as this call's
        own resolution: a LATER call that designates nothing deliberately leaves an
        EARLIER good set intact, and reading only this call's resolution would
        re-arm the gate on that retry and refuse a turn that has its table."""
        self._answer_table_succeeded = True


class FinalizationGate:
    """The finalization block ALLOWANCE for one budget window: the per-round-trip
    flag and the persisted per-window claim (05 §C.1/§C.2, §J.3).

    WINDOW-SCOPED BY CONSTRUCTION. `session_id`, `turn_index` and `window_count`
    are all constant for the whole of one `_run_loop_body` — `window_count` is a
    parameter of it — and together they are the claim key, so they are taken once
    here rather than repeated at four call sites where they could drift.

    THE FLAG IS PER ROUND-TRIP, THE CLAIM IS PER WINDOW, and the two must not be
    confused. `begin_round` resets the flag beside the loop's other per-round
    resets; the claim lives in `SessionDoc.finalization_blocks` and survives every
    resume of this turn (see `SessionStore.claim_finalization_block` for why a
    local counter cannot express "per window" at all).

    IT EMITS THE TWO CLAIM EVENTS ITSELF (`loop_finalization_block_spent`,
    `loop_finalization_block_claim_failed`) because nothing observable happens
    between the claim and either of them. The REFUSAL events —
    `loop_finalization_refused`, `loop_enforcement_exhausted` and the answer-shape
    pair — stay at the loop's four call sites, interleaved with the `tool_result`
    rewrites and draft clears that are the loop's own.
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

    @property
    def refused_this_round(self) -> bool:
        """Whether a finalization refusal has already happened in the round-trip in
        progress. Read by the loop after `guard.record_iteration` to decide whether
        a budget cap reached HERE is the "cap during a refused round" case (05 §F,
        fourth forced path) rather than an ordinary pause."""
        return self._refused_this_round

    def begin_round(self) -> None:
        """Start a response batch: clear the per-round-trip flag.

        The window's forced re-round is consumed PER ROUND-TRIP, not per refused
        call (05 §C.2) — so a `[answerWithTable, answerWithTable]` batch is refused
        twice and advances the persisted counter once."""
        self._refused_this_round = False

    async def may_refuse(self, kind: FinalizationBlockKind) -> bool:
        """Whether a finalization refusal may proceed — ONE forced re-round per
        budget window OF THIS TURN PER KIND, CONSUMED PER ROUND-TRIP (05 §C.1/§C.2,
        §J.3). On `True` the round is marked refused, which is what makes the
        second and later refusals of one batch free.

        TWO INDEPENDENT ALLOWANCES, SELECTED BY `kind`:

          `intents`      | the pending-intents refusals, exits #1 and #2 (§B)
          `answer_shape` | the untabled-multi-row refusal, exit #1 only (§J)

        THEY SHARED ONE ALLOWANCE UNTIL 2026-08-12, AND THAT WAS A MEASURED DEFECT.
        The sharing was deliberate — it bounded the worst case at one extra
        round-trip per window — but on the multi-intent questions this release
        exists for, the two gates fire in sequence rather than in competition: the
        model finishes with intents pending (intents nudge, grant gone), closes the
        ledger, then finishes in prose again with its tables still untabled. Live, 2
        of 4 three-part runs went exactly that way and the shape gate could only
        emit `loop_answer_shape_exhausted` — starved on the question it was built
        for (traces `900a85a4`, `16f090db`). Splitting the allowance raises the
        worst case to TWO extra round-trips per window, still bounded by
        `max_budget_windows`, and makes the common sequence terminate correctly.

        PRECEDENCE IS NOT EXPRESSED HERE. The call site keeps the shape gate as an
        `elif` on the pending-intents branch, so at most one refusal happens per
        round-trip; this method only knows which allowance is being asked for.

        `turn_index` is part of the claim key, not context; so is `kind`.
        `window_count` restarts at 1 on every external turn while
        `SessionDoc.finalization_blocks` persists across the whole session — see
        `session/models.py::finalization_block_key` for what a window-only key cost.

        The per-round gate is not a nicety. Exit #2's refusal happens inside the
        per-tool-call loop, which processes up to 8 calls from ONE model response:
        a model emitting `[answerWithTable, answerWithTable]` would otherwise burn
        both chances in a single round-trip, force-block on the second, and
        finalize — having been given NO re-round at all, with
        `ENFORCEMENT_EXHAUSTED` written for intents it was never asked twice about.
        So the second and later refusals in one batch return the same retryable
        error but do not advance the persisted counter — and make no store call and
        emit no event at all.

        THE ROUND FLAG STAYS ONE FLAG ACROSS BOTH KINDS, and does not need to be
        per-kind: the two kinds cannot both refuse in one round-trip.
        `answer_shape` lives only at exit #1 (`not result.tool_calls`) and only in
        the `elif` of the pending-intents branch, while the batched exit-#2 refusals
        the flag exists for require tool calls. A round-trip therefore has at most
        one refusing kind, and the flag means what it always meant.

        DEGRADE-NEVER-FAIL, same posture as `_force_block_pending_intents`.
        `claim_finalization_block` is a CAS read-modify-write: it can raise
        `CASMismatchError` after five lost retries (a concurrent resume racing this
        turn is enough) or a transient connection error. Both would otherwise
        propagate out of `_run_loop_body` and abort the turn AT THE MOMENT THE MODEL
        HAS A FINISHED ANSWER — the worst possible time. In-memory and scripted
        doubles cannot fail, so the suite is green by construction and this only
        bites against a real store.

        A FAILED CLAIM IS TREATED AS `False`: the caller force-blocks the surviving
        intents with `ENFORCEMENT_EXHAUSTED` and finalizes. That records a
        disposition and ends the turn. Treating it as `True` would grant a re-round
        whose consumption was never persisted, so the next round-trip would ask the
        store again, fail again, and re-round again — unbounded, bounded only by the
        budget window.
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
        return granted
