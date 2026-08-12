"""AgentLoop — the per-turn state machine (design §4.1), built on Pass-A's seams.

Implements the design §4.1 turn state machine exactly:

    1. (caller) builds `RuntimeCredentials` (app.py, from the inbound request).
    2. `ContextAssembler.assemble(session_id, column_scope)` -> canonical messages.
    3. loop:
         a. `ModelClient.send_turn(messages, tools)` -> `ModelTurnResult`.
         b. no tool_calls -> done. [TERMINATION: normal]
         c. for each tool_call:
              - `askUser` -> pause checkpoint, return control. [TERMINATION: pause]
              - else -> `ToolDispatcher.dispatch(...)`, persist `TrailEntry`
                (+full result via `SessionStore.write_full_result`) + a tool
                message. Dispatched **sequentially**, not concurrently: the
                design flags parallel tool-call dispatch as a possible
                optimization "capped — see §11 tunables", but §11 leaves the
                cap value an open question, and sequential dispatch keeps
                `BudgetGuard` iteration accounting and trail ordering
                trivially deterministic for Phase 0 — a pure performance
                question deferred, like design §11 OQ-J's MCP connection
                pooling.
         d. `BudgetGuard` check -> continue, or budget-cap pause
            (or the hard outer ceiling force-stop). [TERMINATION: budget /
            hard ceiling]
    4. `resume()` — a separate entry point — CAS-consumes the checkpoint
       (`SessionStore.resume_checkpoint`, D45), threads the answer back in,
       and re-enters the loop with a FRESH `BudgetGuard` window; a fresh
       *window grant* (D55) is only counted for `reason="budget_cap"` +
       "continue"/"refine" answers — a "stop" answer ends the turn with the
       best partial result already in the trail; an `askUser`-reason resume
       just continues the same window count (it was not a budget grant).

Read-only, no D56 verify gate (Phase 1): the final assistant message is
returned to the user as-is.

`askUser` is intercepted here and ONLY here — it is never handed to
`ToolDispatcher.dispatch` (design §3.3 "askUser is intercepted upstream in
the agent loop, never reaches this dispatcher").

Runtime tools (`resolveValues` + the three read tools `searchBlueprints`/
`getBlueprint`/`searchKnowledge`) are intercepted here via the `runtime_tools`
registry (read-tools-design §2), handled symmetrically: each never reaches
`ToolDispatcher.dispatch` under its own name (only any inner tool it issues
does), but — unlike `askUser`, which pauses — each returns an INLINE `ToolResult`
so the loop's existing TrailEntry + write_full_result + budget path handles it
identically to a dispatched tool. Each therefore counts as exactly ONE
`tool_calls_made` and respects `max_tool_calls_per_iteration` + wall-clock like
any other tool call. An advertised-but-unwired runtime tool returns a clean
local unavailable error, never an MCP unknown-tool denial (§6).

Statelessness across pauses (D45): both `run()` and `resume()` rebuild the
canonical message list from the `SessionStore` on every single model
round-trip (`_build_canonical_messages`) rather than carrying an in-memory
working-message list across the pause boundary — any process can resume any
paused session because there is no in-process state that survives a pause.

D5 model-invisibility (load-bearing): `RuntimeCredentials` (jwt, session_id,
raw column_scope) is threaded as an explicit argument to
`ToolDispatcher.dispatch` and to `ContextAssembler.assemble` (scope only) —
it is NEVER placed into the canonical `messages` list handed to
`ModelClient.send_turn`. `tests/runtime/loop/test_agent_loop.py` scans every
message payload `ScriptedModelClient` records across a multi-tool-call turn
for the JWT/session_id substrings to prove this end-to-end.

Deviation from the design doc (noted for review): the design's §4.1
"BudgetGuard.check(iterations, tokens, wall_clock)" is driven off
`RuntimeSettings` values, but this class accepts the four budget scalars
(`max_loop_iterations`, `max_wall_clock_seconds`, `max_budget_windows`,
`token_budget`) directly as constructor arguments rather than a whole
`RuntimeSettings` object — a narrower, more directly-testable dependency
surface. `app.py` (the composition root) is the only caller expected to
thread these through from `RuntimeSettings`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.analysis_state import (
    MAX_STATE_CALLS,
    surplus_state_call_rejected,
)
from data_agent.runtime.composite.analysis_state import TOOL_NAME as UPDATE_ANALYSIS_STATE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import TOOL_NAME as ANSWER_TABLE_TOOL_NAME
from data_agent.runtime.composite.answer_with_table import (
    clean_answer_text,
    clean_blueprint_id,
    resolve_designation,
)
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.context.assembly import (
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
)
from data_agent.runtime.context.budget import fit_request_to_budget
from data_agent.runtime.dispatch.denial_mapping import (
    FINALIZATION_BLOCKED_PENDING_INTENTS_CODE,
)
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    ToolPause,
    ToolResult,
)
from data_agent.runtime.hooks.answer_table import (
    AnswerTableEvent,
    AnswerTableHooks,
    references_scratch,
)
from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.runtime.observability.progress_summarizer import ProgressSummarizer
from data_agent.runtime.observability.redaction import hash_scope
from data_agent.runtime.sanitize import MAX_FIELD_CHARS, sanitize_text
from data_agent.runtime.session.models import (
    AnalysisState,
    PauseCheckpoint,
    TrackedIntent,
    TrailEntry,
    TurnMessage,
    live_analysis_state,
)
from data_agent.runtime.session.store import SessionStore

from .budget_guard import new_budget_window
from .read_guard import IDEMPOTENT_READ_TOOLS, idempotent_read_signature

if TYPE_CHECKING:
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
    """What a `RuntimeTool` may know about the turn it is running in (03 §C.1).

    `turn_index` ONLY. It exists because `updateAnalysisState` must write
    turn-scoped state, and the two alternatives are both wrong:

      - Take it from `app.py`. `runtime_tools` is built in `_build_agent_loop`,
        which has only `turn_index_hint` — documented at its own definition as
        "best-effort … never load-bearing for correctness, purely a telemetry
        label". Making it load-bearing introduces a TOCTOU gap against the loop's
        own computation.
      - Re-derive it from the store. `/turn` and `/turn/resume` use DIFFERENT
        formulas (`messages[-1].turn_index + 1` vs `messages[-1].turn_index`), so
        duplicating the derivation guarantees eventual disagreement.

    IT MUST NOT CARRY THE TRAIL. `_run_loop_body`'s only trail load sits ABOVE
    the round-trip loop and is immediately reduced to signatures for
    `seen_read_calls`; every entry is appended later. A snapshot taken there
    contains NOTHING from the current window, so evidence written in round 1 and
    cited in round 2 would fail as "unknown tool_call_id" — every completion and
    block, on every turn, while looking correctly wired. A tool that needs the
    trail loads it itself, filtered to `turn_index`.
    """

    turn_index: int


class RuntimeTool(Protocol):
    """A model-facing tool implemented in the RUNTIME (not the MCP), intercepted
    in the loop and returning an inline `ToolResult` — the `resolveValues` shape
    (read-tools-design §2). `askUser` is NOT a `RuntimeTool`: it is TERMINAL (it
    pauses, it does not return a `ToolResult`), so it stays a hardcoded branch.

    *turn* is passed by `_run_runtime_tool` on every dispatch. It is keyword-
    optional so a tool that does not care about the turn simply ignores it; the
    signature is EXPLICIT rather than a side channel because six implementers and
    no production constraint made the honest version cheap."""

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

_BUDGET_CAP_QUESTION = "This is taking a while — continue, refine, or stop?"
_BUDGET_CAP_OPTIONS = ["continue", "refine", "stop"]

# The repeated-idempotent-read guard primitives (`IDEMPOTENT_READ_TOOLS` +
# `idempotent_read_signature`) now live in `loop/read_guard.py` — a neutral leaf
# shared with `context/discovery_emulation.py` so that module no longer reaches
# into this one's private namespace at runtime. Imported at the top of this file.


def _repeated_read_guard_event(
    tool_name: str, tool_call_id: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the self-describing `loop_repeated_idempotent_read_guarded` observer
    payload so the exported GUARDRAIL span reads unambiguously in a trace: it was a
    SECOND, duplicate read that was deduped — NOT the first fetch being blocked.

    Only CATALOG-safe identifier args are surfaced (`database`/`table` — the same
    scalar identifiers a real `tool.<name>` dispatch span already exposes); free-form
    args (notably `explainQuery`'s `sql`) are deliberately NEVER placed on the span,
    keeping the default D25/OTLP shape-only posture intact. `deduped=True` +
    `guard_reason` + a human-readable `note` make the span self-explain next to the
    real `tool.<name>` span of the first, dispatched call."""
    database = arguments.get("database")
    table = arguments.get("table")
    db = database if isinstance(database, str) and database else None
    tbl = table if isinstance(table, str) and table else None
    if db and tbl:
        dedup_target = f"{db}.{tbl}"
    elif tbl:
        dedup_target = tbl
    elif db:
        dedup_target = db
    else:
        dedup_target = ""
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "deduped": True,
        "guard_reason": "already_served_this_turn",
        "dedup_target": dedup_target,
        "note": (
            f"duplicate {tool_name}({dedup_target}) — already served this turn; not re-dispatched"
        ),
    }
    if db:
        payload["database"] = db
    if tbl:
        payload["table"] = tbl
    return payload


