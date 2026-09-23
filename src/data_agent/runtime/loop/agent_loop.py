"""AgentLoop — the per-turn state machine.

Per turn: assemble the canonical messages, then loop `send_turn` -> dispatch each
requested tool -> budget check, until an explicit answer tool finishes, a tool pauses,
or the budget window ends. A response without tool calls is rejected and retried. `resume()` is the separate entry point that
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
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from data_agent.runtime.answer_scrub import ANSWER_PROSE_REDACTED_EVENT, scrub_answer_prose
from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.capabilities.preparation import PreparationCache
from data_agent.runtime.composite.analysis_state import (
    MAX_STATE_CALLS,
    split_serves_intent,
    surplus_state_call_rejected,
)
from data_agent.runtime.composite.analysis_state import TOOL_NAME as UPDATE_ANALYSIS_STATE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import TOOL_NAME as ANSWER_TABLE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import (
    AnswerTable,
    BlueprintRun,
    DesignationItem,
    blueprint_run_from_result,
    clean_answer_text,
    enrich_table,
    finalize_designations,
    is_answer_table_in_scope,
    is_zero_row_count,
    resolve_designations,
    terminal_sql_by_id,
)
from data_agent.runtime.composite.answer_with_text import TOOL_NAME as ANSWER_TEXT_TOOL_NAME
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.composite.scope_refusal import scope_request_refused
from data_agent.runtime.context import scope_filter
from data_agent.runtime.context.assembly import (
    _REPEATED_IDEMPOTENT_READ_NUDGE,
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
    turn_date_anchor_day,
)
from data_agent.runtime.context.budget import fit_request_to_budget, render_entry
from data_agent.runtime.dispatch.denial_mapping import UNKNOWN_TOOL_CODE
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
from data_agent.runtime.loop.reliability import (
    MODEL_CALL_TIMEOUT_EVENT,
    MODEL_CALL_TIMEOUT_TEXT,
    cancellation_checkpoint,
    observe_turn_abort,
)
from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.runtime.model.conversation import (
    assign_unique_call_ids,
    conversation_call_ids,
    restore_response_batches,
)
from data_agent.runtime.observability.progress_summarizer import ProgressSummarizer
from data_agent.runtime.observability.redaction import hash_scope
from data_agent.runtime.observability.tracing import mark_current_span_error
from data_agent.runtime.sanitize import sanitize_text
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
)
from .blueprint_gate import BlueprintGate
from .budget_guard import BudgetGuard
from .clarification import normalize_clarification
from .delivery import (
    CURRENT_DELIVERY,
    DependencyFailureError,
    delivery_boundary,
    delivery_version,
    dependency_failure,
    partial_delivery_text,
    review_delivery,
    review_once,
)
from .dispatch_gates import BlueprintSearchGate, advertised_names, refusal
from .finalization import (
    DATA_ANSWER_TOOLS,
    EMPTY_ANSWER_EXHAUSTED_EVENT,
    EMPTY_ANSWER_FALLBACK_TEXT,
    EMPTY_ANSWER_REFUSED_EVENT,
    MAX_NUDGE_DRAFT_CHARS,
    AnswerShapeCounter,
    FinalizationGate,
    empty_answer_nudge_text,
    finalization_nudge_text,
    pending_intents,
    refreshed_analysis_state,
)
from .help_grounding import HELP_TOOLS, HELP_UNAVAILABLE_TEXT, HelpGrounding
from .judge_ship_guard import (
    JudgeShipGuard,
    capability_hedge_text,
    coherent_capability_ship_text,
    ship_decline_text,
    table_hedge_text,
)
from .loop_safety import (
    HELP_BREAKER_TEXT,
    NO_PROGRESS_COACH,
    NO_PROGRESS_TEXT,
    LoopSafety,
)
from .proposal import ReviewState, deliverable_evidence, fingerprint
from .read_guard import ReadGuard, repeated_read_guard_event
from .turn_accumulators import (
    AnswerEnvelope,
    TurnAccumulators,
    accumulate_enrichment,
    capture_terminal_sql,
)

_ALTERNATIVE_ANSWER_EVIDENCE_TOOLS = frozenset({"getHelpCenterDocument"})
_TEXT_ANSWER_EVIDENCE_TOOLS = frozenset(
    {"runQuery", "runBlueprint", "getTableSchema", "getHelpCenterDocument"}
)

_NAVIGATION_EXECUTION_CLAIM = re.compile(
    r"\b(?:i(?:'ll|\s+will|\s+am|'m|\s+have|'ve)?\s+)(?:already\s+)?(?:open(?:ed|ing)?|navigat(?:e|ed|ing)|tak(?:e|en|ing)|took|send|sent|redirect(?:ed|ing)?)\b|"
    r"\b(?:opening|navigating|redirecting)\b.{0,30}\b(?:now|you)\b",
    re.IGNORECASE,
)


def _has_navigation_option(cards: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(card.get("metadata"), Mapping)
        and card["metadata"].get("preamble_url") == "ember:GenericButton"
        for card in cards
    )


def _claims_navigation_was_performed(text: str, cards: Sequence[Mapping[str, Any]]) -> bool:
    return _has_navigation_option(cards) and bool(
        _NAVIGATION_EXECUTION_CLAIM.search(text.replace("’", "'"))
    )


def _answer_judge_refusal_kind(verdict: JudgeVerdict) -> FinalizationBlockKind:
    return (
        "help_center_grounding"
        if verdict.violation == "unsupported_by_evidence"
        else "answer_judge"
    )


if TYPE_CHECKING:
    from opentelemetry.trace import Tracer


ToolsProvider = Callable[[RuntimeCredentials], Awaitable[list[dict[str, Any]]]]
_logger = logging.getLogger(__name__)

# A runtime tool that crashes or returns a contract-violating result is
# contained at the registry seam (read-tools-design §2 hardening, prep for
# runBlueprint): the loop returns this clean error rather than aborting the turn
# or leaking `str(exc)`. Distinct from the tools' own `_guarded` self-protection
# (defense in depth — both layers hold).
RUNTIME_TOOL_INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"
_RUNTIME_TOOL_INTERNAL_ERROR_MESSAGE = "That tool hit an internal error. Please try again."

MAX_ASK_USER_OPTIONS = 5
_DECLINED_CLARIFICATION_ANSWERS = frozenset(
    {
        "skip",
        "skip this",
        "pass",
        "no thanks",
        "no thank you",
        "the user declined to answer the question",
        "i decline to answer",
        "i'd rather not answer",
        "i would rather not answer",
        "i don't know",
        "i do not know",
        "not sure",
        "i'm not sure",
        "i am not sure",
        "prefer not to answer",
        "i prefer not to answer",
        "i don't want to answer",
        "i do not want to answer",
        "can't answer",
        "cannot answer",
    }
)


def _ask_user_options(value: Any) -> list[str] | None:
    """Return at most five usable choices; malformed model output becomes free text."""
    if not isinstance(value, list):
        return None
    options = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return options[:MAX_ASK_USER_OPTIONS] or None


def _declines_clarification(answer: str) -> bool:
    normalized = " ".join(answer.strip().lower().rstrip(".!?").split())
    return normalized in _DECLINED_CLARIFICATION_ANSWERS


def _question_key(question: str) -> str:
    # Ignore conversational scaffolding so a cosmetic paraphrase cannot evade the
    # declined-question guard ("Which department?" / "What department should I use?").
    stop = {
        "a",
        "an",
        "do",
        "for",
        "i",
        "is",
        "mean",
        "please",
        "should",
        "the",
        "to",
        "use",
        "what",
        "which",
        "you",
    }
    words = [word for word in re.findall(r"[a-z0-9]+", question.lower()) if word not in stop]
    return " ".join(sorted(words))


@dataclass(frozen=True)
class TurnContext:
    """What a `RuntimeTool` may know about the turn it is running in.

    `turn_index` and the turn's original user question. The index must come from the
    loop's own computation. The two
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
    question: str = ""


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
        tool_call_id: str | None = None,
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

TurnStatus = Literal[
    "done", "paused_ask_user", "paused_budget_cap", "stopped_hard_ceiling", "stopped_no_progress"
]

# WHICH EXIT produced the prose the answer scrub inspected (ISSUES I1) — the
# `exit` label on `loop_answer_prose_redacted`, and nothing else. It is a
# PARAMETER of `_finish`, never derived from `status`, for the same reason
# *event* and *provenance* are: `status="done"` is reached by explicit answer
# tools whose disclosure profiles differ, and a derivation could not tell them apart.
# `"pause"` covers every non-`done` finisher — the `askUser` pause, the budget
# cap, the hard ceiling and `_pause_from_runtime_tool` — because all four carry
# the same thing: best-effort partial prose from a turn that did not answer.
# `"ask_user_question"` is the one label that is NOT about `assistant_text`: it
# tags the `askUser` QUESTION, which is model prose shown to the user too.
AnswerExitLabel = Literal[
    "answer_with_text",
    "answer_with_table",
    "capability",
    "pause",
    "ask_user_question",
    "declined_clarification",
    "runtime_fallback",
    "no_progress",
]

_BUDGET_CAP_QUESTION = "This is taking a while — continue, refine, or stop?"
_BUDGET_CAP_OPTIONS = ["continue", "refine", "stop"]

# The repeated-idempotent-read guard now lives WHOLE in `loop/read_guard.py` — a
# neutral, stdlib-only leaf. That is
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
    review: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
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
    capability_cards: list[dict[str, Any]] | None = None


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
    prefetched_blueprints: bool = False


ANSWER_TABLE_BLUEPRINT_NOT_RUN_CODE = "ANSWER_TABLE_BLUEPRINT_NOT_RUN"


