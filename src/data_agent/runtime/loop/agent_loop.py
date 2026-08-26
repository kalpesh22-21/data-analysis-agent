"""AgentLoop — the per-turn state machine.

Per turn: assemble the canonical messages, then loop `send_turn` -> dispatch each
requested tool -> budget check, until the model returns no tool calls (done), a tool
pauses, or the budget window ends. `resume()` is the separate entry point that
CAS-consumes the checkpoint (D45), threads the answer back in, and re-enters with a FRESH
`BudgetGuard` window; only a `budget_cap` resume answered "continue"/"refine" counts a new
window grant, and a "stop" ends the turn with the best partial result already in the trail.

Tool calls are dispatched SEQUENTIALLY, which keeps `BudgetGuard` iteration accounting and
trail ordering trivially deterministic.

`askUser` is intercepted here and ONLY here — it never reaches `ToolDispatcher.dispatch`.
Runtime tools (`resolveValues` plus the three read tools) are intercepted here too, but
each returns an INLINE `ToolResult`, so the trail/budget path treats them identically to a
dispatched tool. An advertised-but-unwired runtime tool returns a clean local error, never
an MCP unknown-tool denial.

Statelessness across pauses (D45): both `run()` and `resume()` rebuild the canonical
message list from the `SessionStore` on EVERY model round-trip, so any process can resume
any paused session — no in-process state survives a pause.

D5 (load-bearing): `RuntimeCredentials` is threaded as an explicit argument to
`ToolDispatcher.dispatch` and to `ContextAssembler.assemble` (scope only). It is NEVER
placed into the canonical `messages` list handed to `ModelClient.send_turn`.

Two DIFFERENT token ceilings live on this class and must not be confused:
`max_token_spend` is the per-window SPEND ceiling handed to `BudgetGuard`;
`request_token_budget` is the per-request OCCUPANCY ceiling handed to
`fit_request_to_budget`. A sum answers the first question and never the second.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from data_agent.runtime.answer_scrub import ANSWER_PROSE_REDACTED_EVENT, scrub_answer_prose
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    MAX_STATE_CALLS,
    SUBSTANTIVE_TOOLS,
    find_locking_tool,
    split_serves_intent,
    surplus_state_call_rejected,
)
from data_agent.runtime.composite.analysis_state import TOOL_NAME as UPDATE_ANALYSIS_STATE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import TOOL_NAME as ANSWER_TABLE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import (
    AnswerTable,
    BlueprintRun,
    DesignationItem,
    clean_answer_text,
    enrich_table,
    finalize_designations,
    is_answer_table_in_scope,
    is_zero_row_count,
    resolve_designations,
    terminal_sql_by_id,
)
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.context import scope_filter
from data_agent.runtime.context.assembly import (
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
    turn_date_anchor_day,
)
from data_agent.runtime.context.budget import fit_request_to_budget, render_entry
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    ToolPause,
    ToolResult,
    _default_observer,
)
from data_agent.runtime.dispatch.tool_envelope import in_tool_span
from data_agent.runtime.hooks.answer_table import (
    AnswerTableEvent,
    AnswerTableHooks,
    references_scratch,
)
from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.runtime.observability.progress_summarizer import ProgressSummarizer
from data_agent.runtime.observability.redaction import hash_scope
from data_agent.runtime.session.models import (
    AnalysisState,
    FinalizationBlockKind,
    PauseCheckpoint,
    TrackedIntent,
    TrailEntry,
    TurnMessage,
    live_analysis_state,
)
from data_agent.runtime.session.store import SessionStore
from data_agent.timeutil import now_iso

from .answer_judge import (
    ANSWER_JUDGE_EXHAUSTED_EVENT,
    ANSWER_JUDGE_FAILED_EVENT,
    ANSWER_JUDGE_REFUSED_EVENT,
    ANSWER_JUDGE_SKIPPED_EVENT,
    APPROVED,
    ASK_USER_JUDGE_EXHAUSTED_EVENT,
    ASK_USER_JUDGE_REFUSED_EVENT,
    AnswerJudge,
    JudgeBrief,
    JudgeSite,
    JudgeVerdict,
    answer_judge_nudge_text,
    ask_user_judge_nudge_text,
)
from .answer_rules import (
    ANSWER_RULE_EXHAUSTED_EVENT,
    ANSWER_RULE_REFUSED_EVENT,
    first_match,
    reported_figures,
)
from .blueprint_gate import BlueprintGate
from .budget_guard import BudgetGuard
from .finalization import (
    ANSWER_SHAPE_EXHAUSTED_EVENT,
    ANSWER_SHAPE_REFUSED_EVENT,
    DATA_ANSWER_TOOLS,
    EMPTY_ANSWER_EXHAUSTED_EVENT,
    EMPTY_ANSWER_FALLBACK_TEXT,
    EMPTY_ANSWER_REFUSED_EVENT,
    AnswerShapeCounter,
    FinalizationGate,
    answer_judge_rejected,
    answer_shape_nudge_text,
    answer_table_no_table_designated,
    empty_answer_nudge_text,
    finalization_blocked,
    finalization_nudge_text,
    pending_intents,
    refreshed_analysis_state,
)
from .read_guard import ReadGuard, idempotent_read_signature, repeated_read_guard_event
from .turn_accumulators import (
    AnswerEnvelope,
    TurnAccumulators,
    accumulate_enrichment,
    capture_terminal_sql,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.context.discovery_emulation import EmulatedDiscovery

ToolsProvider = Callable[[RuntimeCredentials], Awaitable[list[dict[str, Any]]]]
# Emulated-discovery injection (context/discovery_emulation.py): the per-window
# sweep that emulates `listDatabases`+`listTables` and returns an `EmulatedDiscovery`
# (the synthetic rendered entries + the guard signatures, or an empty result to
# degrade). `None` provider (default) = feature absent, byte-identical.
EmulatedDiscoveryProvider = Callable[[RuntimeCredentials], Awaitable["EmulatedDiscovery"]]

_logger = logging.getLogger(__name__)

# A runtime tool that crashes or returns a contract-violating result is
# contained at the registry seam (read-tools-design §2 hardening, prep for
# runBlueprint): the loop returns this clean error rather than aborting the turn
# or leaking `str(exc)`. Distinct from the tools' own `_guarded` self-protection
# (defense in depth — both layers hold).
RUNTIME_TOOL_INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"
_RUNTIME_TOOL_INTERNAL_ERROR_MESSAGE = "That tool hit an internal error. Please try again."


@dataclass(frozen=True)
class TurnContext:
    """What a `RuntimeTool` may know about the turn it is running in.

        `turn_index` ONLY, and it must come from the loop's own computation. The two
        alternatives are both wrong: `app.py` has only `turn_index_hint`, documented as
        best-effort telemetry, so making it load-bearing introduces a TOCTOU gap; and
        re-deriving it from the store means duplicating two DIFFERENT formulas (`/turn` uses
        `messages[-1].turn_index + 1`, `/turn/resume` uses `messages[-1].turn_index`), which
        guarantees eventual disagreement.

        IT MUST NOT CARRY THE TRAIL. `_run_loop_body`'s only trail load sits ABOVE the
        round-trip loop, so a snapshot taken there contains NOTHING from the current window:
        evidence written in round 1 and cited in round 2 would fail as "unknown tool_call_id"
        on every turn, while looking correctly wired. A tool that needs the trail loads it
        itself, filtered to `turn_index`.
    """

    turn_index: int


class RuntimeTool(Protocol):
    """A model-facing tool implemented in the RUNTIME (not the MCP), intercepted in the
        loop and returning an inline `ToolResult`. `askUser` is NOT a `RuntimeTool`: it is
        TERMINAL — it pauses rather than returning a `ToolResult` — so it stays a hardcoded
        branch.

        *turn* is passed by `_run_runtime_tool` on every dispatch, keyword-optional so a tool
        that does not care about the turn simply ignores it.
    """

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult: ...


# Runtime tools are ADVERTISED (their schemas are locally authored, always
# present) but intercepted in the loop — they must NEVER be dispatched to the MCP
# under their own name (there is no such MCP tool). When one is advertised but
# not wired into `runtime_tools` (its backing stack is absent), the loop returns
# a clean local unavailable error keyed here, rather than an incoherent MCP
# unknown-tool denial (the `resolveValues` L2 precedent, generalized — §6).
_RUNTIME_TOOL_UNAVAILABLE_CODE: dict[str, str] = {
    "resolveValues": "RESOLVE_VALUES_UNAVAILABLE",
    "searchBlueprints": "RETRIEVAL_TOOL_UNAVAILABLE",
    "getBlueprint": "RETRIEVAL_TOOL_UNAVAILABLE",
    "searchKnowledge": "RETRIEVAL_TOOL_UNAVAILABLE",
    "runBlueprint": "RUN_BLUEPRINT_UNAVAILABLE",
}
_RUNTIME_TOOL_UNAVAILABLE_MESSAGE: dict[str, str] = {
    "RESOLVE_VALUES_UNAVAILABLE": "Value resolution is not available right now.",
    "RETRIEVAL_TOOL_UNAVAILABLE": "Blueprint and knowledge search is not available right now.",
    "RUN_BLUEPRINT_UNAVAILABLE": (
        "The blueprint fast path is not available right now — answer from the raw tools."
    ),
}

TurnStatus = Literal["done", "paused_ask_user", "paused_budget_cap", "stopped_hard_ceiling"]

# WHICH EXIT produced the prose the answer scrub inspected (ISSUES I1) — the
# `exit` label on `loop_answer_prose_redacted`, and nothing else. It is a
# PARAMETER of `_finish`, never derived from `status`, for the same reason
# *event* and *provenance* are: `status="done"` is reached by TWO exits (the
# no-tool-calls finish and the `answerWithTable` finish) whose disclosure
# profiles are entirely different, and a derivation could not tell them apart.
# `"pause"` covers every non-`done` finisher — the `askUser` pause, the budget
# cap, the hard ceiling and `_pause_from_runtime_tool` — because all four carry
# the same thing: best-effort partial prose from a turn that did not answer.
# `"ask_user_question"` is the one label that is NOT about `assistant_text`: it
# tags the `askUser` QUESTION, which is model prose shown to the user too.
AnswerExitLabel = Literal["no_tool_calls", "answer_with_table", "pause", "ask_user_question"]

_BUDGET_CAP_QUESTION = "This is taking a while — continue, refine, or stop?"
_BUDGET_CAP_OPTIONS = ["continue", "refine", "stop"]

# The repeated-idempotent-read guard now lives WHOLE in `loop/read_guard.py` — a
# neutral, stdlib-only leaf shared with `context/discovery_emulation.py` so that
# module no longer reaches into this one's private namespace at runtime. That is
# the home of `IDEMPOTENT_READ_TOOLS`, `idempotent_read_signature`, the window-
# scoped `ReadGuard` (state + decision) and both event payload builders. What stays
# HERE are the guard's EFFECTS, which need the session store and the trail model:
# the data-free marker `TrailEntry`, its append, and the `loop_repeated_idempotent_
# read_guarded` emission that must follow it. Imported at the top of this file.


# The shared wall-clock stamp (`data_agent/timeutil.py`), aliased to the name this
# module's ~6 call sites already use. The stamp MUST be the same string format the
# session store writes, because the two are merged and sorted together — which is why
# it is one function and not one per writer.
#
# Wall-clock `ts` is now the CONTEXT ORDERER (context/assembly.py merges the two
# streams by `(turn_index, ts, stream_rank)`), not just a display stamp. It need
# not be perfectly monotonic: `turn_index` dominates the sort, so any clock
# skew/backward step can only misorder items WITHIN a single turn — never across
# turns, and never in a way that breaks assistant/tool pairing (that is enforced
# structurally downstream), so there is no API-400 risk from a ts wobble.
_now_iso = now_iso


def _first_user_question(messages: list[TurnMessage], turn_index: int) -> str | None:
    """The first user message of *turn_index* — the turn's originating question,
    used to re-run retrieval on a resume (design §6). `None` if absent."""
    for message in messages:
        if message.turn_index == turn_index and message.role == "user":
            return message.content
    return None


def _runtime_tool_unavailable(tool_name: str, code: str) -> ToolResult:
    """A clean local error for an advertised-but-unwired runtime tool — never dispatched to
        the MCP under its own name. Shared by `resolveValues` and the three read tools.
    """
    return ToolResult(
        status="error",
        tool_name=tool_name,
        error_code=code,
        retryable=False,
        user_message=_RUNTIME_TOOL_UNAVAILABLE_MESSAGE.get(
            code, "That tool is not available right now."
        ),
        provenance=None,
        result_preview=None,
        result_full=None,
    )


def _runtime_tool_internal_error(tool_name: str) -> ToolResult:
    """A clean local error for a runtime tool that RAISED out of `handler.run`
    (S2) — the turn survives, `str(exc)` is never surfaced (logged server-side)."""
    return ToolResult(
        status="error",
        tool_name=tool_name,
        error_code=RUNTIME_TOOL_INTERNAL_ERROR_CODE,
        retryable=False,
        user_message=_RUNTIME_TOOL_INTERNAL_ERROR_MESSAGE,
        provenance=None,
        result_preview=None,
        result_full=None,
    )


def _sanitize_runtime_provenance(
    provenance: Any, tool_name: str
) -> frozenset[tuple[str, str]] | None:
    """Validate a `RuntimeTool`'s returned `provenance` BEFORE it is persisted: it must be
        `None` or a `frozenset` of `(str, str)` tuples — the exact shape
        `context/scope_filter.is_provenance_in_scope` unpacks. Anything else is coerced to
        `None` fail-closed (dropped from replay) with a server-side warning, rather than
        crashing the NEXT round-trip inside the D44 replay filter.
    """
    if provenance is None:
        return None
    if isinstance(provenance, frozenset) and all(
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], str)
        for item in provenance
    ):
        return provenance
    _logger.warning(
        "runtime tool %s returned malformed provenance (%s); coercing to None (fail-closed)",
        tool_name,
        type(provenance).__name__,
    )
    return None


@dataclass(frozen=True)
class TurnOutcome:
    """The result of one `AgentLoop.run()`/`resume()` call."""

    status: TurnStatus
    assistant_text: str | None
    pending_question: dict[str, Any] | None
    tool_calls_made: int
    # UI Slice 1 (docs/decisions/ui-slice1-enriched-result-contract.md §1):
    # additive, nullable enrichment for the SSE `result` event — the loop
    # populates these best-effort at every return site (never load-bearing for
    # correctness). All default `None` so existing construction sites stay valid
    # and an old client ignores the unknown keys (backward compatible).
    # Every read-only query the turn actually EXECUTED, in first-occurrence order
    # (was `sql`; renamed so it can never be mistaken for "the answer"). This is
    # the audit/explain list — what ran, including intermediate probes and
    # sanity checks.
    sql_executed: list[str] | None = None
    # The single query the MODEL designated as the answer, via `presentTable`
    # (composite/present_table.py). The UI runs THIS one itself, paginated, rather
    # than the model transcribing rows into its prose. `None` when the answer is a
    # scalar/single row — or when the model simply did not call the tool, since the
    # designation is advisory.
    #
    # It replaced `result_table` (a `ResultPreview` of the last successful query):
    # that shipped a fixed 20-row preview the user could not page past, and it was
    # picked by the runtime ("last successful query"), which is wrong exactly when
    # a turn resolves values or probes before answering.
    answer_sql: str | None = None
    blueprint_use: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    provenance: frozenset[tuple[str, str]] | None = None
    # 08: EVERY table the model designated, in the order it designated them, each
    # with its own optional `caption`, `blueprint_use` chip and `verification`
    # badge. Additive — the three singular fields above keep their meaning and are
    # DERIVED from `answer_tables[0]` in exactly one place (`turn_accumulators.
    # answer_envelope`), so
    # they can never disagree with the list. `None` when the turn designated no
    # table, the same `[] -> None` fork as `sql_executed`/`assumptions`.
    answer_tables: list[dict[str, Any]] | None = None
    # recordAssumptions (docs/decisions/ui-assumptions-contract.md): the
    # model-declared, plain-English assumptions behind the answer — a first-class
    # result field mirroring `sql` in EVERY respect (additive, nullable, `[] ->
    # None` fork, accumulated across budget windows at every return site).
    assumptions: list[str] | None = None


@dataclass(frozen=True)
class _CanonicalRequest:
    """One round-trip's canonical message list, plus WHICH tool results the model can
        actually READ in it.

        The second field exists because "is this result still in context?" cannot be answered
        from `messages` alone, and the repeated-read guard's trim-aware exemption depends on
        the answer. Two different things put a `tool` message with a given `tool_call_id` into
        the list: a REAL rendered result, and a data-free SENTINEL (D94's "result withheld",
        or the repeated-read nudge). A membership test over ids alone cannot tell them apart,
        and treating a sentinel id as "visible" would tell the guard a schema is readable
        while the model is looking at "result withheld … Do not retry" — with no way to
        recover it. Sentinel ids are excluded HERE, at the one place that still knows which
        render item was which.
    """

    messages: list[dict[str, Any]]
    readable_tool_call_ids: frozenset[str]


ANSWER_TABLE_BLUEPRINT_NOT_RUN_CODE = "ANSWER_TABLE_BLUEPRINT_NOT_RUN"


def _answer_table_blueprint_not_run(blueprint_id: str) -> ToolResult:
    """The nudge for `answerWithTable(blueprint_id=X)` where X never ran this turn.

        Without it the call SUCCEEDS and — because it carries `answer` — TERMINATES the turn,
        so the user gets prose with no table and the model never learns why.

        A non-`ok` status is what makes this work end-to-end: it stops the terminal exit
        firing, and `scope_filter.filter_trail`'s current-turn exemption is status-gated to
        `status != "ok"`, so the entry reaches the model this same turn instead of being
        dropped as undetermined-provenance history.

        The instructional text below is NOT what the model reads. `TrailEntry` has no
        `user_message` field at all, and `context/budget.py::_render_entry` sets it from
        `classify_denial(entry.error_code)` unconditionally — so the model sees the
        DENIAL-TABLE text. That is why `ANSWER_TABLE_BLUEPRINT_NOT_RUN` is registered in
        `dispatch/denial_mapping.py`, and why the two strings are kept in step; the string
        here reaches only non-model readers (logs, `/query/page`'s error body).
    """
    return ToolResult(
        status="error",
        tool_name=ANSWER_TABLE_TOOL_NAME,
        error_code=ANSWER_TABLE_BLUEPRINT_NOT_RUN_CODE,
        retryable=True,
        user_message=(
            f"You referenced blueprint '{blueprint_id}', but you have not run it in "
            "this turn, so there is no table to show. Call runBlueprint with that "
            "blueprint first, then call answerWithTable again."
        ),
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        # The channel that actually reaches the model. It NAMES the blueprint, which
        # the denial-table fallback cannot: `classify_denial` sees only the code, so
        # its text can say "that blueprint" but never which one.
        denial_detail=(
            f"You referenced blueprint '{blueprint_id}', but you have not run it in "
            "this turn, so there is no table to show. Call runBlueprint with "
            f"'{blueprint_id}' first, then call answerWithTable again."
        ),
    )


# The empty-designation event. It stays HERE, unlike the refusal it accompanies
# (`answer_table_no_table_designated`, now in `loop/finalization.py`): it is emitted
# inside `_resolve_answer_tables` below, on BOTH the refused and the allowance-spent
# paths, so it belongs to the resolution rather than to the decision.
ANSWER_TABLE_EMPTY_DESIGNATION_EVENT = "loop_answer_table_empty_designation"


# The blueprint-definition gate — its refusal ToolResult, its error code
# (`BLUEPRINT_DEFINITION_NOT_READ_CODE`) and the `BlueprintGate` that decides —
# now lives in `loop/blueprint_gate.py`. The gate is imported at the top of this
# file; the loop keeps only the EFFECTS (the `_maybe_start_summary` skip, the
# dispatch-chain short-circuit, the trail entry the refusal is written into).


# ---------------------------------------------------------------------------
# Finalization enforcement (Release 1, docs/decisions/release-1/
# 05-finalization-enforcement.md). This is where `analysisState` gets teeth.
#
# THE INVARIANT IS SCOPED (05 §F.1): no intent ends `pending` on any turn that
# reaches a TERMINAL outcome (`done` / `stopped_hard_ceiling`). A turn abandoned
# at an `askUser`/budget-cap/blueprint pause, or whose resume loses a CAS race, is
# a NON-TERMINATED turn and legitimately leaves its intents `pending` — an
# unscoped assertion would fail against any real store.
#
# ENFORCEMENT APPLIES ONLY TO THE LIVE STATE (05 §A). Everything below reads the
# window-local loaded through `live_analysis_state`, so a state left behind by an
# abandoned earlier turn cannot refuse an unrelated later turn (and cannot have
# `ENFORCEMENT_EXHAUSTED` written onto its record by one).
#
# THE DECISION LAYER LIVES IN `loop/finalization.py`: the two refusals, the two
# nudges, `pending_intents`/`refreshed_analysis_state`, the answer-shape events and
# the two window-scoped objects (`AnswerShapeCounter`, `FinalizationGate`). What
# stays below is the ENFORCEMENT ITSELF — the exits, their precedence, the
# `tool_result` rewrites, the nudge's one-round-trip lifecycle and the force-block
# writes, all of which are the loop's own effects and control flow.
# ---------------------------------------------------------------------------

# How many SURPLUS `updateAnalysisState` calls (beyond `MAX_STATE_CALLS`) in one
# model response are answered with a persisted rejection entry before the rest are
# dropped unanswered. Two, not one: the model may legitimately be mid-correction,
# and one rejection reads as an accident where two read as a rule. Each rejection
# costs a full `append_trail_entry` CAS write plus an entry pinned in the
# current-turn budget region, which is why the number is small and fixed rather
# than "however many the model sent" (03 §E.2 bounded the state WRITES at two but
# left the rejection WRITES unbounded).
_MAX_SURPLUS_STATE_REJECTIONS = 2

# How many of a turn's full results the figure-corroboration pass (09 §D.4) may read
# back from the KV before giving up. A CAP, not a budget: the pass short-circuits on
# the first match, so this only bounds the miss case — a long turn holding a dozen
# results, where reading all of them would put a dozen store round-trips on the
# terminal path to establish a fact that is optional by construction.
_MAX_CORROBORATION_READS = 4


class _NoLiveStateToForceError(Exception):
    """Raised from inside the force-block merge when the live state vanished between the
        loop's read and the store's write. Aborts the write with nothing persisted, rather
        than resurrecting a state the model never saw.
    """


# K2 (live-eval L5): the model tags a call with `serves_intent` BEFORE it has
# declared any intents. `split_serves_intent` strips the tag and reports
# `no_live_state` — degrade-not-fail, so the work still runs — but the ONLY record
# of the drop was an observer event the model cannot see. The observed
# consequence: the model believes its work is tracked, never calls
# `updateAnalysisState`, and the turn finishes untracked.
#
# This is the feedback the model was missing. It names the tool that repairs the
# situation AND the deadline, because the deadline is real: `analysis_state.py`
# locks late initialization once a SUBSTANTIVE tool has run (`SUBSTANTIVE_TOOLS`,
# `find_locking_tool`), so "declare them later" is only true until then.
#
# ⚠ THE ADVICE IS ONLY TRUE WHILE THAT DOOR IS OPEN, so the note is GATED on it:
# it fires only when NO substantive tool has run this turn (see `_run_loop_body`'s
# `substantive_ran`). Once one has, `updateAnalysisState` would be refused
# NON-RETRYABLY, and telling the model to call it would turn a silent drop into an
# instructed dead end. In that state no true corrective advice exists — the turn
# cannot be tracked any more — so the note is SUPPRESSED and the pre-slice
# behaviour (silent drop + `loop_intent_tag_dropped`) stands. Telemetry is
# unaffected either way: every drop is still reported.
_INTENT_TAG_DROPPED_NOTE = (
    "Note: your serves_intent tag was ignored — no intents are declared yet. Call "
    "updateAnalysisState to declare your intents before your next substantive call "
    "(runQuery/runBlueprint), or the turn will finish untracked."
)


def _tool_trail_entry_to_canonical(
    entry: dict[str, Any], intent_note_call_ids: Collection[str] = ()
) -> list[dict[str, Any]]:
    """One rendered tool-trail entry -> a synthetic `[assistant-with-tool_calls,
        tool-result]` canonical pair.

        Required because D22 discards the model's original free text around a tool call, so
        replay must synthesize a minimal, API-valid exchange rather than replaying the
        original verbatim.

        *intent_note_call_ids* (K2, the silent-drop feedback seam): the `tool_call_id`s
        whose result must carry `_INTENT_TAG_DROPPED_NOTE` — the model tagged the call with
        `serves_intent` while NO analysisState existed, so the tag was stripped and, until
        now, nothing told it. See `_run_loop_body`'s window-local of the same name for the
        once-per-round selection and the one-round-trip lifetime. Defaults to empty so the
        emulated-discovery caller (and every existing test) renders byte-identically.
    """
    tool_call_id = entry["tool_call_id"]
    assistant_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": entry["tool_name"],
                    "arguments": json.dumps(entry.get("args") or {}),
                },
            }
        ],
    }
    # D94 Part 1: an entry flagged `withheld_sentinel` is a synthetic,
    # non-data-bearing sentinel injected by `context/assembly.py` for a
    # current-turn `ok`+`None` stranded result — its `content` is used verbatim as
    # the tool result (never JSON-wrapped with a payload), filling the dangling
    # tool_call's required slot to break the retry-until-budget-cap loop. The
    # explicit flag (not a bare `content` key) keeps a future `_render_entry` field
    # from ever silently rerouting a normal tool entry to verbatim rendering.
    if entry.get("withheld_sentinel"):
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": entry["content"],
        }
    else:
        content: dict[str, Any] = {
            "status": entry["status"],
            "error_code": entry.get("error_code"),
            # S4: the static, PII-safe denial message (never raw MCP error
            # text) so the model can see WHY a retryable call failed and
            # self-correct — see context/budget.py::_render_entry.
            "user_message": entry.get("user_message"),
            "result_preview": entry.get("result_preview"),
        }
        # A SUCCESSFUL, D56-verified runBlueprint result is the trusted answer for
        # this intent. Surface an explicit, in-band marker + a terse human-readable
        # note so the model treats it as authoritative and goes straight to the final
        # answer — it must NOT re-derive/re-verify the same intent with ad-hoc
        # runQuerys (see prompts.py). Present ONLY for a verified blueprint result;
        # a runQuery, a denied/errored blueprint, or a blueprint that failed verify
        # never carries the flag, so those tool messages are byte-identical to before.
        if entry.get("authoritative"):
            content["authoritative"] = True
            # J6: an EMPTY blueprint result is still the authoritative answer for
            # its intent ("there are none" is an answer, and re-deriving it with
            # ad-hoc runQuerys is the loop this marker exists to prevent) — but it
            # was NOT verified. The D56 grain teeth are `row_count ==
            # distinct_grain_count`, which at zero rows is `0 == 0` and passes for
            # every blueprint ever written. Telling the model "verified" there is
            # the same over-claim the badge just stopped making, aimed at the one
            # reader that will repeat it in prose. The no-re-derivation instruction
            # is UNCHANGED; only the word "verified" is withdrawn.
            #
            # `is_zero_row_count` (not `== 0`) is the slice's shared bool-exclusion
            # predicate: `isinstance(True, int)`, so a poisoned `row_count: false`
            # would otherwise read as an empty result and retract the claim over a
            # value that says nothing about the row count. Same rule the badge
            # applies, from the same function.
            if is_zero_row_count((entry.get("result_preview") or {}).get("row_count")):
                content["note"] = (
                    "Blueprint result — authoritative; do not re-derive with "
                    "additional queries. It returned NO ROWS, so nothing was "
                    "verified: report the empty result as the answer, and do not "
                    "describe it as verified."
                )
            else:
                content["note"] = (
                    "Verified blueprint result — authoritative; do not re-derive with "
                    "additional queries."
                )
        # J7 — the data-anchored window note, on its OWN key beside `note` rather than
        # appended to it. Three reasons it does not share: it is independent of
        # `authoritative` (an unverified blueprint's window is anchored the same way), the
        # J6a empty-result branch above is a careful sentence that string-concatenation
        # would blur, and a distinct key is what lets a test assert one is present without
        # asserting the other's exact wording. The runtime never sets both keys to
        # overlapping claims: `note` says whether to TRUST the rows, `window_note` says
        # what period they COVER.
        if entry.get("window_note"):
            content["window_note"] = entry["window_note"]
        # K2: the corrective note for a `serves_intent` tag dropped because no
        # analysisState existed. It gets its OWN key, never `note` (and never
        # `window_note`): the three are independent — a verified blueprint result can
        # itself carry a dropped tag — and overwriting the authoritative note would
        # trade a do-not-re-derive instruction for a bookkeeping one.
        #
        # Only this JSON branch carries it — a `withheld_sentinel` entry renders its
        # content VERBATIM (D94), and appending to that string would corrupt a contract
        # other code matches on. A tagged call that renders as a sentinel therefore
        # loses this round's note; that is self-correcting, because the selection at the
        # drop site re-fires on any LATER round where the model tags again without a
        # state.
        if tool_call_id in intent_note_call_ids:
            content["runtime_note"] = _INTENT_TAG_DROPPED_NOTE
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(content, default=str),
        }
    return [assistant_message, tool_message]


def _assembled_to_canonical(
    messages: list[dict[str, Any]], intent_note_call_ids: Collection[str] = ()
) -> list[dict[str, Any]]:
    """`AssembledContext.messages` (the interleaved render list) -> the canonical
        `ModelClient.send_turn` message shape.

        Four render shapes: the base `system` message; a `user` message (a prior-turn
        question, the current question, an askUser answer, or the retrieval block); an
        `assistant` TEXT message passed straight through; and a `tool` render item that
        expands to a synthetic `assistant(tool_calls)` + `tool(result)` PAIR.

        Defensive dedup: the API requires every `tool_call_id` in a turn to be UNIQUE with
        exactly one matching `tool` response, so a legacy or corrupt trail — or a
        paused-and-resumed DAG that re-appended a colliding id — would emit two `tool`
        messages with one id and abort the turn with a 400. A duplicate is dropped here,
        keeping the FIRST: fail-closed toward a valid, if lossy, replay.
    """
    canonical: list[dict[str, Any]] = []
    seen_tool_call_ids: set[str] = set()
    for message in messages:
        role = message["role"]
        if role == "system":
            canonical.append({"role": "system", "content": message["content"]})
        elif role == "user":
            # A prior-turn question, the current question, an askUser answer, or the
            # retrieval cards block — all replay as ordinary `user` content. None is
            # a second `system` message, so the base prompt stays the sole one.
            canonical.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            # A prior turn's free-text answer (`TurnMessage` role="assistant"), now
            # interleaved into history in its chronological slot. Passed straight
            # through as a plain assistant message (no tool_calls — those flow via
            # the `tool` render-item pair-expansion branch below).
            canonical.append({"role": "assistant", "content": message["content"]})
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id in seen_tool_call_ids:
                _logger.warning(
                    "dropping duplicate tool_call_id %r from replay (keeping the first) "
                    "to keep the message list API-valid",
                    tool_call_id,
                )
                continue
            if isinstance(tool_call_id, str):
                seen_tool_call_ids.add(tool_call_id)
            canonical.extend(_tool_trail_entry_to_canonical(message, intent_note_call_ids))
        else:  # pragma: no cover - assemble only ever emits system/user/assistant/tool
            raise ValueError(f"Unexpected assembled-context message role: {role!r}")
    return canonical


class AgentLoop:
    """The turn state machine — wires `ModelClient`, `ToolDispatcher`,
    `ContextAssembler`, and `SessionStore` into one askUser/budget-aware loop."""

    def __init__(
        self,
        *,
        model_client: ModelClient,
        tool_dispatcher: ToolDispatcher,
        context_assembler: ContextAssembler,
        session_store: SessionStore,
        tools_provider: ToolsProvider,
        max_loop_iterations: int,
        max_wall_clock_seconds: float,
        max_budget_windows: int,
        max_token_spend: int | None = None,
        request_token_budget: int | None = None,
        request_budget_pinned_recent_tool_pairs: int = 3,
        max_tool_calls_per_iteration: int = 8,
        clock: Callable[[], float] = time.monotonic,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        runtime_tools: Mapping[str, RuntimeTool] | None = None,
        blueprint_executor: Any = None,
        discovery_emulation_provider: EmulatedDiscoveryProvider | None = None,
        progress_summarizer: ProgressSummarizer | None = None,
        # Answer-table lifecycle seams (D72, hooks/answer_table.py). Defaults to an
        # EMPTY registry — dormant, every hook point a no-op, byte-identical to not
        # having them. `app.py` does not populate it; activating one is a
        # deliberate registration, never a config flag.
        answer_table_hooks: AnswerTableHooks | None = None,
        # The ANSWER JUDGE (09). `None` = the feature is ABSENT and every terminal
        # exit is byte-identical to before it existed — which is what every Layer-1
        # loop test that does not wire one gets, and what `app.py` wires when
        # `answer_judge_enabled` is False. A judge object that is itself disabled
        # would also approve everything, but it would still be an object on the
        # terminal path; `None` is the stronger statement and the cheaper one.
        answer_judge: AnswerJudge | None = None,
        # Seconds of wall clock a judge rejection needs to be worth making (09 §H).
        # Below this the judge is SKIPPED and the answer ships: a rejection issued at
        # 168s of a 180s window buys a regeneration the guard cuts off mid-round, and
        # the user is then asked "continue, refine, or stop?" having been shown
        # nothing — a serviceable answer converted into an empty pause.
        answer_judge_min_headroom_seconds: float = 25.0,
        # How many preview rows the judge's brief carries per result. THE SAME NUMBER
        # THE MODEL'S CONTEXT USES, and that is the entire requirement (09 §D.3) —
        # a judge holding more rows than the model held faults it for the preview cap.
        # Defaulted to `RuntimeSettings.preview_row_count`'s own default so a Layer-1
        # loop test that wires neither still agrees with a deployment that wires both;
        # `app.py` passes `settings.preview_row_count` to this AND to the assembler.
        preview_row_count: int = 20,
    ) -> None:
        self._model_client = model_client
        self._tool_dispatcher = tool_dispatcher
        self._context_assembler = context_assembler
        self._session_store = session_store
        self._tools_provider = tools_provider
        # Emulated-discovery injection (context/discovery_emulation.py): when wired
        # (app.py, gated on `discovery_emulation_enabled`), `_run_loop_body` invokes this
        # ONCE per budget window BEFORE the model loop to emulate `listDatabases`+
        # `listTables` and splices the synthetic assistant/tool pairs into every
        # per-round-trip rebuild (ephemeral) AND seeds the repeated-idempotent-read
        # guard with their signatures. `None` (default, Layer-1 loop tests) =
        # feature absent → byte-identical.
        self._discovery_emulation_provider = discovery_emulation_provider
        # The runtime-tool registry (read-tools-design §2): model-facing tools
        # implemented in the runtime (`resolveValues` + the three read tools),
        # intercepted here and never dispatched to the MCP under their own name.
        # Empty by default so Layer-1 loop tests that exercise only MCP tools
        # need not wire any. `askUser` is NOT here — it is terminal (see below).
        self._runtime_tools: Mapping[str, RuntimeTool] = runtime_tools or {}
        # The `BlueprintExecutor` (Slice C, §2.5) — reached ONLY on a mid-DAG
        # resume (`AgentLoop.resume` re-enters it at `awaiting_node`). `None` when
        # runBlueprint is not wired; a blueprint mid-DAG checkpoint can then never
        # exist, so the re-entry branch is inert.
        self._blueprint_executor = blueprint_executor
        self._max_loop_iterations = max_loop_iterations
        self._max_wall_clock_seconds = max_wall_clock_seconds
        self._max_budget_windows = max_budget_windows
        # Per-window SPEND ceiling: Σ(prompt + completion) over this window's
        # round-trips, as the provider reports it. NOT an occupancy limit — see
        # `request_token_budget` immediately below for that, and
        # `loop/budget_guard.py`'s module docstring for why the distinction is
        # load-bearing. `None` (Layer-1 loop tests) = no spend ceiling; `app.py`
        # wires `settings.max_window_token_spend`.
        self._max_token_spend = max_token_spend
        # Total-request fit budget (2026-08 fix): the absolute token cap on the
        # FULL canonical list handed to `send_turn`, applied in
        # `_build_canonical_messages` as the final step so the base prompt is never
        # front-truncated out of the model window. `None` (Layer-1 loop tests that
        # do not wire it) disables the fit step — byte-identical to before it
        # existed. `app.py` wires `settings.request_token_budget()`.
        self._request_token_budget = request_token_budget
        # K (interleave blocker fix, 2026-08): how many of the CURRENT turn's
        # most-recent tool pairs `fit_request_to_budget` pins. The current turn's
        # tool pairs sit in the tail (their `ts` follows the question) in the
        # interleaved layout; pinning only the most-recent K — not ALL of them —
        # lets a runaway turn's OLDER pairs be trimmed so the request stays bounded,
        # while K protects the D94 re-fetch/self-correct loop. Default 3.
        self._request_budget_pinned_recent_tool_pairs = request_budget_pinned_recent_tool_pairs
        self._max_tool_calls_per_iteration = max_tool_calls_per_iteration
        self._answer_judge = answer_judge
        self._answer_judge_min_headroom_seconds = answer_judge_min_headroom_seconds
        self._preview_row_count = preview_row_count
        self._clock = clock
        self._observer = observer
        # R7: the ONE span the loop opens itself — the blueprint approval-RESUME
        # re-entry (`_resume_blueprint`), which bypasses `RunBlueprintTool` and so
        # bypasses the envelope that would otherwise open it. Optional exactly like
        # every other tracer seam (`ToolDispatcher`, `RuntimeToolBase`): `None`
        # (Layer-1 loop tests, no Phoenix) means the span is simply never created.
        self._tracer = tracer
        # LLM-generated progress summaries (opt-in, `progress_summary_enabled`).
        # `None` (default) → the feature is behaviorally absent: `_summary_tasks`
        # stays empty, so the window's `finally` takes no extra tick and the cancel
        # is a no-op. When wired (app.py, gated on the flag + an OpenAI
        # key), each tool CALL fires a FIRE-AND-FORGET summarization task tracked in
        # `_summary_tasks` so a still-pending task can be best-effort cancelled when
        # the turn ends (never awaited before dispatch, never blocking the result).
        self._progress_summarizer = progress_summarizer
        self._answer_table_hooks = answer_table_hooks or AnswerTableHooks()
        self._summary_tasks: set[asyncio.Task[None]] = set()

    async def run(
        self, *, session_id: str, credentials: RuntimeCredentials, user_message: str
    ) -> TurnOutcome:
        """Start a brand-new external turn (design §4.1 step 1-3, first window)."""
        doc = await self._session_store.get_or_create_session(session_id)
        turn_index = (doc.messages[-1].turn_index + 1) if doc.messages else 0
        await self._session_store.append_message(
            session_id,
            TurnMessage(turn_index=turn_index, role="user", content=user_message, ts=_now_iso()),
        )
        # B3: ONE per-turn-scoped `ModelClient` handle, used for every `send_turn`
        # of this external turn (across all budget-window resumes) — never
        # `self._model_client` directly, whose Responses/Chat fallback stickiness
        # (D71 §4.2) is shared state across concurrent turns/sessions and would
        # otherwise let one turn stomp another's mid-turn. See
        # `model/client.py::begin_turn_client`.
        turn_model_client = begin_turn_client(self._model_client)
        return await self._run_loop_body(
            session_id=session_id,
            credentials=credentials,
            window_count=1,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=user_message,
        )

    async def resume(
        self, *, session_id: str, credentials: RuntimeCredentials, answer: str
    ) -> TurnOutcome:
        """CAS-consume the pause checkpoint (D45) and continue the loop (design §4.1 step 4).

        Raises:
            AlreadyConsumedError: no pending checkpoint, or it was already consumed.
            CASMismatchError: a concurrent resume already won the race.
        """
        doc, cas = await self._session_store.get_session_with_cas(session_id)
        checkpoint = doc.pause_checkpoint  # may be None/consumed; resume_checkpoint enforces D45
        updated_doc = await self._session_store.resume_checkpoint(session_id, cas, answer)
        turn_index = updated_doc.messages[-1].turn_index if updated_doc.messages else 0

        prior_window_count = checkpoint.budget_window_count if checkpoint else 1
        pause_reason = checkpoint.reason if checkpoint else "askUser"

        # Blueprint mid-DAG resume (D45, §2.5): a checkpoint carrying a
        # `blueprint_id` + an `awaiting_node` re-ENTERS the executor at that node
        # with the completed SCALAR outputs rehydrated — completed nodes never
        # re-run (the CAS-consume above is the exactly-once guarantee). A slot
        # `askUser` pause (`awaiting_node is None`) is NOT this path — it re-runs
        # via the model loop below, the Slice-B contract.
        if (
            checkpoint is not None
            and checkpoint.blueprint_id is not None
            and checkpoint.awaiting_node is not None
            and self._blueprint_executor is not None
        ):
            return await self._resume_blueprint(
                session_id=session_id,
                credentials=credentials,
                checkpoint=checkpoint,
                answer=answer,
                turn_index=turn_index,
                window_count=prior_window_count,
            )

        if pause_reason == "budget_cap":
            normalized = answer.strip().lower()
            if normalized.startswith("stop"):
                # 05 §F — the THIRD `done` return, and the one that inherits
                # nothing: it returns from inside `resume()` BEFORE `_run_loop_body` is
                # ever entered, with `tool_calls_made=0`, so it needs its own
                # force-block call. The turn reaches a terminal outcome here, so
                # the scoped invariant applies and every surviving `pending` intent
                # is recorded `USER_STOPPED`. The §A turn gate applies here too —
                # `live_analysis_state` is what stops a state left behind by an
                # abandoned EARLIER turn being rewritten by this one's stop.
                await self._force_block_pending_intents(
                    session_id=session_id,
                    turn_index=turn_index,
                    state=live_analysis_state(updated_doc, turn_index),
                    reason_code="USER_STOPPED",
                )
                # M2 (approved behaviour change, 2026-08-17): THE TRAIL REBUILD NOW
                # SERVES BOTH PATHS. This return used to carry `assumptions=None` /
                # `answer_tables=None` on the rationale that "the in-loop
                # accumulators are gone with the prior window" — the premise is
                # true, but the conclusion was not: the two producers 20 lines
                # below (`_compute_turn_assumptions` / `_compute_turn_answer_tables`)
                # rebuild exactly those facts from the persisted trail, and the
                # "continue" answer has been getting them all along. So a user who
                # answered "stop" lost the table and the assumptions that a user who
                # answered "continue" kept, from the same trail, on the same turn.
                # Same source, same turn, same two calls — now on both branches.
                stop_assumptions = await self._compute_turn_assumptions(session_id, turn_index)
                stop_tables, _stop_blueprint_runs = await self._compute_turn_answer_tables(
                    session_id, turn_index
                )
                # Read the outcome fields THROUGH a `TurnAccumulators` rather than
                # hand-shaping them here: it owns the `[] -> None` forks (§1 fork 1)
                # and `answer_envelope` is THE ONE PLACE the envelope is computed
                # (08 §E). Nothing is invented by doing so — `answer_sql` /
                # `blueprint_use` / `verification` are PROJECTIONS of the designated
                # tables, never independently accumulated, so they appear exactly
                # when a rebuilt table supplies them and stay `None` when the trail
                # designated none. The three seeds this cannot supply (`sql`, and the
                # turn-level `blueprint_use`/`verification` no-table fallbacks) are
                # the same three the continue path below cannot supply either, and
                # for the same reason: only the blueprint approval-resume holds a
                # `result_full` that no trail entry has been written for yet.
                stop_accum = TurnAccumulators(
                    assumptions=stop_assumptions, answer_tables=stop_tables
                )
                stop_envelope = stop_accum.envelope()
                return TurnOutcome(
                    status="done",
                    assistant_text=("Stopping here — here is what I found before the budget cap."),
                    pending_question=None,
                    tool_calls_made=0,
                    # UI Slice 1: a `done` return — surface the turn's lineage from
                    # the trail (the fail-closed source of truth).
                    provenance=await self._compute_turn_provenance_union(session_id, turn_index),
                    answer_sql=stop_envelope.answer_sql,
                    blueprint_use=stop_envelope.blueprint_use,
                    verification=stop_envelope.verification,
                    answer_tables=stop_envelope.answer_tables,
                    assumptions=stop_accum.assumptions,
                )
            window_count = prior_window_count + 1  # D55: "continue"/"refine" grants a fresh window
        else:
            window_count = prior_window_count  # askUser resume is not a new budget grant

        # Retrieval re-runs on the ORIGINAL turn's question (design §6): the
        # first user message of this turn (an askUser answer is appended as a
        # later user message of the SAME turn_index — the originating question
        # is the first). `None` when it cannot be found → retrieval simply does
        # not run on this resume (graceful).
        question = _first_user_question(updated_doc.messages, turn_index)
        # Live/history parity: rehydrate any assumptions the model recorded in an
        # earlier window of this turn (before the askUser / budget-cap pause) from
        # the trail, so the resumed turn's live `result` event carries the same
        # assumptions `project_history` reconstructs from the same trail. Without
        # this seed, live and history would disagree for a paused-then-resumed turn.
        seed_assumptions = await self._compute_turn_assumptions(session_id, turn_index)
        # The WHOLE designated set, not just the primary (08 §M): a three-part
        # answer that paused must come back with three tables. The blueprint-run map
        # rides along so a blueprint that ran BEFORE the pause stays designatable.
        seed_answer_tables, seed_blueprint_runs = await self._compute_turn_answer_tables(
            session_id, turn_index
        )
        # B3 per-turn handle — see `model/client.py::begin_turn_client`.
        turn_model_client = begin_turn_client(self._model_client)
        return await self._run_loop_body(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            # THREE of the six seeds, and the absence of the other three is a
            # decision, not an omission: `sql`, `blueprint_use` and `verification`
            # are turn-level enrichment that only the BLUEPRINT approval-resume
            # can carry forward, because only it holds a `result_full` that no
            # trail entry has been written for yet. This path replays the trail,
            # and the trail's own enrichment is already reflected in what
            # `_compute_turn_answer_tables` returns.
            accumulators=TurnAccumulators(
                assumptions=seed_assumptions,
                answer_tables=seed_answer_tables,
                blueprint_runs=seed_blueprint_runs,
            ),
        )

    async def _build_canonical_messages(
        self,
        session_id: str,
        column_scope: frozenset[str],
        current_turn_index: int,
        *,
        question: str | None,
        user_id: str | None,
        retrieval_memo: dict[tuple[str, str], Any],
        withheld_call_ids: set[str],
        discovery_canonical: list[dict[str, Any]] | None = None,
        finalization_nudge: str | None = None,
        intent_note_call_ids: Collection[str] = (),
    ) -> _CanonicalRequest:
        """Rebuild the canonical `send_turn` message list for ONE round-trip (D45: rebuilt
                every round-trip, never carried across a pause).

                *current_turn_index* exempts the CURRENT turn's own denied/errored entries —
                whose provenance is always `None` — from D44's strict replay drop, so the model
                can self-correct. Cross-turn D44 is unchanged, and a denied entry carries no
                result rows regardless.

                *question*/*user_id*/*retrieval_memo* thread retrieval through `assemble`. The
                memo is turn-window-local, so retrieval embeds at most ONCE per window despite
                the per-round-trip rebuild; *withheld_call_ids* is the same pattern for the D94
                diagnostic. Both are inert when no retrieval pipeline is wired.

                *discovery_canonical* is the per-window emulated-discovery pair list, computed
                ONCE in `_run_loop_body`. It is spliced immediately AFTER the CURRENT turn's
                question (the LAST `user` message), so the turn reads sequentially — question,
                then the discovery the model "already did" for it, then its own work — and so it
                sits inside the range `fit_request_to_budget` pins as the current turn (droppable
                only under real pressure). Splicing after the leading `system` run instead hoists
                every pair ABOVE turn 0's question and exposes them to prior-turn trimming; that
                position remains only as the fallback when there is no `user` message at all.

                *finalization_nudge* shares that splice site and never-persisted posture but NOT
                its lifetime: it lives EXACTLY ONE ROUND-TRIP (the caller clears it immediately
                after this call), because a once-per-window value would repeat the nudge forever,
                including after the intents were closed, and — being anchored at the tail — would
                migrate to be the newest message on every rebuild.

                *intent_note_call_ids* (K2) shares the nudge's EXACTLY-ONE-ROUND-TRIP lifetime
                and for the same reason — the caller clears it immediately after this call, so a
                note the model has already been shown is not re-attached to the same tool result
                on every later rebuild of the window. Unlike the nudge it is not a standalone
                message: it rides the tool result of the call that was tagged (see
                `_tool_trail_entry_to_canonical`), which is what makes it legible as feedback ON
                that call.

                SPLICE ORDER, which this loop owns as the later insertion: `ContextAssembler`
                inserts the `analysisState` block immediately BEFORE the current question, and the
                nudge is appended at the TAIL, AFTER that — appending it first would make it the
                last `user` message and land the state block after the question. A trailing `user`
                message is safe there: `_current_turn_start` anchors on the first `user` after the
                last plain-assistant answer, so the current-turn pin does not move.
        """
        assembled = await self._context_assembler.assemble(
            session_id,
            column_scope,
            current_turn_index=current_turn_index,
            user_message=question,
            user_id=user_id,
            retrieval_memo=retrieval_memo,
            withheld_call_ids=withheld_call_ids,
            observer=self._observer,
        )
        # The ids whose render item is a DATA-FREE SENTINEL, not a readable result
        # (D94's withheld marker, or the repeated-read "you already have this"
        # nudge). Captured here, from the RENDER items, because that is the last
        # point at which the two are distinguishable: `_assembled_to_canonical`
        # flattens both into an identically-shaped `{"role": "tool", ...}` message,
        # after which only fragile content-string matching could tell them apart.
        sentinel_tool_call_ids = {
            message["tool_call_id"]
            for message in assembled.messages
            if message.get("role") == "tool"
            and message.get("withheld_sentinel")
            and isinstance(message.get("tool_call_id"), str)
        }
        canonical = _assembled_to_canonical(assembled.messages, intent_note_call_ids)
        if discovery_canonical:
            # The emulated pairs are spliced in AFTER `_assembled_to_canonical`'s
            # §6.2 duplicate-`tool_call_id` dedup already ran over the real trail, so
            # a (pathological) persisted trail entry whose id collided with an
            # `emulated-listTables-<db>` id would otherwise emit TWO `tool` messages
            # with one id → an API 400 that poisons every round-trip. Drop any
            # emulated PAIR whose `tool_call_id` is already present in the real
            # canonical list (keep the real one), so the spliced result stays
            # id-unique by construction.
            existing_tool_call_ids = {m["tool_call_id"] for m in canonical if m["role"] == "tool"}
            deduped_discovery: list[dict[str, Any]] = []
            # discovery_canonical is a flat run of [assistant, tool] pairs; the tool
            # message of each pair carries the shared id.
            for pair_start in range(0, len(discovery_canonical), 2):
                pair = discovery_canonical[pair_start : pair_start + 2]
                tool_call_id = next(
                    (m.get("tool_call_id") for m in pair if m["role"] == "tool"), None
                )
                if tool_call_id in existing_tool_call_ids:
                    _logger.warning(
                        "skipping emulated discovery pair with tool_call_id %r — it collides "
                        "with a real trail entry; keeping the real one to stay API-valid",
                        tool_call_id,
                    )
                    continue
                deduped_discovery.extend(pair)
            # Splice immediately AFTER the SESSION'S FIRST question, so the emulated
            # discovery appears ONCE, at the beginning, and STAYS there.
            #
            # Anchoring on the LAST `user` message instead made the pairs migrate:
            # they are ephemeral (never persisted), so every rebuild re-spliced them
            # after whatever the newest question was. Mid-session the model saw a
            # fresh block of listDatabases/listTables appear AFTER it had already
            # fetched schemas — discovery arriving later than the work it was meant
            # to precede.
            #
            # The anchor is the end of the FIRST CONTIGUOUS RUN of `user` messages,
            # not simply the first `user` message, because
            # `context/assembly.py::_insert_retrieval` inserts the retrieval-cards
            # block as a `user` message IMMEDIATELY BEFORE the current question. On
            # the very first turn that block therefore PRECEDES turn-0's question and
            # is itself the first `user` message — splicing after it would drop the
            # pairs between the cards and the question they belong to. Consuming the
            # whole contiguous run lands after the question in both shapes:
            #   first turn : [cards, q0]           -> after q0
            #   later turn : [q0] then tool/assistant -> after q0
            first_user = next(
                (i for i, m in enumerate(canonical) if m["role"] == "user"), None
            )
            if first_user is not None:
                insert_at = first_user
                while insert_at < len(canonical) and canonical[insert_at]["role"] == "user":
                    insert_at += 1
            else:
                # No dialogue at all (Layer-1 assemble) — fall back to after the
                # leading `system` run so the base prompt stays the pinned head.
                insert_at = 0
                while insert_at < len(canonical) and canonical[insert_at]["role"] == "system":
                    insert_at += 1
            canonical[insert_at:insert_at] = deduped_discovery

        # The finalization nudge, at the TAIL (05 §D.1 — see the docstring for why
        # the order is this way round, and §B.2 for why it is a `user` message
        # rather than a synthetic tool result). Spliced BEFORE the fit below so it
        # is counted and pinned like the current question it follows, never added
        # to a request that was already fitted without it.
        if finalization_nudge:
            canonical.append({"role": "user", "content": finalization_nudge})

        # Conversation dialogue is now interleaved INTO `assembled.messages` by
        # `ContextAssembler.assemble` (it reads `doc.messages` and merges the two
        # streams chronologically), including the D44 `filter_messages` scope gate —
        # so there is no longer a separate append of prior user/assistant messages
        # here. `assembled.messages` already IS the full interleaved request minus
        # the discovery splice above and the fit below.

        # Total-request fit (2026-08 fix, the core of this change): the assembled
        # list above has no total token budget of its own — the trail compaction
        # bounds only the trail, and the base prompt / retrieval+summary context /
        # appended conversation are all added afterward, so after N turns the FULL
        # request grew past the model context window. Sent as an ordinary LEADING
        # message, the base prompt was then FRONT-truncated out first (the dropped
        # system-prompt bug). Fit the whole list to `request_token_budget` here —
        # the single site every `send_turn` payload passes through — dropping the
        # OLDEST middle units (oldest trail pairs / conversation turns) while
        # pinning the base prompt at [0] and the current question at the tail, and
        # preserving assistant<->tool pairing. Never silent: a drop is logged
        # (structured) and emitted as a guardrail span event. `None` budget (loop
        # tests that do not wire it) skips the fit entirely (byte-identical).
        if self._request_token_budget is not None:
            fit = fit_request_to_budget(
                canonical,
                token_budget=self._request_token_budget,
                pinned_recent_tool_pairs=self._request_budget_pinned_recent_tool_pairs,
                # Pin the emulated-discovery pairs (invariant 7): anchored at the
                # session's first question they are prior-turn trail, so the tier-0
                # sweep would drop them first — stranding the model with a guard that
                # says "already served" for a listing it can no longer see.
                pinned_tool_call_ids=frozenset(
                    m["tool_call_id"]
                    for m in (discovery_canonical or [])
                    if m["role"] == "tool"
                ),
            )
            if fit.dropped_messages:
                _logger.warning(
                    "request-budget trim (session=%s): dropped %d message-unit(s) "
                    "(%d messages, ~%d tokens; by kind: %s) to fit the model window "
                    "(budget=%d tokens, kept ~%d tokens)",
                    session_id,
                    fit.dropped_units,
                    fit.dropped_messages,
                    fit.dropped_tokens,
                    dict(fit.dropped_by_kind),
                    self._request_token_budget,
                    fit.kept_tokens,
                )
                self._observer(
                    "loop_request_budget_trimmed",
                    {
                        "dropped_units": fit.dropped_units,
                        "dropped_messages": fit.dropped_messages,
                        "dropped_tokens": fit.dropped_tokens,
                        "dropped_conversation": fit.dropped_by_kind.get("conversation", 0),
                        "dropped_trail": fit.dropped_by_kind.get("trail", 0),
                        "dropped_retrieval": fit.dropped_by_kind.get("retrieval", 0),
                        "dropped_summary": fit.dropped_by_kind.get("summary", 0),
                        "kept_tokens": fit.kept_tokens,
                        "budget": self._request_token_budget,
                    },
                )
            canonical = fit.messages
        # Computed AFTER the fit, so a pair the budget dropped is correctly reported
        # as unreadable — that trim is exactly the condition the guard exemption
        # exists to detect. Sentinel ids are subtracted rather than never added, so
        # a message that survived the fit but says only "result withheld" is not
        # mistaken for the result it replaced.
        readable_tool_call_ids = frozenset(
            message["tool_call_id"]
            for message in canonical
            if message.get("role") == "tool"
            and isinstance(message.get("tool_call_id"), str)
            and message["tool_call_id"] not in sentinel_tool_call_ids
        )
        return _CanonicalRequest(
            messages=canonical, readable_tool_call_ids=readable_tool_call_ids
        )

    async def _compute_turn_provenance_union(
        self, session_id: str, turn_index: int
    ) -> frozenset[tuple[str, str]] | None:
        """Union of every `TrailEntry.provenance` produced at *turn_index*, across every budget
                window of this external turn — the tag applied to that turn's final assistant
                `TurnMessage` (D44). Fail-closed: any undetermined (`None`) tool-result provenance
                makes the whole turn's assistant message undetermined too. A turn with no tool
                calls at all is determined-empty (`frozenset()`), always kept on replay.
        """
        trail = await self._session_store.load_trail(session_id)
        turn_entries = [entry for entry in trail if entry.turn_index == turn_index]
        if not turn_entries:
            return frozenset()
        union: set[tuple[str, str]] = set()
        for entry in turn_entries:
            # A repeated-idempotent-read guard entry is a data-free nudge (its
            # `ok`+`None` provenance exists only to route it through the D94
            # stranded-sentinel path). It fetched NO data — the real served read
            # is a separate entry whose provenance is already unioned here — so it
            # must NOT poison this union to `None` and drop the turn's answer from
            # future-turn replay.
            if entry.status == "ok" and entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE:
                continue
            # A successful `recordAssumptions` entry carries NO warehouse data (it
            # only echoes the model's plain-English assumptions). It now has
            # DETERMINED-EMPTY (`frozenset()`) provenance, so skipping it here and
            # unioning it are equivalent — an empty set contributes nothing. Kept
            # as belt-and-braces against a regression to `None`, which would
            # otherwise collapse this union and tag the turn's answer undetermined,
            # losing it from history + replay
            # (docs/decisions/ui-assumptions-contract.md).
            #
            # Keeping the assumption STRINGS out of a later turn's model context is
            # a separate rule with its own home:
            # `context/assembly.py::_is_stale_model_text_entry`.
            if entry.status == "ok" and entry.tool_name == "recordAssumptions":
                continue
            if entry.provenance is None:
                return None
            union.update(entry.provenance)
        return frozenset(union)

    async def _compute_turn_assumptions(self, session_id: str, turn_index: int) -> list[str]:
        """Reconstruct the plain-English assumptions recorded at *turn_index* from the
                persisted `recordAssumptions` entries (deduped, first-occurrence order, via the
                SAME `fold_assumptions` the loop and `session_history` use). Seeds a resumed
                window on BOTH resume paths, so assumptions recorded in an earlier window are not
                dropped and the live result matches what `project_history` reconstructs.
        """
        trail = await self._session_store.load_trail(session_id)
        gathered: list[str] = []
        for entry in trail:
            if (
                entry.turn_index == turn_index
                and entry.status == "ok"
                and entry.tool_name == "recordAssumptions"
            ):
                fold_assumptions(gathered, entry.args.get("assumptions"))
        return gathered

    async def _compute_turn_answer_tables(
        self, session_id: str, turn_index: int
    ) -> tuple[list[AnswerTable], dict[str, BlueprintRun]]:
        """Reconstruct the model-designated answer TABLES for *turn_index* from the persisted
                `answerWithTable` entries — the `_compute_turn_assumptions` sibling, seeding a
                resumed window on BOTH resume paths so a designation made BEFORE a pause survives
                it.

                THE WHOLE LIST, not the first element: a three-part answer that paused must come
                back with three tables, or the resume silently degrades the exact turns
                multi-table exists for. The blueprint-run map is returned alongside because a
                blueprint that ran BEFORE the pause must stay designatable after it.

                LAST successful designation wins, matching the in-window rule. BOTH designation
                forms are reconstructed: reading only `args["sql"]` silently drops every blueprint
                designation, which is the form the live model actually emits (observed sending
                `sql=""` alongside `blueprint_id`). AND BOTH ARGUMENT SHAPES — entries written
                before the `tables` key carry `sql`/`blueprint_id` at the TOP LEVEL, and
                `resolve_designations` folds that shape in, so this seed keeps working on any
                session document ever written, with no migration.

                Resolving a `blueprint_id` here needs the blueprint's `terminal_sql`, which lives
                behind a D46 KV pointer. That de-reference happens ONLY on the resume path, once
                per blueprint, and a missing or expired ref leaves the id unresolved rather than
                raising.
        """
        trail = await self._session_store.load_trail(session_id)
        turn_entries = [
            e for e in trail if e.turn_index == turn_index and e.status == "ok"
        ]
        # blueprint_id -> BlueprintRun, rebuilt from this turn's successful runs.
        blueprint_runs: dict[str, BlueprintRun] = {}
        for entry in turn_entries:
            if entry.tool_name != "runBlueprint" or entry.result_full_ref is None:
                continue
            result_full = await self._session_store.read_full_result(
                session_id, entry.result_full_ref
            )
            if isinstance(result_full, dict):
                capture_terminal_sql(
                    "runBlueprint",
                    ToolResult(
                        status="ok", tool_name="runBlueprint", error_code=None,
                        retryable=None, user_message=None, provenance=None,
                        result_preview=None, result_full=result_full,
                    ),
                    into=blueprint_runs,
                    arguments=entry.args,
                )

        terminal_by_id = terminal_sql_by_id(blueprint_runs)
        designated: list[AnswerTable] = []
        for entry in turn_entries:
            if entry.tool_name != ANSWER_TABLE_TOOL_NAME:
                continue
            designation = resolve_designations(entry.args, terminal_by_id)
            # An item naming a blueprint that cannot be resolved here is simply
            # absent — a replay has no model to nudge, and the in-window path
            # already refused that call if it was going to.
            finalized = finalize_designations(designation.items)
            if not finalized.tables:
                continue
            provenance = entry.answer_table_provenance
            designated = [
                enrich_table(
                    table,
                    blueprint_runs,
                    provenance=(
                        provenance[index]
                        if provenance is not None and index < len(provenance)
                        else None
                    ),
                )
                for index, table in enumerate(finalized.tables)
            ]
        return designated, blueprint_runs

    def _maybe_start_summary(
        self, tool_name: str, tool_call_id: str, arguments: dict[str, Any]
    ) -> None:
        """Fire a FIRE-AND-FORGET progress-summary task for one tool CALL (opt-in).

                Non-blocking is load-bearing: the LLM call is scheduled CONCURRENTLY and the
                caller never awaits it, so the summarizer can never add latency to the tool nor
                delay the turn result. If the line never arrives, the instant template label
                stands. A SHALLOW snapshot of `arguments` is passed so a rebinding of the
                top-level keys cannot race the background read. No-op when the summarizer is not
                wired.
        """
        if self._progress_summarizer is None:
            return
        task: asyncio.Task[None] = asyncio.create_task(
            self._summarize_and_emit(tool_name, tool_call_id, dict(arguments))
        )
        self._summary_tasks.add(task)
        task.add_done_callback(self._summary_tasks.discard)

    async def _summarize_and_emit(
        self, tool_name: str, tool_call_id: str, arguments: dict[str, Any]
    ) -> None:
        """Await the summarizer and emit the value-rich progress line — fail-soft: an error or
                timeout yields `None` (dropped), and even the observer emit is guarded, so a late
                arrival after the emitter is closed can never raise into this fire-and-forget task.
        """
        try:
            summary = await self._progress_summarizer.summarize(tool_name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if not summary:
            return
        try:
            # The ProgressEmitter drops a post-close emit (its `_closed` guard), and
            # the tracing guardrail observer ignores non-`loop_` events — so this is
            # safe against a turn that has already ended. The broad guard is defense
            # in depth so no observer wiring can ever break the turn from here.
            self._observer(
                "tool_progress_summary",
                {"summary": summary, "tool_name": tool_name, "tool_call_id": tool_call_id},
            )
        except Exception:
            _logger.debug("progress-summary emit failed for %s (ignored)", tool_name)

    def _cancel_pending_summaries(self) -> None:
        """Best-effort cancel any still-pending summary tasks at turn end — the turn result
                never blocks on them. Clearing here is belt-and-suspenders so a resumed window
                starts clean.
        """
        for task in list(self._summary_tasks):
            if not task.done():
                task.cancel()
        self._summary_tasks.clear()

    async def _run_runtime_tool(
        self,
        handler: RuntimeTool,
        tool_name: str,
        arguments: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext,
    ) -> ToolResult:
        """Run one registry handler with a crash guard and a returned-provenance-type
                validation, so a misbehaving runtime tool cannot abort the turn or persist a
                replay-poisoning provenance.

                *turn* is the loop's OWN `turn_index`, threaded explicitly — the only correct
                source. Passed to every runtime tool, ignored by the ones that do not need it.
        """
        try:
            result = await handler.run(arguments, credentials, turn=turn)
        except Exception:
            # Never propagate the raw exception (would abort the turn) or leak
            # `str(exc)` — log server-side only, return a clean canned error.
            _logger.exception(
                "runtime tool %s raised (session=%s)", tool_name, credentials.session_id
            )
            return _runtime_tool_internal_error(tool_name)
        sanitized = _sanitize_runtime_provenance(result.provenance, tool_name)
        if sanitized is not result.provenance:
            result = replace(result, provenance=sanitized)
        return result

    async def _force_block_pending_intents(
        self,
        *,
        session_id: str,
        turn_index: int,
        state: AnalysisState | None,
        reason_code: str,
        budget_cap_reached: bool = False,
    ) -> AnalysisState | None:
        """Mark every surviving `pending` intent `blocked` with a RUNTIME reason code,
                immediately before a turn reaches a terminal outcome.

                Four callers, three codes:

                  | hard ceiling                              | `BUDGET_EXHAUSTED`      |
                  | budget cap reached DURING a refused round | `ENFORCEMENT_EXHAUSTED` |
                  | block counter spent, intents pending      | `ENFORCEMENT_EXHAUSTED` |
                  | budget-cap resume answered "stop"         | `USER_STOPPED`          |

                `ENFORCEMENT_EXHAUSTED` MEANS "ENFORCEMENT COULD NOT ESTABLISH A DISPOSITION" —
                NOT that the system proved the intent impossible. Zero rows is often the correct
                answer, some denial probes cost one metadata call, and a user who withdraws an ask
                mid-clarification lands here legitimately under that reading.

                `budget_cap_reached` is TELEMETRY ONLY and is set by the refused-round caller
                alone. That path really did reach the cap, so an operator watching budget pressure
                must see it — but the cap is NOT the cause of the disposition, so it does not go on
                the intent record. Emitted as a bare `True` on `loop_intent_force_blocked` and
                OMITTED otherwise, so the other three callers' payloads are unchanged.

                THIS PATH WRITES THE RUNTIME CODES DIRECTLY. It must NOT be routed through
                `validate_block_evidence`, which allowlists `MODEL_REASON_CODES` and would reject
                every code above, by design. There is no evidence to cite: `evidence_tool_call_id`
                stays `None`, which is exactly what distinguishes a runtime-forced block from a
                model-declared one in the ledger.

                The turn gate is already applied by the caller (the `state` handed in is the LIVE
                one) and again by the store, whose merge callback receives `live_analysis_state`.
                No live state, or nothing pending, is a no-op with no write at all.

                DEGRADE-NEVER-FAIL: this runs on terminal paths that are already returning a
                result to the user, so a store failure is logged and swallowed — losing the forced
                disposition is bad, aborting the user's answer to record it is worse.
        """
        pending = pending_intents(state)
        if not pending:
            return state

        forced_ids: list[str] = []

        def _merge(current: AnalysisState | None) -> AnalysisState:
            # Captured INSIDE the merge, and reset at the top of it: on a CAS
            # retry the write lands on a different document, and telemetry
            # reporting transitions that were never written would be quietly wrong.
            forced_ids.clear()
            if current is None:
                # The live state vanished between the read and the write (only a
                # concurrent turn boundary can do this). Refuse rather than
                # resurrecting a state from the stale snapshot.
                raise _NoLiveStateToForceError()
            intents: list[TrackedIntent] = []
            for intent in current.intents:
                if intent.status != "pending":
                    intents.append(intent)
                    continue
                forced_ids.append(intent.intent_id)
                intents.append(
                    replace(intent, status="blocked", reason_code=reason_code)
                )
            return AnalysisState(turn_index=turn_index, intents=tuple(intents))

        try:
            new_state = await self._session_store.apply_analysis_state(
                session_id, turn_index, _merge
            )
        except _NoLiveStateToForceError:
            _logger.warning(
                "no live analysis state to force-block at turn end (session=%s, "
                "turn=%d, reason=%s)",
                session_id,
                turn_index,
                reason_code,
            )
            return state
        except Exception:
            _logger.exception(
                "failed to force-block pending intents (session=%s, turn=%d, "
                "reason=%s) — the turn still returns its result",
                session_id,
                turn_index,
                reason_code,
            )
            return state

        for intent_id in forced_ids:
            # D25: ids are runtime-assigned and carry no user content;
            # `description` is model-authored from the user's question and is NEVER
            # emitted, on this or any other event.
            self._observer(
                "loop_analysis_state_transition",
                {
                    "intent_id": intent_id,
                    "from_status": "pending",
                    "to_status": "blocked",
                    "reason_code": reason_code,
                },
            )
            force_blocked: dict[str, Any] = {
                "intent_id": intent_id,
                "reason_code": reason_code,
            }
            if budget_cap_reached:
                force_blocked["budget_cap_reached"] = True
            self._observer("loop_intent_force_blocked", force_blocked)
        return new_state

    def _judge_would_run(
        self,
        *,
        guard: BudgetGuard,
        gate: FinalizationGate,
        kind: FinalizationBlockKind,
    ) -> bool:
        """Whether a judge call at this moment could change anything. SYNC AND FREE, and
                separated from `_judge` so it can be answered BEFORE the brief is built.

                THE SEPARATION IS THE POINT, not a refactor. Building a brief costs a session
                load plus up to `_MAX_CORROBORATION_READS` KV reads; doing that first and
                deciding afterwards put real store I/O on every terminal exit of every turn —
                INCLUDING with the feature switched off, which is the shipped default. Both
                halves of the cost have to sit behind the same test.

                THREE REASONS TO SKIP, and each is provably pointless rather than merely
                cheap:

                  FEATURE ABSENT — nothing to ask.

                  ALLOWANCE SPENT — a rejection cannot act, so the verdict would be bought and
                  then discarded. `has_spent` is window-local by design; see its docstring for
                  the one path it under-reports on and why that costs at most one call.

                  NO WALL-CLOCK HEADROOM (09 §H) — a rejection issued near the cap buys a
                  regeneration `guard.exceeded` cuts off mid-round, and the turn then returns
                  `paused_budget_cap` with the draft already cleared. The user is asked
                  "continue, refine, or stop?" and shown NOTHING, having had a serviceable answer
                  moments earlier. That is the judge making the product worse, and it is the
                  single failure mode most likely to make the feature net-negative.
        """
        if self._answer_judge is None:
            return False
        if gate.has_spent(kind):
            self._observer(ANSWER_JUDGE_SKIPPED_EVENT, {"reason": "allowance_spent"})
            return False
        usage = guard.usage()
        if (
            usage.max_wall_clock_seconds - usage.elapsed_seconds
            < self._answer_judge_min_headroom_seconds
        ):
            self._observer(ANSWER_JUDGE_SKIPPED_EVENT, {"reason": "wall_clock"})
            return False
        return True

    async def _judge(
        self,
        make_brief: Callable[[], Awaitable[JudgeBrief]],
        *,
        guard: BudgetGuard,
        gate: FinalizationGate,
        kind: FinalizationBlockKind,
    ) -> JudgeVerdict:
        """Run the answer judge for one site, or APPROVE without spending anything (09).

                *make_brief* IS A FACTORY, NOT A BRIEF, so the store reads it performs happen
                only after `_judge_would_run` has said a verdict could be acted on. An eagerly
                built brief is a session load and up to four KV reads charged to every terminal
                exit, disabled deployments included.

                THE FACTORY IS ALSO GUARDED. `_judge_results` reads the session document, and a
                transient store error there would otherwise propagate out of `_run_loop_body`
                AND ABORT A TURN WHOSE ANSWER IS ALREADY IN HAND — the exact fail-open violation
                this feature must never commit, arriving through the call site rather than
                through `review()`. Degrading to `APPROVED` costs one un-judged answer.

                Every skip and every failure returns the SAME `APPROVED` object as the judge's
                own fail-open paths, so no caller can branch on why (09 §E).
        """
        if not self._judge_would_run(guard=guard, gate=gate, kind=kind):
            return APPROVED
        try:
            brief = await make_brief()
        except Exception:
            _logger.exception(
                "could not assemble the answer-judge brief — approving and shipping"
            )
            self._observer(ANSWER_JUDGE_FAILED_EVENT, {"reason": "brief_failed"})
            return APPROVED
        assert self._answer_judge is not None  # `_judge_would_run` established it
        return await self._answer_judge.review(brief)

    async def _judge_results(
        self, session_id: str, turn_index: int, column_scope: frozenset[str]
    ) -> tuple[tuple[Mapping[str, Any], ...], str | None, tuple[TrailEntry, ...]]:
        """This turn's data-bearing results as the MODEL saw them, plus the date anchor —
                the two brief fields that cannot be read off a window-local (09 §D).

                SCOPE-FILTERED FIRST, RENDERED SECOND, and the order is the contract
                `context/budget.py` states for the model-request path: `render_entry` has no
                scope information of its own and performs no filtering, so handing it a raw
                trail would put warehouse rows the caller is not entitled to in front of the
                judge. `filter_trail` is called with `current_turn_index=None` — the
                current-turn exemption exists to let the MODEL see its own denials and
                self-correct, and a judge has nothing to self-correct; a `None`-provenance
                entry is undetermined and must stay dropped.

                `DATA_ANSWER_TOOLS` ONLY, successful ones. Those are the results the answer was
                written FROM; a `getTableSchema` grounds the model, not the answer, and a wide
                one is ~4k tokens of column documentation the judge has no criterion for. The
                set is imported rather than re-spelled so it cannot drift from the answer-shape
                gate's.

                ⚠ ONE EXTRA `load_trail` PER JUDGED EXIT (issues-stack A3 already counts three
                per window). It is paid ONLY when the judge is enabled AND reached a terminal
                exit AND passed both skips — at most once per window — and the alternative is
                threading a growing trail snapshot through `_run_loop_body` for a feature that
                is off by default.
        """
        doc = await self._session_store.get_or_create_session(session_id)
        in_scope = scope_filter.filter_trail(doc.tool_trail, column_scope)
        rendered = tuple(
            render_entry(entry, self._preview_row_count)
            for entry in in_scope
            if entry.turn_index == turn_index
            and entry.status == "ok"
            and entry.tool_name in DATA_ANSWER_TOOLS
        )
        # The IN-SCOPE entries ride along so `_corroborated_figures` can reuse this one
        # read (issues-stack A3 counts three per window already) — it needs
        # `result_full_ref`, which the rendered view deliberately does not carry.
        #
        # `in_scope`, NEVER `doc.tool_trail`. Corroboration only ever ADDS a `True`, so a
        # raw-trail scan could not leak content — but it could derive that `True` from a
        # result the scope filter dropped, i.e. state a fact about data the model's own
        # context no longer holds. The judge would then weigh a figure against evidence
        # neither it nor the model was entitled to see.
        return (
            rendered,
            turn_date_anchor_day(doc.messages, turn_index),
            tuple(in_scope),
        )

    async def _corroborated_figures(
        self, session_id: str, turn_index: int, prose: str, trail: Sequence[TrailEntry]
    ) -> bool | None:
        """`True` when a figure the prose reports is FOUND in what this turn's queries
                actually returned; `None` when nothing was established. **Never `False`.**

                THIS IS 05 §L.7's ESCAPE HATCH, BUILT AS AN ESCAPE HATCH. That section works
                through why corroboration cannot be a TRIGGER and the reasoning is unchanged: a
                derived figure never matches literally ("rose 12% year over year"), rounding
                breaks it (`9184` reported as "about 9,200"), and formatting diverges. Every one
                of those produces a NON-match on a perfectly good answer.

                So the absence of a match is not evidence and is never reported as any. A `False`
                would be read by the judge as "this figure was looked for and is not in the
                data", which is a finding the runtime cannot support and which would push it
                toward `contradicts_result` on exactly the answers §L.7 lists. `True` is the only
                thing this can honestly say, and it says it so a judge weighing a figure it
                cannot verify from 20 preview rows has one fact it can trust.

                IT READS `result_full`, the one place in this feature that does — and 09 §D.3's
                rule survives it, because the judge never SEES the full result. It sees a
                boolean derived from it. The full result is where a corroborating row lives when
                the preview cap cut it, which is the entire reason for the read.

                COSTS ONE `read_full_result` PER DATA CALL, capped, and short-circuits on the
                first match. Skipped entirely when the prose reports no figure — most answers.
        """
        figures = reported_figures(prose)
        if not figures:
            return None
        refs = [
            entry.result_full_ref
            for entry in trail
            if entry.turn_index == turn_index
            and entry.status == "ok"
            and entry.tool_name in DATA_ANSWER_TOOLS
            and entry.result_full_ref is not None
        ][:_MAX_CORROBORATION_READS]
        for ref in refs:
            try:
                full = await self._session_store.read_full_result(session_id, ref)
            except Exception:
                # DEGRADE-NEVER-FAIL, and here the degradation is already the honest
                # answer: a failed read establishes nothing, which is what `None` means.
                _logger.warning(
                    "could not read a full result for figure corroboration "
                    "(session=%s, turn=%d) — reporting 'not checked'",
                    session_id,
                    turn_index,
                )
                continue
            if full is None:
                continue
            haystack = json.dumps(full, default=str)
            # Digits-only on BOTH sides: a result cell serialises as `9184` while the
            # prose writes `9,184`, and the separator is a presentation choice made on
            # one side only.
            stripped = haystack.replace(",", "")
            if any(figure in stripped for figure in figures):
                return True
        return None

    def _judge_brief(
        self,
        site: JudgeSite,
        *,
        question: str,
        accum: TurnAccumulators,
        analysis_state: AnalysisState | None,
        date_anchor: str | None = None,
        draft: str = "",
        pending_question: str = "",
        results: tuple[Mapping[str, Any], ...] = (),
        designated_tables: tuple[tuple[str | None, str], ...] = (),
        figure_corroborated: bool | None = None,
    ) -> JudgeBrief:
        """Assemble the judge's brief from what the loop already holds (09 §D.2).

                PURE, AND NO STORE READS. Every field is a window-local or an accumulator at the
                moment a terminal exit is reached, which is what keeps the common case at ~2-4k
                tokens and one model round-trip rather than a re-assembly of the turn.

                `results` AND `date_anchor` ARE THE CALLER'S, from `_judge_results` — the only
                two fields that need I/O and the only two that need a scope decision. Building
                them here would put a `filter_trail` call inside a brief builder where nobody
                would look for it, and would make this method impossible to test without a
                store. The `ask_user` site passes neither.
        """
        return JudgeBrief(
            site=site,
            question=question,
            date_anchor=date_anchor,
            intents=tuple(
                (
                    intent.intent_id,
                    intent.description,
                    intent.status,
                    intent.reason_code,
                )
                for intent in (analysis_state.intents if analysis_state else ())
            ),
            assumptions=tuple(accum.assumptions or ()),
            sql_executed=tuple(accum.sql_executed or ()),
            results=results,
            draft=draft,
            designated_tables=designated_tables,
            figure_corroborated=figure_corroborated,
            pending_question=pending_question,
        )

    async def _finish(
        self,
        *,
        session_id: str,
        turn_index: int,
        status: TurnStatus,
        exit_label: AnswerExitLabel,
        assistant_text: str | None,
        tool_calls_made: int,
        accum: TurnAccumulators,
        checkpoint: PauseCheckpoint | None = None,
        provenance: frozenset[tuple[str, str]] | None = None,
        persist_text: str | None = None,
        event: tuple[str, dict[str, Any]] | None = None,
    ) -> TurnOutcome:
        """THE ORDER every in-body `TurnOutcome` return performs its effects in, in one place:
                checkpoint write, assistant-message append, envelope read, observer event, return.

                THE ORDER IS THE POINT, because it is invisible at every call site and only one
                reading is correct: `_compute_turn_provenance_union` must be read BEFORE the
                message it tags is appended; the envelope must be read AFTER the round's folds;
                and the observer must fire AFTER every store write, so an observer that reads the
                session back never races the write it is announcing.

                THIS OWNS NO STATE, and is a method rather than an object for that reason — the
                accumulators and the checkpoint are mutable locals of a running loop body.

                NOTHING IS DERIVED, and the three parameters that look derivable are the point:

                  - *event* is passed, never computed from *status*: the budget-cap "stop" answer
                    returns `status="done"` and emits NO event, so "done means loop_turn_done" is
                    FALSE and any code assuming it starts emitting a spurious finish.
                  - *provenance* is passed, never computed: it is non-`None` at the `done` exits
                    ONLY, and computing it here would put a SECOND
                    `_compute_turn_provenance_union` call in the codebase. The discipline is
                    exactly one per done-exit, made at the site, whose single value tags the
                    persisted message AND rides the outcome. A pause carries `None` deliberately —
                    it has no persisted assistant message for a lineage tag to belong to.
                  - *exit_label* is passed because `status="done"` is reached by BOTH done exits,
                    so no derivation can tell a no-tool-calls finish from an `answerWithTable`
                    one — which is exactly the distinction the answer-scrub telemetry is read for.

                THE ANSWER-PROSE SCRUB RUNS HERE, at the top: every exit that hands prose to a user
                goes through this function, so one call covers all five and the SAME scrubbed
                string necessarily feeds both the outcome and the persisted message.

                *persist_text* is a value, not a flag, and BOTH `done` exits now pass it
                unconditionally: the `answerWithTable` exit because `clean_answer_text` already
                returned a non-empty string, the no-tool-calls exit because the empty-answer gate
                (05 §K) substitutes `EMPTY_ANSWER_FALLBACK_TEXT` before the call. The invariant it
                used to carry — no EMPTY assistant message in history, which `context/assembly.py`
                would replay into every later turn — is unchanged and simply moved upstream to
                that substitution. It stays a value rather than becoming unconditional here
                because the PAUSE exits pass `None`: a pause has no answer to persist.

                WHAT STAYED AT THE SITES: everything whose POSITION relative to this call is the
                behaviour — `_force_block_pending_intents` (unconditional at the hard ceiling, only
                for a refused round at the budget cap), the provenance union, and the
                `PauseCheckpoint` construction.

                TWO EXITS DO NOT ROUTE THROUGH THIS, deliberately: `resume()`'s budget-cap "stop"
                return, which happens before `_run_loop_body` is entered and builds its own
                accumulators from the trail rebuild, so everything here is a no-op for it; and
                `_pause_from_runtime_tool`, which is already a single-purpose finisher and receives
                its `AnswerEnvelope` as a parameter.
        """
        # THE ANSWER-PROSE SCRUB (ISSUES I1) — FIRST, above every effect, so there
        # is exactly ONE scrubbed string and it is the one that reaches BOTH the
        # user (`TurnOutcome.assistant_text`) and history (the persisted
        # `TurnMessage`). Scrubbing at the two sites separately would be two
        # chances to drift, and a live answer that disagrees with
        # `/session/history` on exactly the redacted turns is the failure this
        # position exists to prevent (`session_history` projects the persisted
        # message, so the persisted string IS what the user re-reads tomorrow).
        #
        # THE STRUCTURED PAYLOAD BELOW IS UNTOUCHED, deliberately (the I2
        # decision): `sql_executed`, `answer_sql`, `blueprint_use`, `verification`
        # and `answer_tables` keep naming exactly what ran. Prose is the agent's
        # voice; those fields are the audit surface.
        assistant_text, redaction_count = scrub_answer_prose(
            assistant_text, provenance=provenance
        )
        if persist_text is not None:
            # THE SCRUBBED STRING, REUSED — never a second scrub. Both call sites
            # that persist pass the same string they pass as *assistant_text*, so
            # this assignment is an identity for them and fail-closed for anything
            # else: an unscrubbed string can never be the thing that gets written.
            persist_text = assistant_text
        if checkpoint is not None:
            await self._session_store.write_pause_checkpoint(session_id, checkpoint)
        if persist_text is not None:
            await self._session_store.append_message(
                session_id,
                TurnMessage(
                    turn_index=turn_index,
                    role="assistant",
                    content=persist_text,
                    ts=_now_iso(),
                    provenance=provenance,
                ),
            )
        # AFTER every fold and `commit_round` of the round — this is called at the
        # exit, never hoisted, so the envelope describes the window as it ends.
        envelope = accum.envelope()
        if redaction_count:
            # ONLY when something was redacted, so the event rate IS the disclosure
            # rate. Below the store writes with the finish event, under the same
            # rule. COUNT AND LABEL ONLY: a redacted token may be a column name,
            # which is deliberately not on the D25 attribute allowlist, so neither
            # the token nor the prose around it is ever placed on this payload.
            self._observer(
                ANSWER_PROSE_REDACTED_EVENT,
                {"redaction_count": redaction_count, "exit": exit_label},
            )
        if event is not None:
            # LAST, after every store write above: an observer that reads the
            # session back must never see it mid-update.
            self._observer(*event)
        return TurnOutcome(
            status=status,
            assistant_text=assistant_text,
            # From the checkpoint OBJECT, not re-derived from the question text —
            # the outcome and the persisted checkpoint hand the client the same dict.
            pending_question=checkpoint.pending_question if checkpoint is not None else None,
            tool_calls_made=tool_calls_made,
            # `[]` (no successful query this turn) -> `None`, so the UI treats
            # "no SQL panel" and "empty SQL" identically (§1 fork 1).
            sql_executed=accum.sql_executed,
            answer_sql=envelope.answer_sql,
            blueprint_use=envelope.blueprint_use,
            verification=envelope.verification,
            answer_tables=envelope.answer_tables,
            provenance=provenance,
            # `[]` (no recordAssumptions this turn) -> `None`, same fork as
            # `sql_executed`: the UI treats "no assumptions" and "empty" identically.
            assumptions=accum.assumptions,
        )

    async def _pause_from_runtime_tool(
        self,
        *,
        session_id: str,
        pause: ToolPause,
        window_count: int,
        assistant_text: str | None,
        tool_calls_made: int,
        sql_executed: list[str] | None = None,
        envelope: AnswerEnvelope | None = None,
        assumptions: list[str] | None = None,
        serves_intent: str | None = None,
    ) -> TurnOutcome:
        """Honor a runtime tool's `ToolPause` — write the checkpoint (with the additive
                `blueprint_*` mid-DAG state) and return `paused_ask_user`, the same terminal
                contract as `askUser`. The loop owns `budget_window_count`; the tool cannot know it.

                The four enrichment accumulators are threaded through best-effort, so a "runQuery
                succeeded, then runBlueprint paused on a slot question" turn surfaces the partial
                SQL and table on this pause flavor too, matching a direct `askUser` pause.
        """
        checkpoint = PauseCheckpoint(
            reason=pause.reason,
            pending_question=pause.pending_question,
            awaiting="user_answer",
            consumed=False,
            budget_window_count=window_count,
            blueprint_id=pause.blueprint_id,
            slot_bindings_json=pause.slot_bindings_json,
            completed_nodes_json=pause.completed_nodes_json,
            awaiting_node=pause.awaiting_node,
            # Carry the intent tag ACROSS the pause. A pausing tool writes no trail
            # entry, so the tag the model put on the `runBlueprint` call would
            # otherwise die here and the resumed entry — written under a fresh
            # `tool_call_id` — would be untagged, leaving the intent it was run for
            # closable only by citing an id the model never chose.
            serves_intent=serves_intent,
        )
        await self._session_store.write_pause_checkpoint(session_id, checkpoint)
        # ISSUES I1, the same scrub `_finish` applies — this exit does not route
        # through it (see `_finish`'s "TWO EXITS DO NOT ROUTE THROUGH THIS"), and a
        # pause is still model prose on a user's screen. `provenance=None`: a pause
        # has no determined turn provenance (there is no persisted assistant
        # message to tag), which costs only the quoted-value arm — the corpus-id,
        # qualified and snake_case rules need no knowledge of the turn.
        #
        # The checkpoint's `pending_question` is NOT scrubbed here: it is authored
        # by the pausing runtime tool (a blueprint slot prompt), not by the model,
        # and it never reaches the observer payload's published attributes
        # (`question` is not on the D25 allowlist).
        assistant_text, redaction_count = scrub_answer_prose(assistant_text, provenance=None)
        if redaction_count:
            self._observer(
                ANSWER_PROSE_REDACTED_EVENT,
                {"redaction_count": redaction_count, "exit": "pause"},
            )
        self._observer(
            "loop_paused_ask_user",
            {"question": pause.pending_question.get("question", "")},
        )
        return TurnOutcome(
            status="paused_ask_user",
            assistant_text=assistant_text,
            pending_question=checkpoint.pending_question,
            tool_calls_made=tool_calls_made,
            sql_executed=sql_executed,
            answer_sql=envelope.answer_sql if envelope else None,
            blueprint_use=envelope.blueprint_use if envelope else None,
            verification=envelope.verification if envelope else None,
            answer_tables=envelope.answer_tables if envelope else None,
            assumptions=assumptions,
        )

    async def _resume_blueprint(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        checkpoint: PauseCheckpoint,
        answer: str,
        turn_index: int,
        window_count: int,
    ) -> TurnOutcome:
        """Re-enter the paused blueprint at `awaiting_node` (D45). The executor is stateless —
                everything needed to continue is in the checkpoint, so a FRESH process resumes
                identically. The outcome maps the same way `runBlueprint`'s first call does:

                  - `Paused` (another approval or degrade) -> write a new checkpoint with the
                    grown completed-nodes state and return `paused_ask_user`;
                  - `Completed`/`Failed` -> persist a `runBlueprint` trail entry, so replay carries
                    the result and provenance, and CONTINUE the model loop.

                The executor re-fires the deterministic slot/rule resolves on resume for settled
                bindings — accepted, since those probes are read-only and idempotent.
        """
        try:
            slot_bindings = json.loads(checkpoint.slot_bindings_json or "{}")
            if not isinstance(slot_bindings, dict):
                slot_bindings = {}
        except (TypeError, ValueError):
            slot_bindings = {}

        # n1: telemetry symmetry with a first-call runBlueprint (which emits a TOOL
        # span). The resume path bypasses the tool, so emit the same dispatch
        # progress events here around the executor re-entry.
        # S3 (B4 parity): the CAS-consume already happened, so a RAISING executor
        # (e.g. a neo4j blip on the authoritative re-fetch) must NOT abort the turn
        # and strand the user with a consumed checkpoint — contain it exactly like
        # `_run_runtime_tool` and continue the loop with a canned internal error.
        async def _resume_work() -> ToolResult:
            try:
                outcome = await self._blueprint_executor.resume(
                    blueprint_id=checkpoint.blueprint_id,
                    slot_bindings=slot_bindings,
                    completed_nodes_json=checkpoint.completed_nodes_json,
                    awaiting_node=checkpoint.awaiting_node,
                    approval_answer=answer,
                    credentials=credentials,
                )
                return self._blueprint_outcome_to_tool_result(outcome)
            except Exception:
                _logger.exception(
                    "runBlueprint resume raised (session=%s)", credentials.session_id
                )
                return _runtime_tool_internal_error("runBlueprint")

        self._observer("tool_dispatch_start", {"tool_name": "runBlueprint"})
        # R7: the missing HALF of the runBlueprint TOOL span. A turn that paused for
        # approval reached Phoenix with a span for the first call and NOTHING for the
        # work the resume actually did. `in_tool_span` and not `tracing.tool_span`
        # directly, because this IS the envelope's discipline (optimistic `ok`,
        # `record_exception=False`, the status stamped from the outcome in one
        # expression) and a hand-rolled copy here is exactly the drift the envelope
        # was extracted to remove.
        #
        # SAME `tool_name` as the first call, so `tool.name` still names the tool the
        # loop dispatched and agrees with the progress events either side of it; the
        # resume half is told apart by `tool.args.resumed=True` — the same vocabulary
        # the resumed trail entry already writes (`args={"id": …, "resumed": True}`),
        # inside the existing `tool.args.*` namespace rather than a new one.
        #
        # D25 fail-closed: an EXPLICIT allowlist of structural scalars, NOT
        # `tool_span_args(model_args)`. `awaiting_node` is a blueprint-authored node
        # ORDER and the slot bindings are reported as a COUNT, so no slot VALUE and no
        # word of the user's approval `answer` has a path onto this span under any
        # posture — the loop is handed no `otlp_disable_redaction` switch to flip.
        tool_result = await in_tool_span(
            self._tracer,
            tool_name="runBlueprint",
            args={
                "id": checkpoint.blueprint_id,
                "resumed": True,
                "awaiting_node": checkpoint.awaiting_node,
                "slot_count": len(slot_bindings),
            },
            work=_resume_work,
        )
        self._observer(
            "tool_dispatch_ok" if tool_result.status == "ok" else "tool_dispatch_error",
            {"tool_name": "runBlueprint", "error_code": tool_result.error_code},
        )

        if tool_result.pause is not None:
            # Another mid-DAG pause — write the fresh checkpoint (grown
            # completed-nodes state) and pause again, exactly as the first call.
            # No enrichment seed: an `ExecPaused` result has no `result_full`, so a
            # blueprint answer has not been produced yet (best-effort all-`None`,
            # §1 nullability for `paused_ask_user`).
            return await self._pause_from_runtime_tool(
                session_id=session_id,
                pause=tool_result.pause,
                window_count=window_count,
                assistant_text=None,
                tool_calls_made=0,
                # A SECOND pause in the same blueprint run (approval after slots):
                # carry the tag forward from the checkpoint being consumed, or the
                # chain loses it at the second link.
                serves_intent=checkpoint.serves_intent,
            )

        # UI Slice 1 Fix 1: fold this COMPLETED blueprint result into seed
        # enrichment so the resumed loop's FINAL `done` result event carries the
        # same sql_executed/blueprint_use/verification a non-paused blueprint
        # answer would (a fresh `_run_loop_body` window would otherwise start empty and
        # drop it). Raw slots ride the checkpoint's `slot_bindings`. A no-op on a
        # non-`ok` (failed/degraded) resume → no seed, matching a raw-loop fallback.
        seed_sql: list[str] = []
        seed_blueprint_use, seed_verification = accumulate_enrichment(
            "runBlueprint",
            {"slot_bindings": slot_bindings},
            tool_result,
            turn_sql=seed_sql,
            blueprint_use=None,
            verification=None,
        )
        # Seed the blueprint-run map too, so the resumed window can honor an
        # `answerWithTable(blueprint_id=…)` naming the blueprint that completed
        # BEFORE this approval pause — otherwise the designation resolves to nothing
        # and the user loses the table on exactly the verified path.
        seed_blueprint_runs: dict[str, BlueprintRun] = {}
        capture_terminal_sql(
            "runBlueprint",
            tool_result,
            into=seed_blueprint_runs,
            arguments={"slot_bindings": slot_bindings},
        )

        # Persist the completed/failed runBlueprint result as a trail entry so the
        # continued loop (and any replay) sees it, then let the model narrate.
        result_full_ref: str | None = None
        if tool_result.result_full is not None:
            result_full_ref = await self._session_store.write_full_result(
                session_id, str(uuid.uuid4()), tool_result.result_full
            )
        entry = TrailEntry(
            turn_index=turn_index,
            tool_call_id=str(uuid.uuid4()),
            tool_name="runBlueprint",
            args={"id": checkpoint.blueprint_id, "resumed": True},
            status=tool_result.status,
            error_code=tool_result.error_code,
            provenance=tool_result.provenance,
            result_preview=tool_result.result_preview,
            result_full_ref=result_full_ref,
            ts=_now_iso(),
            authoritative=tool_result.authoritative,
            denial_detail=tool_result.denial_detail,
            # J7: a resumed blueprint's window is anchored the same way an unpaused
            # one's is, so the note has to survive the pause too — dropping it here is
            # exactly how the resume path lost the `authoritative` marker before.
            window_note=tool_result.window_note,
            # The tag survives the pause on the checkpoint, so the intent this
            # blueprint was run for closes by tag exactly as an unpaused one does.
            serves_intent=checkpoint.serves_intent,
        )
        await self._session_store.append_trail_entry(session_id, entry)

        question = _first_user_question(
            (await self._session_store.get_or_create_session(session_id)).messages,
            turn_index,
        )
        # recordAssumptions parity with the enrichment seed: rehydrate any
        # assumptions the model recorded BEFORE this blueprint approval-pause from
        # the trail, so the resumed window's final answer still carries them.
        seed_assumptions = await self._compute_turn_assumptions(session_id, turn_index)
        # `presentTable` parity with the assumptions seed: a designation the model
        # made BEFORE this blueprint approval-pause must survive it, or the resumed
        # answer comes back with `answer_sql=None` and the UI silently loses the table.
        # The WHOLE set, and the trail-rebuilt run map is MERGED UNDER the freshly
        # completed one so this resume's own result wins on a key collision.
        seed_answer_tables, trail_runs = await self._compute_turn_answer_tables(
            session_id, turn_index
        )
        seed_blueprint_runs = {**trail_runs, **seed_blueprint_runs}
        # B3 per-turn handle — see `model/client.py::begin_turn_client`.
        turn_model_client = begin_turn_client(self._model_client)
        return await self._run_loop_body(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            # ALL SIX seeds — this is the only path that has them. The resumed
            # window starts knowing everything the pre-pause window knew plus the
            # blueprint that completed during this resume, whose trail entry was
            # written moments ago and whose enrichment no replay could rebuild.
            accumulators=TurnAccumulators(
                sql=seed_sql,
                answer_tables=seed_answer_tables,
                blueprint_runs=seed_blueprint_runs,
                blueprint_use=seed_blueprint_use,
                verification=seed_verification,
                assumptions=seed_assumptions,
            ),
        )

    def _blueprint_outcome_to_tool_result(self, outcome: Any) -> ToolResult:
        """Map a `BlueprintExecutor` `ExecOutcome` to a `ToolResult` — the SAME mapping
                `RunBlueprintTool._execute` uses, reused here so a mid-DAG resume produces
                byte-identical results, INCLUDING the verified `authoritative` marker. Sharing the
                one mapper is what stops the resume path from silently losing it.
        """
        from data_agent.runtime.blueprint.tool import blueprint_outcome_to_tool_result

        mapped = blueprint_outcome_to_tool_result(outcome)
        if mapped is not None:
            return mapped
        return _runtime_tool_internal_error("runBlueprint")

    async def _resolve_answer_tables(
        self,
        arguments: dict[str, Any],
        *,
        blueprint_runs: Mapping[str, BlueprintRun],
        credentials: RuntimeCredentials,
        session_id: str,
        turn_index: int,
    ) -> tuple[list[AnswerTable], str | None, bool]:
        """Resolve ONE `answerWithTable` call into its designated answer tables.

                Returns `(tables, unresolved_blueprint_id, carried_designation)`.

                THE THIRD VALUE IS NOT DERIVABLE FROM THE FIRST TWO, which is why it is returned
                rather than inferred. An empty `tables` list has two completely different causes
                needing opposite handling: the model NAMED NOTHING (`carried_designation=False` —
                a defect, and the caller nudges), or it named something the runtime then dropped
                for a reason the model cannot act on (out of the caller's column scope, a
                duplicate, an over-cap entry). Nudging the second would tell the model to fix a
                payload that was already correct.

                The order is fixed and each step is there for a measured reason:

                  1. Choose the source list — `tables` when it carries a designation, else the
                     LEGACY top-level `sql`/`blueprint_id` pair folded in as one entry. The fold
                     is there for a model working from a stale context, and for the replay paths
                     that share this resolver and read pre-`tables` trail entries forever.
                  2. Resolve each item through the EXISTING `resolve_designation`. There is no
                     second resolution path, which is why multi-table costs no new resolver and
                     cannot drift from the single-table one.
                  3. An item naming a blueprint that did not run this turn REFUSES THE WHOLE CALL
                     (the caller turns the returned id into the retryable
                     `_answer_table_blueprint_not_run` nudge), after the dormant
                     ON_ANSWER_TABLE_UNRESOLVED seam has had first refusal. Dropping it instead
                     would silently lose a deliverable's table.
                  4. Dedupe on resolved SQL, then cap at `MAX_ANSWER_TABLES`.
                  5. Per-table provenance — an additive, positionally parallel read-path check,
                     NOT this entry's provenance.

                THE HOOKS FIRE PER TABLE, NOT PER CALL. A hook-substituted query LOSES ITS
                VERIFICATION AND ITS CHIP: the D56 gate verified a query that is no longer the one
                being paged. Inert today (both seams are empty), stated in code so a future hook
                cannot silently inherit a badge.
        """
        event_base = {
            # D5: hashed, never the raw session id — a hook is never given one.
            "session_id_hash": hash_scope(frozenset({session_id})),
            "turn_index": turn_index,
        }
        designation = resolve_designations(arguments, terminal_sql_by_id(blueprint_runs))
        # Read BEFORE anything is dropped. `designation.items` holds every entry that
        # carried a designation at all, resolved or not — so this is "did the model
        # name a table", which is the only question the nudge below may act on.
        carried_designation = bool(designation.items)

        resolved_items: list[DesignationItem] = []
        unresolved_blueprint_id: str | None = None
        for item in designation.items:
            if item.sql is None and item.named_blueprint is not None:
                _logger.warning(
                    "answerWithTable designated blueprint %r, which did not run "
                    "successfully this turn — no answer table (session=%s)",
                    item.named_blueprint,
                    session_id,
                )
                replacement = self._answer_table_hooks.resolve_unresolved(
                    AnswerTableEvent(
                        blueprint_id=item.named_blueprint, sql=None, **event_base
                    )
                )
                if replacement is None:
                    if unresolved_blueprint_id is None:
                        unresolved_blueprint_id = item.named_blueprint
                    continue
                # Substituted IN POSITION, and stripped of `blueprint_id` so the
                # replacement inherits neither chip nor badge.
                item = replace(item, sql=replacement, blueprint_id=None)
            if item.sql is None:
                continue
            if references_scratch(item.sql):
                durable = self._answer_table_hooks.resolve_ephemeral(
                    AnswerTableEvent(
                        blueprint_id=item.named_blueprint, sql=item.sql, **event_base
                    )
                )
                if durable is not None:
                    item = replace(item, sql=durable, blueprint_id=None)
            resolved_items.append(item)

        if unresolved_blueprint_id is not None:
            # The whole call is refused; nothing below would be surfaced anyway, and
            # computing provenance for tables that will not ship is pure cost.
            return [], unresolved_blueprint_id, carried_designation

        finalized = finalize_designations(resolved_items)
        for _ in range(designation.dropped_unresolvable):
            self._observer("loop_answer_table_item_dropped", {"reason": "unresolvable"})
        for _ in range(finalized.dropped_duplicate):
            self._observer("loop_answer_table_item_dropped", {"reason": "duplicate"})
        for _ in range(finalized.dropped_over_cap):
            self._observer("loop_answer_table_item_dropped", {"reason": "over_cap"})

        tables: list[AnswerTable] = []
        for table in finalized.tables:
            provenance = await self._tool_dispatcher.capture_sql_provenance(
                table.sql, credentials
            )
            enriched = enrich_table(table, blueprint_runs, provenance=provenance)
            # The read-path check, applied live through the SAME predicate
            # `session_history.project_history` uses. It bites only for a designated
            # `sql=` naming columns this caller cannot read — the table would 403 at
            # `POST /query/page` anyway, so offering it is a consistency defect.
            if not is_answer_table_in_scope(enriched.provenance, credentials.column_scope):
                self._observer("loop_answer_table_item_dropped", {"reason": "out_of_scope"})
                continue
            tables.append(enriched)

        if tables:
            self._observer(
                "loop_answer_tables_designated",
                {
                    "table_count": len(tables),
                    "blueprint_table_count": sum(
                        1 for t in tables if t.blueprint_use is not None
                    ),
                    # J6: what an observer means by "verified" is the CLAIM, not the
                    # presence of a block. A blueprint that returned zero rows now
                    # carries an explicit `empty — unverifiable` block (`passed:
                    # False`), and counting it here would keep reporting a
                    # verification rate the runtime is no longer claiming. It still
                    # counts in `table_count` and `blueprint_table_count` — a
                    # blueprint DID produce it.
                    "verified_table_count": sum(
                        1 for t in tables if (t.verification or {}).get("passed") is True
                    ),
                },
            )
        elif not carried_designation:
            # THE MODEL NAMED NO TABLE AT ALL. Emitted here, where the fact is
            # established, rather than at the nudge site — the nudge is bounded by a
            # per-window allowance, so counting it there would under-report the
            # behaviour precisely once it starts repeating. Payload-free: there is
            # nothing to count and nothing shape-only to say.
            self._observer(ANSWER_TABLE_EMPTY_DESIGNATION_EVENT, {})
        return tables, None, carried_designation

    def _observe_uncovered_intents(
        self,
        state: AnalysisState | None,
        *,
        tables: Sequence[AnswerTable],
        result_sql_by_call_id: Mapping[str, str],
    ) -> None:
        """Emit `loop_answer_table_intent_uncovered` when a COMPLETED intent's result is not
                among the designated tables.

                DERIVATION IS A CHECK HERE, NEVER A SOURCE. The tables are LISTED by the model
                because the evidence call is the wrong query: a designated `sql` is deliberately
                not required to be one the agent ran, because the executed query usually carries a
                LIMIT the agent chose for its own reading and paging needs the un-capped shape.
                Deriving the tables from the evidence would page that capped query and silently
                truncate every grid. (`getTableSchema` evidence has no pageable SQL at all, and a
                `blocked` intent's evidence is a denial.)

                NOT A REFUSAL. A scalar part of a multi-part answer correctly belongs in the prose,
                so this counts a signal, not an error. Best-effort by construction: an intent whose
                evidence call ran in an EARLIER window contributes nothing, because
                `result_sql_by_call_id` is window-local.
        """
        if state is None:
            return
        designated = {table.sql for table in tables}
        uncovered = sum(
            1
            for intent in state.intents
            if intent.status == "completed"
            and intent.evidence_tool_call_id is not None
            and result_sql_by_call_id.get(intent.evidence_tool_call_id) is not None
            and result_sql_by_call_id[intent.evidence_tool_call_id] not in designated
        )
        if uncovered:
            self._observer(
                "loop_answer_table_intent_uncovered", {"intent_count": uncovered}
            )

    async def _run_loop_body(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        window_count: int,
        turn_index: int,
        model_client: ModelClient,
        question: str | None = None,
        # This window's answer accumulators (`loop/turn_accumulators.py`), ALREADY
        # SEEDED by the caller when the turn is resuming: `resume()` rebuilds what
        # the trail knows, and `_resume_blueprint` adds the enrichment of the
        # blueprint that completed during the resume itself (UI Slice 1 Fix 1),
        # which no replay could reconstruct. `None` (a brand-new turn, or any
        # caller with nothing to carry) → a fresh, empty window.
        accumulators: TurnAccumulators | None = None,
    ) -> TurnOutcome:
        """The turn-window driver. Its `try` opens as the FIRST statement, above the
                tools/discovery preamble, so a `tools_provider` failure still runs the
                straggler-summary cancel in the `finally` below.
        """
        try:
            # The live MCP authenticates every request, including tools/list, so
            # the tools_provider seam is called WITH this turn's credentials on
            # every window (2026-07-01 fix) — it is expected to cache the fetched
            # catalogue itself (see mcp/tool_schema.py::ToolSchemaCache) since the
            # catalogue is scope-independent; this is not a live MCP round-trip
            # on every call in practice, just a credentialed one the first time.
            tools = await self._tools_provider(credentials)

            # The loop's OWN turn index, handed to every runtime tool (03 §C.1). It is
            # built here, from the parameter `run`/`resume` computed, so no tool ever
            # re-derives it or reads `app.py`'s explicitly non-load-bearing hint.
            turn_context = TurnContext(turn_index=turn_index)

            # Emulated-discovery injection (context/discovery_emulation.py): emulate
            # `listDatabases`+`listTables` ONCE per budget window, BEFORE the model loop.
            # Both run() and resume() re-enter `_run_loop_body`, so "once per window" is the
            # right cadence. It is computed HERE — next to `_tools_provider`, DELIBERATELY
            # ABOVE the `BudgetGuard(...)` below — so its 1 + N MCP round-trips run OUTSIDE the
            # budget window's wall clock and never consume `max_wall_clock_seconds` (nor
            # re-charge it on every `continue` resume): this injected context is "never
            # budgeted". Two effects, both ephemeral (never persisted):
            #   1. `discovery_canonical` — the synthetic assistant/tool pairs, threaded
            #      into every per-round-trip rebuild below as the earliest tool history.
            #   2. `emulation_read_signatures` — seeded into the `ReadGuard` below so a
            #      model RE-call of either tool is served locally (the "already served"
            #      nudge) instead of hitting the MCP.
            # Degrade-not-fail: any failure → no pairs + no seed, and the model falls
            # back to calling the two tools itself. D5: the sweep goes through the
            # dispatcher (credentials attached only at the MCP transport boundary),
            # never through the context assembler.
            discovery_canonical: list[dict[str, Any]] = []
            emulation_read_signatures: set[tuple[str, str]] = set()
            # `signature -> tool_call_id` for the emulated pairs, seeded into the
            # `ReadGuard` below (see the loop that fills it for why).
            emulated_served_call_ids: dict[tuple[str, str], str] = {}
            if self._discovery_emulation_provider is not None:
                emulation: EmulatedDiscovery | None = None
                try:
                    emulation = await self._discovery_emulation_provider(credentials)
                except Exception:
                    _logger.exception(
                        "discovery-emulation provider failed (session=%s) — injecting nothing",
                        credentials.session_id,
                    )
                if emulation is not None:
                    for rendered_entry in emulation.entries:
                        discovery_canonical.extend(_tool_trail_entry_to_canonical(rendered_entry))
                        # Point each emulated signature at the synthetic entry that
                        # serves it, so the trim-aware re-fetch exemption can see the
                        # listing IS readable and lets the guard dedup a model re-call —
                        # the whole point of the sweep. Without a pointer the exemption
                        # would read "no readable source" and re-dispatch to the MCP,
                        # undoing the saving. `fit_request_to_budget` pins these pairs by
                        # id (invariant 7), so they stay readable for the whole window.
                        emulated_sig = idempotent_read_signature(
                            rendered_entry["tool_name"], rendered_entry.get("args") or {}
                        )
                        emulated_served_call_ids[emulated_sig] = rendered_entry["tool_call_id"]
                    emulation_read_signatures = emulation.read_signatures

            # A FRESH window per `_run_loop_body` entry — the D55 "fresh window on continue"
            # seam: both run() and resume() re-enter here, so a granted continue starts
            # its iteration/token/wall-clock counters from zero.
            guard = BudgetGuard(
                max_iterations=self._max_loop_iterations,
                max_wall_clock_seconds=self._max_wall_clock_seconds,
                max_token_spend=self._max_token_spend,
                clock=self._clock,
            )
            tool_calls_made = 0
            last_assistant_text: str | None = None
            # Turn-window-local retrieval memo (design §3.3): keyed by
            # (question, scope_hash) inside `assemble`, it makes the pipeline embed/
            # recall at most ONCE across every round-trip of this window despite the
            # D45 per-round-trip context rebuild. Not persisted — pure in-turn memo.
            retrieval_memo: dict[tuple[str, str], Any] = {}
            # D94 Part 2: turn-window-local de-dup for the withheld-provenance
            # diagnostic — same lifecycle as `retrieval_memo` (fresh per window,
            # not persisted) so the event fires at most once per stranded call.
            withheld_call_ids: set[str] = set()
            # Repeated-idempotent-read guard (generalizes D94), `loop/read_guard.py`: it
            # holds the already-served read signatures for THIS turn plus the pointers
            # and exemption counts the trim-aware re-fetch escape needs. Turn-window-local
            # like the memos above, BUT seeded from the persisted trail below so it
            # survives both the D45 per-round-trip rebuild (its state would otherwise
            # reset every `send_turn`) AND a budget-window `continue` resume (a fresh
            # `_run_loop_body` window starts here with an empty guard). Seeding from every
            # prior `ok` idempotent-read entry of this turn is what lets it recognize a
            # repeat it did not itself serve in the current window.
            read_guard = ReadGuard(self._observer)
            # The blueprint-definition gate, `loop/blueprint_gate.py`: it holds every
            # blueprint id this turn has already EXPANDED with a successful
            # `getBlueprint`, and `runBlueprint` for an id that is NOT in it is refused
            # before the executor runs. Turn-window-local like the guard above, and
            # seeded from the persisted trail below for the same two reasons — the D45
            # per-round-trip rebuild would otherwise reset it on every `send_turn`, and a
            # budget-window `continue` resume starts a fresh `_run_loop_body` window with an
            # empty in-memory set, so a model that expanded the blueprint before the cap
            # would be refused for work it had done. TURN-SCOPED (the walk below is
            # turn-filtered); see the class docstring for why that cost is accepted.
            blueprint_gate = BlueprintGate(self._observer)
            # ONE read for the trail seed AND the live analysis state (05 §E): the
            # session doc carries both, and `load_trail` is itself just a read of this
            # same document, so this is that read — not an extra one.
            session_doc = await self._session_store.get_or_create_session(session_id)
            # FINALIZATION ENFORCEMENT reads from HERE (05 §E). The state changes
            # mid-turn, so a once-per-window read would be wrong — but a store read at
            # each terminal exit would cost a round-trip on EVERY turn, including the
            # single-intent ones that never declare a state at all. So it is loaded
            # once into this window-local and REFRESHED IN PLACE from each
            # `updateAnalysisState` result below; 03 §E.2's partition guarantees state
            # calls are dispatched first, so the local is current at both exits, and
            # the fast path is an `is None` test on a local (`pending_intents`).
            analysis_state = live_analysis_state(session_doc, turn_index)
            # UI Slice 1 (docs/decisions/ui-slice1-enriched-result-contract.md §3) and
            # 08: this window's answer accumulators, `loop/turn_accumulators.py`. Same
            # lifecycle as the memos above — fresh per window, never persisted, folded
            # from each successful tool call below and read at every `TurnOutcome(...)`
            # return site. Handed in ALREADY SEEDED by a resume (see the parameter), so
            # the fallback here is the brand-new-turn case.
            #
            # CONSTRUCTED HERE, ABOVE THE GATES, because the answer-shape counter's seed
            # is one of its reads: a resumed window that already has the user's table
            # must not re-arm a gate whose whole job is to notice a missing one.
            accum = accumulators if accumulators is not None else TurnAccumulators()
            # --- ANSWER-SHAPE GATE state (05 §J), `loop/finalization.py` ---------
            #
            # How many SUCCESSFUL multi-row `runQuery`/`runBlueprint` calls this TURN has
            # made, and whether any `answerWithTable` has put a table in front of the
            # user. Both are TURN-scoped facts held in this window-scoped counter, so
            # both are seeded — from the trail walk below for the reason the read guard
            # is seeded (a budget-cap `continue`, an `askUser` resume and a mid-DAG
            # blueprint resume each start a fresh `_run_loop_body` with an empty counter,
            # and a gate that forgot the rows the model already has would go silent on
            # exactly the long turns that produce several tables), and from
            # the accumulators' seeded designations for the blueprint approval-resume
            # path, whose designation was made before the pause.
            #
            # THE TRAIL WALK IS ALREADY TURN-FILTERED (`prior_entry.turn_index !=
            # turn_index` skips below), which is also the cross-turn replay protection: a
            # multi-row query from turn 3 cannot make turn 4's prose answer a defect, and
            # the `claim_finalization_block` key is `(turn_index, window, kind)` too, so a stale
            # refusal cannot be replayed onto a later turn.
            answer_shape = AnswerShapeCounter(accum.has_answer_tables)
            # The finalization block allowance (05 §C.1/§C.2, §J.3), also
            # `loop/finalization.py`: the per-round-trip refusal flag and the persisted
            # per-window claim behind all four refusal sites below. Its three ids are
            # constant for this whole window, so they are handed over once here rather
            # than repeated at every call site.
            finalization_gate = FinalizationGate(
                self._session_store,
                self._observer,
                session_id=session_id,
                turn_index=turn_index,
                window_count=window_count,
            )
            for prior_entry in session_doc.tool_trail:
                if prior_entry.turn_index != turn_index or prior_entry.status != "ok":
                    continue
                # Repeated-idempotent-read guard seed. Non-read entries are ignored
                # inside `observe_prior_read` — "what counts as a read" is the guard's
                # question, not this walk's. `data_free` marks the guard's OWN marker
                # entry: it proves the signature was served, but the READ it deduped is
                # where the result lives, so it must not become the pointer the
                # readability test follows.
                read_guard.observe_prior_read(
                    prior_entry.tool_name,
                    prior_entry.args,
                    prior_entry.tool_call_id,
                    data_free=prior_entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE,
                )
                # NOT an `elif`: `getBlueprint` is BOTH a guarded idempotent read and the
                # thing the blueprint-definition gate is keyed on, so it seeds both.
                if prior_entry.tool_name == "getBlueprint":
                    # `status == "ok"` is the whole predicate — no `found` check. The
                    # result body sits behind a D46 KV pointer, so reading it here would
                    # cost a store round-trip per entry, and a `{found: false}` expansion
                    # cannot make a `runBlueprint` succeed anyway (the executor re-fetches
                    # the definition and fails on its own merits). The gate's job is
                    # "did you look", not "did you find".
                    blueprint_gate.observe_prior_definition_read(prior_entry.args.get("id"))
                # ANSWER-SHAPE GATE (05 §J), seeded from the same walk — BOTH of its
                # facts, in one call. `status == "ok"` is guaranteed by the skip above,
                # and is passed explicitly anyway so the multi-row predicate reads the
                # same at both of its call sites. The `answerWithTable` half of that seed
                # is deliberately ASYMMETRIC with the live site (name alone here,
                # substance there); `AnswerShapeCounter.observe_prior_entry` carries the
                # rationale, which is about what a persisted entry can cheaply be asked.
                answer_shape.observe_prior_entry(
                    prior_entry.tool_name, prior_entry.status, prior_entry.result_preview
                )
            # Seed the guard with the emulated-discovery signatures swept above (outside
            # the budget window) so a model re-call of listDatabases/listTables is served
            # locally, not re-dispatched to the MCP — together with the pointers to the
            # synthetic entries that serve them, or the trim-aware exemption would find no
            # readable source and re-dispatch the very calls the sweep exists to avoid.
            # Both empty when the feature is off/degraded.
            read_guard.seed_emulation(emulation_read_signatures, emulated_served_call_ids)
            # The finalization nudge (05 §B.2/§D), ephemeral and NEVER persisted. It
            # lives EXACTLY ONE ROUND-TRIP: set when an exit-#1 finalization is
            # refused, spliced into the next rebuild, and cleared immediately after
            # that rebuild below.
            finalization_nudge: str | None = None
            # K2: the `tool_call_id`s whose next-round tool result must carry
            # `_INTENT_TAG_DROPPED_NOTE`. Same ephemeral, never-persisted,
            # EXACTLY-ONE-ROUND-TRIP lifetime as `finalization_nudge`: filled during
            # this round's dispatch (the drop site below), read by the NEXT rebuild,
            # and emptied immediately after that rebuild. The drop happens while the
            # round's tool results do not exist yet, which is why the feedback is
            # necessarily deferred one round rather than injected inline.
            intent_note_call_ids: set[str] = set()
            # K2, THE GATE ON THAT NOTE: has a SUBSTANTIVE tool run on this TURN? Once
            # one has, `analysis_state.py` refuses a first declaration NON-RETRYABLY, so
            # the note's "call updateAnalysisState before your next substantive call"
            # becomes an instruction to earn a refusal — worse than the silence it
            # replaces. The note is suppressed at the end of the dispatch batch below
            # whenever this is true.
            #
            # TURN-SCOPED, NOT WINDOW-SCOPED, and therefore SEEDED — the lock it mirrors
            # is a property of the persisted trail for this `turn_index`, and a budget-cap
            # `continue`, an askUser resume and a blueprint resume each enter a fresh
            # `_run_loop_body`. An unseeded window-local would read False in window 2 while
            # window 1's runQuery had already closed the door, which is exactly the false
            # advice this gate exists to prevent. `find_locking_tool` is the runtime's OWN
            # predicate (so the two cannot drift), over the doc already loaded above — no
            # extra store read. Note it does NOT filter on `status`: the walk below skips
            # non-`ok` entries, but a FAILED runQuery locks late init just the same, which
            # is why this is a separate call and not folded into that walk.
            substantive_ran = find_locking_tool(session_doc.tool_trail, turn_index) is not None

            while True:
                request = await self._build_canonical_messages(
                    session_id,
                    credentials.column_scope,
                    turn_index,
                    question=question,
                    user_id=None,
                    retrieval_memo=retrieval_memo,
                    withheld_call_ids=withheld_call_ids,
                    discovery_canonical=discovery_canonical,
                    finalization_nudge=finalization_nudge,
                    intent_note_call_ids=intent_note_call_ids,
                )
                canonical_messages = request.messages
                # ONE ROUND-TRIP ONLY (05 §D). `discovery_canonical` is computed once
                # per window and re-spliced into every rebuild; copying THAT lifetime
                # would repeat the nudge forever — including after the intents are
                # closed — and, because it is ephemeral and sits at the tail, would
                # migrate it to be the newest message on every rebuild, appearing
                # after tool results it predates.
                finalization_nudge = None
                # K2, same one-round-trip rule: the note has now been rendered into the
                # request the model is about to see. A REBIND, not `.clear()`, because
                # the set was just handed to the builder.
                intent_note_call_ids = set()
                # Hand the guard every tool result the model can actually READ this
                # round-trip — after `fit_request_to_budget` has had its say, and with
                # data-free sentinels excluded (see `_CanonicalRequest`). That set is what
                # the trim-aware re-fetch exemption asks whether an already-served read is
                # still legible. `begin_round` also clears the guard's served-THIS-BATCH
                # set; see its docstring for why that reset is load-bearing (a read
                # dispatched moments ago cannot yet be in `readable_tool_call_ids`, and
                # must not be mistaken for one the budget trimmed away).
                read_guard.begin_round(request.readable_tool_call_ids)
                # Set by a SUCCESSFUL answerWithTable in this iteration's batch; drives
                # terminal exit #2 below. Reset per iteration — a designation only ends
                # the turn it was made in.
                designated_answer_text: str | None = None
                # The judge's feedback if it refused an `answerWithTable` EARLIER IN THIS
                # BATCH. Reset per iteration beside `designated_answer_text`, and for the
                # same reason: it describes one response, not one turn.
                #
                # A model response can carry up to 8 tool calls, and a batch of two
                # `answerWithTable`s is the shape 05 §C.2 built the per-round free-refusal
                # path for. The judge cannot use that path — its cost-avoidance peek
                # (`has_spent`) runs before `may_refuse` and turns the second call into a
                # SKIP, which approves — so without this local the second call terminated
                # the turn in the round the judge had just refused it.
                judge_refusal_this_round: str | None = None
                # Start the blueprint gate's response batch: it stages the ids expanded
                # by a `getBlueprint` in THIS response and holds them apart from the
                # committed set until the batch drains (`commit_round`, below the
                # dispatch loop) — see that site for why a same-response
                # `[getBlueprint(x), runBlueprint(x)]` pair must NOT pass the gate.
                # Reset per iteration, beside `designated_answer_text`.
                blueprint_gate.begin_round()
                # The window's forced re-round is consumed PER ROUND-TRIP, not per
                # refused call (05 §C.2) — so a `[answerWithTable, answerWithTable]`
                # batch is refused twice and advances the persisted counter once. Reset
                # here, beside `designated_answer_text`, for the same reason.
                finalization_gate.begin_round()
                self._observer("loop_model_call_start", {"window": window_count})
                result = await model_client.send_turn(canonical_messages, tools)
                last_assistant_text = result.assistant_text

                # --- FINALIZATION ENFORCEMENT, terminal exit #1 (05 §B.2) ---------
                #
                # The model produced prose, not a tool call, so this turn is about to
                # end `done` — and there is NO ERROR CHANNEL here: nothing to attach a
                # denial to, because nothing was called. A synthetic tool message
                # cannot stand alone either (`_assembled_to_canonical` only ever emits
                # a `tool` message by expanding a trail entry into an
                # `assistant(tool_calls) + tool` PAIR), and fabricating such a pair —
                # which discovery emulation legitimately does — would mean naming a
                # function the model can see in its tools list, re-splicing/deduping/
                # pinning it on every rebuild, and routing its text through
                # `classify_denial` anyway, all for something that should live one
                # round-trip. So the refusal is an EPHEMERAL `user`-role injection.
                #
                # NOTHING IS PERSISTED on this path: not the refused answer, not the
                # nudge. Both are within-turn control flow, and a persisted nudge would
                # appear in `/session/history` as something the user said.
                refused_finalization = False
                # The fast path, and it must stay this cheap: `pending_intents(None)`
                # is an `is None` test on a window-local — no store read, on the
                # overwhelming majority of turns that never declare a state at all.
                pending_at_exit = pending_intents(analysis_state) if not result.tool_calls else ()
                if pending_at_exit:
                    if await finalization_gate.may_refuse("intents"):
                        refused_finalization = True
                        self._observer(
                            "loop_finalization_refused",
                            {"exit": "no_tool_calls", "pending_count": len(pending_at_exit)},
                        )
                        finalization_nudge = finalization_nudge_text(
                            result.assistant_text, pending_at_exit
                        )
                        # CLEAR THE DRAFT. `last_assistant_text` was set above and is
                        # returned as `assistant_text` on the hard-ceiling and
                        # budget-cap paths — so a refused, incomplete answer could
                        # still reach the user there while never appearing in history,
                        # making live and history disagree on exactly the enforcement
                        # path.
                        last_assistant_text = None
                    else:
                        # The window's one forced re-round is spent and the intents are
                        # still pending. Record the disposition the runtime CAN
                        # establish and let finalization proceed — see
                        # `_force_block_pending_intents` for what the code does and does
                        # not claim.
                        analysis_state = await self._force_block_pending_intents(
                            session_id=session_id,
                            turn_index=turn_index,
                            state=analysis_state,
                            reason_code="ENFORCEMENT_EXHAUSTED",
                        )
                        self._observer(
                            "loop_enforcement_exhausted",
                            {"intent_count": len(pending_at_exit)},
                        )
                elif not result.tool_calls and answer_shape.armed:
                    # --- THE ANSWER-SHAPE GATE (05 §J) --------------------------
                    #
                    # The model is ending the turn in bare prose while holding
                    # multi-row results it never tabled. "Presenting a table" is an
                    # UNCONDITIONAL prompt rule and the only strong one with no runtime
                    # enforcement; measured live it failed ~5/8 of expected-table runs,
                    # and its worst mode was an APOLOGY — the model asserting it could
                    # no longer call the tool, on a turn nothing had refused and nothing
                    # had ended. A belief about turn mechanics is not something a prompt
                    # can correct from inside the same turn; only the runtime can, by
                    # refusing the finish once and handing back a round.
                    #
                    # `elif`: THE PENDING-INTENTS REFUSAL TAKES PRECEDENCE and its
                    # behaviour is untouched. It is the more specific complaint (there is
                    # work the model has not done, not merely work it has not presented),
                    # so at most one refusal happens per round-trip.
                    #
                    # ITS OWN ALLOWANCE, `kind="answer_shape"` (05 §J.3, revised
                    # 2026-08-12 on live data). This gate SHARED the intents allowance
                    # for one release, which bounded the worst case at one extra
                    # round-trip per window and looked like the conservative choice. It
                    # was not: on multi-intent questions the two gates fire in SEQUENCE,
                    # not in competition — prose with intents pending (intents nudge,
                    # allowance gone), then the ledger closed, then prose again with the
                    # tables still untabled. 2 of 4 live three-part runs went exactly
                    # that way and this gate could only emit `..._exhausted`, starved on
                    # the question it exists for (traces `900a85a4`, `16f090db`).
                    # Separate allowances make that sequence terminate; the price is a
                    # worst case of TWO extra round-trips per window, still bounded by
                    # `max_budget_windows`.
                    if await finalization_gate.may_refuse("answer_shape"):
                        refused_finalization = True
                        self._observer(
                            ANSWER_SHAPE_REFUSED_EVENT,
                            {"multi_row_calls": answer_shape.multi_row_calls},
                        )
                        finalization_nudge = answer_shape_nudge_text(
                            result.assistant_text, answer_shape.multi_row_calls
                        )
                        # CLEAR THE DRAFT, for the reason the pending-intents path
                        # clears it: `last_assistant_text` is returned as
                        # `assistant_text` on the hard-ceiling and budget-cap paths, so
                        # a refused answer could still reach the user there while never
                        # appearing in history.
                        last_assistant_text = None
                    else:
                        # THIS GATE'S OWN allowance for the window is spent, which now
                        # means only one thing: it already refused once here and the
                        # model answered in prose again. (Before the allowances were
                        # split it also meant "the intents nudge took it", which made
                        # this counter ambiguous and hid the starvation above.) THE
                        # PROSE PASSES. The runtime records what it can and never
                        # hard-locks a turn: the same posture `ENFORCEMENT_EXHAUSTED`
                        # takes for intents, minus the ledger write, because there is no
                        # ledger for answer shape and the user's answer is in hand.
                        self._observer(ANSWER_SHAPE_EXHAUSTED_EVENT, {})

                # --- THE ANSWER RULES (05 §L) -----------------------------------
                #
                # NOT AN `elif`, for §K.4's reason: the branches above are entered when
                # their complaint QUALIFIES, not when they refuse, so chaining would
                # silence this on any round where an earlier gate had already spent its
                # grant. `not refused_finalization` keeps the one-refusal-per-round-trip
                # rule the chain expresses structurally.
                #
                # BEFORE THE EMPTY-ANSWER GATE, though the order is free: `first_match`
                # returns `None` for blank prose, so the two conditions are disjoint by
                # construction and neither can pre-empt the other.
                #
                # THE ALLOWANCE IS THE RULE'S, not this site's — a grounding rule spends
                # `ungrounded_answer`, a form rule spends the shape gate's own grant. So
                # adding a rule does not add a round-trip to the window's worst case
                # unless it is a genuinely new complaint.
                answer_rule = (
                    first_match(result.assistant_text, accum.sql_executed)
                    if not result.tool_calls and not refused_finalization
                    else None
                )
                if answer_rule is not None:
                    if await finalization_gate.may_refuse(answer_rule.charges_to):
                        refused_finalization = True
                        self._observer(
                            ANSWER_RULE_REFUSED_EVENT, {"rule": answer_rule.name}
                        )
                        finalization_nudge = answer_rule.nudge(result.assistant_text)
                        # CLEAR THE DRAFT, for the reason the two gates above clear it:
                        # `last_assistant_text` is returned as `assistant_text` on the
                        # hard-ceiling and budget-cap paths, so a refused answer could
                        # otherwise reach the user there while never entering history.
                        last_assistant_text = None
                    else:
                        # The kind's allowance for this window is spent. THE PROSE
                        # PASSES — the runtime records what it can and never hard-locks
                        # a turn (§J.5), and here that posture is load-bearing rather
                        # than inherited: these rules read SHAPE, not truth, so a second
                        # refusal would be the runtime destroying an answer it cannot
                        # prove is wrong. The event is what makes the pass visible, and
                        # its rate is what says whether a rule is tuned right.
                        self._observer(
                            ANSWER_RULE_EXHAUSTED_EVENT, {"rule": answer_rule.name}
                        )

                # --- THE EMPTY-ANSWER GATE (05 §K) ------------------------------
                #
                # NOT AN `elif`, and that is the whole placement decision. The two
                # branches above are entered when their complaint QUALIFIES, not when
                # they actually refuse — an exhausted allowance still takes the branch
                # and falls into its `else`. As an `elif` this gate would therefore go
                # silent on any round where an earlier gate had already spent its grant,
                # which is §J.3's starvation argument arriving one gate later: a silent
                # finish is exactly the outcome that must always get a second word in.
                #
                # `not refused_finalization` KEEPS THE ONE-REFUSAL-PER-ROUND-TRIP RULE
                # the chain expressed structurally: if either gate above refused, this
                # round already has its nudge and its cleared draft, and a second
                # refusal would overwrite the more specific complaint with a vaguer one.
                if (
                    not result.tool_calls
                    and not refused_finalization
                    and not (result.assistant_text or "").strip()
                ):
                    #
                    # The model ended the turn with NO tool calls AND NO prose. Nothing
                    # was refused, nothing failed, no error was raised — the round-trip
                    # simply carried no words, and every downstream stage handles that
                    # silently: exit #1 below persists nothing (`or None`), the `result`
                    # event carries `assistant_text: null`, and the UI renders
                    # `text || ""` beside `status: done`. The user gets a blank bubble
                    # labelled as a completed answer, and NOTHING anywhere records that
                    # it happened. It is the only turn outcome that produces no
                    # artifact of any kind.
                    #
                    # Measured causes are three, and this gate is deliberately blind to
                    # which: a genuinely empty completion, a completion cut short by the
                    # provider (`incomplete_reason`), and — until the same change fixed
                    # it in `model/openai_client.py` — a REFUSAL whose text the parser
                    # dropped, which looked identical from here. The response to all
                    # three is the same one round-trip back.
                    #
                    # LAST, and the ordering is not arbitrary: pending intents and
                    # untabled results are both MORE SPECIFIC complaints about a turn
                    # that at least said something, and each already clears the draft
                    # when it refuses. This check is what remains — the model said
                    # nothing anyone can act on — so it yields to a refusal made above
                    # it and fires whenever none was.
                    #
                    # ITS OWN ALLOWANCE (`kind="empty_answer"`), for the reason
                    # `session/models.py` records at the enum: sharing would make the
                    # silent finish the one failure the runtime could never get a
                    # second word in about, precisely because it is checked last.
                    if await finalization_gate.may_refuse("empty_answer"):
                        refused_finalization = True
                        self._observer(
                            EMPTY_ANSWER_REFUSED_EVENT,
                            # `""`, never `None`: the observer's allowlist filter keeps
                            # `str | int | float | bool` and drops everything else, so a
                            # `None` would vanish from the span and make "ordinary
                            # completion" indistinguishable from "attribute missing".
                            {"incomplete_reason": result.incomplete_reason or ""},
                        )
                        finalization_nudge = empty_answer_nudge_text(result.incomplete_reason)
                        # NO DRAFT TO CLEAR — `last_assistant_text` is already empty by
                        # the branch condition. Assigned anyway, and NOT as ceremony:
                        # the hard-ceiling and budget-cap paths return it verbatim, and
                        # `""` reaching them would be a blank answer surfacing on
                        # exactly the enforcement path this gate exists to close.
                        last_assistant_text = None
                    else:
                        # THIS GATE'S allowance for the window is spent: it refused once,
                        # handed back a round, and the model came back empty AGAIN. The
                        # posture is the answer-shape gate's — record and let the turn
                        # finish, never hard-lock — but the finish itself differs, and
                        # must: there is no answer in hand to pass through. The exit
                        # below substitutes `EMPTY_ANSWER_FALLBACK_TEXT` so the user is
                        # told what happened instead of shown a blank.
                        self._observer(
                            EMPTY_ANSWER_EXHAUSTED_EVENT,
                            {"incomplete_reason": result.incomplete_reason or ""},
                        )

                # --- THE ANSWER JUDGE, exit #1 (09 §C.1) ------------------------
                #
                # LAST, AFTER EVERY FREE CHECK, and the ordering is the cost model
                # rather than a precedence claim. §B/§J/§L/§K are regexes and counters;
                # this one is a MODEL CALL. `not refused_finalization` means the judge
                # is never paid for on a round some cheaper check already won — a
                # pasted markdown table costs zero judge tokens — and it keeps the
                # one-refusal-per-round-trip rule the chain expresses structurally.
                #
                # THE ORDER AGAINST §K IS FREE, for §L.5's reason: this requires
                # non-blank prose and §K fires only on blank, so the two conditions are
                # disjoint by construction. Placed after it anyway, so a silent finish
                # never reaches a model call.
                #
                # THE TWO SKIPS ARE INSIDE `_judge`, not here: a spent allowance and a
                # wall clock with no room for a rejection to act in both make the call
                # pointless, and both are invisible from the verdict.
                if (
                    not result.tool_calls
                    and not refused_finalization
                    and (result.assistant_text or "").strip()
                ):
                    # A FACTORY, so the session load and the corroboration KV reads
                    # happen only if a verdict could be acted on — see `_judge`.
                    async def _prose_brief(
                        _q: str = question,
                        _state: AnalysisState | None = analysis_state,
                        _draft: str = result.assistant_text or "",
                    ) -> JudgeBrief:
                        results, anchor, in_scope = await self._judge_results(
                            session_id, turn_index, credentials.column_scope
                        )
                        return self._judge_brief(
                            "exit_prose",
                            question=_q,
                            accum=accum,
                            analysis_state=_state,
                            date_anchor=anchor,
                            draft=_draft,
                            results=results,
                            # 09 §D.4 / 05 §L.7: `True` or "not checked", never `False`.
                            figure_corroborated=await self._corroborated_figures(
                                session_id, turn_index, _draft, in_scope
                            ),
                        )

                    verdict = await self._judge(
                        _prose_brief,
                        guard=guard,
                        gate=finalization_gate,
                        kind="answer_judge",
                    )
                    if not verdict.approved:
                        if await finalization_gate.may_refuse("answer_judge"):
                            refused_finalization = True
                            self._observer(
                                ANSWER_JUDGE_REFUSED_EVENT,
                                {"violation": verdict.violation, "site": "exit_prose"},
                            )
                            finalization_nudge = answer_judge_nudge_text(
                                result.assistant_text, verdict.feedback
                            )
                            # CLEAR THE DRAFT, for the reason all three gates above
                            # clear it: `last_assistant_text` is returned as
                            # `assistant_text` on the hard-ceiling and budget-cap paths,
                            # so a refused answer could otherwise reach the user there
                            # while never entering history.
                            last_assistant_text = None
                        else:
                            # THE PROSE PASSES. §J.5/§L.8's posture, and load-bearing
                            # here rather than inherited: this check reads MEANING, and
                            # a second refusal would be the runtime destroying an answer
                            # on the say-so of a model it cannot appeal. The event is
                            # what makes the pass visible — see 09 §F.1 for why this
                            # branch is reached only across a resume.
                            self._observer(
                                ANSWER_JUDGE_EXHAUSTED_EVENT,
                                {"violation": verdict.violation, "site": "exit_prose"},
                            )

                if not result.tool_calls and not refused_finalization:
                    # B1/D44 (2026-07-01 clarification) AND UI Slice 1: the union of
                    # this turn's tool-result provenance — the tag for the final
                    # assistant message (so it is scope re-filtered on replay exactly
                    # like the trail itself) AND the enriched `result` event's lineage.
                    # Computed ONCE here (the single fail-closed source of truth; do
                    # not re-derive in-loop).
                    turn_provenance = await self._compute_turn_provenance_union(session_id, turn_index)
                    # THE SILENT-FINISH SUBSTITUTION (05 §K). Reaching here with empty
                    # prose means the empty-answer gate above already spent its
                    # allowance on this window — it refused one finish, handed back a
                    # round, and the model came back with nothing a second time. The
                    # turn must end, so the only question left is WHAT THE USER IS
                    # SHOWN, and the honest sentence beats the blank bubble that
                    # shipped before it.
                    #
                    # `.strip()`, not a truthiness test: `"   "` renders exactly as
                    # blank as `""` does and must take the same path.
                    final_text = result.assistant_text
                    if not (final_text or "").strip():
                        final_text = EMPTY_ANSWER_FALLBACK_TEXT
                    return await self._finish(
                        session_id=session_id,
                        turn_index=turn_index,
                        status="done",
                        exit_label="no_tool_calls",
                        assistant_text=final_text,
                        tool_calls_made=tool_calls_made,
                        accum=accum,
                        provenance=turn_provenance,
                        # PERSISTED UNCONDITIONALLY NOW, like the `answerWithTable`
                        # exit — and for that exit's reason: `final_text` is non-empty
                        # by construction above, so the `or None` append guard this
                        # carried has nothing left to guard against. It existed to keep
                        # an EMPTY assistant message out of history; the substitution
                        # removes the empty message rather than the record of the turn.
                        # What it used to produce was a turn that reached the user as a
                        # blank bubble and reached `/session/history` as nothing at all
                        # — live and history disagreeing on exactly the turns that
                        # failed, which is the divergence `_finish` scrubs before
                        # persisting to prevent.
                        persist_text=final_text,
                        event=("loop_turn_done", {"tool_calls_made": tool_calls_made}),
                    )

                # A REFUSED EXIT-#1 ROUND FALLS THROUGH FROM HERE — deliberately, and
                # this is what charges it to the budget (05 §C.3). `result.tool_calls`
                # is empty on that path, so every step between here and
                # `guard.record_iteration` below is inert: both partitions produce
                # empty lists, `ask_user_call` is `None`, the dispatch loop body never
                # runs, and `designated_answer_text` stays `None`. Control therefore
                # reaches `record_iteration` and the existing hard-ceiling / budget-cap
                # handling, and only then loops back for the re-round.
                #
                # Returning `continue` here instead would make the forced re-round
                # FREE — no iteration, no tokens — leaving `BudgetGuard.exceeded` able
                # to trip only on the 60-second wall clock, which every resume
                # restarts. Anything added below that is NOT inert for an empty
                # `tool_calls` list must be guarded explicitly.

                # --- analysisState: PARTITION BEFORE CAPPING (03 §E.2) ------------
                #
                # `capped_tool_calls = result.tool_calls[:max_tool_calls_per_iteration]`
                # below SILENTLY DISCARDS the overflow — no error, no trail entry, no
                # signal to the model. Partitioning the already-capped list would
                # therefore lose a state call that follows eight substantive ones,
                # BEFORE any reordering could help, and the turn would run unprotected
                # with nothing to show for it. So the partition runs on the RAW list
                # and only the substantive remainder is capped.
                #
                # State calls are EXEMPT from `max_tool_calls_per_iteration` but
                # BOUNDED at `MAX_STATE_CALLS`: unbounded, N state calls in one
                # response are N full session-doc CAS read-modify-writes and N trail
                # entries inside `fit_request_to_budget`'s pinned region, with
                # `guard.exceeded` checked only AFTER each one. Batching is already
                # mandatory, so the surplus is the model misbehaving and is rejected
                # (with a trail entry, so it learns) inside the dispatch loop.
                #
                # THE EXEMPTION IS BOUNDED ON THE LIST ITSELF, not only on how many are
                # DISPATCHED. `MAX_STATE_CALLS` caps the state WRITES at two, but every
                # surplus call still costs a `append_trail_entry` — a full CAS
                # read-modify-write each, plus an entry pinned in the current-turn budget
                # region — so a degenerate response with 20 state calls was 18 store
                # writes in one round-trip, while over-cap `other_calls` are simply
                # dropped. The first `_MAX_SURPLUS_STATE_REJECTIONS` surplus calls are
                # still rejected WITH an entry (the model must learn why); the rest are
                # dropped exactly as over-cap `other_calls` are — no trail entry, no
                # response for that call id.
                #
                # Dispatching them FIRST reinstates the intent of the original
                # "commit state, then dispatch" barrier in sequential form. That
                # barrier was dropped along with bounded concurrency, but the
                # ORDERING guarantee it carried was never the concurrency part.
                state_calls = [
                    tc for tc in result.tool_calls if tc.name == UPDATE_ANALYSIS_STATE_TOOL_NAME
                ][: MAX_STATE_CALLS + _MAX_SURPLUS_STATE_REJECTIONS]
                other_calls = [
                    tc for tc in result.tool_calls if tc.name != UPDATE_ANALYSIS_STATE_TOOL_NAME
                ][: self._max_tool_calls_per_iteration]

                # SCANNED OVER THE RAW LIST, for the same reason the state partition is
                # (03 §E.2): `other_calls` is TRUNCATED to
                # `max_tool_calls_per_iteration`, and capping silently discards the
                # overflow. Selecting the pause from the capped list meant a batch like
                # `[runQuery, runQuery, askUser]` at cap 2 dispatched both queries and
                # NEVER ASKED THE USER — the model's clarifying question swallowed by the
                # runtime, the turn finishing `done` on a question it should have paused
                # on. Pre-Release-1 this scan ran over `result.tool_calls`; narrowing it
                # was an unintended consequence of introducing the partition.
                #
                # Selecting it here changes nothing about ORDER: `capped_tool_calls`
                # below still holds only the state calls when a pause is present, so
                # state is committed first and the pause is honoured after the loop
                # (03 §E.1) — an `askUser` past the cap now pauses exactly as one before
                # the cap always did.
                ask_user_call = next(
                    (tc for tc in result.tool_calls if tc.name == "askUser"), None
                )

                # S3: never dispatch an unbounded number of tool calls from one
                # model response — cap per iteration (RuntimeSettings-configurable,
                # default 8), applied to `other_calls` above. Any calls beyond the cap
                # are simply not dispatched this round (best-partial); nothing is
                # persisted for them, so they leave no trail entry and are not
                # "silently denied" — the model just does not see a response for them
                # and may re-request on the next round-trip if it still wants them.
                #
                # E.1: when the response ALSO pauses, only the STATE calls are
                # dispatched and the pause is honoured below — commit the state, then
                # pause. `askUser` short-circuits the whole response, so a batch of
                # `updateAnalysisState` + `askUser` (the natural round-1 shape for
                # "three asks, one ambiguous") used to discard the state write
                # entirely: no trail entry, no tool result, and D22 discards the
                # surrounding free text, so after the resume the model had no record
                # it ever tried, and "clarification cannot broaden the protected set"
                # would bite on a set that was never created. Everything OTHER than
                # the state calls still waits for the resume, exactly as before.
                capped_tool_calls = (
                    list(state_calls)
                    if ask_user_call is not None
                    else [*state_calls, *other_calls]
                )
                state_calls_dispatched = 0
                for tool_call in capped_tool_calls:
                    # K2 gate: this call, if it is one of the four, closes the late-init
                    # door for the rest of the turn. Recorded on the NAME and BEFORE
                    # dispatch, deliberately: `find_locking_tool` keys on the persisted
                    # entry's `tool_name` regardless of status, so a denial, an error and
                    # a guard-served repeat all lock it just as a success does. Erring
                    # toward suppression costs at most one note; erring the other way
                    # costs the model a non-retryable refusal it was told to walk into.
                    if tool_call.name in SUBSTANTIVE_TOOLS:
                        substantive_ran = True
                    # --- CALL-TIME INTENT TAGGING: split the tag off FIRST ---------
                    #
                    # `serves_intent` is a runtime concept the model puts on a
                    # `runQuery`/`runBlueprint`/`getTableSchema` to say which tracked
                    # intent the call is for. It must come off the arguments BEFORE
                    # anything else looks at them, and this is the only place it is
                    # removed:
                    #
                    #   - the live MCP server rejects an argument its own schema does
                    #     not declare, and `runBlueprint`'s executor validates its
                    #     arguments too — so an un-stripped tag breaks the real call;
                    #   - the repeated-read guard computes the signature inside
                    #     `ReadGuard.classify` from the args handed to it below, and
                    #     two identical `getTableSchema` fetches tagged for different
                    #     intents must still dedup to one signature;
                    #   - `TrailEntry.args` is what replay re-renders, and the tag has
                    #     its own persisted field there.
                    #
                    # `analysis_state` is the CURRENT window-local, and 03 §E.2 puts
                    # every state call first in this same batch — so a model that
                    # declares its intents and tags its work in ONE message still has
                    # its tags validated against a state that exists by the time the
                    # tagged calls are reached. An unknown/stale tag is DROPPED, not
                    # refused: the work runs, the entry is untagged, and the drop is
                    # reported (never the offending value — D25: an invalid tag is
                    # arbitrary model text, unlike a valid one).
                    call_args, serves_intent, tag_drop_reason = split_serves_intent(
                        tool_call.name, tool_call.arguments, analysis_state
                    )
                    if tag_drop_reason is not None:
                        self._observer(
                            "loop_intent_tag_dropped",
                            {"tool_name": tool_call.name, "reason": tag_drop_reason},
                        )
                        # K2: select this call to carry the corrective note on the
                        # NEXT rebuild. `no_live_state` ONLY — the other two reasons
                        # (`unknown_intent_id`, `not_a_string`) mean a state DOES
                        # exist and the tag was merely wrong, for which "declare your
                        # intents" is false advice. At most ONE per round: the set is
                        # emptied after each rebuild, so an empty set here means no
                        # call in THIS batch has claimed the note yet — a
                        # `[getTableSchema, getTableSchema]` batch tagged against no
                        # state says it once, not once per call.
                        if tag_drop_reason == "no_live_state" and not intent_note_call_ids:
                            intent_note_call_ids.add(tool_call.id)

                    # Repeated-idempotent-read guard (generalizes D94): the model
                    # re-issued an identical, already-served idempotent read (e.g.
                    # `getTableSchema(employee)` for the Nth time). Its result is
                    # DETERMINED provenance so the D94 ok+None sentinel never fires —
                    # yet re-fetching it is pure waste that can spin to the budget
                    # ceiling. Do NOT re-dispatch to the MCP; instead persist a
                    # data-free guard TrailEntry (ok+None, marked
                    # IDEMPOTENT_READ_ALREADY_SERVED_CODE) that `context/assembly.py`
                    # renders — via the SAME withheld-sentinel path D94 uses — as a
                    # "you already have this, proceed" nudge filling this repeat call's
                    # dangling tool-slot (keeping the OpenAI one-tool_call→one-result
                    # pairing valid). It is counted in `tool_calls_made` for reporting
                    # only; termination is bounded regardless because every round-trip
                    # still records an iteration against the budget window
                    # (`guard.record_iteration` below), so the worst case remains
                    # windows × iterations. The real served read stays in history under
                    # its own tool_call_id.
                    #
                    # AFTER the tag split, never before: two identical `getTableSchema`
                    # fetches tagged for different intents must dedup to ONE signature,
                    # so the guard is handed `call_args` (tag-stripped), not
                    # `tool_call.arguments`.
                    #
                    # `classify` also applies the TRIM-AWARE RE-FETCH EXEMPTION — a repeat
                    # whose serving result is no longer readable is re-dispatched for real,
                    # bounded per signature per window — and emits that decision's own
                    # events. See `ReadGuard.classify` for why the exemption exists and why
                    # it is bounded; `read_decision.declined` is the whole answer here.
                    read_decision = read_guard.classify(tool_call.name, call_args)
                    if read_decision.declined:
                        # A guarded `getBlueprint` STILL SATISFIES the blueprint-definition
                        # gate. Being deduped means the definition is already in the
                        # model's context (the guard's trim-aware exemption is what makes
                        # that true), which is exactly what the gate asks. Without this a
                        # deadlock is reachable:
                        # expand, run refused for an unrelated reason (a missing
                        # slot), re-expand defensively, get a data-free marker, and never
                        # satisfy the gate again.
                        #
                        # NOTE this is the OPPOSITE of 04 condition 5, which REJECTS the
                        # same marker as completion evidence. The two gates ask different
                        # questions — "does the model have the definition?" versus "did
                        # work actually happen?" — and a dedup answers yes to the first
                        # and no to the second.
                        if tool_call.name == "getBlueprint":
                            blueprint_gate.note_definition_in_context(call_args.get("id"))
                        tool_calls_made += 1
                        guard_entry = TrailEntry(
                            turn_index=turn_index,
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.name,
                            args=dict(call_args),
                            status="ok",
                            error_code=IDEMPOTENT_READ_ALREADY_SERVED_CODE,
                            # `None` (undetermined) is deliberate: it routes this
                            # data-free entry through the D94 stranded-sentinel path.
                            # `_compute_turn_provenance_union` excludes it by marker so
                            # it never poisons the turn's replay provenance.
                            provenance=None,
                            result_preview=None,
                            result_full_ref=None,
                            ts=_now_iso(),
                            # Recorded even though this entry can NEVER be valid
                            # evidence (04 condition 5 rejects the guard marker): the
                            # tag is what the model actually sent, and `resolve_tagged_
                            # evidence` needs to SEE it to refuse it with the actionable
                            # "cite the call that actually served this result" message
                            # rather than the misleading "nothing is tagged for it".
                            serves_intent=serves_intent,
                        )
                        await self._session_store.append_trail_entry(session_id, guard_entry)
                        self._observer(
                            "loop_repeated_idempotent_read_guarded",
                            repeated_read_guard_event(
                                tool_call.name, tool_call.id, call_args
                            ),
                        )
                        if guard.exceeded:
                            break
                        continue

                    # --- BLUEPRINT-DEFINITION GATE ---------------------------------
                    #
                    # `runBlueprint` is refused unless this turn already holds a
                    # SUCCESSFUL `getBlueprint` for the same id. A card carries no SQL,
                    # so without this the model routes on an authored prose `intent`
                    # string and can run — and confidently report — an analysis that
                    # measures something else entirely. See `loop/blueprint_gate.py` for
                    # the full rationale; the decision and its event are ITS, and only
                    # the effects below are the loop's.
                    #
                    # DECIDED HERE, BEFORE `_maybe_start_summary` AND BEFORE DISPATCH, so
                    # the executor never runs, no inner `runQuery` is issued, and the
                    # only trail entry written for this call is the refusal itself.
                    #
                    # A DEDUP-GUARDED `getBlueprint` SATISFIES THE GATE (see the guard
                    # branch above, which folds a guarded repeat's id in): being deduped
                    # means the definition is already in the model's context, which is
                    # exactly what the gate asks. That is the OPPOSITE of 04 condition 5,
                    # which REJECTS the same marker as completion evidence — the two
                    # gates ask different questions ("does the model have the
                    # definition?" versus "did work actually happen?").
                    #
                    # THE RESUME PATH DOES NOT PASS THROUGH HERE. A mid-DAG checkpoint
                    # resume re-enters `self._blueprint_executor.resume(...)` directly from
                    # `_resume_blueprint` and appends its own `runBlueprint` trail entry;
                    # it never reaches this dispatch site, so a resumed blueprint is
                    # structurally ungated rather than exempted by a branch. A resume is
                    # the continuation of an already-gated invocation, not a fresh decision
                    # to run a blueprint, so that is the correct outcome — and
                    # `tests/runtime/loop/test_blueprint_definition_gate.py` pins it, so a
                    # refactor that routes resumes through dispatch cannot silently start
                    # gating them.
                    #
                    # NOT APPLIED TO AN UNWIRED `runBlueprint` (`handler is None`): there is
                    # no blueprint stack at all, so "expand it first" would send the model
                    # after a tool that cannot help it, replacing the honest
                    # `RUN_BLUEPRINT_UNAVAILABLE` → raw-loop fallback (§6) with a loop.
                    handler = self._runtime_tools.get(tool_call.name)
                    gate_refusal: ToolResult | None = None
                    if tool_call.name == "runBlueprint" and handler is not None:
                        gate_refusal = blueprint_gate.check_run_blueprint(call_args)

                    # LLM-generated progress summary (opt-in, `progress_summary_enabled`):
                    # fire the value-rich present-tense line CONCURRENTLY, BEFORE dispatch
                    # and WITHOUT awaiting it, so it never adds latency to the tool. The
                    # instant `tool_dispatch_start` template label still fires as today
                    # (inside the dispatcher / the runtime tools); this line is additive,
                    # arriving when ready. No-op when the feature is off. Skipped for a
                    # gated call: nothing is about to run, so narrating it would be a lie.
                    if gate_refusal is None:
                        self._maybe_start_summary(tool_call.name, tool_call.id, call_args)

                    # Runtime-tool registry (read-tools-design §2): a runtime tool
                    # (`resolveValues` + the three read tools) is intercepted here —
                    # it never reaches `dispatch` under its own name (only any inner
                    # tool it issues does). It returns the SAME `ToolResult`
                    # dataclass, so the trail/budget path below is unchanged and it
                    # counts as exactly one `tool_calls_made`. An advertised runtime
                    # tool that is not wired returns a clean local unavailable error
                    # (§6), never an incoherent MCP unknown-tool denial.
                    # `handler` was resolved above, at the gate.
                    if gate_refusal is not None:
                        # First in the chain: nothing else may dispatch this call.
                        tool_result = gate_refusal
                    elif tool_call.name == UPDATE_ANALYSIS_STATE_TOOL_NAME:
                        # 03 §E.2: the cap exemption is BOUNDED. The surplus is
                        # REJECTED rather than dropped — it gets a trail entry through
                        # the normal path below, so the model sees why and batches
                        # next time, instead of silently losing a write.
                        #
                        # Only the FIRST `_MAX_SURPLUS_STATE_REJECTIONS` of them reach
                        # here at all: the partition above truncates `state_calls` to
                        # `MAX_STATE_CALLS + _MAX_SURPLUS_STATE_REJECTIONS`, so the
                        # number of store writes a single response can force is fixed,
                        # not proportional to how many calls the model emitted.
                        state_calls_dispatched += 1
                        if state_calls_dispatched > MAX_STATE_CALLS:
                            tool_result = surplus_state_call_rejected()
                            self._observer(
                                "loop_analysis_state_rejected",
                                {"reason": "surplus_state_call", "intent_count": 0},
                            )
                        elif handler is not None:
                            tool_result = await self._run_runtime_tool(
                                handler,
                                tool_call.name,
                                call_args,
                                credentials,
                                turn_context,
                            )
                        else:  # pragma: no cover - always wired by app.py
                            tool_result = _runtime_tool_internal_error(tool_call.name)
                    elif handler is not None:
                        tool_result = await self._run_runtime_tool(
                            handler,
                            tool_call.name,
                            call_args,
                            credentials,
                            turn_context,
                        )
                    elif tool_call.name in _RUNTIME_TOOL_UNAVAILABLE_CODE:
                        tool_result = _runtime_tool_unavailable(
                            tool_call.name, _RUNTIME_TOOL_UNAVAILABLE_CODE[tool_call.name]
                        )
                    else:
                        # `question` (this turn's raw user text) is handed over for
                        # ONE purpose: an over-cap `getTableSchema` keeps every column
                        # NAMED and spends its remaining detail budget on the columns
                        # the question is about (`dispatch/schema_preview.py`). It
                        # orders that fit and nothing else — it is never written into
                        # the result, the trail, or a span (D25). This is the only
                        # dispatch call site that supplies it.
                        tool_result = await self._tool_dispatcher.dispatch(
                            tool_call.name,
                            call_args,
                            credentials,
                            tool_call_id=tool_call.id,
                            question=question,
                        )

                    # REFRESH THE ENFORCEMENT LOCAL (05 §E). The state call just wrote
                    # the state and returned it in full, so the local is updated from
                    # the result rather than re-read from the store. A rejected call
                    # (or an unreadable result) leaves the loaded value standing.
                    if tool_call.name == UPDATE_ANALYSIS_STATE_TOOL_NAME:
                        refreshed = refreshed_analysis_state(tool_result, turn_index)
                        if refreshed is not None:
                            analysis_state = refreshed

                    # --- FINALIZATION ENFORCEMENT, terminal exit #2 (05 §B.1) -----
                    #
                    # A successful `answerWithTable` carrying non-blank prose ENDS the
                    # turn once the batch drains. Refuse it here — BEFORE the trail
                    # entry is written, so the persisted entry IS the refusal — and
                    # crucially BEFORE `_resolve_answer_sql` below, which fires the two
                    # dormant `hooks/answer_table.py` seams: a designation that is
                    # about to be refused must not fire the answer-table lifecycle,
                    # and checking afterwards would also clobber the more actionable
                    # blueprint-not-run message with this one.
                    #
                    # The full terminal condition is mirrored exactly (`ok` + a `dict`
                    # of arguments + non-blank `answer`), because a call that would NOT
                    # have terminated the turn is not a finalization and must not be
                    # refused as one.
                    if (
                        tool_call.name == ANSWER_TABLE_TOOL_NAME
                        and tool_result.status == "ok"
                        and isinstance(call_args, dict)
                        and clean_answer_text(call_args.get("answer")) is not None
                    ):
                        pending_at_answer = pending_intents(analysis_state)
                        if pending_at_answer:
                            if await finalization_gate.may_refuse("intents"):
                                self._observer(
                                    "loop_finalization_refused",
                                    {
                                        "exit": "answer_with_table",
                                        "pending_count": len(pending_at_answer),
                                    },
                                )
                                # A RETRYABLE error, so the turn continues and the
                                # batch drains normally: `tool_result` is no longer
                                # `ok`, so neither `_resolve_answer_tables` below nor
                                # the terminal-exit check fires for this call.
                                tool_result = finalization_blocked(pending_at_answer)
                            else:
                                analysis_state = await self._force_block_pending_intents(
                                    session_id=session_id,
                                    turn_index=turn_index,
                                    state=analysis_state,
                                    reason_code="ENFORCEMENT_EXHAUSTED",
                                )
                                self._observer(
                                    "loop_enforcement_exhausted",
                                    {"intent_count": len(pending_at_answer)},
                                )

                    # answerWithTable naming a blueprint it never ran: turn the call
                    # into a retryable NUDGE rather than letting it terminate the turn
                    # with no table. Done HERE, before the trail entry is written, so the
                    # persisted entry IS the nudge and the model sees it on the next
                    # round-trip. Only when there is no raw `sql` to fall back on, and
                    # only after `_resolve_answer_tables` has had its go — which includes
                    # giving the dormant ON_ANSWER_TABLE_UNRESOLVED hook first refusal, so
                    # a registered hook that supplies a replacement wins over the nudge.
                    #
                    # AN ITEM OF `tables` IS TREATED EXACTLY LIKE THE TOP-LEVEL PAIR: it
                    # names a blueprint that did not run, the whole call is refused, and
                    # the model is told which one. Dropping the item instead would
                    # silently lose a deliverable's table, which is the failure
                    # multi-table exists to fix.
                    resolved_answer_tables: list[AnswerTable] = []
                    if (
                        tool_call.name == ANSWER_TABLE_TOOL_NAME
                        and tool_result.status == "ok"
                        and isinstance(call_args, dict)
                    ):
                        # Resolved ONCE — the hooks fire here and nowhere else.
                        (
                            resolved_answer_tables,
                            unresolved_blueprint,
                            carried_designation,
                        ) = await self._resolve_answer_tables(
                            call_args,
                            blueprint_runs=accum.blueprint_runs,
                            credentials=credentials,
                            session_id=session_id,
                            turn_index=turn_index,
                        )
                        if unresolved_blueprint is not None:
                            _logger.info(
                                "answerWithTable named blueprint %r that did not run this "
                                "turn — nudging the model to run it first (session=%s)",
                                unresolved_blueprint,
                                session_id,
                            )
                            tool_result = _answer_table_blueprint_not_run(unresolved_blueprint)
                        elif (
                            not carried_designation
                            and answer_shape.multi_row_calls
                            and clean_answer_text(call_args.get("answer")) is not None
                        ):
                            # THE EMPTY DESIGNATION (08 §O). The model called the table
                            # tool, named no table, and — because the call carries prose
                            # — would TERMINATE the turn right here, through the exit the
                            # 05 §J shape gate does not watch. Measured live as
                            # `status=done`, no table, no event, no log: the user asked
                            # for a breakdown, the turn held six rows of it, and the
                            # answer was prose. See `answer_table_no_table_designated`.
                            #
                            # `answer_shape.multi_row_calls` SCOPES IT, and the scope is the
                            # whole of the false-positive protection: with nothing
                            # multi-row in hand there is no table being withheld, and a
                            # zero-row "none found" answered in prose is CORRECT (live
                            # q6). Nudging that would charge a right answer a round-trip.
                            #
                            # THE NON-BLANK `answer` CHECK MIRRORS THE TERMINAL
                            # CONDITION, for the reason the pending-intents refusal just
                            # above states: a call that would NOT have ended the turn is
                            # not a finalization and must not be refused as one. A
                            # blank-`answer` empty call is a habit call, not an answer —
                            # it does not terminate anything, so nothing is being
                            # silently lost, and refusing it would burn this window's
                            # allowance on it and leave the REAL prose finish that
                            # follows unrefusable. The flag fix below is what covers that
                            # case: it stops the empty call disarming the exit-#1 gate.
                            #
                            # SAME ALLOWANCE AS THE SHAPE GATE (`kind="answer_shape"`),
                            # because it is the same complaint arriving through the other
                            # exit — otherwise one mistake could be refused twice in a
                            # window, once per exit. When the grant is spent the prose
                            # PASSES and the turn ends: never a hard lock, the posture
                            # `ENFORCEMENT_EXHAUSTED` takes for intents. The event is
                            # emitted in `_resolve_answer_tables` either way, so the
                            # behaviour stays visible after the allowance is gone.
                            if await finalization_gate.may_refuse("answer_shape"):
                                _logger.info(
                                    "answerWithTable designated no table while %d "
                                    "multi-row result(s) went untabled — nudging "
                                    "(session=%s)",
                                    answer_shape.multi_row_calls,
                                    session_id,
                                )
                                self._observer(
                                    ANSWER_SHAPE_REFUSED_EVENT,
                                    {"multi_row_calls": answer_shape.multi_row_calls},
                                )
                                tool_result = answer_table_no_table_designated()
                            else:
                                self._observer(ANSWER_SHAPE_EXHAUSTED_EVENT, {})
                        else:
                            self._observe_uncovered_intents(
                                analysis_state,
                                tables=resolved_answer_tables,
                                result_sql_by_call_id=accum.result_sql_by_call_id,
                            )
                    # --- THE ANSWER JUDGE, exit #2 (09 §C.2) --------------------
                    #
                    # THE SITE THAT MATTERS MOST, and the one 05 §L.9 leaves
                    # entirely unchecked today: no answer rule runs here, so a
                    # multi-part answer that closes both intents, designates one
                    # table and discusses one subject ends `done` with no event and
                    # no log line. That is the population where "was every part
                    # answered" has teeth — and §L.5 records the route into it, the
                    # `markdown_table` nudge telling the model to call
                    # answerWithTable instead.
                    #
                    # THE TERMINAL CONDITION IS MIRRORED EXACTLY (`ok` + a `dict` of
                    # arguments + non-blank `answer`), as the pending-intents
                    # refusal above mirrors it: a call that would NOT have ended the
                    # turn is not a finalization and must not be judged as one. The
                    # `ok` half also means every refusal above — pending intents,
                    # blueprint-not-run, empty designation, the unrun query — has
                    # already rewritten `tool_result` and the judge is not paid for.
                    #
                    # THE ALLOWANCE IS SHARED WITH EXIT #1 (`kind="answer_judge"`):
                    # the two exits are two doors out of ONE finish, so a model
                    # pushed here by an exit-#1 nudge must not be judged twice for
                    # the same answer.
                    if (
                        tool_call.name == ANSWER_TABLE_TOOL_NAME
                        and tool_result.status == "ok"
                        and isinstance(call_args, dict)
                        and clean_answer_text(call_args.get("answer")) is not None
                    ):
                        table_draft = clean_answer_text(call_args.get("answer")) or ""
                        if judge_refusal_this_round is not None:
                            # A SECOND `answerWithTable` IN THE SAME BATCH, after the
                            # judge already refused one. It gets the SAME refusal for
                            # free — no second model call, no second claim.
                            #
                            # THIS BRANCH IS THE WHOLE FIX for a defect that shipped
                            # past review once. Without it the sequence was: call A
                            # judged and refused (which records the grant), call B
                            # reaches `_judge`, `has_spent` reports the allowance gone,
                            # the judge is SKIPPED, and skipping returns APPROVED — so
                            # B stayed `ok` and TERMINATED THE TURN in the very round
                            # the judge had refused it. The user received a near-copy of
                            # the refused answer and the feedback reached the model
                            # never. `may_refuse`'s own free-refusal path exists for
                            # exactly this shape (05 §C.2) and the judge could not reach
                            # it, because its cost-avoidance peek runs first.
                            tool_result = answer_judge_rejected(judge_refusal_this_round)
                        elif finalization_gate.refused_this_round:
                            # ANOTHER gate refused earlier in this same round-trip (the
                            # empty-designation nudge, say). One refusal per round-trip
                            # is the rule the whole chain expresses, so the judge does
                            # not run — and, unlike the shape above, has nothing to
                            # re-issue. Skipping here also keeps the judge off the
                            # free-grant path, where a refusal would be issued without a
                            # claim and the window's bound would quietly become two.
                            self._observer(
                                ANSWER_JUDGE_SKIPPED_EVENT, {"reason": "round_refused"}
                            )
                        else:

                            async def _table_brief(
                                _q: str = question,
                                _state: AnalysisState | None = analysis_state,
                                _draft: str = table_draft,
                                _tables: tuple[AnswerTable, ...] = tuple(
                                    resolved_answer_tables
                                ),
                            ) -> JudgeBrief:
                                results, anchor, in_scope = await self._judge_results(
                                    session_id, turn_index, credentials.column_scope
                                )
                                return self._judge_brief(
                                    "exit_table",
                                    question=_q,
                                    accum=accum,
                                    analysis_state=_state,
                                    date_anchor=anchor,
                                    draft=_draft,
                                    results=results,
                                    designated_tables=tuple(
                                        (table.caption, table.sql) for table in _tables
                                    ),
                                    figure_corroborated=await self._corroborated_figures(
                                        session_id, turn_index, _draft, in_scope
                                    ),
                                )

                            table_verdict = await self._judge(
                                _table_brief,
                                guard=guard,
                                gate=finalization_gate,
                                kind="answer_judge",
                            )
                            if not table_verdict.approved:
                                if await finalization_gate.may_refuse("answer_judge"):
                                    self._observer(
                                        ANSWER_JUDGE_REFUSED_EVENT,
                                        {
                                            "violation": table_verdict.violation,
                                            "site": "exit_table",
                                        },
                                    )
                                    # A RETRYABLE error, so the turn continues and the
                                    # batch drains normally — `tool_result` is no longer
                                    # `ok`, so the terminal-exit check below does not
                                    # fire for this call and the persisted entry IS the
                                    # refusal. NO DRAFT CLEAR is needed: exit #2 keeps
                                    # the model's prose in `TrailEntry.args`.
                                    tool_result = answer_judge_rejected(
                                        table_verdict.feedback
                                    )
                                    # Remembered for the REST OF THIS BATCH, so a second
                                    # answerWithTable is refused with the same words
                                    # rather than sailing through the skip above.
                                    judge_refusal_this_round = table_verdict.feedback
                                else:
                                    # THE ANSWER PASSES. §J.5/§L.8's posture — the runtime
                                    # never hard-locks a turn, and this check reads MEANING
                                    # rather than truth.
                                    #
                                    # ⚠ THIS `else` BINDS TO `may_refuse`, NOT TO
                                    # `approved`. It sat one level out for a while and the
                                    # consequence was invisible offline: an APPROVED answer
                                    # took this branch and published
                                    # `loop_answer_judge_exhausted` with an empty
                                    # `violation`, so the metric that says "the judge was
                                    # overruled" fired on every clean tabled answer. Caught
                                    # by reading a live Phoenix trace, not by a test.
                                    self._observer(
                                        ANSWER_JUDGE_EXHAUSTED_EVENT,
                                        {
                                            "violation": table_verdict.violation,
                                            "site": "exit_table",
                                        },
                                    )

                    # §2.5 pausing-runtime-tool seam: a runtime tool may signal a
                    # pause (today only `runBlueprint`, on a slot-resolution
                    # `askUser`). This GENERALIZES the terminal `askUser` branch
                    # above — the loop writes the checkpoint and returns
                    # `paused_ask_user` exactly as for `askUser`, before persisting a
                    # trail entry or counting the call (a paused tool did not
                    # complete, mirroring `askUser`). A dispatched MCP tool never
                    # sets `.pause`, so this is inert on the normal path.
                    if tool_result.pause is not None:
                        # Computed INSIDE the branch that consumes it. It used to be
                        # read once per tool call and used on the ~0.1% of them that
                        # pause; `TurnAccumulators.envelope` is pure (no store, no
                        # observer — `turn_accumulators.answer_envelope` and
                        # `rollup_verification`/`AnswerTable.to_doc` below it only build
                        # values), so where it is called cannot be observed, only how
                        # often. `tests/runtime/loop/test_turn_exit_contract.py::
                        # test_an_in_loop_pause_carries_the_envelope_of_the_same_batch`
                        # pins that it is still read LATE — after the folds of the
                        # calls that drained before this one.
                        envelope = accum.envelope()
                        return await self._pause_from_runtime_tool(
                            session_id=session_id,
                            pause=tool_result.pause,
                            window_count=window_count,
                            assistant_text=result.assistant_text,
                            tool_calls_made=tool_calls_made,
                            # Fix 2: surface whatever succeeded earlier in this window.
                            sql_executed=accum.sql_executed,
                            envelope=envelope,
                            assumptions=accum.assumptions,
                            # Carry this call's intent tag onto the checkpoint (see
                            # `_pause_from_runtime_tool`): the trail entry for this work
                            # is written after the resume, under a new id.
                            serves_intent=serves_intent,
                        )
                    tool_calls_made += 1

                    result_full_ref: str | None = None
                    if tool_result.result_full is not None:
                        result_full_ref = await self._session_store.write_full_result(
                            session_id, str(uuid.uuid4()), tool_result.result_full
                        )

                    entry = TrailEntry(
                        turn_index=turn_index,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        args=dict(call_args),
                        status=tool_result.status,
                        error_code=tool_result.error_code,
                        provenance=tool_result.provenance,
                        result_preview=tool_result.result_preview,
                        result_full_ref=result_full_ref,
                        ts=_now_iso(),
                        authoritative=tool_result.authoritative,
                        denial_detail=tool_result.denial_detail,
                        # J7 — set only by a `runBlueprint` whose blueprint DECLARES a
                        # data-anchored window; `None` for every other call, so every other
                        # entry serialises byte-identically.
                        window_note=tool_result.window_note,
                        # The validated tag (or `None`). Persisted on the entry rather
                        # than left in `args`, so it survives replay/resume and is
                        # readable by `updateAnalysisState` without re-parsing
                        # arguments — and so it never re-enters the model's own
                        # rendered tool call, where it would be noise.
                        serves_intent=serves_intent,
                        # 08 §D.2 — ADDITIVE, and deliberately NOT this entry's own
                        # `provenance` (which stays `frozenset()`): the D44 USES set of
                        # each designated query, positionally parallel to the tables
                        # `resolve_designations` reads back out of `args`. It exists so
                        # a scope narrowing on RELOAD can drop the out-of-scope tables
                        # instead of nothing, and it must never reach
                        # `_compute_turn_provenance_union` — that union is fail-closed,
                        # so one unparseable designated query would collapse it and
                        # drop the turn's whole answer from every later replay.
                        answer_table_provenance=(
                            tuple(table.provenance for table in resolved_answer_tables)
                            if resolved_answer_tables
                            else None
                        ),
                    )
                    await self._session_store.append_trail_entry(session_id, entry)

                    # UI Slice 1 (§3.2): accumulate the enriched-result fields from
                    # this SUCCESSFUL tool call (runQuery arg SQL + preview; runBlueprint
                    # `result_full` SQL/blueprint_id/verify + preview). Shared with the
                    # blueprint approval-resume seed path via `accumulate_enrichment`.
                    accum.note_enrichment(tool_call.name, call_args, tool_result)
                    accum.capture_blueprint_run(
                        tool_call.name,
                        tool_result,
                        arguments=call_args if isinstance(call_args, dict) else None,
                    )
                    # The intent-coverage CHECK's raw material (08 §B.1): which query's
                    # rows this call produced, keyed by the id an intent cites as its
                    # evidence. NOT a source for the tables themselves — deriving those
                    # from the evidence call would page the agent's own LIMIT-ed reading
                    # query and silently truncate every grid.
                    accum.note_result_sql(tool_call.name, tool_call.id, call_args, tool_result)
                    # ANSWER-SHAPE GATE (05 §J): count this call if it is a successful
                    # data-returning call with more than one row. Read from the same
                    # `result_preview` that was just persisted on the entry above, so the
                    # in-window count and the trail seed can never disagree about what
                    # happened. Counted AFTER the finalization/blueprint-not-run rewrites
                    # of `tool_result`, so a refused call (now non-`ok`) is not counted.
                    answer_shape.note_call(
                        tool_call.name, tool_result.status, tool_result.result_preview
                    )
                    # recordAssumptions (docs/decisions/ui-assumptions-contract.md):
                    # fold a SUCCESSFUL call's plain-English assumptions into the
                    # turn accumulator, same discipline as the enrichment above.
                    accum.note_assumptions(tool_call.name, call_args, tool_result)
                    # answerWithTable (composite/answer_with_table.py): the
                    # model-designated answer tables. Same discipline again — read from
                    # the call ARGUMENTS on success — except LAST designation wins,
                    # over the whole SET, since a turn has one answer.
                    accum.note_answer_tables(
                        tool_call.name, tool_result, resolved_answer_tables
                    )
                    # TERMINAL: a successful `answerWithTable` carries the final prose,
                    # so the turn ends on it. Recorded here and acted on AFTER the whole
                    # tool batch drains, so a model that batches recordAssumptions +
                    # answerWithTable still gets both folded before the turn closes.
                    if (
                        tool_call.name == ANSWER_TABLE_TOOL_NAME
                        and tool_result.status == "ok"
                    ):
                        # ANSWER-SHAPE GATE (05 §J): the turn HAS tabled its answer, so
                        # the gate is done for this turn.
                        #
                        # SET FROM SUBSTANCE, NOT FROM THE CALL (08 §O). This read
                        # `status == "ok"` alone, and a live probe showed what that
                        # bought: a mid-turn `{answer: "", tables: []}` succeeds — the
                        # tool is stateless and refuses nothing — disarmed the gate with
                        # ZERO designations, and the model's later bare-prose finish then
                        # passed unrefused. The flag is supposed to mean "the user has a
                        # grid", so it is set only when one exists.
                        #
                        # `accum.has_answer_tables` (the accumulator, folded just above)
                        # rather than `resolved_answer_tables` alone, because a LATER
                        # call that designates nothing deliberately leaves an EARLIER
                        # good set intact (`note_answer_tables`: "a malformed retry
                        # cannot silently drop a good table"). Reading only this call's
                        # resolution would re-arm the gate on that retry and refuse a
                        # turn that has its table.
                        #
                        # Still set for the blank-`answer` call that does not terminate,
                        # PROVIDED it designated something: those tables reach the user
                        # through the envelope, which is what the gate protects.
                        if resolved_answer_tables or accum.has_answer_tables:
                            answer_shape.note_answer_succeeded()
                        designated_answer_text = (
                            clean_answer_text(call_args.get("answer"))
                            if isinstance(call_args, dict)
                            else None
                        )

                    # Record a SUCCESSFUL idempotent read so an identical repeat later
                    # this turn is caught by the guard above. Only `ok` reads are
                    # "already served" — a denied/errored read is NOT recorded, so a
                    # legitimate retry after a transient failure is never suppressed.
                    # The DECISION is handed back rather than the arguments, so the
                    # signature recorded is provably the one that was tested.
                    if read_decision.is_idempotent_read and tool_result.status == "ok":
                        read_guard.record_served(read_decision, tool_call.id)

                    # Record a SUCCESSFUL `getBlueprint` so the blueprint-definition gate
                    # lets that id run. STAGED in the gate's per-response set, not
                    # committed until the batch drains (see `commit_round` below the
                    # loop).
                    if (
                        tool_call.name == "getBlueprint"
                        and tool_result.status == "ok"
                        and isinstance(call_args, dict)
                    ):
                        blueprint_gate.note_definition_in_context(call_args.get("id"))

                    # S3: also check the budget INSIDE the per-tool-call loop (not only
                    # once per outer iteration) so a slow batch of capped calls that
                    # blows the wall-clock window mid-dispatch stops cleanly instead of
                    # finishing the whole batch regardless.
                    #
                    # WHAT THIS CAN ACTUALLY TRIP ON: the WALL CLOCK, and only it (A2).
                    # `guard.exceeded` reads three counters, but the other two cannot
                    # change here — this round's `record_iteration` runs AFTER the batch
                    # drains (below, once the round-trip's `total_tokens` is known), and
                    # if the iteration or token-spend arm had already been over its cap
                    # on a PREVIOUS round the loop would have ended that round instead of
                    # dispatching this batch. Elapsed time is the one input that advances
                    # while tools run, so it is the one arm that can newly trip mid-batch.
                    #
                    # That is the intended behaviour, not a gap to close: iterations and
                    # spend are charged per ROUND-TRIP, so charging them mid-batch would
                    # mean charging a round the model has not been billed for yet.
                    # Documented because the plain reading of "check the budget" promises
                    # all three, and a future reader debugging a batch that ran past an
                    # iteration cap should not have to re-derive this from the ordering.
                    if guard.exceeded:
                        break

                # K2 SUPPRESSION, the fold. HERE, once the batch has drained, and NOT at
                # the drop site: within one batch the tagged call and the substantive one
                # can arrive in EITHER order — `[tagged getTableSchema, untagged runQuery]`
                # selects the note before the runQuery is seen, and the tagged call may
                # itself BE the runQuery — so a decision made per call would be right only
                # half the time. Draining first makes the batch's ordering irrelevant.
                #
                # The note is dropped, not reworded: with the door shut there is no true
                # corrective advice to give, and the pre-slice behaviour (silent drop, plus
                # `loop_intent_tag_dropped`, which fired above and is untouched) is the
                # correct fallback.
                if substantive_ran:
                    intent_note_call_ids.clear()

                # BLUEPRINT-DEFINITION GATE, the fold. HERE, once the batch has drained,
                # and deliberately not mid-batch: ids expanded in THIS response become
                # runnable only from the NEXT one, because the result of a `getBlueprint`
                # issued in this response does not reach the model until the next
                # round-trip. See `BlueprintGate.commit_round` for the full rationale —
                # the position of this call is the half of it that lives here.
                blueprint_gate.commit_round()

                # TERMINATION: pause. Honoured AFTER the state calls above have been
                # committed (03 §E.1) and BEFORE anything else in the batch is
                # dispatched — `capped_tool_calls` held only the state calls on this
                # path, so every other call still waits for the resume exactly as it
                # always did.
                if ask_user_call is not None:
                    # --- THE askUser JUDGE (09 §C.3) ------------------------
                    #
                    # THE CHEAPEST SITE IN THE DESIGN TO REJECT AT, and the only one
                    # where the proposed "the question names a COLUMN instead of a
                    # THING" complaint can be made at all: `askUser` is intercepted
                    # here and never reaches either terminal exit, so 05 §L.9's
                    # "pauses are not gated" leaves it unchecked today.
                    #
                    # NOTHING IS AT RISK. The pause has not happened, so the user has
                    # seen nothing; a rejection costs one round-trip that is invisible
                    # to them, where a rejection at an answer exit risks an answer the
                    # model already had.
                    #
                    # JUDGED ON THE RAW ARGUMENT, BEFORE THE SCRUB BELOW, and the
                    # ordering is the whole point. `scrub_answer_prose` already turns
                    # "which AnnualSalary did you mean?" into "which [schema detail
                    # withheld] did you mean?" — 05 §L.3's trap exactly, a
                    # half-redacted string that is neither usable nor honest. Judging
                    # the scrubbed form would ask the model to repair a string it did
                    # not write; judging the raw one gets a question that never needed
                    # redacting.
                    raw_question = str(ask_user_call.arguments.get("question", ""))
                    async def _ask_brief(
                        _q: str = question,
                        _state: AnalysisState | None = analysis_state,
                        _asked: str = raw_question,
                    ) -> JudgeBrief:
                        # No store reads at this site — the question is judged on the
                        # turn's own bookkeeping — but the factory shape is kept so all
                        # three sites read identically.
                        return self._judge_brief(
                            "ask_user",
                            question=_q,
                            accum=accum,
                            analysis_state=_state,
                            pending_question=_asked,
                        )

                    ask_verdict = await self._judge(
                        _ask_brief,
                        guard=guard,
                        gate=finalization_gate,
                        kind="ask_user_judge",
                    )
                    if not ask_verdict.approved:
                        # ITS OWN ALLOWANCE (`ask_user_judge`), never the answer
                        # judge's: a rejected ANSWER earlier in this window must not
                        # silence the check that keeps a schema-worded question off
                        # the user's screen, and the two complaints are made at
                        # different moments about different text.
                        if await finalization_gate.may_refuse("ask_user_judge"):
                            self._observer(
                                ASK_USER_JUDGE_REFUSED_EVENT,
                                {"violation": ask_verdict.violation},
                            )
                            finalization_nudge = ask_user_judge_nudge_text(
                                raw_question, ask_verdict.feedback
                            )
                            # NO DRAFT TO CLEAR — `last_assistant_text` belongs to the
                            # ANSWER exits and this path does not finish. What is
                            # discarded is the `askUser` call itself, which was never
                            # persisted (it is intercepted, never dispatched), so the
                            # nudge's echo is the model's only surviving copy.
                            #
                            # The state calls of this batch HAVE been dispatched and
                            # persisted already (03 §E.1) — they are on the trail and
                            # replay normally on the next round-trip, so re-rounding
                            # here does not lose the ledger update that rode along.
                            continue
                        # THE WINDOW'S ALLOWANCE IS SPENT. THE PAUSE PROCEEDS — the
                        # runtime never hard-locks a turn (05 §J.5), and here shipping
                        # the question is strictly better than the alternatives:
                        # refusing again spends the window on a disagreement, and
                        # suppressing the pause would end the turn with no answer and
                        # no question.
                        #
                        # ⚠ REACHED ONLY WHEN THE PERSISTED CLAIM IS SPENT AND THIS
                        # GATE DOES NOT KNOW IT. Within one `_run_loop_body` the
                        # `has_spent` peek inside `_judge` pre-empts this branch and no
                        # model call is made at all — so the ordinary
                        # refuse-then-ask-again sequence emits `..._skipped`, NOT this
                        # event. What lands here is the askUser RESUME: `window_count`
                        # is unchanged across one (D55), a fresh gate is built on
                        # re-entry, and the claim it finds was spent by the previous
                        # invocation. Keeping both is deliberate — the peek is the
                        # cheap common case, and this is the honest handler for the
                        # case the peek cannot see, which would otherwise drop a
                        # judged rejection with no event at all.
                        self._observer(
                            ASK_USER_JUDGE_EXHAUSTED_EVENT,
                            {"violation": ask_verdict.violation},
                        )
                    # THE QUESTION IS MODEL PROSE SHOWN TO THE USER, so it is
                    # scrubbed exactly like an answer (ISSUES I1) — "which
                    # department_id did you mean?" discloses as much as an answer
                    # would. Scrubbed HERE, at the single point the string is
                    # extracted, so the persisted `PauseCheckpoint`, the
                    # `TurnOutcome.pending_question` projected from it, and the
                    # `loop_paused_ask_user` event below all carry the same text —
                    # a resume replays the checkpoint, so a question scrubbed only
                    # on the way out would come back unscrubbed. `provenance=None`:
                    # this is a pause, and none is computed on this path.
                    #
                    # `options` are NOT scrubbed: they are the answer choices the
                    # user clicks, and are values by construction.
                    question, question_redactions = scrub_answer_prose(
                        str(ask_user_call.arguments.get("question", "")), provenance=None
                    )
                    if question_redactions:
                        self._observer(
                            ANSWER_PROSE_REDACTED_EVENT,
                            {
                                "redaction_count": question_redactions,
                                "exit": "ask_user_question",
                            },
                        )
                    options = ask_user_call.arguments.get("options")
                    checkpoint = PauseCheckpoint(
                        reason="askUser",
                        pending_question={"question": question, "options": options},
                        awaiting="user_answer",
                        consumed=False,
                        budget_window_count=window_count,
                    )
                    # Best-effort partials (§1) ride along; `provenance` is left unset
                    # (the fail-closed union is reused only on the `done` returns).
                    return await self._finish(
                        session_id=session_id,
                        turn_index=turn_index,
                        status="paused_ask_user",
                        exit_label="pause",
                        assistant_text=result.assistant_text,
                        tool_calls_made=tool_calls_made,
                        accum=accum,
                        checkpoint=checkpoint,
                        event=("loop_paused_ask_user", {"question": question}),
                    )

                # TERMINAL EXIT #2 (answerWithTable). The loop's other exit is a model
                # turn with NO tool calls; this one fires when the model ended the turn
                # THROUGH a tool, carrying its final prose in the call. It is checked
                # AFTER the whole batch drains so a batched recordAssumptions +
                # answerWithTable still folds both before the turn closes.
                #
                # Everything below mirrors the no-tool-calls exit exactly — the same
                # `_compute_turn_provenance_union` (the single fail-closed source of
                # truth) and the same persisted assistant `TurnMessage` — because
                # replay, `session_history`, and the D44 scope gate all read that
                # message. A divergence here would make history disagree with the live
                # answer for exactly the turns that produced a table.
                if designated_answer_text is not None:
                    turn_provenance = await self._compute_turn_provenance_union(
                        session_id, turn_index
                    )
                    return await self._finish(
                        session_id=session_id,
                        turn_index=turn_index,
                        status="done",
                        exit_label="answer_with_table",
                        assistant_text=designated_answer_text,
                        tool_calls_made=tool_calls_made,
                        accum=accum,
                        provenance=turn_provenance,
                        # UNCONDITIONAL, unlike the no-tool-calls exit's `or None`, and
                        # allowed to be: `designated_answer_text` came through
                        # `clean_answer_text`, which returns `None` for anything that
                        # strips to empty — so reaching here means a non-empty string.
                        persist_text=designated_answer_text,
                        event=("loop_turn_done", {"tool_calls_made": tool_calls_made}),
                    )

                # SPEND, not occupancy: `total_tokens` is this round-trip's prompt +
                # completion, and every round replays the conversation, so the sum is
                # what the window COST — not how full the model's context is. It is
                # measured against `max_token_spend` (a spend ceiling), never against
                # the context window; occupancy is enforced per-request by
                # `fit_request_to_budget` above. Cached prompt tokens are counted at
                # full weight on purpose (loop/budget_guard.py module docstring).
                guard.record_iteration(tokens_used=int(result.usage.get("total_tokens") or 0))

                if guard.exceeded:
                    if window_count >= self._max_budget_windows:
                        # 05 §F: `stopped_hard_ceiling` IS a terminal outcome, so the
                        # scoped invariant applies — every surviving `pending` intent
                        # is recorded `BUDGET_EXHAUSTED` before the turn ends. A no-op
                        # (no write, no telemetry) when there is no live state or
                        # nothing pending, which is every ordinary turn.
                        analysis_state = await self._force_block_pending_intents(
                            session_id=session_id,
                            turn_index=turn_index,
                            state=analysis_state,
                            reason_code="BUDGET_EXHAUSTED",
                        )
                        # Best-effort partials (§1): whatever succeeded before the hard
                        # ceiling; `provenance` is left unset (done-only).
                        return await self._finish(
                            session_id=session_id,
                            turn_index=turn_index,
                            status="stopped_hard_ceiling",
                            exit_label="pause",
                            assistant_text=last_assistant_text,
                            tool_calls_made=tool_calls_made,
                            accum=accum,
                            event=("loop_hard_ceiling_stop", {"window": window_count}),
                        )
                    # 05 §F, fourth forced path: the budget cap reached DURING A
                    # REFUSED ROUND. §C.3 created it — the forced re-round is charged,
                    # so the charge itself can trip the cap, and the model's answer was
                    # refused a moment ago and its nudge (ephemeral) is already gone.
                    #
                    # GATED ON THE REFUSAL, deliberately. An ORDINARY budget-cap pause
                    # is NOT finalization (§G): it returns a non-`done` status with
                    # intents legitimately pending, the user may still answer
                    # "continue", and force-blocking there would write a terminal
                    # disposition onto a turn that is still running. Only the refused
                    # round gets it — and if the user answers "stop" instead, `resume()`
                    # force-blocks with `USER_STOPPED` on its own path.
                    #
                    # `ENFORCEMENT_EXHAUSTED`, NOT `BUDGET_EXHAUSTED` (05 §F, reversed
                    # 2026-08-12 on live evidence). What ran out here is ENFORCEMENT,
                    # not capacity: the measured turn had its answer at 25s and capped
                    # at 61.6s on the WALL clock while tokens moved +385 across the
                    # final three rounds — the 36 seconds went to two rejected
                    # `updateAnalysisState` calls and one refused `answerWithTable`.
                    # More budget would have changed nothing, so labelling it
                    # `BUDGET_EXHAUSTED` told 07 §E.2's reader a capacity story and sent
                    # them to raise a ceiling that was not the problem. The capacity
                    # fact is real and is kept — as `budget_cap_reached` on the
                    # telemetry event, where it informs without misattributing.
                    # The hard-ceiling branch ABOVE is untouched and still writes
                    # `BUDGET_EXHAUSTED`: there the ceiling genuinely is the cause.
                    if finalization_gate.refused_this_round:
                        # The gate's round flag is now set by the ANSWER-SHAPE
                        # gate too (05 §J) — which is correct and needs no branch here:
                        # that gate only fires when NOTHING is pending, so
                        # `_force_block_pending_intents` finds no pending intent, writes
                        # nothing and emits nothing. A shape refusal that runs into the
                        # cap therefore leaves the ledger and the telemetry byte-identical
                        # to a cap with no refusal at all.
                        analysis_state = await self._force_block_pending_intents(
                            session_id=session_id,
                            turn_index=turn_index,
                            state=analysis_state,
                            reason_code="ENFORCEMENT_EXHAUSTED",
                            budget_cap_reached=True,
                        )
                    checkpoint = PauseCheckpoint(
                        reason="budget_cap",
                        pending_question={
                            "question": _BUDGET_CAP_QUESTION,
                            "options": _BUDGET_CAP_OPTIONS,
                        },
                        awaiting="user_answer",
                        consumed=False,
                        budget_window_count=window_count,
                    )
                    # Best-effort partials (§1): whatever succeeded before the cap;
                    # `provenance` is left unset (done-only).
                    return await self._finish(
                        session_id=session_id,
                        turn_index=turn_index,
                        status="paused_budget_cap",
                        exit_label="pause",
                        assistant_text=last_assistant_text,
                        tool_calls_made=tool_calls_made,
                        accum=accum,
                        checkpoint=checkpoint,
                        event=("loop_paused_budget_cap", {"window": window_count}),
                    )
                # Under budget — loop back to 3a within the same window.
        finally:
            # Give any ALREADY-FINISHED fire-and-forget summary task a single
            # event-loop tick to land its emit before we cancel stragglers. This
            # does NOT wait on an in-flight/slow summary (one `sleep(0)` is one
            # scheduler iteration — a still-suspended summary stays pending and is
            # cancelled just below, so the turn result never blocks on it); it only
            # lets a summary that already completed during dispatch deliver its
            # line. Gated on a non-empty task set so the feature-off path adds no
            # extra tick and stays byte-identical. The drain-tick is itself nested
            # in try/finally so that if the TURN task is being cancelled during
            # shutdown (the `sleep(0)` then raises CancelledError), the straggler
            # cancel still ALWAYS runs — a summary task must never outlive the turn.
            try:
                if self._summary_tasks:
                    await asyncio.sleep(0)
            finally:
                self._cancel_pending_summaries()


__all__ = [
    "AgentLoop",
    "EmulatedDiscoveryProvider",
    "RuntimeTool",
    "ToolsProvider",
    "TurnContext",
    "TurnOutcome",
]