def _now_iso() -> str:
    # Wall-clock `ts` is now the CONTEXT ORDERER (context/assembly.py merges the two
    # streams by `(turn_index, ts, stream_rank)`), not just a display stamp. It need
    # not be perfectly monotonic: `turn_index` dominates the sort, so any clock
    # skew/backward step can only misorder items WITHIN a single turn — never across
    # turns, and never in a way that breaks assistant/tool pairing (that is enforced
    # structurally downstream), so there is no API-400 risk from a ts wobble.
    return datetime.now(UTC).isoformat()


def _default_observer(event: str, payload: dict[str, Any]) -> None:
    return None


def _first_user_question(messages: list[TurnMessage], turn_index: int) -> str | None:
    """The first user message of *turn_index* — the turn's originating question,
    used to re-run retrieval on a resume (design §6). `None` if absent."""
    for message in messages:
        if message.turn_index == turn_index and message.role == "user":
            return message.content
    return None


def _runtime_tool_unavailable(tool_name: str, code: str) -> ToolResult:
    """A clean local error for an advertised-but-unwired runtime tool (§6) —
    never dispatched to the MCP under its own name. Shared by `resolveValues`
    and the three read tools."""
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
    """Validate a `RuntimeTool`'s returned `provenance` BEFORE it is persisted
    (S2). It must be `None` or a `frozenset` of `(str, str)` tuples — the exact
    shape `context/scope_filter.is_provenance_in_scope` unpacks. Anything else
    (a contract violator) is coerced to `None` fail-closed (dropped from replay)
    with a server-side warning, rather than crashing the NEXT round-trip inside
    the D44 replay filter."""
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
    # recordAssumptions (docs/decisions/ui-assumptions-contract.md): the
    # model-declared, plain-English assumptions behind the answer — a first-class
    # result field mirroring `sql` in EVERY respect (additive, nullable, `[] ->
    # None` fork, accumulated across budget windows at every return site).
    assumptions: list[str] | None = None


ANSWER_TABLE_BLUEPRINT_NOT_RUN_CODE = "ANSWER_TABLE_BLUEPRINT_NOT_RUN"


def _answer_table_blueprint_not_run(blueprint_id: str) -> ToolResult:
    """The nudge for `answerWithTable(blueprint_id=X)` where X never ran this turn.

    Without it the call SUCCEEDS and — because it carries `answer` — TERMINATES the
    turn, so the user gets prose with no table and the model never learns why. The
    designation is advisory, but silently swallowing a designation the model
    explicitly made is the wrong kind of advisory.

    A non-`ok` status is what makes this work end-to-end: it stops the terminal exit
    firing (so the turn continues and the model can fix it), and
    `scope_filter.filter_trail`'s current-turn exemption is status-gated to
    `status != "ok"`, so the entry reaches the model this same turn instead of being
    dropped as undetermined-provenance history.

    The instructional text below is NOT what the model reads. `TrailEntry` has no
    `user_message` field at all, and `context/budget.py::_render_entry` — the single
    producer of every model-facing tool message — sets it from
    `classify_denial(entry.error_code)` unconditionally. So the model sees the
    DENIAL-TABLE text, on the first rebuild and every one after; the string here only
    reaches non-model readers (logs, `/query/page`'s error body).
    That is why `ANSWER_TABLE_BLUEPRINT_NOT_RUN` is registered in
    `dispatch/denial_mapping.py`: without an entry there, `classify_denial` falls
    back to "Something went wrong processing that request." and the model is told
    nothing actionable. The two strings are kept in step deliberately.
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
# ---------------------------------------------------------------------------

# `FINALIZATION_BLOCKED_PENDING_INTENTS_CODE` is imported from
# `dispatch/denial_mapping.py` (its canonical home) and re-exported here, because
# `context/assembly.py` needs the same literal to drop the refusal entry from a
# LATER turn's replay and cannot import this module.

# How much of the model's refused draft answer is quoted back to it in the nudge.
# Generous: the point is that the model does not have to REGENERATE the answer it
# just wrote (exit #1 persists nothing and D22 discards free text around tool
# calls), so a truncated quote costs a rewrite of the tail only.
_MAX_NUDGE_DRAFT_CHARS = 2000

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
    """Raised from inside the force-block merge when the live state vanished
    between the loop's read and the store's write (a concurrent turn boundary is
    the only way). Aborts the write with nothing persisted, rather than
    resurrecting a state the model never saw."""


def _pending_intents(state: AnalysisState | None) -> tuple[TrackedIntent, ...]:
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


def _finalization_blocked(pending: Sequence[TrackedIntent]) -> ToolResult:
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


def _finalization_nudge(draft: str | None, pending: Sequence[TrackedIntent]) -> str:
    """The ephemeral `user`-role message injected in place of exit #1's missing
    error channel (05 §B.2/§B.3).

    IT CARRIES THE DRAFT BACK. Exit #2's refusal preserves the model's prose for
    free — it lives in `tool_call.arguments`, is persisted as `TrailEntry.args`,
    and is replayed by `_render_entry`. Exit #1 preserves NOTHING: the answer is
    not persisted (by design — a persisted nudge or draft would surface in
    `/session/history` as something the user said) and D22 discards free text
    around tool calls, so without this quote the model has no record it just wrote
    a final answer and must regenerate it blind.
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