def _answer_table_result_invalid(result_id: str, available: Sequence[str]) -> ToolResult:
    detail = (
        f"Table result_id '{result_id}' does not identify an accessible, successful "
        "warehouse execution from this turn. Discovery and capability preparation "
        "results cannot be displayed as warehouse tables. Select an existing runQuery "
        "or verified runBlueprint result_id; put prepared UI options in capability_refs. "
        "This is a presentation repair: do not rerun completed analysis or pass a "
        "result_id to runBlueprint. Available table result IDs: "
        + json.dumps(list(available))
        + ". Correct the table and evidence references, then call finalizeAnswer again."
    )
    return ToolResult(
        status="error",
        tool_name=ANSWER_TABLE_TOOL_NAME,
        error_code="ANSWER_TABLE_RESULT_INVALID",
        retryable=True,
        user_message=detail,
        denial_detail=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
    )


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
            "blueprint first, then call finalizeAnswer again."
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
            f"'{blueprint_id}' first, then call finalizeAnswer again."
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
    "No intents are declared yet, so this execution could not be tagged. "
    "Declare them with updateAnalysisState and bind this existing result_id explicitly. "
    "Use serves_intents for subsequent work."
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
    once-per-round selection and the one-round-trip lifetime.
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
    if entry.get("prefetch_context"):
        assistant_message["context_kind"] = "retrieval"
    # D94 Part 1: an entry flagged `withheld_sentinel` is a synthetic,
    # non-data-bearing sentinel injected by `context/assembly.py` for a
    # current-turn `ok`+`None` stranded result — its `content` is used verbatim as
    # the tool result (never JSON-wrapped with a payload), filling the dangling
    # tool_call's required slot to break the retry-until-budget-cap loop. The
    # explicit flag (not a bare `content` key) keeps a future `_render_entry` field
    # from ever silently rerouting a normal tool entry to verbatim rendering.
    if entry.get("prefetch_context"):
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": entry["content"],
            "context_kind": "retrieval",
        }
    elif entry.get("withheld_sentinel"):
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": entry["content"],
        }
    else:
        content: dict[str, Any] = {
            "tool_name": entry["tool_name"],
            "turn_index": entry.get("turn_index"),
            "status": entry["status"],
            "error_code": entry.get("error_code"),
            # S4: the static, PII-safe denial message (never raw MCP error
            # text) so the model can see WHY a retryable call failed and
            # self-correct — see context/budget.py::_render_entry.
            "user_message": entry.get("user_message"),
            "result_preview": entry.get("result_preview"),
            "result_id": tool_call_id,
            "measurement_review": entry.get("measurement_review"),
        }
        if entry.get("sql_diagnostic"):
            content["sql_diagnostic"] = entry["sql_diagnostic"]
        if entry.get("status") != "ok":
            content["evidence_usage"] = (
                "Failure reference only; never supports completion or data claims. Permissions denials can support NO_ACCESS; SQL_REPAIR_EXHAUSTED can support EXECUTION_FAILED. Other SQL failures require a corrected approach and do not prove data is absent."
            )
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
    if entry.get("model_response"):
        assistant_message["_model_response"] = entry["model_response"]
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
        model_call_timeout_seconds: float = 120.0,
        max_read_calls_per_tool: int = 3,
        max_no_progress_rounds: int = 4,
        help_center_failure_limit: int = 2,
        max_token_spend: int | None = None,
        request_token_budget: int | None = None,
        request_budget_pinned_recent_tool_pairs: int = 3,
        max_tool_calls_per_iteration: int = 8,
        clock: Callable[[], float] = time.monotonic,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        runtime_tools: Mapping[str, RuntimeTool] | None = None,
        blueprint_executor: Any = None,
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
        judge_catalog: Any = None,
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
        self._use_reasoning_metadata = bool(getattr(model_client, "_use_reasoning_metadata", False))
        self._model_client = model_client
        self._tool_dispatcher = tool_dispatcher
        self._context_assembler = context_assembler
        self._session_store = session_store
        self._tools_provider = tools_provider
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
        self._model_call_timeout_seconds = model_call_timeout_seconds
        self._max_read_calls_per_tool = max_read_calls_per_tool
        self._max_no_progress_rounds = max_no_progress_rounds
        self._help_center_failure_limit = help_center_failure_limit
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
        self._judge_catalog = judge_catalog
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
        # Summaries complete before dispatch; the summarizer owns its timeout.
        self._progress_summarizer = progress_summarizer
        self._answer_table_hooks = answer_table_hooks or AnswerTableHooks()

    @observe_turn_abort
    @delivery_boundary
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
        if CURRENT_DELIVERY.get():
            CURRENT_DELIVERY.get().turn_index = turn_index
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

    @observe_turn_abort
    @delivery_boundary
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

        if CURRENT_DELIVERY.get():
            CURRENT_DELIVERY.get().turn_index = turn_index
        prior_window_count = checkpoint.budget_window_count if checkpoint else 1
        pause_reason = checkpoint.reason if checkpoint else "askUser"
        declined_question = (
            str((checkpoint.pending_question or {}).get("question", ""))
            if checkpoint is not None
            and checkpoint.reason == "askUser"
            and _declines_clarification(answer)
            else None
        )

        try:
            return await self._continue_consumed_resume(
                session_id=session_id,
                credentials=credentials,
                answer=answer,
                checkpoint=checkpoint,
                updated_doc=updated_doc,
                turn_index=turn_index,
                prior_window_count=prior_window_count,
                pause_reason=pause_reason,
                declined_question=declined_question,
            )
        except DependencyFailureError:
            # The boundary persists a terminal failure; do not reopen this consumed pause.
            raise
        except BaseException:
            try:
                reopened = await asyncio.shield(
                    self._session_store.reopen_failed_resume(session_id, answer)
                )
                if not reopened:
                    _logger.warning(
                        "failed resume could not be reopened safely (session=%s)", session_id
                    )
            except Exception:
                _logger.exception("failed to reopen resume checkpoint (session=%s)", session_id)
            raise

    async def _continue_consumed_resume(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        answer: str,
        checkpoint: PauseCheckpoint | None,
        updated_doc: Any,
        turn_index: int,
        prior_window_count: int,
        pause_reason: str,
        declined_question: str | None = None,
    ) -> TurnOutcome:
        """Continue after atomically claiming a checkpoint; the caller handles rollback."""

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
                stop_capability_cards = await self._compute_turn_capability_cards(
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
                    assumptions=stop_assumptions,
                    answer_tables=stop_tables,
                    capability_cards=stop_capability_cards,
                )
                return await self._finish(
                    session_id=session_id,
                    turn_index=turn_index,
                    status="done",
                    exit_label="runtime_fallback",
                    tool_calls_made=0,
                    accum=stop_accum,
                    assistant_text="Stopping here — here is what I found before the budget cap.",
                    provenance=await self._compute_turn_provenance_union(session_id, turn_index),
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
        seed_capability_cards = await self._compute_turn_capability_cards(
            session_id, turn_index, credentials.column_scope
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
            declined_question=declined_question,
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
                capability_cards=seed_capability_cards,
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
        capability_memo: dict[str, Any],
        withheld_call_ids: set[str],
        finalization_nudge: str | None = None,
        intent_note_call_ids: Collection[str] = (),
        user_jwt: str | None = None,
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

        *finalization_nudge* is appended at the tail and never persisted. It lives EXACTLY ONE ROUND-TRIP (the caller clears it immediately
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
            user_jwt=user_jwt,
            retrieval_memo=retrieval_memo,
            capability_memo=capability_memo,
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
        canonical = restore_response_batches(
            canonical,
            scope_hash=scope_filter.compute_scope_hash(column_scope),
            use_reasoning_metadata=self._use_reasoning_metadata,
        )
        if self._request_token_budget is not None:
            fit = fit_request_to_budget(
                canonical,
                token_budget=self._request_token_budget,
                pinned_recent_tool_pairs=self._request_budget_pinned_recent_tool_pairs,
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
            messages=canonical,
            readable_tool_call_ids=readable_tool_call_ids,
            prefetched_blueprints=assembled.retrieved_counts[0] > 0,
        )

    async def _compute_turn_provenance_union(
        self, session_id: str, turn_index: int
    ) -> frozenset[tuple[str, str]] | None:
        """Union of every `TrailEntry.provenance` produced at *turn_index*, across every budget
        window of this external turn — the tag applied to that turn's final assistant
        `TurnMessage` (D44). Fail-closed: any undetermined (`None`) tool-result provenance
        from a data-bearing result makes the assistant message undetermined too. Data-free failures
        contribute no data provenance. A turn with no tool
        calls at all is determined-empty (`frozenset()`), always kept on replay.
        """
        trail = await self._session_store.load_trail(session_id)
        turn_entries = [entry for entry in trail if entry.turn_index == turn_index]
        if not turn_entries:
            return frozenset()
        union: set[tuple[str, str]] = set()
        for entry in turn_entries:
            # A failure with no returned data cannot taint successful evidence.
            # Its diagnostic remains current-turn-only in replay. Partial/error
            # payloads still participate and fail closed if provenance is unknown.
            if (
                entry.status != "ok"
                and entry.result_preview is None
                and entry.result_full_ref is None
            ):
                continue
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
        turn_entries = [e for e in trail if e.turn_index == turn_index and e.status == "ok"]
        # blueprint_id -> BlueprintRun, rebuilt from this turn's successful runs.
        blueprint_runs: dict[str, BlueprintRun] = {}
        for entry in turn_entries:
            if entry.tool_name == "runQuery" and isinstance(entry.args.get("sql"), str):
                blueprint_runs[entry.tool_call_id] = BlueprintRun(terminal_sql=entry.args["sql"])
            if entry.tool_name != "runBlueprint" or entry.result_full_ref is None:
                continue
            result_full = await self._session_store.read_full_result(
                session_id, entry.result_full_ref
            )
            if isinstance(result_full, dict):
                capture_terminal_sql(
                    "runBlueprint",
                    ToolResult(
                        status="ok",
                        tool_name="runBlueprint",
                        error_code=None,
                        retryable=None,
                        user_message=None,
                        provenance=None,
                        result_preview=None,
                        result_full=result_full,
                    ),
                    into=blueprint_runs,
                    arguments=entry.args,
                )
                captured = blueprint_run_from_result(
                    result_full, slots=entry.args.get("slot_bindings")
                )
                if captured:
                    blueprint_runs[entry.tool_call_id] = captured[1]

        terminal_by_id = terminal_sql_by_id(blueprint_runs)
        designated: list[AnswerTable] = []
        for entry in turn_entries:
            if entry.tool_name == ANSWER_TEXT_TOOL_NAME and entry.args.get("tables") == []:
                designated = []
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
        doc = await self._session_store.get_or_create_session(session_id)
        excluded = doc.review_states.get(str(turn_index), {}).get("excluded_components", ())
        excluded_sql = {c.get("sql") for c in excluded if c.get("sql")}
        return [t for t in designated if t.sql not in excluded_sql], blueprint_runs

    async def _compute_turn_capability_cards(
        self, session_id: str, turn_index: int, column_scope: frozenset[str] | None = None
    ) -> list[dict[str, Any]]:
        trail = await self._session_store.load_trail(session_id)
        cards: list[dict[str, Any]] = []
        doc = await self._session_store.get_or_create_session(session_id)
        excluded = doc.review_states.get(str(turn_index), {}).get("excluded_components", ())
        excluded_names = {c["capability_ref"] for c in excluded if c["kind"] == "capability"}
        for entry in trail:
            if entry.tool_name in excluded_names:
                continue
            if column_scope is not None and (entry.model_response or {}).get(
                "scope_hash"
            ) != scope_filter.compute_scope_hash(column_scope):
                continue
            if (
                entry.turn_index != turn_index
                or entry.status != "ok"
                or not entry.capability_terminal
                or entry.result_full_ref is None
            ):
                continue
            payload = await self._session_store.read_full_result(session_id, entry.result_full_ref)
            if isinstance(payload, dict):
                card = {key: value for key, value in payload.items() if key != "answer"}
                if card not in cards:
                    cards.append(card)
        return cards

    async def _maybe_start_summary(
        self, tool_name: str, tool_call_id: str, arguments: dict[str, Any], *, judge_approved=False
    ) -> None:
        """Emit the optional summary before dispatch so a late start cannot reopen a spinner."""
        if self._progress_summarizer is not None:
            try:
                await asyncio.wait_for(
                    asyncio.create_task(
                        self._summarize_and_emit(
                            tool_name, tool_call_id, dict(arguments), judge_approved=judge_approved
                        )
                    ),
                    timeout=1.0,
                )
            except TimeoutError:
                pass

    async def _summarize_and_emit(
        self, tool_name: str, tool_call_id: str, arguments: dict[str, Any], *, judge_approved=False
    ) -> None:
        """Await the summarizer and emit the value-rich progress line — fail-soft: an error or
        timeout yields `None` (dropped), and observer failures are contained.
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
                {
                    "summary": summary,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    **({"judge_approved": True} if judge_approved else {}),
                },
            )
        except Exception:
            _logger.debug("progress-summary emit failed for %s (ignored)", tool_name)

    async def _run_runtime_tool(
        self,
        handler: RuntimeTool,
        tool_name: str,
        arguments: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext,
        tool_call_id: str,
    ) -> ToolResult:
        """Run one registry handler with a crash guard and a returned-provenance-type
        validation, so a misbehaving runtime tool cannot abort the turn or persist a
        replay-poisoning provenance.

        *turn* is the loop's OWN `turn_index`, threaded explicitly — the only correct
        source. Passed to every runtime tool, ignored by the ones that do not need it.
        """
        managed = getattr(handler, "emits_dispatch_events", False)
        if not managed:
            self._observer(
                "tool_dispatch_start", {"tool_name": tool_name, "tool_call_id": tool_call_id}
            )
        try:
            result = await handler.run(arguments, credentials, turn=turn, tool_call_id=tool_call_id)
        except Exception:
            # Never propagate the raw exception (would abort the turn) or leak
            # `str(exc)` — log server-side only, return a clean canned error.
            _logger.exception(
                "runtime tool %s raised (session=%s)", tool_name, credentials.session_id
            )
            result = _runtime_tool_internal_error(tool_name)
            managed = False
        if not managed:
            self._observer(
                "tool_dispatch_" + result.status,
                {
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "error_code": result.error_code,
                },
            )
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
                intents.append(replace(intent, status="blocked", reason_code=reason_code))
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
        load plus execution-metadata reads; doing that first and
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
        if self._answer_judge is None or not getattr(self._answer_judge, "enabled", True):
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
            _logger.exception("could not assemble the answer-judge brief — approving and shipping")
            self._observer(ANSWER_JUDGE_FAILED_EVENT, {"reason": "brief_failed"})
            return APPROVED
        assert self._answer_judge is not None  # `_judge_would_run` established it
        try:
            return await review_once(self, brief)
        except Exception:
            return APPROVED

    async def _judge_results(
        self,
        session_id: str,
        turn_index: int,
        column_scope: frozenset[str],
        *,
        include_all_successful: bool = False,
        exclude_result_ids: frozenset[str] = frozenset(),
    ) -> tuple[tuple[Mapping[str, Any], ...], str | None, tuple[TrailEntry, ...]]:
        """Read authorized evidence before rendering bounded previews for review.

        Scope-filter first, without the current-turn denial exemption. Unknown or
        excluded provenance must not inform a judgment. Successful warehouse, Help
        Center and prepared UI results are included; catalog documentation is added
        separately by the post-execution evidence builder. The authorized trail is
        returned for that builder so it does not need a second session read.
        """
        doc = await self._session_store.get_or_create_session(session_id)
        in_scope = [
            e
            for e in scope_filter.filter_trail(doc.tool_trail, column_scope)
            if e.tool_call_id not in exclude_result_ids
        ]
        rendered = tuple(
            {
                k: v
                for k, v in render_entry(entry, self._preview_row_count).items()
                if k != "model_response"
            }
            for entry in in_scope
            if entry.turn_index == turn_index
            and entry.status == "ok"
            and (
                include_all_successful
                or entry.tool_name in DATA_ANSWER_TOOLS
                or entry.tool_name in _ALTERNATIVE_ANSWER_EVIDENCE_TOOLS
                or entry.capability_terminal
            )
        )
        # Reuse this authorized trail to build the final evidence package.
        return (
            rendered,
            turn_date_anchor_day(doc.messages, turn_index),
            tuple(in_scope),
        )

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
        pending_options: tuple[str, ...] = (),
        results: tuple[Mapping[str, Any], ...] = (),
        designated_tables: tuple[tuple[str | None, str], ...] = (),
        ship_guard: JudgeShipGuard | None = None,
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
            pending_question=pending_question,
            pending_options=pending_options,
            designated_tool_call_ids=tuple(
                ref
                for ref, sql in accum.result_sql_by_call_id.items()
                if any(sql == ident for _, ident in designated_tables)
            ),
            assumptions_recorded_after_refusal=tuple(
                item
                for item in (accum.assumptions or ())
                if ship_guard is not None
                and ship_guard.unsafe_ship()
                and item not in ship_guard.assumptions_before_refusal
            ),
            capability_presented=accum.capability_judge_context,
            measurement_contracts=tuple(
                r["measurement_review"] for r in results if r.get("measurement_review")
            ),
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
        capability_cards: list[dict[str, Any]] | None = None,
        ship_guard: JudgeShipGuard | None = None,
        judge_site: str = "exit_prose",
        completion_notice: str | None = None,
        failure: dict[str, Any] | None = None,
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

        Stop-on-resume and runtime-tool pauses also use this boundary, so their
        exact delivered contents are reviewed before persistence and emission.
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
        await cancellation_checkpoint("finish")
        if CURRENT_DELIVERY.get():
            saved_doc = await self._session_store.get_or_create_session(session_id)
            saved_review = saved_doc.review_states.get(str(turn_index), {})
            if saved_review.get("violation"):
                accum.apply_ship_disposition("decline_only", ())
                capability_cards = None
                if ship_guard is None:
                    ship_guard = JudgeShipGuard()
                    ship_guard.note_refusal(
                        saved_review.get("site", "exit_prose"), saved_review["violation"]
                    )
        ship_disposition = ship_guard.disposition(judge_site) if ship_guard is not None else None
        retained_assumption_count = None
        if assistant_text == HELP_UNAVAILABLE_TEXT:
            ship_disposition = "decline_only"
            accum.apply_ship_disposition(ship_disposition, ())
            retained_assumption_count = 0
            capability_cards = accum.capability_cards
            persist_text = assistant_text
        elif ship_disposition:
            violation = ship_guard.unsafe_ship()[1]
            accum.apply_ship_disposition(ship_disposition, ship_guard.assumptions_before_refusal)
            retained_assumption_count = len(accum.assumptions or ())
            capability_cards = accum.capability_cards
            builder = {
                "decline_only": ship_decline_text,
                "ship_tables_with_hedge": table_hedge_text,
                "ship_cards_with_hedge": capability_hedge_text,
            }[ship_disposition]
            assistant_text = builder(violation)
            persist_text = assistant_text
            self._observer(
                "loop_answer_judge_ship_guarded",
                {"violation": violation, "site": judge_site, "disposition": ship_disposition},
            )
        if capability_cards or accum.capability_cards:
            coherent_text = coherent_capability_ship_text(
                assistant_text,
                capability_cards or accum.capability_cards,
                ship_guard.unsafe_ship() if ship_guard else None,
            )
            if coherent_text != assistant_text:
                ship_disposition = "ship_cards_with_hedge"
                accum.apply_ship_disposition(ship_disposition, tuple(accum.assumptions or ()))
                retained_assumption_count = len(accum.assumptions or ())
                assistant_text = coherent_text
        if assistant_text in {
            "I could not verify every requested part from the available evidence.",
            EMPTY_ANSWER_FALLBACK_TEXT,
        }:
            # Runtime limitation text contains no warehouse facts. Unknown provenance
            # from failed work must not hide this message in scope-filtered history.
            provenance = frozenset()
        if (
            CURRENT_DELIVERY.get()
            and self._answer_judge is not None
            and getattr(self._answer_judge, "enabled", True)
            and (
                completion_notice
                or assistant_text
                == "I could not verify every requested part from the available evidence."
            )
        ):
            parts = partial_delivery_text(saved_doc, turn_index, accum)
            if parts:
                assistant_text = (
                    "\n\n".join(p for p in (assistant_text, parts) if p) if failure else parts
                )
        if completion_notice:
            assistant_text = "\n\n".join(
                part for part in (assistant_text, completion_notice) if part
            )
        assistant_text, redaction_count = scrub_answer_prose(assistant_text, provenance=provenance)
        review, withheld = await review_delivery(
            self, session_id, turn_index, assistant_text, accum, checkpoint
        )
        if withheld:
            accum.apply_ship_disposition("decline_only", ())
            capability_cards = None
            ship_disposition = "decline_only"
            retained_assumption_count = 0
            assistant_text = "I don't have enough verified information to answer your question."
            if completion_notice:
                assistant_text += "\n\n" + completion_notice
            provenance = frozenset()
            if checkpoint is not None:
                # Do not publish or leave resumable an explicitly rejected question.
                checkpoint = replace(checkpoint, consumed=True)
                status = "done"
        if persist_text is not None or (status == "done" and assistant_text):
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
                    ship_disposition=ship_disposition,
                    retained_assumption_count=retained_assumption_count,
                    review=review,
                    failure=failure,
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
            pending_question=checkpoint.pending_question
            if checkpoint is not None and not checkpoint.consumed
            else None,
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
            capability_cards=capability_cards or accum.capability_cards,
            review=review,
            failure=failure,
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
        serves_intents: tuple[str, ...] = (),
    ) -> TurnOutcome:
        """Honor a runtime tool's `ToolPause` — write the checkpoint (with the additive
        `blueprint_*` mid-DAG state) and return `paused_ask_user`, the same terminal
        contract as `askUser`. The loop owns `budget_window_count`; the tool cannot know it.

        The four enrichment accumulators are threaded through best-effort, so a "runQuery
        succeeded, then runBlueprint paused on a slot question" turn surfaces the partial
        SQL and table on this pause flavor too, matching a direct `askUser` pause.
        """
        pending_question = normalize_clarification(
            pause.pending_question.get("question"), pause.pending_question.get("options")
        )
        checkpoint = PauseCheckpoint(
            reason=pause.reason,
            pending_question=pending_question,
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
            serves_intents=serves_intents,
        )
        doc = await self._session_store.get_or_create_session(session_id)
        turn_index = doc.messages[-1].turn_index if doc.messages else 0
        from data_agent.runtime.composite.answer_with_table import AnswerTable

        accum = TurnAccumulators(
            assumptions=assumptions,
            sql=sql_executed,
            answer_tables=[AnswerTable(**t) for t in (envelope.answer_tables or ())]
            if envelope
            else (),
            blueprint_use=envelope.blueprint_use if envelope else None,
            verification=envelope.verification if envelope else None,
        )
        return await self._finish(
            session_id=session_id,
            turn_index=turn_index,
            status="paused_ask_user",
            exit_label="pause",
            assistant_text=assistant_text,
            tool_calls_made=tool_calls_made,
            accum=accum,
            checkpoint=checkpoint,
            event=("loop_paused_ask_user", {"question": pending_question.get("question", "")}),
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
        resume_call_id = str(uuid.uuid4())
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
                _logger.exception("runBlueprint resume raised (session=%s)", credentials.session_id)
                return _runtime_tool_internal_error("runBlueprint")

        self._observer(
            "tool_dispatch_start", {"tool_name": "runBlueprint", "tool_call_id": resume_call_id}
        )
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
            {
                "tool_name": "runBlueprint",
                "tool_call_id": resume_call_id,
                "error_code": tool_result.error_code,
            },
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
                serves_intents=checkpoint.serves_intents,
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
            tool_call_id=resume_call_id,
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
            serves_intents=checkpoint.serves_intents,
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
        seed_capability_cards = await self._compute_turn_capability_cards(
            session_id, turn_index, credentials.column_scope
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
                capability_cards=seed_capability_cards,
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
    ) -> tuple[list[AnswerTable], ToolResult | None, bool]:
        """Resolve ONE `answerWithTable` call into its designated answer tables.

        Returns `(tables, refusal, carried_designation)`.

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
             with a retryable `_answer_table_blueprint_not_run` nudge, after the dormant
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
        blueprint_runs = dict(blueprint_runs)
        result_runs: dict[str, BlueprintRun] = {}
        trail = await self._session_store.load_trail(session_id)
        for entry in scope_filter.filter_trail(
            trail, credentials.column_scope, current_turn_index=None
        ):
            if entry.turn_index != turn_index or entry.status != "ok":
                continue
            if entry.tool_name == "runQuery" and isinstance(entry.args.get("sql"), str):
                result_runs[entry.tool_call_id] = BlueprintRun(terminal_sql=entry.args["sql"])
            elif (
                entry.tool_name == "runBlueprint" and entry.authoritative and entry.result_full_ref
            ):
                full = await self._session_store.read_full_result(session_id, entry.result_full_ref)
                captured = blueprint_run_from_result(full, slots=entry.args.get("slot_bindings"))
                if captured:
                    result_runs[entry.tool_call_id] = captured[1]
        # Execution IDs and legacy blueprint IDs are different namespaces. Validate
        # before merging for the shared resolver, and never invoke blueprint hooks
        # or suggest execution to repair an invalid result selection.
        table_items = arguments.get("tables")
        for item in table_items if isinstance(table_items, list) else []:
            if isinstance(item, dict) and item.get("result_id"):
                ref = item["result_id"]
                if not isinstance(ref, str) or ref.strip() not in result_runs:
                    return [], _answer_table_result_invalid(str(ref), list(result_runs)), True
        blueprint_runs.update(result_runs)
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
                    "finalizeAnswer designated blueprint %r, which did not run "
                    "successfully this turn — no answer table (session=%s)",
                    item.named_blueprint,
                    session_id,
                )
                replacement = self._answer_table_hooks.resolve_unresolved(
                    AnswerTableEvent(blueprint_id=item.named_blueprint, sql=None, **event_base)
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
                    AnswerTableEvent(blueprint_id=item.named_blueprint, sql=item.sql, **event_base)
                )
                if durable is not None:
                    item = replace(item, sql=durable, blueprint_id=None)
            resolved_items.append(item)

        if unresolved_blueprint_id is not None:
            # The whole call is refused; nothing below would be surfaced anyway, and
            # computing provenance for tables that will not ship is pure cost.
            return [], _answer_table_blueprint_not_run(unresolved_blueprint_id), carried_designation

        finalized = finalize_designations(resolved_items)
        for _ in range(designation.dropped_unresolvable):
            self._observer("loop_answer_table_item_dropped", {"reason": "unresolvable"})
        for _ in range(finalized.dropped_duplicate):
            self._observer("loop_answer_table_item_dropped", {"reason": "duplicate"})
        for _ in range(finalized.dropped_over_cap):
            self._observer("loop_answer_table_item_dropped", {"reason": "over_cap"})

        tables: list[AnswerTable] = []
        for table in finalized.tables:
            provenance = await self._tool_dispatcher.capture_sql_provenance(table.sql, credentials)
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
                    "blueprint_table_count": sum(1 for t in tables if t.blueprint_use is not None),
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
            self._observer("loop_answer_table_intent_uncovered", {"intent_count": uncovered})

    async def _run_loop_body(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        window_count: int,
        turn_index: int,
        model_client: ModelClient,
        question: str | None = None,
        declined_question: str | None = None,
        # This window's answer accumulators (`loop/turn_accumulators.py`), ALREADY
        # SEEDED by the caller when the turn is resuming: `resume()` rebuilds what
        # the trail knows, and `_resume_blueprint` adds the enrichment of the
        # blueprint that completed during the resume itself (UI Slice 1 Fix 1),
        # which no replay could reconstruct. `None` (a brand-new turn, or any
        # caller with nothing to carry) → a fresh, empty window.
        accumulators: TurnAccumulators | None = None,
    ) -> TurnOutcome:
        """Run one turn window, with ordered summaries before each dispatch."""
        # The live MCP authenticates every request, including tools/list, so
        # the tools_provider seam is called WITH this turn's credentials on
        # every window (2026-07-01 fix) — it is expected to cache the fetched
        # catalogue itself (see mcp/tool_schema.py::ToolSchemaCache) since the
        # catalogue is scope-independent; this is not a live MCP round-trip
        # on every call in practice, just a credentialed one the first time.
        try:
            tools = await self._tools_provider(credentials)
        except Exception as exc:
            failure = dependency_failure(exc, "tool_schema")
            if failure:
                raise failure from exc
            raise

        # The loop's OWN turn index, handed to every runtime tool (03 §C.1). It is
        # built here, from the parameter `run`/`resume` computed, so no tool ever
        # re-derives it or reads `app.py`'s explicitly non-load-bearing hint.
        turn_context = TurnContext(turn_index=turn_index, question=question)

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
        capability_memo: dict[str, Any] = {}
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
        loop_safety = LoopSafety(
            read_limit=self._max_read_calls_per_tool,
            no_progress_limit=self._max_no_progress_rounds,
            help_failure_limit=self._help_center_failure_limit,
            observer=self._observer,
        )
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
        used_call_ids = {entry.tool_call_id for entry in session_doc.tool_trail}
        for prior in scope_filter.filter_trail(session_doc.tool_trail, credentials.column_scope):
            if prior.turn_index == turn_index:
                loop_safety.observe_help(prior.tool_name, prior.error_code, emit=False)
        preparation_cache = PreparationCache(
            self._session_store,
            session_id,
            turn_index,
            scope_filter.compute_scope_hash(credentials.column_scope),
            session_doc.tool_trail,
        )
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
        successful_text_evidence_tools: set[str] = set()
        help_grounding = HelpGrounding()
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
            if prior_entry.turn_index != turn_index:
                continue
            if prior_entry.tool_name in HELP_TOOLS:
                preview = prior_entry.result_preview
                payload = (
                    preview.preview_rows[0][0]
                    if preview and preview.preview_rows and preview.preview_rows[0]
                    else None
                )
                help_grounding.observe(prior_entry.tool_name, prior_entry.status, payload)
            if prior_entry.status != "ok":
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
                served_round=(prior_entry.model_response or {}).get("round"),
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
            if prior_entry.tool_name in _TEXT_ANSWER_EVIDENCE_TOOLS and (
                prior_entry.tool_name != "getHelpCenterDocument" or help_grounding.document_fetched
            ):
                successful_text_evidence_tools.add(prior_entry.tool_name)
        # The finalization nudge (05 §B.2/§D), ephemeral and NEVER persisted. It
        # lives EXACTLY ONE ROUND-TRIP: set when an exit-#1 finalization is
        # refused, spliced into the next rebuild, and cleared immediately after
        # that rebuild below.
        finalization_nudge: str | None = (
            "The user declined to answer the previous clarification. Do not ask that "
            "question again. Use the safest reasonable interpretation and answer as far "
            "as possible; if the request cannot be completed without it, end with a "
            "concise, honest explanation of what is missing."
            if declined_question
            else None
        )
        if (
            finalization_nudge is None
            and len(
                [m for m in session_doc.messages if m.role == "user" and m.turn_index == turn_index]
            )
            > 1
        ):
            finalization_nudge = (
                "This turn is resuming. The later user messages answer or refine the original "
                "request. Apply the supplied clarification now; an instruction in the original "
                "request to ask first has already been satisfied. Continue the remaining work."
            )
        declined_repeat_refused = False
        blueprint_search_gate = BlueprintSearchGate(question, session_doc.tool_trail, turn_index)
        empty_finishes = 0
        ship_guard = JudgeShipGuard()
        review_state = ReviewState.restore(
            session_doc.review_states.get(str(turn_index)),
            scope_filter.compute_scope_hash(credentials.column_scope),
        )
        accum.exclude_components(review_state.excluded_components)
        if review_state.violation:
            ship_guard.note_refusal(
                review_state.site,
                review_state.violation,
                assumptions=review_state.assumptions_before_refusal,
            )
            if review_state.feedback:
                finalization_nudge = answer_judge_nudge_text("", review_state.feedback)
                if review_state.excluded_components:
                    from .proposal import omission_nudge

                    finalization_nudge = omission_nudge(review_state.excluded_components).lstrip()
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

        discovery_rounds = 0
        while True:
            loop_safety.begin_round()
            await cancellation_checkpoint("context", guard.usage().iterations + 1)
            request = await self._build_canonical_messages(
                session_id,
                credentials.column_scope,
                turn_index,
                question=question,
                user_id=None,
                retrieval_memo=retrieval_memo,
                capability_memo=capability_memo,
                withheld_call_ids=withheld_call_ids,
                finalization_nudge=finalization_nudge,
                intent_note_call_ids=intent_note_call_ids,
                user_jwt=credentials.jwt,
            )
            canonical_messages = request.messages
            if loop_safety.help_open:
                tools = [
                    t
                    for t in tools
                    if (t.get("name") or t.get("function", {}).get("name")) not in HELP_TOOLS
                ]
                canonical_messages.append({"role": "system", "content": HELP_BREAKER_TEXT})
            blueprint_search_gate.observe_context(canonical_messages)
            if request.prefetched_blueprints:
                blueprint_search_gate.observe_prefetch()
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
            proposal_args = None
            proposal_call_id = None
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
            await cancellation_checkpoint("model_call", guard.usage().iterations + 1)
            limit = min(
                self._model_call_timeout_seconds,
                max(0.001, self._max_wall_clock_seconds - guard.usage().elapsed_seconds),
            )
            call_started = self._clock()
            try:
                async with asyncio.timeout(limit):
                    result = await model_client.send_turn(canonical_messages, tools)
            except TimeoutError:
                mark_current_span_error("model_call_timeout")
                self._observer(
                    MODEL_CALL_TIMEOUT_EVENT,
                    {
                        "elapsed": round(self._clock() - call_started, 3),
                        "limit": limit,
                        "iteration": guard.usage().iterations + 1,
                    },
                )
                await self._force_block_pending_intents(
                    session_id=session_id,
                    turn_index=turn_index,
                    state=analysis_state,
                    reason_code="ENFORCEMENT_EXHAUSTED",
                )
                return await self._finish(
                    session_id=session_id,
                    turn_index=turn_index,
                    status="done",
                    exit_label="runtime_fallback",
                    assistant_text=None,
                    completion_notice=MODEL_CALL_TIMEOUT_TEXT,
                    tool_calls_made=tool_calls_made,
                    accum=accum,
                    ship_guard=ship_guard,
                    judge_site=(
                        "exit_capability"
                        if accum.capability_cards
                        else "exit_table"
                        if accum.has_answer_tables
                        else "exit_prose"
                    ),
                    provenance=frozenset(),
                    event=("loop_turn_done", {"tool_calls_made": tool_calls_made}),
                )
            except Exception as exc:
                failure = dependency_failure(exc, "model")
                if failure:
                    raise failure from exc
                raise
            used_call_ids.update(conversation_call_ids(canonical_messages))
            result = assign_unique_call_ids(result, used_call_ids, self._observer)
            last_assistant_text = result.assistant_text
            model_response = {
                "id": str(uuid.uuid4()),
                "round": guard.usage().iterations + 1,
                "window": window_count,
                "scope_hash": scope_filter.compute_scope_hash(credentials.column_scope),
                "content": result.assistant_text,
                "reasoning_metadata": result.reasoning_metadata
                if self._use_reasoning_metadata
                else {},
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": c.raw_arguments
                            if c.raw_arguments is not None
                            else json.dumps(c.arguments),
                        },
                    }
                    for c in result.tool_calls
                ],
            }

            # --- FINALIZATION ENFORCEMENT, terminal exit #1 (05 §B.2) ---------
            #
            # The model produced prose, not a tool call, so this turn is about to
            # end `done` — and there is NO ERROR CHANNEL here: nothing to attach a
            # denial to, because nothing was called. A synthetic tool message
            # cannot stand alone either (`_assembled_to_canonical` only ever emits
            # a `tool` message by expanding a trail entry into an
            # `assistant(tool_calls) + tool` PAIR), and fabricating such a pair —
            # would mean naming a
            # function the model can see in its tools list, re-splicing/deduping/
            # pinning it on every rebuild, and routing its text through
            # `classify_denial` anyway, all for something that should live one
            # round-trip. So the refusal is an EPHEMERAL `user`-role injection.
            #
            # NOTHING IS PERSISTED on this path: not the refused answer, not the
            # nudge. Both are within-turn control flow, and a persisted nudge would
            # appear in `/session/history` as something the user said.
            empty_finishes = (
                empty_finishes + 1
                if not result.tool_calls and not (result.assistant_text or "").strip()
                else 0
            )
            empty_refused = False
            if empty_finishes:
                empty_refused = await finalization_gate.may_refuse("empty_answer")
                self._observer(
                    EMPTY_ANSWER_REFUSED_EVENT if empty_refused else EMPTY_ANSWER_EXHAUSTED_EVENT,
                    {"incomplete_reason": result.incomplete_reason or ""},
                )
            if empty_finishes and not empty_refused:
                if pending_intents(analysis_state):
                    self._observer(
                        "loop_enforcement_exhausted",
                        {"intent_count": len(pending_intents(analysis_state))},
                    )
                    analysis_state = await self._force_block_pending_intents(
                        session_id=session_id,
                        turn_index=turn_index,
                        state=analysis_state,
                        reason_code="ENFORCEMENT_EXHAUSTED",
                    )
                accum.apply_ship_disposition("decline_only", ())
                return await self._finish(
                    session_id=session_id,
                    turn_index=turn_index,
                    status="done",
                    ship_guard=ship_guard,
                    exit_label="runtime_fallback",
                    assistant_text=EMPTY_ANSWER_FALLBACK_TEXT,
                    tool_calls_made=tool_calls_made,
                    accum=accum,
                    provenance=frozenset(),
                    persist_text=EMPTY_ANSWER_FALLBACK_TEXT,
                    event=("loop_turn_done", {"tool_calls_made": tool_calls_made}),
                )
            if not result.tool_calls:
                finalization_nudge = (
                    (
                        empty_answer_nudge_text(result.incomplete_reason) + "\n"
                        if empty_refused
                        else ""
                    )
                    + "Your assistant message has not completed this turn. If you can answer now, "
                    "call finalizeAnswer with your complete text in answer. For a simple "
                    "conversational reply, use empty tables, capability_refs, and evidence lists; "
                    "no lookup is needed. If the request still needs work, continue with the "
                    "appropriate capability or data tools, then call finalizeAnswer. "
                    "For supported claims, evidence contains successful result IDs, not tool names."
                )
                last_assistant_text = None
            # No dispatch occurs for an invalid plain response. It still reaches the
            # budget accounting below, so repeated violations are bounded.

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
                (tc for tc in result.tool_calls if tc.name == "askUser" and not tc.argument_error),
                None,
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
                list(state_calls) if ask_user_call is not None else [*state_calls, *other_calls]
            )
            if ask_user_call is not None:
                kept_ids = {c.id for c in capped_tool_calls} | {ask_user_call.id}
                dropped = [c for c in result.tool_calls if c.id not in kept_ids]
                if dropped:
                    self._observer(
                        "loop_ask_user_batch_calls_dropped",
                        {
                            "dropped_count": len(dropped),
                            "dropped_tool_names": json.dumps([c.name for c in dropped]),
                            "dropped_tool_call_ids": json.dumps([c.id for c in dropped]),
                            "iteration": guard.usage().iterations + 1,
                            "reason": "ask_user_batch_pause",
                        },
                    )
            state_calls_dispatched = 0
            blueprint_search_gate.observe_batch(capped_tool_calls)
            dispatched_ids = set()
            for tool_call in capped_tool_calls:
                await cancellation_checkpoint("dispatch")
                capability_reused = False
                dispatched_ids.add(tool_call.id)
                is_unified_finalizer = tool_call.name == "finalizeAnswer"
                if is_unified_finalizer:
                    from .proposal import validate_proposal_args

                    error = validate_proposal_args(tool_call.arguments)
                    if error:
                        tool_call = replace(tool_call, argument_error=error)
                    tool_call = replace(
                        tool_call,
                        name=ANSWER_TABLE_TOOL_NAME
                        if tool_call.arguments.get("tables")
                        else ANSWER_TEXT_TOOL_NAME,
                    )
                raw_tags = tool_call.arguments.get("serves_intents", [])
                valid_tags = (
                    {i.intent_id for i in analysis_state.intents} if analysis_state else set()
                )
                serves_intents = (
                    tuple(
                        dict.fromkeys(t for t in raw_tags if isinstance(t, str) and t in valid_tags)
                    )
                    if isinstance(raw_tags, list)
                    else ()
                )
                if "serves_intents" in tool_call.arguments:
                    tool_call = replace(
                        tool_call,
                        arguments={
                            k: v for k, v in tool_call.arguments.items() if k != "serves_intents"
                        },
                    )
                # K2 gate: this call, if it is one of the four, closes the late-init
                # door for the rest of the turn. Recorded on the NAME and BEFORE
                # dispatch, deliberately: `find_locking_tool` keys on the persisted
                # entry's `tool_name` regardless of status, so a denial, an error and
                # a guard-served repeat all lock it just as a success does. Erring
                # toward suppression costs at most one note; erring the other way
                # costs the model a non-retryable refusal it was told to walk into.
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
                intent_handler = self._runtime_tools.get(tool_call.name)
                call_args, serves_intent, tag_drop_reason = split_serves_intent(
                    tool_call.name,
                    tool_call.arguments,
                    analysis_state,
                    additional_taggable=bool(
                        intent_handler is not None
                        and getattr(intent_handler, "intent_taggable", False)
                    ),
                )
                if tool_call.name in {ANSWER_TABLE_TOOL_NAME, ANSWER_TEXT_TOOL_NAME}:
                    from .proposal import filter_excluded_components

                    call_args = filter_excluded_components(
                        call_args, review_state.excluded_components
                    )
                    if tool_call.name == ANSWER_TABLE_TOOL_NAME and call_args.get("tables") == []:
                        if not is_unified_finalizer:
                            self._observer("loop_answer_table_empty_designation", {})
                        tool_call = replace(tool_call, name=ANSWER_TEXT_TOOL_NAME)
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
                        model_response=model_response,
                        serves_intents=serves_intents,
                        status="ok",
                        error_code=IDEMPOTENT_READ_ALREADY_SERVED_CODE,
                        denial_detail=(
                            (
                                _REPEATED_IDEMPOTENT_READ_NUDGE + " "
                                if read_decision.source_readable
                                else ""
                            )
                            + read_decision.nudge()
                        ),
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
                        {
                            **repeated_read_guard_event(
                                tool_call.name,
                                tool_call.id,
                                call_args,
                                source_readable=read_decision.source_readable,
                            ),
                            "serving_tool_call_id": read_decision.served_call_id,
                            "serving_round": read_decision.served_round,
                        },
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
                gate_refusal: ToolResult | None = loop_safety.check(tool_call.name)
                if tool_call.argument_error:
                    gate_refusal = ToolResult(
                        status="error",
                        tool_name=tool_call.name,
                        error_code="INVALID_TOOL_ARGUMENTS",
                        retryable=True,
                        user_message=tool_call.argument_error,
                        denial_detail=tool_call.argument_error,
                        provenance=frozenset(),
                        result_preview=None,
                        result_full=None,
                    )
                if gate_refusal is None and tool_call.name not in advertised_names(
                    tools,
                    self._runtime_tools,
                    {"askUser", UPDATE_ANALYSIS_STATE_TOOL_NAME, *_RUNTIME_TOOL_UNAVAILABLE_CODE},
                ):
                    gate_refusal = refusal(tool_call.name, UNKNOWN_TOOL_CODE)
                    self._observer(
                        "loop_unknown_tool_rejected",
                        {
                            "tool_name": sanitize_text(tool_call.name, 64),
                            "tool_call_id": tool_call.id,
                            "reason": "name_not_in_advertised_catalog",
                        },
                    )
                elif gate_refusal is None:
                    gate_refusal = blueprint_search_gate.check(tool_call.name)
                    if gate_refusal is not None:
                        self._observer("loop_blueprint_search_suggested", {"tool_name": "runQuery"})
                if (
                    gate_refusal is None
                    and tool_call.name == "runBlueprint"
                    and handler is not None
                ):
                    gate_refusal = blueprint_gate.check_run_blueprint(call_args)

                if gate_refusal is None and tool_call.name == "runQuery":
                    from data_agent.runtime.dispatch.sql_diagnostics import repeated_sql_failure

                    current_doc = await self._session_store.get_or_create_session(session_id)
                    if repeated_sql_failure(
                        str(call_args.get("sql", "")),
                        current_doc.tool_trail,
                        turn_index,
                        scope_hash=scope_filter.compute_scope_hash(credentials.column_scope),
                    ):
                        gate_refusal = replace(
                            refusal(tool_call.name, "SQL_REPAIR_EXHAUSTED"),
                            retryable=False,
                            provenance=None,
                        )
                        self._observer("loop_sql_repair_exhausted", {"tool_call_id": tool_call.id})

                if gate_refusal is None and tool_call.name in {
                    ANSWER_TABLE_TOOL_NAME,
                    ANSWER_TEXT_TOOL_NAME,
                }:
                    scope_rule = first_match(
                        clean_answer_text(call_args.get("answer")) or "",
                        accum.sql_executed,
                        question,
                        has_alternative_evidence=bool(
                            accum.capability_cards
                            or accum.has_answer_tables
                            or successful_text_evidence_tools
                        ),
                    )
                    if (
                        scope_rule
                        and scope_rule.name == "out_of_scope_request"
                        and await finalization_gate.may_refuse(scope_rule.charges_to)
                    ):
                        gate_refusal = scope_request_refused(
                            tool_call.name, self._observer, site="answer_scope_rule"
                        )

                if gate_refusal is None:
                    loop_safety.record_attempt(tool_call.name)
                if gate_refusal is None and tool_call.name == "runQuery":
                    from .measurement import validate_join_cardinality

                    detail = await validate_join_cardinality(
                        call_args.get("sql", ""), self._tool_dispatcher, credentials
                    )
                    if detail:
                        gate_refusal = ToolResult(
                            status="error",
                            tool_name=tool_call.name,
                            error_code="AGGREGATION_RISK",
                            retryable=True,
                            user_message=detail,
                            denial_detail=detail,
                            provenance=frozenset(),
                            result_preview=None,
                            result_full=None,
                        )
                # A gated call does not run. Otherwise summarize before its start event.
                if gate_refusal is None and tool_call.name not in {
                    ANSWER_TABLE_TOOL_NAME,
                    ANSWER_TEXT_TOOL_NAME,
                }:
                    await self._maybe_start_summary(tool_call.name, tool_call.id, call_args)

                # Runtime-tool registry (read-tools-design §2): a runtime tool
                # (`resolveValues` + the three read tools) is intercepted here —
                # it never reaches `dispatch` under its own name (only any inner
                # tool it issues does). It returns the SAME `ToolResult`
                # dataclass, so the trail/budget path below is unchanged and it
                # counts as exactly one `tool_calls_made`. An advertised runtime
                # tool that is not wired returns a clean local unavailable error
                # (§6), never an incoherent MCP unknown-tool denial.
                # `handler` was resolved above, at the gate.
                local_dispatch = gate_refusal is None and (
                    (
                        tool_call.name == UPDATE_ANALYSIS_STATE_TOOL_NAME
                        and (handler is None or state_calls_dispatched >= MAX_STATE_CALLS)
                    )
                    or (handler is None and tool_call.name in _RUNTIME_TOOL_UNAVAILABLE_CODE)
                )
                if local_dispatch:
                    self._observer(
                        "tool_dispatch_start",
                        {"tool_name": tool_call.name, "tool_call_id": tool_call.id},
                    )
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
                            tool_call.id,
                        )
                    else:  # pragma: no cover - always wired by app.py
                        tool_result = _runtime_tool_internal_error(tool_call.name)
                elif handler is not None:
                    cached_preparation = (
                        await preparation_cache.lookup(tool_call.name, call_args)
                        if getattr(handler, "repeat_guard_eligible", False)
                        else None
                    )
                    capability_reused = cached_preparation is not None
                    tool_result = (
                        cached_preparation
                        if capability_reused
                        else await self._run_runtime_tool(
                            handler,
                            tool_call.name,
                            call_args,
                            credentials,
                            turn_context,
                            tool_call.id,
                        )
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

                if local_dispatch:
                    self._observer(
                        "tool_dispatch_" + tool_result.status,
                        {
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "error_code": tool_result.error_code,
                        },
                    )

                if gate_refusal is None:
                    loop_safety.observe_help(tool_call.name, tool_result.error_code)
                loop_safety.observe_result(
                    tool_call.name, call_args, tool_result, reused=capability_reused
                )

                if tool_call.name == UPDATE_ANALYSIS_STATE_TOOL_NAME:
                    refreshed = refreshed_analysis_state(tool_result, turn_index)
                    if refreshed is not None:
                        analysis_state = refreshed
                resolved_answer_tables = []
                if tool_call.name == ANSWER_TABLE_TOOL_NAME and tool_result.status == "ok":
                    resolved_answer_tables, table_refusal, _ = await self._resolve_answer_tables(
                        call_args,
                        blueprint_runs=accum.blueprint_runs,
                        credentials=credentials,
                        session_id=session_id,
                        turn_index=turn_index,
                    )
                    if table_refusal:
                        tool_result = table_refusal
                if (
                    tool_call.name in {ANSWER_TABLE_TOOL_NAME, ANSWER_TEXT_TOOL_NAME}
                    and tool_result.status == "ok"
                ):
                    proposal_args = dict(call_args)
                    proposal_call_id = tool_call.id
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
                    for pending in result.tool_calls:
                        if pending.id in dispatched_ids and pending.id != tool_call.id:
                            continue
                        await self._session_store.append_trail_entry(
                            session_id,
                            TrailEntry(
                                turn_index=turn_index,
                                tool_call_id=pending.id,
                                tool_name=pending.name,
                                args=pending.arguments,
                                status="error",
                                error_code="TOOL_PAUSED"
                                if pending.id == tool_call.id
                                else "TOOL_NOT_EXECUTED",
                                denial_detail="Awaiting clarification."
                                if pending.id == tool_call.id
                                else "Not executed because an earlier call paused. Reissue if needed.",
                                provenance=frozenset(),
                                result_preview=None,
                                result_full_ref=None,
                                ts=_now_iso(),
                                model_response=model_response,
                            ),
                        )
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
                        serves_intents=serves_intents,
                    )
                tool_calls_made += 1
                result_full_ref: str | None = None
                # Final-answer arguments contain derived prose. Give their trail
                # entries the same evidence scope as the persisted answer, rather
                # than the handler's empty (data-free confirmation) provenance.
                if tool_call.name in {ANSWER_TABLE_TOOL_NAME, ANSWER_TEXT_TOOL_NAME}:
                    tool_result = replace(
                        tool_result,
                        provenance=await self._compute_turn_provenance_union(
                            session_id, turn_index
                        ),
                    )
                if tool_result.result_full is not None:
                    result_full_ref = await self._session_store.write_full_result(
                        session_id, str(uuid.uuid4()), tool_result.result_full
                    )

                entry = TrailEntry(
                    turn_index=turn_index,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    args=dict(call_args),
                    model_response=model_response,
                    serves_intents=serves_intents,
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
                    capability_terminal=tool_result.terminal
                    or bool(
                        isinstance(tool_result.result_full, dict)
                        and tool_result.result_full.get("prepared")
                    ),
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

                if getattr(handler, "repeat_guard_eligible", False):
                    preparation_cache.record(tool_call.name, call_args, tool_result, tool_call.id)
                if capability_reused:
                    self._observer(
                        "loop_repeated_capability_call_guarded",
                        {
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "deduped": True,
                        },
                    )

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
                accum.note_loaded_capability(tool_call.name, tool_result)
                # answerWithTable (composite/answer_with_table.py): the
                # model-designated answer tables. Same discipline again — read from
                # the call ARGUMENTS on success — except LAST designation wins,
                # over the whole SET, since a turn has one answer.
                accum.note_answer_tables(tool_call.name, tool_result, resolved_answer_tables)
                if is_unified_finalizer and tool_result.status == "ok":
                    accum.select_answer_tables(resolved_answer_tables or ())
                help_grounding.observe(tool_call.name, tool_result.status, tool_result.result_full)
                if (
                    tool_result.status == "ok"
                    and tool_call.name in _TEXT_ANSWER_EVIDENCE_TOOLS
                    and (
                        tool_call.name != "getHelpCenterDocument" or help_grounding.document_fetched
                    )
                ):
                    successful_text_evidence_tools.add(tool_call.name)
                if tool_result.status == "ok" and (
                    tool_result.terminal
                    or (
                        isinstance(tool_result.result_full, dict)
                        and tool_result.result_full.get("prepared")
                    )
                ):
                    prior_deduped = accum.capability_cards_deduped
                    accum.note_capability_card(tool_result)
                    if accum.capability_cards_deduped > prior_deduped:
                        self._observer("loop_capability_card_deduped", {"dropped": 1})
                # TERMINAL: a successful `answerWithTable` carries the final prose,
                # so the turn ends on it. Recorded here and acted on AFTER the whole
                # tool batch drains, so a model that batches recordAssumptions +
                # answerWithTable still gets both folded before the turn closes.
                if tool_call.name == ANSWER_TABLE_TOOL_NAME and tool_result.status == "ok":
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
                    (
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

            # BLUEPRINT-DEFINITION GATE, the fold. HERE, once the batch has drained,
            # and deliberately not mid-batch: ids expanded in THIS response become
            # runnable only from the NEXT one, because the result of a `getBlueprint`
            # issued in this response does not reach the model until the next
            # round-trip. See `BlueprintGate.commit_round` for the full rationale —
            # the position of this call is the half of it that lives here.
            blueprint_gate.commit_round()
            for skipped in result.tool_calls:
                if skipped.id in dispatched_ids:
                    continue
                await self._session_store.append_trail_entry(
                    session_id,
                    TrailEntry(
                        turn_index=turn_index,
                        tool_call_id=skipped.id,
                        tool_name=skipped.name,
                        args=skipped.arguments,
                        status="error",
                        error_code="CLARIFICATION_REQUESTED"
                        if ask_user_call is not None and skipped.id == ask_user_call.id
                        else "TOOL_NOT_EXECUTED",
                        denial_detail=(
                            "Clarification requested. Apply the next user answer or decline and continue without repeating it. If runtime feedback rejects this question, follow that feedback."
                            if ask_user_call is not None and skipped.id == ask_user_call.id
                            else "This call was not executed because the batch paused or reached its call limit. Reissue it if still needed."
                        ),
                        provenance=frozenset(),
                        result_preview=None,
                        result_full_ref=None,
                        ts=_now_iso(),
                        model_response=model_response,
                    ),
                )

            # TERMINATION: pause. Honoured AFTER the state calls above have been
            # committed (03 §E.1) and BEFORE anything else in the batch is
            # dispatched — `capped_tool_calls` held only the state calls on this
            # path, so every other call still waits for the resume exactly as it
            # always did.
            if ask_user_call is not None:
                raw_question = str(ask_user_call.arguments.get("question", ""))
                declined_key = _question_key(declined_question) if declined_question else ""
                if declined_key and _question_key(raw_question) == declined_key:
                    if not declined_repeat_refused:
                        declined_repeat_refused = True
                        finalization_nudge = (
                            "You repeated the clarification the user declined. Do not ask it "
                            "again. Answer using a safe assumption, or finish now with the "
                            "specific reason an answer is not possible."
                        )
                        continue
                    honest = (
                        "I can’t complete this reliably without the information you chose "
                        "not to provide, so I’m stopping instead of asking the same question "
                        "again."
                    )
                    await self._force_block_pending_intents(
                        session_id=session_id,
                        turn_index=turn_index,
                        state=analysis_state,
                        reason_code="USER_STOPPED",
                    )
                    return await self._finish(
                        session_id=session_id,
                        turn_index=turn_index,
                        status="done",
                        exit_label="declined_clarification",
                        assistant_text=honest,
                        tool_calls_made=tool_calls_made,
                        accum=accum,
                        persist_text=honest,
                        provenance=await self._compute_turn_provenance_union(
                            session_id, turn_index
                        ),
                    )

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
                async def _ask_brief(
                    _q: str = question,
                    _state: AnalysisState | None = analysis_state,
                    _asked: str = raw_question,
                    _options: tuple[str, ...] = tuple(
                        [
                            o
                            for o in ask_user_call.arguments.get("options", [])
                            if isinstance(o, str)
                        ]
                        if isinstance(ask_user_call.arguments.get("options"), list)
                        else ()
                    ),
                ) -> JudgeBrief:
                    results, anchor, _ = await self._judge_results(
                        session_id,
                        turn_index,
                        credentials.column_scope,
                        include_all_successful=True,
                    )
                    brief = self._judge_brief(
                        "ask_user",
                        question=_q,
                        accum=accum,
                        ship_guard=ship_guard,
                        analysis_state=_state,
                        date_anchor=anchor,
                        results=results,
                        pending_question=_asked,
                        pending_options=_options,
                    )

                    return replace(
                        brief,
                        clarification_answers=tuple(
                            m.content
                            for m in session_doc.messages
                            if m.role == "user" and m.turn_index == turn_index
                        )[1:],
                    )

                ask_verdict = await self._judge(
                    _ask_brief,
                    guard=guard,
                    gate=finalization_gate,
                    kind="ask_user_judge",
                )
                if not ask_verdict.approved:
                    review_state.question_refusals[
                        fingerprint(
                            {
                                "question": raw_question,
                                "options": ask_user_call.arguments.get("options") or [],
                            }
                        )
                    ] = ask_verdict.violation
                    await self._session_store.write_review_state(
                        session_id, turn_index, review_state.to_doc()
                    )
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
                normalized = normalize_clarification(
                    question, ask_user_call.arguments.get("options")
                )
                question, options = normalized["question"], normalized["options"]
                if ask_verdict.approved and ask_verdict.reviewed:
                    review_state.question_refusals.clear()
                    review_state.delivery_version = delivery_version(
                        result.assistant_text, accum, normalized
                    )
                    review_state.delivery_status = "approved"
                    await self._session_store.write_review_state(
                        session_id, turn_index, review_state.to_doc()
                    )
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

            if proposal_args is not None:
                final_text = clean_answer_text(proposal_args.get("answer")) or ""
                final_text, proposal_redactions = scrub_answer_prose(
                    final_text,
                    provenance=await self._compute_turn_provenance_union(session_id, turn_index),
                )
                if proposal_redactions:
                    self._observer(
                        ANSWER_PROSE_REDACTED_EVENT,
                        {
                            "redaction_count": proposal_redactions,
                            "exit": "answer_with_table"
                            if proposal_args.get("tables")
                            else "answer_with_text",
                        },
                    )
                self._observe_uncovered_intents(
                    analysis_state,
                    tables=accum.answer_tables,
                    result_sql_by_call_id=accum.result_sql_by_call_id,
                )
                current_trail = await self._session_store.load_trail(session_id)
                evidence_trail = [
                    e
                    for e in scope_filter.filter_trail(
                        current_trail, credentials.column_scope, current_turn_index=turn_index
                    )
                    if e.turn_index == turn_index
                ]
                from .proposal import (
                    omission_nudge,
                    omit_components,
                    selected_components,
                )

                deliverables, evidence_error = deliverable_evidence(
                    analysis_state, proposal_args, evidence_trail
                )
                refs = proposal_args.get("capability_refs", [])
                if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                    evidence_error = "capability_refs must contain prepared option IDs."
                    refs = []
                if refs:
                    missing = set(refs) - (
                        accum.prepared_capability_names & self._runtime_tools.keys()
                    )
                    if missing:
                        evidence_error = "A requested UI option is not prepared. Load and prepare it before finalizing."
                # Explicit selection; preparation alone is never user-visible finalization.
                if "capability_refs" in proposal_args:
                    accum.select_capabilities(refs)
                final_text = (
                    coherent_capability_ship_text(
                        final_text, accum.capability_cards, ship_guard.unsafe_ship()
                    )
                    if accum.capability_cards
                    else final_text
                )
                if help_grounding.needs_decline(
                    successful_text_evidence_tools, bool(accum.capability_cards)
                ):
                    final_text = HELP_UNAVAILABLE_TEXT
                component_catalog = selected_components(
                    proposal_args,
                    evidence_trail,
                    {
                        **{key: run.terminal_sql for key, run in accum.blueprint_runs.items()},
                        **accum.result_sql_by_call_id,
                    },
                )
                complete = {
                    "answer": final_text,
                    "tables": [t.to_doc() for t in accum.answer_tables],
                    "assumptions": accum.assumptions,
                    "capabilities": accum.capability_cards,
                    "deliverables": deliverables,
                }
                version = fingerprint(complete)
                site = (
                    "exit_capability"
                    if accum.capability_cards
                    else "exit_table"
                    if accum.has_answer_tables
                    else "exit_prose"
                )
                combined_prose = final_text + "\n" + "\n".join(accum.assumptions or ())
                rule = first_match(
                    combined_prose,
                    accum.sql_executed,
                    question,
                    has_alternative_evidence=bool(
                        any(d["evidence"] for d in deliverables)
                        or accum.capability_cards
                        or accum.has_answer_tables
                    ),
                    declined_clarification=declined_question,
                )
                if (
                    rule
                    and rule.name == "no_evidence"
                    and self._answer_judge is not None
                    and getattr(self._answer_judge, "enabled", True)
                ):
                    # Conversational text needs no warehouse receipt. The judge checks
                    # whether an evidence-free answer makes unsupported substantive claims.
                    rule = None
                feedback = evidence_error
                kind = "ungrounded_answer"
                if not final_text:
                    feedback, kind = (
                        "Provide the complete answer in finalizeAnswer.answer.",
                        "empty_answer",
                    )
                elif pending_intents(analysis_state):
                    feedback, kind = (
                        finalization_nudge_text(final_text, pending_intents(analysis_state)),
                        "intents",
                    )
                elif rule:
                    feedback, kind = rule.nudge(final_text), rule.charges_to
                elif (
                    any(
                        e.tool_name in DATA_ANSWER_TOOLS
                        and e.status == "ok"
                        and e.result_preview is not None
                        and e.result_preview.row_count > 1
                        and e.tool_call_id
                        not in {c["result_id"] for c in review_state.excluded_components}
                        for e in evidence_trail
                    )
                    and not accum.has_answer_tables
                    and not accum.capability_cards
                ):
                    echo = final_text[:MAX_NUDGE_DRAFT_CHARS]
                    if len(echo) < len(final_text):
                        echo += " …[truncated]"
                    feedback, kind = (
                        f"You drafted: {echo}\n\nThe turn is NOT over. Use finalizeAnswer.tables with the result IDs for the requested breakdown. Disclose any missing part and include the complete answer in finalizeAnswer.answer.",
                        "answer_shape",
                    )
                if feedback:
                    if await finalization_gate.may_refuse(kind):
                        if kind == "empty_answer":
                            self._observer(
                                EMPTY_ANSWER_REFUSED_EVENT,
                                {"incomplete_reason": result.incomplete_reason or ""},
                            )
                        if rule and feedback == rule.nudge(final_text):
                            self._observer(ANSWER_RULE_REFUSED_EVENT, {"rule": rule.name})
                        if kind == "answer_shape":
                            self._observer(
                                "loop_answer_shape_refused",
                                {"multi_row_calls": answer_shape.multi_row_calls},
                            )
                        elif kind == "intents":
                            self._observer(
                                "loop_finalization_refused",
                                {
                                    "exit": "answer_with_table"
                                    if accum.has_answer_tables
                                    else "answer_with_text",
                                    "pending_count": len(pending_intents(analysis_state)),
                                },
                            )
                        finalization_nudge = feedback
                    else:
                        if kind == "empty_answer":
                            self._observer(
                                EMPTY_ANSWER_EXHAUSTED_EVENT,
                                {"incomplete_reason": result.incomplete_reason or ""},
                            )
                        if rule and feedback == rule.nudge(final_text):
                            self._observer(ANSWER_RULE_EXHAUSTED_EVENT, {"rule": rule.name})
                        if kind == "answer_shape":
                            self._observer("loop_answer_shape_exhausted", {})
                        final_text = (
                            EMPTY_ANSWER_FALLBACK_TEXT
                            if kind == "empty_answer"
                            else (
                                rule.refusal
                                if rule and rule.refusal
                                else "I could not verify every requested part from the available evidence."
                            )
                        )
                        accum.apply_ship_disposition(
                            "ship_tables_with_hedge" if accum.has_answer_tables else "decline_only",
                            (),
                        )
                else:
                    enabled = self._answer_judge is not None and getattr(
                        self._answer_judge, "enabled", True
                    )
                    verdict = APPROVED
                    remaining = guard.usage().max_wall_clock_seconds - guard.usage().elapsed_seconds
                    if enabled and review_state.calls < 2 and remaining > 0:
                        # Persist consumption BEFORE the external call, so a pause/retry
                        # cannot reset it. The second validation never grants another repair.
                        review_state.calls += 1
                        review_state.answer_version = version
                        await self._session_store.write_review_state(
                            session_id, turn_index, review_state.to_doc()
                        )
                        brief_ready = False
                        try:
                            results, anchor, in_scope = await self._judge_results(
                                session_id,
                                turn_index,
                                credentials.column_scope,
                                exclude_result_ids=frozenset(
                                    c["result_id"] for c in review_state.excluded_components
                                ),
                            )
                            brief = self._judge_brief(
                                site,
                                question=question,
                                accum=accum,
                                analysis_state=analysis_state,
                                date_anchor=anchor,
                                draft=final_text,
                                results=results,
                                ship_guard=ship_guard,
                                designated_tables=tuple(
                                    (t.caption, t.sql) for t in accum.answer_tables
                                ),
                            )
                            brief = replace(
                                brief,
                                deliverables=tuple(deliverables),
                                selected_components=tuple(component_catalog),
                                referenced_result_ids=tuple(proposal_args.get("evidence", ())),
                                capability_presented=tuple(
                                    {
                                        **card,
                                        "result_ids": [
                                            c["result_id"]
                                            for c in component_catalog
                                            if c.get("capability_ref") == card.get("name")
                                        ],
                                    }
                                    for card in brief.capability_presented
                                ),
                                excluded_components=review_state.excluded_components,
                                clarification_answers=tuple(
                                    m.content
                                    for m in session_doc.messages
                                    if m.role == "user" and m.turn_index == turn_index
                                )[1:],
                                designated_tool_call_ids=tuple(
                                    t["result_id"]
                                    for t in proposal_args.get("tables", [])
                                    if isinstance(t, dict) and isinstance(t.get("result_id"), str)
                                ),
                            )
                            from .judge_evidence import enrich_brief

                            brief = await enrich_brief(
                                brief,
                                trail=in_scope,
                                turn_index=turn_index,
                                session_id=session_id,
                                store=self._session_store,
                                catalog_provider=self._judge_catalog,
                                credentials=credentials,
                                analysis_state=analysis_state,
                            )
                            brief_ready = True
                            verdict = await review_once(self, brief)
                        except Exception as exc:
                            self._observer(
                                ANSWER_JUDGE_FAILED_EVENT,
                                {
                                    "reason": "timeout"
                                    if isinstance(exc, TimeoutError)
                                    else "review_failed"
                                    if brief_ready
                                    else "brief_failed"
                                },
                            )
                            verdict = APPROVED
                    if verdict.approved and verdict.reviewed:
                        review_state.approved_version = version
                        review_state.violation = ""
                        ship_guard.note_approval()
                        review_state.delivery_version = delivery_version(final_text, accum)
                        review_state.delivery_status = "approved"
                    elif not verdict.approved:
                        self._observer(
                            ANSWER_JUDGE_REFUSED_EVENT,
                            {"violation": verdict.violation, "site": site},
                        )
                        review_state.site = site
                        review_state.reject(verdict, version, accum.assumptions or ())
                        ship_guard.note_refusal(
                            site,
                            verdict.violation,
                            assumptions=review_state.assumptions_before_refusal,
                        )
                        if not review_state.repaired and review_state.calls < 2 and remaining > 0:
                            review_state.repaired = True
                            feedback = verdict.feedback
                            finalization_nudge = answer_judge_nudge_text(final_text, feedback)
                            finalization_nudge += "\nRepair target: " + json.dumps(
                                {
                                    "intent_id": verdict.intent_id,
                                    "result_ids": verdict.result_ids,
                                    "repair_type": verdict.repair_type,
                                }
                            )
                            if omit_components(review_state, verdict, component_catalog):
                                accum.exclude_components(review_state.excluded_components)
                                finalization_nudge = omission_nudge(
                                    review_state.excluded_components
                                ).lstrip()
                            elif verdict.repair_type == "omit_component":
                                finalization_nudge += " The omission target was invalid or ambiguous; no component was automatically removed. Correct the answer using accessible evidence."
                            if verdict.repair_type in {"prose", "presentation"}:
                                finalization_nudge += " Preserve the existing results. No additional warehouse query is needed."
                                if accum.capability_cards:
                                    finalization_nudge += (
                                        " The selected UI options are already prepared. Repair "
                                        "finalizeAnswer using the existing capability_refs and "
                                        "evidence result IDs; do not reload or prepare them again, "
                                        "or add filters to repair prose. Unresolved selection "
                                        "does not establish how many employees matched."
                                    )
                    await self._session_store.write_review_state(
                        session_id, turn_index, review_state.to_doc()
                    )
                if not feedback or finalization_nudge is None:
                    if review_state.approved_version == version:
                        await self._maybe_start_summary(
                            "finalizeAnswer", proposal_call_id, proposal_args, judge_approved=True
                        )
                        for event in ("tool_dispatch_start", "tool_dispatch_ok"):
                            self._observer(
                                event,
                                {
                                    "tool_name": "finalizeAnswer",
                                    "tool_call_id": proposal_call_id,
                                    "judge_approved": True,
                                },
                            )
                    if pending_intents(analysis_state):
                        self._observer(
                            "loop_enforcement_exhausted",
                            {"intent_count": len(pending_intents(analysis_state))},
                        )
                        await self._force_block_pending_intents(
                            session_id=session_id,
                            turn_index=turn_index,
                            state=analysis_state,
                            reason_code="ENFORCEMENT_EXHAUSTED",
                        )
                    return await self._finish(
                        session_id=session_id,
                        turn_index=turn_index,
                        status="done",
                        exit_label="answer_with_table"
                        if accum.has_answer_tables
                        else "answer_with_text",
                        assistant_text=final_text,
                        persist_text=final_text,
                        accum=accum,
                        tool_calls_made=tool_calls_made,
                        ship_guard=ship_guard,
                        judge_site=site,
                        provenance=await self._compute_turn_provenance_union(
                            session_id, turn_index
                        ),
                        event=("loop_turn_done", {"tool_calls_made": tool_calls_made}),
                    )

            discovery_names = {
                "listDatabases",
                "listTables",
                "getTableSchema",
                "searchKnowledge",
                "searchBlueprints",
                "getBlueprint",
                "searchHelpCenter",
                "getHelpCenterDocument",
                "searchCapabilityTools",
                "getCapabilityTool",
            }
            if result.tool_calls and all(c.name in discovery_names for c in result.tool_calls):
                discovery_rounds += 1
            else:
                discovery_rounds = 0
            if discovery_rounds >= 6 and finalization_nudge is None:
                finalization_nudge = (
                    "You have spent six consecutive rounds on discovery. Assess the evidence "
                    "already received against the original request now. For an overview, finalize "
                    "a concise supported overview rather than inventorying the catalog. If no "
                    "relevant article or option was found, finalize with that limitation. Continue "
                    "discovery only for a specific fact necessary to answer an unresolved part. "
                    "Use finalizeAnswer; empty tables, capability_refs and evidence lists are valid "
                    "when declining unsupported claims."
                )
                discovery_rounds = 0

            # SPEND, not occupancy: `total_tokens` is this round-trip's prompt +
            # completion, and every round replays the conversation, so the sum is
            # what the window COST — not how full the model's context is. It is
            # measured against `max_token_spend` (a spend ceiling), never against
            # the context window; occupancy is enforced per-request by
            # `fit_request_to_budget` above. Cached prompt tokens are counted at
            # full weight on purpose (loop/budget_guard.py module docstring).
            guard.record_iteration(tokens_used=int(result.usage.get("total_tokens") or 0))

            if loop_safety.end_round():
                await self._force_block_pending_intents(
                    session_id=session_id,
                    turn_index=turn_index,
                    state=analysis_state,
                    reason_code="ENFORCEMENT_EXHAUSTED",
                )
                return await self._finish(
                    session_id=session_id,
                    turn_index=turn_index,
                    status="stopped_no_progress",
                    exit_label="no_progress",
                    assistant_text=None,
                    persist_text=NO_PROGRESS_TEXT,
                    completion_notice=NO_PROGRESS_TEXT,
                    tool_calls_made=tool_calls_made,
                    accum=accum,
                    ship_guard=ship_guard,
                    judge_site="exit_capability"
                    if accum.capability_cards
                    else "exit_table"
                    if accum.has_answer_tables
                    else "exit_prose",
                    provenance=frozenset(),
                    event=(
                        "loop_no_new_evidence_stop",
                        {
                            "iteration": guard.usage().iterations,
                            "window": window_count,
                            "stagnant_rounds": loop_safety.stagnant_rounds,
                            "exit": "no_progress",
                        },
                    ),
                )
            if loop_safety.stagnant_rounds == self._max_no_progress_rounds - 1:
                finalization_nudge = (finalization_nudge or "") + "\n" + NO_PROGRESS_COACH

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


__all__ = [
    "AgentLoop",
    "RuntimeTool",
    "ToolsProvider",
    "TurnContext",
    "TurnOutcome",
]