def _refreshed_analysis_state(
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


def _capture_terminal_sql(
    tool_name: str, tool_result: ToolResult, *, into: dict[str, str]
) -> None:
    """Record a SUCCESSFUL blueprint's `terminal_sql` under its id, in place.

    The terminal SQL is the ONE query whose rows are that blueprint's answer
    (`blueprint/executor.py`), exposed explicitly rather than inferred as "the last
    element of `result_full["sql"]`" — rehydrated nodes are appended to that list
    FIRST on a D45 resume, so the positional assumption is not safe.

    Captured HERE, at dispatch, because `result_full` is in hand: a blueprint's
    result is persisted behind a D46 KV pointer (`result_full_ref`), so reading it
    back off the trail later would cost a store round-trip. A no-op for any
    non-`ok` / non-runBlueprint call, so it is safe to call unconditionally."""
    if tool_result.status != "ok" or tool_name != "runBlueprint":
        return
    result_full = tool_result.result_full
    if not isinstance(result_full, dict):
        return
    blueprint_id = result_full.get("blueprint_id")
    terminal_sql = result_full.get("terminal_sql")
    if isinstance(blueprint_id, str) and isinstance(terminal_sql, str) and terminal_sql.strip():
        into[blueprint_id] = terminal_sql


def _tool_trail_entry_to_canonical(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """One rendered tool-trail entry (context/budget.py `_render_entry` shape) ->

    a synthetic `[assistant-with-tool_calls, tool-result]` canonical pair —
    required because D22 discards the model's original "thinking"/free text
    around a tool call, so replay must synthesize a minimal, API-valid
    assistant/tool exchange rather than replaying the original text verbatim.
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
            content["note"] = (
                "Verified blueprint result — authoritative; do not re-derive with "
                "additional queries."
            )
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(content, default=str),
        }
    return [assistant_message, tool_message]


def _assembled_to_canonical(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`AssembledContext.messages` (the interleaved render list, Phase 1)
    -> the canonical `ModelClient.send_turn` message shape (design §1 `model/client.py`).

    The interleaved list carries four render shapes: the base `system` message; a
    `user` message (a prior-turn question, the current question, an askUser answer,
    or the retrieval cards block); an `assistant` TEXT message (a prior turn's
    answer — new in the interleave, passed straight through); and a `tool` render
    item (a trail entry / withheld sentinel) that expands to a synthetic
    `assistant(tool_calls)` + `tool(result)` PAIR (D22 discards the model's original
    free text around a tool call, so replay synthesizes a minimal API-valid pair).

    §6.2 defensive dedup: the OpenAI API requires every `tool_call_id` in a turn to
    be UNIQUE with exactly one matching `tool` response. A legacy/corrupt trail (or
    a paused-and-resumed DAG that re-appended a colliding id) would otherwise emit
    two `tool` messages with the same id → an API 400 that aborts the turn. So we
    drop a duplicate `tool_call_id` here (keeping the FIRST) — fail-closed toward a
    valid (if lossy) replay, never a turn-aborting one. This matters more for the
    `runBlueprint` brick whose resume re-enters and appends new trail entries."""
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
            canonical.extend(_tool_trail_entry_to_canonical(message))
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
        token_budget: int | None = None,
        request_token_budget: int | None = None,
        request_budget_pinned_recent_tool_pairs: int = 3,
        max_tool_calls_per_iteration: int = 8,
        clock: Callable[[], float] = time.monotonic,
        observer: ToolObserver = _default_observer,
        runtime_tools: Mapping[str, RuntimeTool] | None = None,
        blueprint_executor: Any = None,
        discovery_emulation_provider: EmulatedDiscoveryProvider | None = None,
        progress_summarizer: ProgressSummarizer | None = None,
        # Answer-table lifecycle seams (D72, hooks/answer_table.py). Defaults to an
        # EMPTY registry — dormant, every hook point a no-op, byte-identical to not
        # having them. `app.py` does not populate it; activating one is a
        # deliberate registration, never a config flag.
        answer_table_hooks: AnswerTableHooks | None = None,
    ) -> None:
        self._model_client = model_client
        self._tool_dispatcher = tool_dispatcher
        self._context_assembler = context_assembler
        self._session_store = session_store
        self._tools_provider = tools_provider
        # Emulated-discovery injection (context/discovery_emulation.py): when wired
        # (app.py, gated on `discovery_emulation_enabled`), `_run_loop` invokes this
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
        self._token_budget = token_budget
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
        self._clock = clock
        self._observer = observer
        # LLM-generated progress summaries (opt-in, `progress_summary_enabled`).
        # `None` (default) → the feature is absent and `_run_loop` is byte-identical
        # to before it existed. When wired (app.py, gated on the flag + an OpenAI
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
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
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
                # nothing: it returns from inside `resume()` BEFORE `_run_loop` is
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
                return TurnOutcome(
                    status="done",
                    assistant_text=("Stopping here — here is what I found before the budget cap."),
                    pending_question=None,
                    tool_calls_made=0,
                    # UI Slice 1: a `done` return — surface the turn's lineage from
                    # the trail (the fail-closed source of truth). The in-loop sql/
                    # table/blueprint accumulators are gone with the prior window, so
                    # they stay `None` (best-effort partial, §1 nullability table).
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
        seed_answer_sql = await self._compute_turn_answer_sql(session_id, turn_index)
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            seed_assumptions=seed_assumptions,
            seed_answer_sql=seed_answer_sql,
        )

    def _begin_model_turn(self) -> ModelClient:
        """Return a per-turn-scoped `ModelClient` handle (B3): Responses/Chat
        fallback stickiness (D71 §4.2), if the underlying client tracks any,
        must live on THIS returned handle — never on `self._model_client`'s
        own shared instance state — so that concurrent turns/sessions sharing
        one `AgentLoop`'s (and, in `app.py`, one process-wide `ModelClient`
        singleton's) fallback stickiness can never stomp each other mid-turn.
        Every `send_turn` call for the rest of this external turn (across any
        number of budget-window resumes) must go through the SAME handle
        returned here, not `self._model_client` directly.
        """
        return begin_turn_client(self._model_client)

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
    ) -> list[dict[str, Any]]:
        """*current_turn_index* (turn-scoped continuity, 2026-07-01): passed
        through to `ContextAssembler.assemble` so the CURRENT in-progress
        turn's own tool-trail entries — including denials/errors, whose
        `provenance` is always `None` (`dispatch/tool_dispatcher.py`) — are
        exempt from D44's strict replay drop and reach the model this same
        turn (design §3.4 self-correction). Cross-turn D44 is unchanged: a
        PRIOR turn's undetermined/denied entry is still always dropped. A
        denied/errored entry never carries result rows regardless
        (`result_preview` is `None`), so this exemption leaks nothing.

        *question*/*user_id*/*retrieval_memo* (Slice-1 retrieval, design §3.3):
        threaded into `assemble` so the retrieval pipeline (when configured)
        pre-injects thin cards + knowledge for this turn's question. The memo is
        turn-window-local (created fresh in `_run_loop`) so retrieval embeds at
        most ONCE per window despite the D45 per-round-trip rebuild. When no
        retrieval pipeline is wired, these are inert (assemble short-circuits).

        *withheld_call_ids* (D94 Part 2): the same turn-window-local memo pattern
        as *retrieval_memo* — a `set[str]` created fresh in `_run_loop` so the
        `loop_result_withheld_provenance` diagnostic fires at most ONCE per
        stranded `tool_call_id` despite the per-round-trip rebuild.

        *discovery_canonical* (emulated-discovery injection): the per-window
        synthetic `assistant(tool_calls=...)+tool(result)` pairs for
        `listDatabases`+`listTables`, already run through
        `_tool_trail_entry_to_canonical` (or `None`/empty). Computed ONCE in
        `_run_loop` and threaded in (never recomputed per round-trip, D45).

        It is spliced in immediately AFTER the CURRENT turn's question (the LAST
        `user` message), so the turn reads sequentially — question, then the
        discovery the model "already did" for it, then the model's own work. This
        is the whole point of emulating the CALLS rather than summarizing them: the
        pairs must sit where the model's own calls would have, which is inside the
        current turn.

        It used to splice after the leading `role=="system"` run instead, hoisting
        every emulated pair ABOVE turn-0's question on the theory that they were
        "the earliest session activity". That read as a block of tool calls before
        the user had asked anything — the sweep is re-run per budget window against
        the CURRENT turn, so it was never prior-session history in the first place,
        and prepending it broke the sequential turn layout the interleave in
        `context/assembly.py` otherwise maintains.

        Splicing after the last `user` message keeps it inside the range
        `context/budget.py::fit_request_to_budget` pins as the current turn, so the
        pairs are treated as current-turn tool pairs (droppable only at tier 2,
        under real budget pressure) rather than as prior-turn history that gets
        trimmed first. When there is no `user` message at all (a Layer-1 assemble
        with no dialogue) it falls back to the old position after the system head.
        `None`/empty (feature off / degraded) leaves the message list byte-identical.

        *finalization_nudge* (05 §B.2/§D): the ephemeral `user`-role message that
        replaces exit #1's missing error channel. It shares `discovery_canonical`'s
        splice site and never-persisted posture but NOT its lifetime — it lives
        EXACTLY ONE ROUND-TRIP (the caller clears it immediately after this call),
        because a once-per-window value would repeat the nudge forever, including
        after the intents were closed, and — being ephemeral and anchored at the
        tail — would migrate to be the newest message on every rebuild, appearing
        after tool results it predates.

        SPLICE ORDER (05 §D.1 — this loop owns it, being the later insertion):
        `ContextAssembler` inserts the `analysisState` block at
        `_last_user_index`, i.e. immediately BEFORE the current question. The nudge
        is appended at the TAIL, AFTER that — appending it first would make it the
        last `user` message and land 03's state block after the question instead of
        before it. A trailing `user` message is safe there: `_current_turn_start`
        anchors on the first `user` after the last plain-assistant answer, so it
        does not move the current-turn pin, and `fit_request_to_budget` classifies
        it as a current-turn non-tool-pair unit, which is pinned.
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
        canonical = _assembled_to_canonical(assembled.messages)
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
        return canonical

    async def _compute_turn_provenance_union(
        self, session_id: str, turn_index: int
    ) -> frozenset[tuple[str, str]] | None:
        """Union of every `TrailEntry.provenance` produced at *turn_index*
        (across every budget window of this external turn) — the tag applied
        to that turn's final assistant `TurnMessage` (B1/D44). Fail-closed:
        any undetermined (`None`) tool-result provenance makes the whole
        turn's assistant message undetermined too. A turn with no tool calls
        at all (a pure clarification/chat turn) is determined-empty
        (`frozenset()`) — always kept on replay.
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
        """Reconstruct the plain-English assumptions recorded at *turn_index* from
        the persisted `recordAssumptions` trail entries (deduped, first-occurrence
        order, via the SAME `fold_assumptions` the loop and `session_history`
        use). Used to SEED a resumed window on BOTH resume paths — the plain
        `resume()` (askUser / budget-continue) and the blueprint approval-resume —
        so assumptions the model recorded in an earlier window are not dropped and
        the resumed turn's live result matches what `project_history` reconstructs."""
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

    async def _compute_turn_answer_sql(self, session_id: str, turn_index: int) -> str | None:
        """Reconstruct the model-designated `answer_sql` for *turn_index* from the
        persisted `answerWithTable` trail entries — the `_compute_turn_assumptions`
        sibling, used to SEED a resumed window on BOTH resume paths so a designation
        the model made BEFORE an askUser / blueprint-approval pause survives it.

        LAST successful designation wins, matching `_accumulate_answer_sql`'s
        in-window rule (a later call supersedes an earlier one). Without this, a turn
        that designated its answer table and THEN paused comes back with
        `answer_sql=None` and the UI silently loses the table.

        BOTH designation forms are reconstructed. Reading only `args["sql"]` looked
        sufficient but silently dropped every blueprint designation — and that is the
        form the live model actually emits: observed in a real turn, it sent
        `sql=""` alongside `blueprint_id`, which cleans to `None`. So a
        blueprint-answered turn that paused lost its table on resume, in exactly the
        case the blueprint path exists for.

        Resolving `blueprint_id` here needs the blueprint's `terminal_sql`, which
        lives in `result_full` behind a D46 KV pointer (the in-window path reads it
        straight off the dispatch result and never pays this cost). The
        de-reference happens ONLY on the resume path, once per blueprint, and a
        missing/expired ref simply leaves that id unresolved rather than raising."""
        trail = await self._session_store.load_trail(session_id)
        turn_entries = [
            e for e in trail if e.turn_index == turn_index and e.status == "ok"
        ]
        # blueprint_id -> terminal_sql, rebuilt from this turn's successful runs.
        terminal_by_id: dict[str, str] = {}
        for entry in turn_entries:
            if entry.tool_name != "runBlueprint" or entry.result_full_ref is None:
                continue
            result_full = await self._session_store.read_full_result(
                session_id, entry.result_full_ref
            )
            if isinstance(result_full, dict):
                _capture_terminal_sql(
                    "runBlueprint",
                    ToolResult(
                        status="ok", tool_name="runBlueprint", error_code=None,
                        retryable=None, user_message=None, provenance=None,
                        result_preview=None, result_full=result_full,
                    ),
                    into=terminal_by_id,
                )

        designated: str | None = None
        for entry in turn_entries:
            if entry.tool_name != ANSWER_TABLE_TOOL_NAME:
                continue
            designated = resolve_designation(entry.args, terminal_by_id) or designated
        return designated

    def _maybe_start_summary(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Fire a FIRE-AND-FORGET progress-summary task for one tool CALL (opt-in).

        Non-blocking is load-bearing: this schedules the LLM call CONCURRENTLY and
        returns immediately — the caller dispatches the tool without ever awaiting
        the summary, so the summarizer can never add latency to the tool nor delay
        the turn result. The line arrives on the progress stream when ready
        (additive to the instant `tool_dispatch_start` template label); if it never
        arrives (slow / failed / cancelled at turn end), the template label stands.
        A SHALLOW snapshot of `arguments` (`dict(...)`) is passed so a rebinding of
        the top-level keys can't race the background read; nested mutable structures
        are shared, which is fine because no in-loop mutation of the call arguments
        exists today. No-op when the summarizer is not wired (feature off)."""
        if self._progress_summarizer is None:
            return
        task: asyncio.Task[None] = asyncio.create_task(
            self._summarize_and_emit(tool_name, dict(arguments))
        )
        self._summary_tasks.add(task)
        task.add_done_callback(self._summary_tasks.discard)

    async def _summarize_and_emit(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Await the summarizer and emit the value-rich progress line — fail-soft:
        a summarizer error/timeout yields `None` (dropped), and even the observer
        emit is guarded so a late arrival after the emitter is closed (or any other
        observer error) can never raise into this fire-and-forget task."""
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
                "tool_progress_summary", {"summary": summary, "tool_name": tool_name}
            )
        except Exception:
            _logger.debug("progress-summary emit failed for %s (ignored)", tool_name)

    def _cancel_pending_summaries(self) -> None:
        """Best-effort cancel any still-pending summary tasks at turn end — the
        turn result never blocks on them (design: the enriching line is optional).
        `discard` in the done-callback keeps the set self-cleaning; clearing here is
        belt-and-suspenders so a resumed window starts clean."""
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
        """Run one registry handler with a B4-style crash guard + a returned-
        provenance-type validation (S2), so a misbehaving runtime tool cannot
        abort the turn or persist a replay-poisoning provenance. The three read
        tools + `resolveValues` already self-guard; this is defense in depth and
        the containment seam the future `runBlueprint` brick relies on.

        *turn* is the loop's OWN `turn_index`, threaded explicitly (03 §C.1) —
        the only correct source. Passed to every runtime tool, ignored by the
        ones that do not need it."""
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
    ) -> AnalysisState | None:
        """Mark every surviving `pending` intent `blocked` with a RUNTIME reason
        code, immediately before a turn reaches a terminal outcome (05 §F).

        Four callers, three codes:

          | hard ceiling                          | `BUDGET_EXHAUSTED`      |
          | budget cap reached DURING a refused round | `BUDGET_EXHAUSTED`  |
          | block counter spent, intents pending  | `ENFORCEMENT_EXHAUSTED` |
          | budget-cap resume answered "stop"     | `USER_STOPPED`          |

        `ENFORCEMENT_EXHAUSTED` MEANS "ENFORCEMENT COULD NOT ESTABLISH A
        DISPOSITION" — **not** that the system proved the intent impossible (Lead,
        2026-08-11). Claiming proof would overstate what the runtime knows: zero
        rows is often the correct answer, some denial probes cost one metadata
        call, and a user who withdraws an ask mid-clarification lands here and does
        so legitimately under that reading.

        THIS PATH WRITES THE RUNTIME CODES DIRECTLY. It must NOT be routed through
        `validate_block_evidence`, which allowlists `MODEL_REASON_CODES` — every
        code above would be rejected by it, by design. There is no evidence to
        cite: `evidence_tool_call_id` stays `None`, which is exactly what
        distinguishes a runtime-forced block from a model-declared one in the
        ledger.

        The §A turn gate is already applied by the caller (the `state` handed in is
        the LIVE one), and again by the store, whose merge callback receives
        `live_analysis_state(...)`. No live state, or nothing pending, is a no-op
        with no write at all.

        DEGRADE-NEVER-FAIL: this runs on terminal paths that are already returning
        a result to the user, so a store failure here is logged and swallowed —
        losing the forced disposition is bad, aborting the user's answer to record
        it is worse.
        """
        pending = _pending_intents(state)
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
            self._observer(
                "loop_intent_force_blocked",
                {"intent_id": intent_id, "reason_code": reason_code},
            )
        return new_state

    async def _grant_forced_reround(
        self,
        *,
        session_id: str,
        turn_index: int,
        window_count: int,
        already_refused_this_round: bool,
    ) -> bool:
        """Whether a finalization refusal may proceed — ONE forced re-round per
        budget window OF THIS TURN, CONSUMED PER ROUND-TRIP (05 §C.1/§C.2).

        `turn_index` is part of the claim key, not context. `window_count` restarts
        at 1 on every external turn while `SessionDoc.finalization_blocks` persists
        across the whole session — see `session/models.py::finalization_block_key`
        for what a window-only key cost.

        The per-round gate is not a nicety. Exit #2's refusal happens inside the
        per-tool-call loop, which processes up to 8 calls from ONE model response:
        a model emitting `[answerWithTable, answerWithTable]` would otherwise burn
        both chances in a single round-trip, force-block on the second, and
        finalize — having been given NO re-round at all, with
        `ENFORCEMENT_EXHAUSTED` written for intents it was never asked twice about.
        So the second and later refusals in one batch return the same retryable
        error but do not advance the persisted counter.

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
        if already_refused_this_round:
            return True
        try:
            granted = await self._session_store.claim_finalization_block(
                session_id, turn_index, window_count
            )
        except Exception:
            _logger.exception(
                "failed to claim the finalization block (session=%s, turn=%d, "
                "window=%d) — treating the re-round as unavailable and finalizing",
                session_id,
                turn_index,
                window_count,
            )
            # D25: shape-only. All three keys are on
            # `observability/tracing.py::_GUARDRAIL_OBSERVER_ATTR_ALLOWLIST`, so this
            # actually reaches Phoenix rather than being a correctly-named span
            # carrying nothing (README finding 10).
            self._observer(
                "loop_finalization_block_claim_failed",
                {
                    "turn_index": turn_index,
                    "window": window_count,
                    "reason": "store_error",
                },
            )
            return False
        if granted:
            self._observer("loop_finalization_block_spent", {"window": window_count})
        return granted

    async def _pause_from_runtime_tool(
        self,
        *,
        session_id: str,
        pause: ToolPause,
        window_count: int,
        assistant_text: str | None,
        tool_calls_made: int,
        sql_executed: list[str] | None = None,
        answer_sql: str | None = None,
        blueprint_use: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        assumptions: list[str] | None = None,
    ) -> TurnOutcome:
        """Honor a runtime tool's `ToolPause` (§2.5) — write the checkpoint (with
        the additive `blueprint_*` mid-DAG state) and return `paused_ask_user`,
        the same terminal contract as `askUser`. The loop owns `budget_window_count`
        (the tool cannot know it), exactly as for the `askUser` checkpoint above.

        UI Slice 1 Fix 2 (pause-path symmetry): the four enrichment accumulators are
        threaded through best-effort so "runQuery succeeded, then runBlueprint paused
        on a slot question" surfaces the partial SQL/table on THIS pause flavor too,
        matching the direct `askUser` pause. Default `None` when no query succeeded."""
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
        )
        await self._session_store.write_pause_checkpoint(session_id, checkpoint)
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
            answer_sql=answer_sql,
            blueprint_use=blueprint_use,
            verification=verification,
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
        """Re-enter the paused blueprint at `awaiting_node` (D45, §2.5). The
        executor is stateless — everything to continue is in the checkpoint, so a
        FRESH process resumes identically (restart-durable). The outcome maps the
        same way `runBlueprint`'s first call does:

          - `Paused` (another approval / degrade) → write a new checkpoint (with
            the grown completed-nodes state) and return `paused_ask_user`;
          - `Completed`/`Failed` → persist a `runBlueprint` trail entry (so replay
            carries the result + provenance) and CONTINUE the model loop — the
            model's next round-trip narrates / does the D56 LLM review (§4.4).

        n2: the executor re-fires the (deterministic) slot/rule resolves on resume
        for settled bindings — the design accepts this deterministic re-fill (the
        probes are read-only + idempotent; §2.5 / Q8).
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
        self._observer("tool_dispatch_start", {"tool_name": "runBlueprint"})
        try:
            outcome = await self._blueprint_executor.resume(
                blueprint_id=checkpoint.blueprint_id,
                slot_bindings=slot_bindings,
                completed_nodes_json=checkpoint.completed_nodes_json,
                awaiting_node=checkpoint.awaiting_node,
                approval_answer=answer,
                credentials=credentials,
            )
            tool_result = self._blueprint_outcome_to_tool_result(outcome)
        except Exception:
            _logger.exception("runBlueprint resume raised (session=%s)", credentials.session_id)
            tool_result = _runtime_tool_internal_error("runBlueprint")
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
            )

        # UI Slice 1 Fix 1: fold this COMPLETED blueprint result into seed
        # enrichment so the resumed loop's FINAL `done` result event carries the
        # same sql_executed/blueprint_use/verification a non-paused blueprint
        # answer would (a fresh `_run_loop` window would otherwise start empty and
        # drop it). Raw slots ride the checkpoint's `slot_bindings`. A no-op on a
        # non-`ok` (failed/degraded) resume → no seed, matching a raw-loop fallback.
        seed_sql: list[str] = []
        seed_blueprint_use, seed_verification = self._accumulate_enrichment(
            "runBlueprint",
            {"slot_bindings": slot_bindings},
            tool_result,
            turn_sql=seed_sql,
            blueprint_use=None,
            verification=None,
        )
        # Seed the id -> terminal-SQL map too, so the resumed window can honor an
        # `answerWithTable(blueprint_id=…)` naming the blueprint that completed
        # BEFORE this approval pause — otherwise the designation resolves to nothing
        # and the user loses the table on exactly the verified path.
        seed_blueprint_terminal_sql: dict[str, str] = {}
        _capture_terminal_sql("runBlueprint", tool_result, into=seed_blueprint_terminal_sql)

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
        seed_answer_sql = await self._compute_turn_answer_sql(session_id, turn_index)
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            seed_sql=seed_sql,
            seed_answer_sql=seed_answer_sql,
            seed_blueprint_terminal_sql=seed_blueprint_terminal_sql,
            seed_blueprint_use=seed_blueprint_use,
            seed_verification=seed_verification,
            seed_assumptions=seed_assumptions,
        )

    def _blueprint_outcome_to_tool_result(self, outcome: Any) -> ToolResult:
        """Map a `BlueprintExecutor` `ExecOutcome` to a `ToolResult` — the SAME
        mapping `RunBlueprintTool._execute` uses (`blueprint_outcome_to_tool_result`),
        reused here for the resume path so a mid-DAG resume produces byte-identical
        results — INCLUDING the verified `authoritative` marker — to a first call.
        Sharing the one mapper is what stops the resume path from silently losing
        the marker (the exact drift this dedup fixes)."""
        from data_agent.runtime.blueprint.tool import blueprint_outcome_to_tool_result

        mapped = blueprint_outcome_to_tool_result(outcome)
        if mapped is not None:
            return mapped
        return _runtime_tool_internal_error("runBlueprint")

    @staticmethod
    def _accumulate_enrichment(
        tool_name: str,
        arguments: dict[str, Any],
        tool_result: ToolResult,
        *,
        turn_sql: list[str],
        blueprint_use: dict[str, Any] | None,
        verification: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Fold one SUCCESSFUL runQuery/runBlueprint result into the turn-window
        enrichment accumulators (UI Slice 1, contract §3.2). `turn_sql` is mutated
        in place (deduped, first-occurrence order); the other two are RETURNED
        for the caller to reassign. A no-op for any non-`ok` / non-query tool call,
        so it is safe to call unconditionally. Shared by the in-loop dispatch path
        AND the blueprint approval-resume seed path so both produce identical
        enrichment (Fix 1: a resumed verified answer keeps its badge + chip).

        No longer tracks a `primary_preview`: the turn result used to carry the LAST
        successful query's `ResultPreview` as `result_table`, a fixed ~20-row window
        the user could not page past AND a choice the RUNTIME made. The answer table
        is now the model-designated `answer_sql` (`presentTable`), which the UI runs
        itself with real paging."""
        if tool_result.status != "ok":
            return blueprint_use, verification
        if tool_name == "runQuery":
            query_sql = arguments.get("sql")
            if query_sql and query_sql not in turn_sql:
                turn_sql.append(query_sql)
            return blueprint_use, verification
        if tool_name == "runBlueprint":
            rf = tool_result.result_full or {}
            for bp_sql in rf.get("sql", []):
                if bp_sql and bp_sql not in turn_sql:
                    turn_sql.append(bp_sql)
            new_blueprint_use = {
                "blueprint_id": rf.get("blueprint_id"),
                "slots": dict(arguments.get("slot_bindings") or {}),
            }
            new_verification = verification
            if rf.get("status") == "verified":
                new_verification = {
                    "passed": True,
                    "method": "blueprint_gate",
                    # None-safe: a `verify: None` must not AttributeError.
                    "grain_checked": bool((rf.get("verify") or {}).get("grain_checked")),
                }
            return new_blueprint_use, new_verification
        return blueprint_use, verification

    def _resolve_answer_sql(
        self,
        arguments: dict[str, Any],
        *,
        blueprint_terminal_sql: dict[str, str],
        session_id: str,
        turn_index: int,
    ) -> str | None:
        """Resolve one `answerWithTable` designation to a concrete pageable query.

        Two inputs, one output. `sql=` is taken as given (it need not be a query the
        agent ran — see `composite/answer_with_table.py`). `blueprint_id=` is looked
        up in *blueprint_terminal_sql*, the turn-window map of
        `blueprint_id -> result_full["terminal_sql"]` captured when each blueprint
        actually RAN. Resolving server-side is what keeps ONE field on the wire and
        stops the UI re-executing a DAG (and re-materializing scratch) per page.

        `sql=` wins if both are given: it is the more specific instruction.

        An unresolvable `blueprint_id` (naming a blueprint that did not run
        successfully this turn) yields `None` — the answer keeps its prose and loses
        its table — after firing the dormant ON_ANSWER_TABLE_UNRESOLVED seam
        (`hooks/answer_table.py`), which may supply a replacement. Re-running the
        blueprint here to find out would decouple the paged table from the D56
        verification that gated the answer the user was shown.

        A resolved query that reads the session-scoped `scratch` database fires the
        dormant ON_ANSWER_TABLE_EPHEMERAL seam: it works now and stops working at
        the scratch TTL, so a hook may swap in a durable equivalent. With no hook
        registered (the shipped default) both seams return `None` and this behaves
        exactly as if they did not exist.
        """
        blueprint_id = clean_blueprint_id(arguments.get("blueprint_id"))
        event_base = {
            # D5: hashed, never the raw session id — a hook is never given one.
            "session_id_hash": hash_scope(frozenset({session_id})),
            "turn_index": turn_index,
        }

        resolved = resolve_designation(arguments, blueprint_terminal_sql)
        if resolved is None and blueprint_id is not None:
            _logger.warning(
                "answerWithTable designated blueprint %r, which did not run "
                "successfully this turn — no answer table (session=%s)",
                blueprint_id,
                session_id,
            )
            resolved = self._answer_table_hooks.resolve_unresolved(
                AnswerTableEvent(blueprint_id=blueprint_id, sql=None, **event_base)
            )

        if resolved is not None and references_scratch(resolved):
            replacement = self._answer_table_hooks.resolve_ephemeral(
                AnswerTableEvent(blueprint_id=blueprint_id, sql=resolved, **event_base)
            )
            if replacement is not None:
                resolved = replacement
        return resolved

    @staticmethod
    def _accumulate_answer_sql(
        tool_name: str,
        tool_result: ToolResult,
        *,
        answer_sql: str | None,
        resolved: str | None,
    ) -> str | None:
        """Fold one SUCCESSFUL `answerWithTable` call into the turn's `answer_sql`
        (mirrors `_accumulate_assumptions`: read from the call ARGUMENTS, never from
        the result). Returns the new value; a no-op returning *answer_sql* unchanged
        for any non-`ok` / non-`answerWithTable` call, so it is safe to call
        unconditionally.

        *resolved* is the output of `_resolve_answer_sql`, computed ONCE by the
        caller and passed in — resolving here as well would fire the
        `hooks/answer_table.py` seams TWICE per designation, which a registered hook
        would see as two events for one model decision.

        LAST designation wins. A second `answerWithTable` means the model changed its
        mind about which query is the answer — the later choice is the current one.
        (`recordAssumptions` accumulates instead, because assumptions are additive;
        an answer table is singular.) A designation that resolves to nothing (blank
        args, or an unresolvable blueprint id) leaves the previous one intact rather
        than clearing it, so a malformed retry cannot silently drop a good table."""
        if tool_result.status != "ok" or tool_name != ANSWER_TABLE_TOOL_NAME:
            return answer_sql
        return resolved or answer_sql

    @staticmethod
    def _accumulate_assumptions(
        tool_name: str,
        arguments: dict[str, Any],
        tool_result: ToolResult,
        *,
        turn_assumptions: list[str],
    ) -> None:
        """Fold one SUCCESSFUL `recordAssumptions` call into `turn_assumptions`
        (mirrors `_accumulate_enrichment`'s `turn_sql` discipline: mutated IN
        PLACE, deduped, first-occurrence order). Read from the call ARGUMENTS via
        the SAME `fold_assumptions` helper `session_history` uses, so the loop and
        the history read-surface agree exactly. A no-op for any non-`ok` /
        non-`recordAssumptions` call, so it is safe to call unconditionally."""
        if tool_result.status != "ok" or tool_name != "recordAssumptions":
            return
        fold_assumptions(turn_assumptions, arguments.get("assumptions"))

    async def _run_loop(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        window_count: int,
        turn_index: int,
        model_client: ModelClient,
        question: str | None = None,
        seed_sql: list[str] | None = None,
        seed_answer_sql: str | None = None,
        seed_blueprint_terminal_sql: dict[str, str] | None = None,
        seed_blueprint_use: dict[str, Any] | None = None,
        seed_verification: dict[str, Any] | None = None,
        seed_assumptions: list[str] | None = None,
    ) -> TurnOutcome:
        """Turn-window driver wrapper: guarantees a best-effort cancel of any
        still-pending fire-and-forget progress-summary tasks when the window ends —
        on a normal return, a pause, OR an exception — so they never outlive the
        turn. The turn result is produced entirely by `_run_loop_body`; this
        wrapper only adds the summary-task cleanup in a `finally`, so it is
        byte-identical to `_run_loop_body` when the summarizer is not wired (the
        task set is always empty and the cancel is a no-op).

        The full keyword-only signature is mirrored explicitly (rather than an
        opaque `**kwargs`) so a typo'd kwarg at any of the three call sites
        (`run`/`resume`/`_resume_blueprint`) is still caught at type-check time.
        """
        try:
            return await self._run_loop_body(
                session_id=session_id,
                credentials=credentials,
                window_count=window_count,
                turn_index=turn_index,
                model_client=model_client,
                question=question,
                seed_sql=seed_sql,
                seed_answer_sql=seed_answer_sql,
                seed_blueprint_use=seed_blueprint_use,
                seed_verification=seed_verification,
                seed_assumptions=seed_assumptions,
            )
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

    async def _run_loop_body(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        window_count: int,
        turn_index: int,
        model_client: ModelClient,
        question: str | None = None,
        # UI Slice 1 Fix 1: seed the turn-window enrichment accumulators from a
        # completed-before-this-window result (the blueprint approval-resume path)
        # so the FINAL `done` result event carries the same enrichment a non-paused
        # answer would. Default `None`/empty → byte-identical to a fresh window.
        seed_sql: list[str] | None = None,
        seed_answer_sql: str | None = None,
        seed_blueprint_terminal_sql: dict[str, str] | None = None,
        seed_blueprint_use: dict[str, Any] | None = None,
        seed_verification: dict[str, Any] | None = None,
        # recordAssumptions parity with `seed_sql`: seed the turn-window
        # assumptions accumulator from a before-this-window source (the blueprint
        # approval-resume path) so a resumed answer keeps assumptions recorded in
        # an earlier window. Default `None`/empty → byte-identical to a fresh window.
        seed_assumptions: list[str] | None = None,
    ) -> TurnOutcome:
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
        # Both run() and resume() re-enter `_run_loop`, so "once per window" is the
        # right cadence. It is computed HERE — next to `_tools_provider`, DELIBERATELY
        # ABOVE `new_budget_window(...)` — so its 1 + N MCP round-trips run OUTSIDE the
        # budget window's wall clock and never consume `max_wall_clock_seconds` (nor
        # re-charge it on every `continue` resume): this injected context is "never
        # budgeted". Two effects, both ephemeral (never persisted):
        #   1. `discovery_canonical` — the synthetic assistant/tool pairs, threaded
        #      into every per-round-trip rebuild below as the earliest tool history.
        #   2. `emulation_read_signatures` — merged into `seen_read_calls` below to
        #      seed the repeated-idempotent-read guard so a model RE-call of either
        #      tool is served locally (the "already served" nudge) instead of hitting
        #      the MCP.
        # Degrade-not-fail: any failure → no pairs + no seed, and the model falls
        # back to calling the two tools itself. D5: the sweep goes through the
        # dispatcher (credentials attached only at the MCP transport boundary),
        # never through the context assembler.
        discovery_canonical: list[dict[str, Any]] = []
        emulation_read_signatures: set[tuple[str, str]] = set()
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
                emulation_read_signatures = emulation.read_signatures

        guard = new_budget_window(
            max_iterations=self._max_loop_iterations,
            max_wall_clock_seconds=self._max_wall_clock_seconds,
            max_tokens=self._token_budget,
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
        # Repeated-idempotent-read guard (generalizes D94): the set of already-
        # served idempotent-read signatures for THIS turn. Turn-window-local like
        # the memos above, BUT seeded from the persisted trail so it survives both
        # the D45 per-round-trip rebuild (the set would otherwise reset every
        # `send_turn`) AND a budget-window `continue` resume (a fresh `_run_loop`
        # window starts here with an empty in-memory set). Seeding from every prior
        # `ok` idempotent-read entry of this turn is what lets the guard recognize a
        # repeat it did not itself serve in the current window.
        seen_read_calls: set[tuple[str, str]] = set()
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
        # the fast path is an `is None` test on a local (`_pending_intents`).
        analysis_state = live_analysis_state(session_doc, turn_index)
        for prior_entry in session_doc.tool_trail:
            if (
                prior_entry.turn_index == turn_index
                and prior_entry.status == "ok"
                and prior_entry.tool_name in IDEMPOTENT_READ_TOOLS
            ):
                seen_read_calls.add(
                    idempotent_read_signature(prior_entry.tool_name, prior_entry.args)
                )
        # Seed the guard with the emulated-discovery signatures swept above (outside
        # the budget window) so a model re-call of listDatabases/listTables is served
        # locally, not re-dispatched to the MCP. Empty when the feature is off/degraded.
        seen_read_calls |= emulation_read_signatures
        # UI Slice 1 (docs/decisions/ui-slice1-enriched-result-contract.md §3):
        # turn-window-local accumulators for the enriched `result` event, same
        # lifecycle as the memos above (fresh per window, not persisted).
        # Populated on each successful runQuery/runBlueprint entry below, read at
        # every `TurnOutcome(...)` return site. Seeded (Fix 1) on the blueprint
        # approval-resume path so a resumed verified answer keeps its enrichment.
        turn_sql: list[str] = list(seed_sql) if seed_sql else []
        answer_sql: str | None = seed_answer_sql
        # `blueprint_id -> result_full["terminal_sql"]` for every blueprint that ran
        # SUCCESSFULLY this turn, captured at dispatch (the result is in hand here,
        # so this needs no D46 KV de-reference). It is what lets
        # `answerWithTable(blueprint_id=…)` resolve to a concrete pageable query
        # without re-running the DAG. Seeded on the approval-resume path so a
        # blueprint that completed BEFORE the pause is still designatable after it.
        blueprint_terminal_sql: dict[str, str] = dict(seed_blueprint_terminal_sql or {})
        blueprint_use: dict[str, Any] | None = seed_blueprint_use
        verification: dict[str, Any] | None = seed_verification
        # recordAssumptions accumulator (mirrors `turn_sql`): the deduped,
        # first-occurrence list of plain-English assumptions the model recorded
        # this turn. Folded from each SUCCESSFUL recordAssumptions call's ARGUMENTS
        # (`_accumulate_assumptions`), read at every `TurnOutcome(...)` return site.
        turn_assumptions: list[str] = list(seed_assumptions) if seed_assumptions else []
        # The finalization nudge (05 §B.2/§D), ephemeral and NEVER persisted. It
        # lives EXACTLY ONE ROUND-TRIP: set when an exit-#1 finalization is
        # refused, spliced into the next rebuild, and cleared immediately after
        # that rebuild below.
        finalization_nudge: str | None = None

        while True:
            canonical_messages = await self._build_canonical_messages(
                session_id,
                credentials.column_scope,
                turn_index,
                question=question,
                user_id=None,
                retrieval_memo=retrieval_memo,
                withheld_call_ids=withheld_call_ids,
                discovery_canonical=discovery_canonical,
                finalization_nudge=finalization_nudge,
            )
            # ONE ROUND-TRIP ONLY (05 §D). `discovery_canonical` is computed once
            # per window and re-spliced into every rebuild; copying THAT lifetime
            # would repeat the nudge forever — including after the intents are
            # closed — and, because it is ephemeral and sits at the tail, would
            # migrate it to be the newest message on every rebuild, appearing
            # after tool results it predates.
            finalization_nudge = None
            # Set by a SUCCESSFUL answerWithTable in this iteration's batch; drives
            # terminal exit #2 below. Reset per iteration — a designation only ends
            # the turn it was made in.
            designated_answer_text: str | None = None
            # The window's forced re-round is consumed PER ROUND-TRIP, not per
            # refused call (05 §C.2) — so a `[answerWithTable, answerWithTable]`
            # batch is refused twice and advances the persisted counter once. Reset
            # here, beside `designated_answer_text`, for the same reason.
            finalization_refused_this_round = False
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
            # The fast path, and it must stay this cheap: `_pending_intents(None)`
            # is an `is None` test on a window-local — no store read, on the
            # overwhelming majority of turns that never declare a state at all.
            pending_at_exit = _pending_intents(analysis_state) if not result.tool_calls else ()
            if pending_at_exit:
                if await self._grant_forced_reround(
                    session_id=session_id,
                    turn_index=turn_index,
                    window_count=window_count,
                    already_refused_this_round=finalization_refused_this_round,
                ):
                    finalization_refused_this_round = True
                    refused_finalization = True
                    self._observer(
                        "loop_finalization_refused",
                        {"exit": "no_tool_calls", "pending_count": len(pending_at_exit)},
                    )
                    finalization_nudge = _finalization_nudge(
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

            if not result.tool_calls and not refused_finalization:
                # B1/D44 (2026-07-01 clarification) AND UI Slice 1: the union of
                # this turn's tool-result provenance — the tag for the final
                # assistant message (so it is scope re-filtered on replay exactly
                # like the trail itself) AND the enriched `result` event's lineage.
                # Computed ONCE here (the single fail-closed source of truth; do
                # not re-derive in-loop).
                turn_provenance = await self._compute_turn_provenance_union(session_id, turn_index)
                if result.assistant_text:
                    await self._session_store.append_message(
                        session_id,
                        TurnMessage(
                            turn_index=turn_index,
                            role="assistant",
                            content=result.assistant_text,
                            ts=_now_iso(),
                            provenance=turn_provenance,
                        ),
                    )
                self._observer("loop_turn_done", {"tool_calls_made": tool_calls_made})
                return TurnOutcome(
                    status="done",
                    assistant_text=result.assistant_text,
                    pending_question=None,
                    tool_calls_made=tool_calls_made,
                    # `[]` (no successful query this turn) -> `None`, so the UI
                    # treats "no SQL panel" and "empty SQL" identically (§1 fork 1).
                    sql_executed=turn_sql or None,
                    answer_sql=answer_sql,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    provenance=turn_provenance,
                    # `[]` (no recordAssumptions this turn) -> `None`, same fork as
                    # `sql`: the UI treats "no assumptions" and "empty" identically.
                    assumptions=turn_assumptions or None,
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
                is_idempotent_read = tool_call.name in IDEMPOTENT_READ_TOOLS
                read_sig = (
                    idempotent_read_signature(tool_call.name, tool_call.arguments)
                    if is_idempotent_read
                    else None
                )
                if is_idempotent_read and read_sig in seen_read_calls:
                    tool_calls_made += 1
                    guard_entry = TrailEntry(
                        turn_index=turn_index,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        args=dict(tool_call.arguments),
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
                    )
                    await self._session_store.append_trail_entry(session_id, guard_entry)
                    self._observer(
                        "loop_repeated_idempotent_read_guarded",
                        _repeated_read_guard_event(
                            tool_call.name, tool_call.id, tool_call.arguments
                        ),
                    )
                    if guard.exceeded:
                        break
                    continue

                # LLM-generated progress summary (opt-in, `progress_summary_enabled`):
                # fire the value-rich present-tense line CONCURRENTLY, BEFORE dispatch
                # and WITHOUT awaiting it, so it never adds latency to the tool. The
                # instant `tool_dispatch_start` template label still fires as today
                # (inside the dispatcher / the runtime tools); this line is additive,
                # arriving when ready. No-op when the feature is off.
                self._maybe_start_summary(tool_call.name, tool_call.arguments)

                # Runtime-tool registry (read-tools-design §2): a runtime tool
                # (`resolveValues` + the three read tools) is intercepted here —
                # it never reaches `dispatch` under its own name (only any inner
                # tool it issues does). It returns the SAME `ToolResult`
                # dataclass, so the trail/budget path below is unchanged and it
                # counts as exactly one `tool_calls_made`. An advertised runtime
                # tool that is not wired returns a clean local unavailable error
                # (§6), never an incoherent MCP unknown-tool denial.
                handler = self._runtime_tools.get(tool_call.name)
                if tool_call.name == UPDATE_ANALYSIS_STATE_TOOL_NAME:
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
                            tool_call.arguments,
                            credentials,
                            turn_context,
                        )
                    else:  # pragma: no cover - always wired by app.py
                        tool_result = _runtime_tool_internal_error(tool_call.name)
                elif handler is not None:
                    tool_result = await self._run_runtime_tool(
                        handler,
                        tool_call.name,
                        tool_call.arguments,
                        credentials,
                        turn_context,
                    )
                elif tool_call.name in _RUNTIME_TOOL_UNAVAILABLE_CODE:
                    tool_result = _runtime_tool_unavailable(
                        tool_call.name, _RUNTIME_TOOL_UNAVAILABLE_CODE[tool_call.name]
                    )
                else:
                    tool_result = await self._tool_dispatcher.dispatch(
                        tool_call.name, tool_call.arguments, credentials
                    )

                # REFRESH THE ENFORCEMENT LOCAL (05 §E). The state call just wrote
                # the state and returned it in full, so the local is updated from
                # the result rather than re-read from the store. A rejected call
                # (or an unreadable result) leaves the loaded value standing.
                if tool_call.name == UPDATE_ANALYSIS_STATE_TOOL_NAME:
                    refreshed = _refreshed_analysis_state(tool_result, turn_index)
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
                    and isinstance(tool_call.arguments, dict)
                    and clean_answer_text(tool_call.arguments.get("answer")) is not None
                ):
                    pending_at_answer = _pending_intents(analysis_state)
                    if pending_at_answer:
                        if await self._grant_forced_reround(
                            session_id=session_id,
                            turn_index=turn_index,
                            window_count=window_count,
                            already_refused_this_round=finalization_refused_this_round,
                        ):
                            finalization_refused_this_round = True
                            self._observer(
                                "loop_finalization_refused",
                                {
                                    "exit": "answer_with_table",
                                    "pending_count": len(pending_at_answer),
                                },
                            )
                            # A RETRYABLE error, so the turn continues and the
                            # batch drains normally: `tool_result` is no longer
                            # `ok`, so neither `_resolve_answer_sql` below nor the
                            # terminal-exit check fires for this call.
                            tool_result = _finalization_blocked(pending_at_answer)
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
                # only after `_resolve_answer_sql` has had its go — which includes
                # giving the dormant ON_ANSWER_TABLE_UNRESOLVED hook first refusal, so
                # a registered hook that supplies a replacement wins over the nudge.
                resolved_answer_sql: str | None = None
                if (
                    tool_call.name == ANSWER_TABLE_TOOL_NAME
                    and tool_result.status == "ok"
                    and isinstance(tool_call.arguments, dict)
                ):
                    # Resolved ONCE — the hooks fire here and nowhere else.
                    resolved_answer_sql = self._resolve_answer_sql(
                        tool_call.arguments,
                        blueprint_terminal_sql=blueprint_terminal_sql,
                        session_id=session_id,
                        turn_index=turn_index,
                    )
                    named_blueprint = clean_blueprint_id(
                        tool_call.arguments.get("blueprint_id")
                    )
                    if named_blueprint is not None and resolved_answer_sql is None:
                        _logger.info(
                            "answerWithTable named blueprint %r that did not run this "
                            "turn — nudging the model to run it first (session=%s)",
                            named_blueprint,
                            session_id,
                        )
                        tool_result = _answer_table_blueprint_not_run(named_blueprint)

                # §2.5 pausing-runtime-tool seam: a runtime tool may signal a
                # pause (today only `runBlueprint`, on a slot-resolution
                # `askUser`). This GENERALIZES the terminal `askUser` branch
                # above — the loop writes the checkpoint and returns
                # `paused_ask_user` exactly as for `askUser`, before persisting a
                # trail entry or counting the call (a paused tool did not
                # complete, mirroring `askUser`). A dispatched MCP tool never
                # sets `.pause`, so this is inert on the normal path.
                if tool_result.pause is not None:
                    return await self._pause_from_runtime_tool(
                        session_id=session_id,
                        pause=tool_result.pause,
                        window_count=window_count,
                        assistant_text=result.assistant_text,
                        tool_calls_made=tool_calls_made,
                        # Fix 2: surface whatever succeeded earlier in this window.
                        sql_executed=turn_sql or None,
                        answer_sql=answer_sql,
                        blueprint_use=blueprint_use,
                        verification=verification,
                        assumptions=turn_assumptions or None,
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
                    args=dict(tool_call.arguments),
                    status=tool_result.status,
                    error_code=tool_result.error_code,
                    provenance=tool_result.provenance,
                    result_preview=tool_result.result_preview,
                    result_full_ref=result_full_ref,
                    ts=_now_iso(),
                    authoritative=tool_result.authoritative,
                    denial_detail=tool_result.denial_detail,
                )
                await self._session_store.append_trail_entry(session_id, entry)

                # UI Slice 1 (§3.2): accumulate the enriched-result fields from
                # this SUCCESSFUL tool call (runQuery arg SQL + preview; runBlueprint
                # `result_full` SQL/blueprint_id/verify + preview). Shared with the
                # blueprint approval-resume seed path via `_accumulate_enrichment`.
                blueprint_use, verification = self._accumulate_enrichment(
                    tool_call.name,
                    tool_call.arguments,
                    tool_result,
                    turn_sql=turn_sql,
                    blueprint_use=blueprint_use,
                    verification=verification,
                )
                _capture_terminal_sql(
                    tool_call.name, tool_result, into=blueprint_terminal_sql
                )
                # recordAssumptions (docs/decisions/ui-assumptions-contract.md):
                # fold a SUCCESSFUL call's plain-English assumptions into the
                # turn accumulator, same discipline as the enrichment above.
                self._accumulate_assumptions(
                    tool_call.name,
                    tool_call.arguments,
                    tool_result,
                    turn_assumptions=turn_assumptions,
                )
                # answerWithTable (composite/answer_with_table.py): the
                # model-designated answer table. Same discipline again — read from
                # the call ARGUMENTS on success — except LAST designation wins
                # rather than accumulating, since a turn has one answer table.
                answer_sql = self._accumulate_answer_sql(
                    tool_call.name,
                    tool_result,
                    answer_sql=answer_sql,
                    resolved=resolved_answer_sql,
                )
                # TERMINAL: a successful `answerWithTable` carries the final prose,
                # so the turn ends on it. Recorded here and acted on AFTER the whole
                # tool batch drains, so a model that batches recordAssumptions +
                # answerWithTable still gets both folded before the turn closes.
                if (
                    tool_call.name == ANSWER_TABLE_TOOL_NAME
                    and tool_result.status == "ok"
                ):
                    designated_answer_text = (
                        clean_answer_text(tool_call.arguments.get("answer"))
                        if isinstance(tool_call.arguments, dict)
                        else None
                    )

                # Record a SUCCESSFUL idempotent read so an identical repeat later
                # this turn is caught by the guard above. Only `ok` reads are
                # "already served" — a denied/errored read is NOT recorded, so a
                # legitimate retry after a transient failure is never suppressed.
                if is_idempotent_read and tool_result.status == "ok":
                    seen_read_calls.add(read_sig)

                # S3: also check the budget INSIDE the per-tool-call loop (not
                # only once per outer iteration) so a slow batch of capped
                # calls that blows the wall-clock window mid-dispatch stops
                # cleanly instead of finishing the whole batch regardless.
                if guard.exceeded:
                    break

            # TERMINATION: pause. Honoured AFTER the state calls above have been
            # committed (03 §E.1) and BEFORE anything else in the batch is
            # dispatched — `capped_tool_calls` held only the state calls on this
            # path, so every other call still waits for the resume exactly as it
            # always did.
            if ask_user_call is not None:
                question = str(ask_user_call.arguments.get("question", ""))
                options = ask_user_call.arguments.get("options")
                checkpoint = PauseCheckpoint(
                    reason="askUser",
                    pending_question={"question": question, "options": options},
                    awaiting="user_answer",
                    consumed=False,
                    budget_window_count=window_count,
                )
                await self._session_store.write_pause_checkpoint(session_id, checkpoint)
                self._observer("loop_paused_ask_user", {"question": question})
                return TurnOutcome(
                    status="paused_ask_user",
                    assistant_text=result.assistant_text,
                    pending_question=checkpoint.pending_question,
                    tool_calls_made=tool_calls_made,
                    # Best-effort partial (§1): whatever succeeded in an earlier
                    # window of this turn; `provenance` stays `None` (the fail-closed
                    # union is reused only on the `done` return).
                    sql_executed=turn_sql or None,
                    answer_sql=answer_sql,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    assumptions=turn_assumptions or None,
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
                await self._session_store.append_message(
                    session_id,
                    TurnMessage(
                        turn_index=turn_index,
                        role="assistant",
                        content=designated_answer_text,
                        ts=_now_iso(),
                        provenance=turn_provenance,
                    ),
                )
                self._observer("loop_turn_done", {"tool_calls_made": tool_calls_made})
                return TurnOutcome(
                    status="done",
                    assistant_text=designated_answer_text,
                    pending_question=None,
                    tool_calls_made=tool_calls_made,
                    sql_executed=turn_sql or None,
                    answer_sql=answer_sql,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    provenance=turn_provenance,
                    assumptions=turn_assumptions or None,
                )

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
                    self._observer("loop_hard_ceiling_stop", {"window": window_count})
                    return TurnOutcome(
                        status="stopped_hard_ceiling",
                        assistant_text=last_assistant_text,
                        pending_question=None,
                        tool_calls_made=tool_calls_made,
                        # Best-effort partial (§1): whatever succeeded before the
                        # hard ceiling; `provenance` stays `None` (done-only).
                        sql_executed=turn_sql or None,
                        answer_sql=answer_sql,
                        blueprint_use=blueprint_use,
                        verification=verification,
                        assumptions=turn_assumptions or None,
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
                if finalization_refused_this_round:
                    analysis_state = await self._force_block_pending_intents(
                        session_id=session_id,
                        turn_index=turn_index,
                        state=analysis_state,
                        reason_code="BUDGET_EXHAUSTED",
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
                await self._session_store.write_pause_checkpoint(session_id, checkpoint)
                self._observer("loop_paused_budget_cap", {"window": window_count})
                return TurnOutcome(
                    status="paused_budget_cap",
                    assistant_text=last_assistant_text,
                    pending_question=checkpoint.pending_question,
                    tool_calls_made=tool_calls_made,
                    # Best-effort partial (§1): whatever succeeded before the cap;
                    # `provenance` stays `None` (done-only).
                    sql_executed=turn_sql or None,
                    answer_sql=answer_sql,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    assumptions=turn_assumptions or None,
                )
            # Under budget — loop back to 3a within the same window.


__all__ = [
    "AgentLoop",
    "EmulatedDiscoveryProvider",
    "RuntimeTool",
    "ToolsProvider",
    "TurnContext",
    "TurnOutcome",
]
