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

Two DIFFERENT token ceilings live on this class and must not be confused
(conflating them was a real defect, fixed 2026-08-12):
`max_token_spend` is the per-window SPEND ceiling (Σ prompt+completion over the
window's round-trips) handed to `BudgetGuard`; `request_token_budget` is the
per-request OCCUPANCY ceiling handed to `fit_request_to_budget`, which trims a
single request so it cannot overflow the model's context window. A sum answers
the first question and never the second.

Deviation from the design doc (noted for review): the design's §4.1
"BudgetGuard.check(iterations, tokens, wall_clock)" is driven off
`RuntimeSettings` values, but this class accepts the four budget scalars
(`max_loop_iterations`, `max_wall_clock_seconds`, `max_budget_windows`,
`max_token_spend`) directly as constructor arguments rather than a whole
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
    blueprint_verification,
    clean_answer_text,
    clean_blueprint_id,
    enrich_table,
    finalize_designations,
    is_answer_table_in_scope,
    resolve_designations,
    rollup_verification,
    terminal_sql_by_id,
)
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.context.assembly import (
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
)
from data_agent.runtime.context.budget import fit_request_to_budget
from data_agent.runtime.dispatch.denial_mapping import (
    ANSWER_TABLE_NO_TABLE_DESIGNATED_CODE,
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
    FinalizationBlockKind,
    PauseCheckpoint,
    ResultPreview,
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


def _read_target_attrs(
    tool_name: str, arguments: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """`(human-readable target, catalog-safe span attributes)` for one guarded
    idempotent read — the single D25 decision about what a read's IDENTITY may
    appear as on a span, shared by the guard event and the re-fetch-exemption event
    so the two can never diverge on that question.

    Only CATALOG-SAFE IDENTIFIER args are surfaced: `database`/`table` (the same
    scalars a real `tool.<name>` dispatch span already exposes) and `getBlueprint`'s
    corpus-authored `id`. Free-form args — notably `explainQuery`'s `sql` — are
    deliberately NEVER placed on a span, so an `explainQuery` read identifies as the
    empty target rather than by its query text. That is the intended trade: a
    less-specific span beats a query literal in the telemetry backend.
    """
    database = arguments.get("database")
    table = arguments.get("table")
    db = database if isinstance(database, str) and database else None
    tbl = table if isinstance(table, str) and table else None
    # `getBlueprint`'s only argument is `id` — matched on the TOOL NAME rather than
    # on the presence of an `id` key, so a future guarded tool that happens to take
    # an `id` cannot start leaking a free-form value onto the span by accident.
    blueprint_id = arguments.get("id") if tool_name == "getBlueprint" else None
    bp = blueprint_id if isinstance(blueprint_id, str) and blueprint_id else None
    if db and tbl:
        target = f"{db}.{tbl}"
    elif tbl:
        target = tbl
    elif db:
        target = db
    elif bp:
        target = bp
    else:
        target = ""
    attrs: dict[str, Any] = {}
    if db:
        attrs["database"] = db
    if tbl:
        attrs["table"] = tbl
    if bp:
        attrs["blueprint_id"] = bp
    return target, attrs


def _repeated_read_guard_event(
    tool_name: str, tool_call_id: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the self-describing `loop_repeated_idempotent_read_guarded` observer
    payload so the exported GUARDRAIL span reads unambiguously in a trace: it was a
    SECOND, duplicate read that was deduped — NOT the first fetch being blocked.

    `deduped=True` + `guard_reason` + a human-readable `note` make the span
    self-explain next to the real `tool.<name>` span of the first, dispatched call.
    `guard_reason` now says `already_served_and_still_readable`, not merely
    `already_served_this_turn`: since the trim-aware exemption landed, "already
    served" is no longer sufficient for the guard to fire, and a trace that still
    claimed it would misdescribe the decision that was actually made."""
    dedup_target, attrs = _read_target_attrs(tool_name, arguments)
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "deduped": True,
        "guard_reason": "already_served_and_still_readable",
        "dedup_target": dedup_target,
        "note": (
            f"duplicate {tool_name}({dedup_target}) — already served this turn and its "
            "result is still readable above; not re-dispatched"
        ),
    }
    payload.update(attrs)
    return payload


# How many times ONE read signature may be re-fetched past the repeated-read guard
# because its result is no longer readable, per budget window.
#
# TWO, matching `_MAX_SURPLUS_STATE_REJECTIONS`'s reasoning: the first exemption
# covers the ordinary case (the pair aged out of the pinned window once), the second
# covers a genuine second trim later in a long turn. A THIRD would mean the item is
# being trimmed as fast as it is re-added — at which point re-adding it cannot help,
# because the budget has already judged that bulk droppable twice, and paying an MCP
# round-trip to reinstate it makes the turn worse rather than better. Beyond the cap
# the guard resumes and the model gets the cheap nudge instead.
#
# It is per WINDOW, not per turn: a budget-cap `continue` resume enters a fresh
# `_run_loop_body` with a fresh allowance, exactly as the finalization re-round does.
# The worst case is therefore `max_budget_windows × 2` re-fetches of one signature
# per turn — bounded, and far from the dozens the guard was built to stop.
_MAX_TRIMMED_READ_REFETCHES = 2


def _trimmed_read_refetch_event(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    granted: int,
    reason: str,
) -> dict[str, Any]:
    """Payload for `loop_trimmed_read_refetch_allowed` / `..._capped` — the
    counterpart to `_repeated_read_guard_event`, for the decision NOT to dedup.

    `refetch_count` is the number ALREADY granted for this signature in this window,
    so `0` on the first exemption and `_MAX_TRIMMED_READ_REFETCHES` on the event that
    reports the cap biting. **The capped event is the signal worth alerting on**: it
    means a turn is thrashing — re-reading something the budget keeps dropping — and
    the real problem is upstream in pinning/summarisation/budget policy, not here.

    Same D25 posture as the guard event (`_read_target_attrs`): catalog-safe
    identifiers only, never `explainQuery`'s SQL."""
    target, attrs = _read_target_attrs(tool_name, arguments)
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "deduped": False,
        "dedup_target": target,
        "reason": reason,
        "refetch_count": granted,
        "refetch_cap": _MAX_TRIMMED_READ_REFETCHES,
        "note": (
            f"{tool_name}({target}) was already served this turn, but its result is no "
            "longer readable in the rebuilt window"
            + (
                "; re-dispatched"
                if granted < _MAX_TRIMMED_READ_REFETCHES
                else "; re-fetch cap reached, deduping instead (the turn is thrashing)"
            )
        ),
    }
    payload.update(attrs)
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
    # 08: EVERY table the model designated, in the order it designated them, each
    # with its own optional `caption`, `blueprint_use` chip and `verification`
    # badge. Additive — the three singular fields above keep their meaning and are
    # DERIVED from `answer_tables[0]` in exactly one place (`_answer_envelope`), so
    # they can never disagree with the list. `None` when the turn designated no
    # table, the same `[] -> None` fork as `sql_executed`/`assumptions`.
    answer_tables: list[dict[str, Any]] | None = None
    # recordAssumptions (docs/decisions/ui-assumptions-contract.md): the
    # model-declared, plain-English assumptions behind the answer — a first-class
    # result field mirroring `sql` in EVERY respect (additive, nullable, `[] ->
    # None` fork, accumulated across budget windows at every return site).
    assumptions: list[str] | None = None


@dataclass(frozen=True)
class _AnswerEnvelope:
    """The four answer-table fields of a `TurnOutcome`, computed together."""

    answer_sql: str | None
    blueprint_use: dict[str, Any] | None
    verification: dict[str, Any] | None
    answer_tables: list[dict[str, Any]] | None


def _answer_envelope(
    tables: Sequence[AnswerTable],
    *,
    blueprint_use: dict[str, Any] | None,
    verification: dict[str, Any] | None,
) -> _AnswerEnvelope:
    """THE ONE PLACE the answer envelope is computed (08 §E).

    Going additive rather than breaking has one real cost — two fields that can
    disagree — and deriving one from the other is what pays it. `answer_sql` and
    `blueprint_use` are projections of `answer_tables[0]`; they are never
    accumulated independently.

    THE PRIMARY IS THE FIRST ITEM, NOT THE LAST. Within one call the first item is
    the model's lead table. (Between calls, last-wins still applies to the whole
    SET — a second `answerWithTable` replaces the list rather than appending to it.)

    `verification` is the conservative AND roll-up over the designated tables
    (`rollup_verification`), which is what stops a verified blueprint for part 1
    badging a hand-written grid for part 2.

    *blueprint_use* / *verification* are the turn-level accumulators and are used
    ONLY when the turn designated NO table at all. That case has no grid to
    over-claim on: the fields then mean what they have always meant — this turn's
    prose answer came from a verified blueprint — which is the enrichment the
    approval-resume seed exists to carry across a pause. As soon as there IS a
    designated table the derived values win outright, including when they are
    `None`, which is the badge-loss this change deliberately lands.
    """
    if not tables:
        return _AnswerEnvelope(
            answer_sql=None,
            blueprint_use=blueprint_use,
            verification=verification,
            answer_tables=None,
        )
    primary = tables[0]
    return _AnswerEnvelope(
        answer_sql=primary.sql,
        blueprint_use=dict(primary.blueprint_use) if primary.blueprint_use else None,
        verification=rollup_verification(tables),
        answer_tables=[table.to_doc() for table in tables],
    )


@dataclass(frozen=True)
class _CanonicalRequest:
    """One round-trip's canonical message list, plus WHICH tool results the model
    can actually READ in it.

    The second field exists because "is this result still in context?" cannot be
    answered from `messages` alone, and the repeated-idempotent-read guard now
    depends on the answer (see `_run_loop_body`'s trim-aware re-fetch exemption).
    Two different things can put a `tool` message with a given `tool_call_id` into
    the list:

      - a REAL rendered result (`context/budget.py::_render_entry` → a JSON payload
        with `result_preview`), which the model can read; and
      - a SENTINEL — D94's "result withheld: provenance could not be determined" or
        the repeated-read "you already have this" nudge — which is data-free by
        construction (`context/assembly.py::_build_withheld_sentinel_message`).

    A membership test over `tool_call_id`s alone cannot tell them apart, and the
    difference is the whole point: a stranded `getTableSchema` renders a sentinel
    under its own id, so treating that id as "visible" would tell the guard the
    schema is readable while the model is looking at *"result withheld … Do not
    retry"* — and the schema could never be recovered. So sentinel ids are excluded
    HERE, at the one place that still knows which render item was which.
    """

    messages: list[dict[str, Any]]
    readable_tool_call_ids: frozenset[str]


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


ANSWER_TABLE_EMPTY_DESIGNATION_EVENT = "loop_answer_table_empty_designation"


def _answer_table_no_table_designated() -> ToolResult:
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


BLUEPRINT_DEFINITION_NOT_READ_CODE = "BLUEPRINT_DEFINITION_NOT_READ"


def _blueprint_definition_not_read(blueprint_id: str) -> ToolResult:
    """The refusal for `runBlueprint(id=X)` where X was never expanded with a
    successful `getBlueprint` earlier in this turn (the getBlueprint-before-run
    rule, recorded in `docs/decisions/release-1/02-blueprint-card-enrichment.md`).

    WHY THE GATE EXISTS. A blueprint card carries `intent`, `slots`, `resolves` and
    `result_grain` — and NO SQL. So the model has been choosing and running
    blueprints on the strength of an AUTHORED PROSE `intent` string; when that
    string misdescribes the query underneath it, the model runs the wrong analysis
    and reports it confidently. The D56 grain gate does not catch this: it verifies
    the result SHAPE matches the declared `result_grain`, never that the blueprint
    answers the question that was asked. Reading the definition is the only step
    that can, and it is the concrete practice the prompt's "Success is not proof of
    correctness" line implies for the blueprint route.

    RETRYABLE, and the fix is one call away — so the turn continues, the model
    expands the blueprint on the next round-trip and runs it on the one after. It
    may batch: `getBlueprint` for several blueprints in one response, `runBlueprint`
    for all of them in the next, so an N-deliverable request costs 2 round-trips,
    not 2N.

    `denial_detail` NAMES THE BLUEPRINT and the exact fix. It has to:
    `context/budget.py::_render_entry` builds the model-facing text as
    `entry.denial_detail or classify_denial(entry.error_code).user_message` and
    never from `ToolResult.user_message` (`TrailEntry` has no such field), and the
    denial table sees only the code, so its text can say "that blueprint" but never
    which one. The two strings are kept in step deliberately —
    `BLUEPRINT_DEFINITION_NOT_READ` is registered in `dispatch/denial_mapping.py`
    for the case where the detail is ever absent. D25: `blueprint_id` is
    corpus-authored, never user content, so naming it is safe.

    `provenance=frozenset()` (determined-empty), matching
    `_answer_table_blueprint_not_run` and `_finalization_blocked`: this refusal
    reads no warehouse data, and `_compute_turn_provenance_union` is fail-closed, so
    a `None` here would collapse the whole turn's union and drop the user's own
    answer from every later turn's replay. It is deliberately NOT added to
    `context/assembly.py::_STALE_CROSS_TURN_ERROR_CODES`: the only model-authored
    text on the entry is the `slot_bindings` in its `args`, and a SUCCESSFUL
    `runBlueprint` entry already replays exactly those cross-turn, so the refusal
    exposes nothing its successful twin does not.
    """
    detail = (
        f"You have not read blueprint '{blueprint_id}' in this turn, so you do not "
        "know what it actually measures — a card's intent line is authored prose, "
        f"the SQL is the analysis. Call getBlueprint('{blueprint_id}') first, then "
        "run it, and check what it does answers what was asked before you do."
    )
    return ToolResult(
        status="error",
        tool_name="runBlueprint",
        error_code=BLUEPRINT_DEFINITION_NOT_READ_CODE,
        retryable=True,
        user_message=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        denial_detail=detail,
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
    """
    return (
        tool_name in _DATA_ANSWER_TOOLS
        and status == "ok"
        and preview is not None
        and preview.row_count > 1
    )


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


def _answer_shape_nudge(draft: str | None, multi_row_calls: int) -> str:
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

    IT CARRIES THE DRAFT BACK for the same reason `_finalization_nudge` does —
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
    tool_name: str,
    tool_result: ToolResult,
    *,
    into: dict[str, BlueprintRun],
    arguments: Mapping[str, Any] | None = None,
) -> None:
    """Record a SUCCESSFUL blueprint's `terminal_sql`, its D56 verification and its
    slot bindings under its id, in place.

    The terminal SQL is the ONE query whose rows are that blueprint's answer
    (`blueprint/executor.py`), exposed explicitly rather than inferred as "the last
    element of `result_full["sql"]`" — rehydrated nodes are appended to that list
    FIRST on a D45 resume, so the positional assumption is not safe.

    Captured HERE, at dispatch, because `result_full` is in hand: a blueprint's
    result is persisted behind a D46 KV pointer (`result_full_ref`), so reading it
    back off the trail later would cost a store round-trip. A no-op for any
    non-`ok` / non-runBlueprint call, so it is safe to call unconditionally.

    ALL THREE VALUES COME FROM THIS ONE SITE (08 §C.3), which is the whole reason
    the map holds a `BlueprintRun` rather than a bare SQL string. A separate
    `blueprint_id -> verification` map filled somewhere else would let a table's
    query and its green badge be paired from two DIFFERENT runs of the same
    blueprint; read out of one `result_full` at one moment, they cannot be."""
    if tool_result.status != "ok" or tool_name != "runBlueprint":
        return
    captured = blueprint_run_from_result(
        tool_result.result_full, slots=(arguments or {}).get("slot_bindings") or {}
    )
    if captured is not None:
        blueprint_id, run = captured
        into[blueprint_id] = run


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
        max_token_spend: int | None = None,
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
        # The WHOLE designated set, not just the primary (08 §M): a three-part
        # answer that paused must come back with three tables. The blueprint-run map
        # rides along so a blueprint that ran BEFORE the pause stays designatable.
        seed_answer_tables, seed_blueprint_runs = await self._compute_turn_answer_tables(
            session_id, turn_index
        )
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            seed_assumptions=seed_assumptions,
            seed_answer_tables=seed_answer_tables,
            seed_blueprint_runs=seed_blueprint_runs,
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
    ) -> _CanonicalRequest:
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

    async def _compute_turn_answer_tables(
        self, session_id: str, turn_index: int
    ) -> tuple[list[AnswerTable], dict[str, BlueprintRun]]:
        """Reconstruct the model-designated answer TABLES for *turn_index* from the
        persisted `answerWithTable` trail entries — the `_compute_turn_assumptions`
        sibling, used to SEED a resumed window on BOTH resume paths so a designation
        the model made BEFORE an askUser / blueprint-approval pause survives it.

        THE WHOLE LIST, not the first element (08 §M): a three-part answer that
        paused must come back with three tables, or the resume silently degrades
        the exact turns multi-table exists for. The blueprint-run map is returned
        alongside it because the resumed window needs it too — a blueprint that ran
        BEFORE the pause must stay designatable after it.

        LAST successful designation wins, matching `_accumulate_answer_tables`'s
        in-window rule (a later call supersedes an earlier one — the model changed
        its mind about which query is the answer). Without this, a turn
        that designated its answer table and THEN paused comes back with
        `answer_sql=None` and the UI silently loses the table.

        BOTH designation forms are reconstructed. Reading only `args["sql"]` looked
        sufficient but silently dropped every blueprint designation — and that is the
        form the live model actually emits: observed in a real turn, it sent
        `sql=""` alongside `blueprint_id`, which cleans to `None`. So a
        blueprint-answered turn that paused lost its table on resume, in exactly the
        case the blueprint path exists for.

        AND BOTH ARGUMENT SHAPES, which is a second thing. Persisted entries written
        before 08 §O carry `{"sql": …}` / `{"blueprint_id": …}` at the TOP LEVEL and
        no `tables` key at all. `resolve_designations` folds that shape in, so this
        seed keeps working on any session document ever written — there is no
        migration, and a resume that could not read an old entry would silently drop
        the user's own answer table rather than fail.

        Resolving `blueprint_id` here needs the blueprint's `terminal_sql`, which
        lives in `result_full` behind a D46 KV pointer (the in-window path reads it
        straight off the dispatch result and never pays this cost). The
        de-reference happens ONLY on the resume path, once per blueprint, and a
        missing/expired ref simply leaves that id unresolved rather than raising."""
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
                _capture_terminal_sql(
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

    async def _compute_turn_answer_sql(self, session_id: str, turn_index: int) -> str | None:
        """`_compute_turn_answer_tables`'s N=1 projection — the primary table's SQL.

        Kept as its own name because that is what it means; it is deliberately the
        SAME projection `_answer_envelope` applies (`answer_tables[0].sql`) rather
        than a second reconstruction, so the seed and the envelope cannot disagree.
        """
        tables, _runs = await self._compute_turn_answer_tables(session_id, turn_index)
        return tables[0].sql if tables else None

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
        budget_cap_reached: bool = False,
    ) -> AnalysisState | None:
        """Mark every surviving `pending` intent `blocked` with a RUNTIME reason
        code, immediately before a turn reaches a terminal outcome (05 §F).

        Four callers, three codes:

          | hard ceiling                          | `BUDGET_EXHAUSTED`      |
          | budget cap reached DURING a refused round | `ENFORCEMENT_EXHAUSTED` |
          | block counter spent, intents pending  | `ENFORCEMENT_EXHAUSTED` |
          | budget-cap resume answered "stop"     | `USER_STOPPED`          |

        `ENFORCEMENT_EXHAUSTED` MEANS "ENFORCEMENT COULD NOT ESTABLISH A
        DISPOSITION" — **not** that the system proved the intent impossible (Lead,
        2026-08-11). Claiming proof would overstate what the runtime knows: zero
        rows is often the correct answer, some denial probes cost one metadata
        call, and a user who withdraws an ask mid-clarification lands here and does
        so legitimately under that reading.

        `budget_cap_reached` is TELEMETRY ONLY and is set by the refused-round
        caller alone. That path reaches the cap, so the capacity fact is real and an
        operator watching budget pressure must still see it — but it is NOT the
        cause of the disposition, so it does not go on the intent record. Live
        evidence (session `s412e8424614e465bbd26d7a2a1400ebe`, trace
        `647416592aff2225d1903ae7c82b8396`): the answer was computed at 25s and the
        turn capped at 61.6s on the WALL CLOCK, with tokens moving +385 across the
        final three rounds — 36 seconds spent on two rejected `updateAnalysisState`
        calls and one refused `answerWithTable`. More budget would have changed
        nothing, and `BUDGET_EXHAUSTED` sent whoever read 07 §E.2's buckets to raise
        a ceiling that was not the problem. Emitted as a bare `True` on
        `loop_intent_force_blocked` and OMITTED otherwise, so the other three
        callers' event payloads are unchanged.

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
            force_blocked: dict[str, Any] = {
                "intent_id": intent_id,
                "reason_code": reason_code,
            }
            if budget_cap_reached:
                force_blocked["budget_cap_reached"] = True
            self._observer("loop_intent_force_blocked", force_blocked)
        return new_state

    async def _grant_forced_reround(
        self,
        *,
        session_id: str,
        turn_index: int,
        window_count: int,
        kind: FinalizationBlockKind,
        already_refused_this_round: bool,
    ) -> bool:
        """Whether a finalization refusal may proceed — ONE forced re-round per
        budget window OF THIS TURN PER KIND, CONSUMED PER ROUND-TRIP (05 §C.1/§C.2,
        §J.3).

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
        round-trip; this function only knows which allowance is being asked for.

        `turn_index` is part of the claim key, not context; so is `kind`. `window_count` restarts
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

        `already_refused_this_round` STAYS ONE FLAG ACROSS BOTH KINDS, and does not
        need to be per-kind: the two kinds cannot both refuse in one round-trip.
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
        if already_refused_this_round:
            return True
        try:
            granted = await self._session_store.claim_finalization_block(
                session_id, turn_index, window_count, kind
            )
        except Exception:
            _logger.exception(
                "failed to claim the %s finalization block (session=%s, turn=%d, "
                "window=%d) — treating the re-round as unavailable and finalizing",
                kind,
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
        envelope: _AnswerEnvelope | None = None,
        assumptions: list[str] | None = None,
        serves_intent: str | None = None,
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
            # Carry the intent tag ACROSS the pause. A pausing tool writes no trail
            # entry, so the tag the model put on the `runBlueprint` call would
            # otherwise die here and the resumed entry — written under a fresh
            # `tool_call_id` — would be untagged, leaving the intent it was run for
            # closable only by citing an id the model never chose.
            serves_intent=serves_intent,
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
                # A SECOND pause in the same blueprint run (approval after slots):
                # carry the tag forward from the checkpoint being consumed, or the
                # chain loses it at the second link.
                serves_intent=checkpoint.serves_intent,
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
        # Seed the blueprint-run map too, so the resumed window can honor an
        # `answerWithTable(blueprint_id=…)` naming the blueprint that completed
        # BEFORE this approval pause — otherwise the designation resolves to nothing
        # and the user loses the table on exactly the verified path.
        seed_blueprint_runs: dict[str, BlueprintRun] = {}
        _capture_terminal_sql(
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
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            seed_sql=seed_sql,
            seed_answer_tables=seed_answer_tables,
            seed_blueprint_runs=seed_blueprint_runs,
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
            # The SAME constructor per-table designation uses, so the turn-level
            # badge and a table's badge can never describe one run differently.
            gate = blueprint_verification(rf)
            new_verification = gate if gate is not None else verification
            return new_blueprint_use, new_verification
        return blueprint_use, verification

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

        THE THIRD VALUE IS NOT DERIVABLE FROM THE FIRST TWO, which is why it is
        returned rather than inferred. An empty `tables` list has two completely
        different causes and they need opposite handling: the model NAMED NOTHING
        (`carried_designation=False` — a defect, and the caller nudges), or it named
        something the runtime then dropped for a reason the model cannot act on — a
        table proven out of the caller's column scope, a duplicate, an over-cap
        entry. Nudging the second would tell the model to fix a payload that was
        already correct.

        `resolve_answer_tables` of 08 §B.4, in the place `_resolve_answer_sql` sat.
        The order is fixed and each step is there for a measured reason:

          1. Choose the source list — `tables` when it carries a designation, else
             the LEGACY top-level `sql`/`blueprint_id` pair folded in as one entry
             (`resolve_designations`). Since 08 §O `tables` is the only shape the
             schema declares, so step 1 normally has nothing to choose; the fold is
             there for a model working from a stale context, and for the replay
             paths that share this resolver and read pre-§O trail entries forever.
          2. Resolve each item through the EXISTING `resolve_designation`. There is
             no second resolution path: an element of `tables` is exactly the
             mapping that function already reads, which is why multi-table costs no
             new resolver and cannot drift from the single-table one.
          3. An item naming a blueprint that did not run this turn REFUSES THE
             WHOLE CALL (the caller turns the returned id into the existing
             retryable `_answer_table_blueprint_not_run` nudge) — after the dormant
             ON_ANSWER_TABLE_UNRESOLVED seam has had first refusal, exactly as at
             N=1. Dropping it instead would silently lose a deliverable's table,
             which is the failure this whole change exists to fix.
          4. Dedupe on resolved SQL, then cap at `MAX_ANSWER_TABLES`
             (`finalize_designations`).
          5. Per-table provenance (08 §D.2) — an additive, positionally parallel
             read-path check, NOT this entry's provenance.

        THE HOOKS FIRE PER TABLE, NOT PER CALL. `AnswerTableEvent` already carries a
        single `blueprint_id`/`sql` pair, so one event per designated table is the
        natural reading and needs no field change. Both seams stay dormant.

        A hook-substituted query LOSES ITS VERIFICATION AND ITS CHIP: the D56 gate
        verified a query that is no longer the one being paged. Inert today (both
        seams are empty), stated in code so a future hook cannot silently inherit a
        badge.
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
                    "verified_table_count": sum(
                        1 for t in tables if t.verification is not None
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
        """Emit `loop_answer_table_intent_uncovered` when a COMPLETED intent's
        result is not among the designated tables (08 §B.1).

        DERIVATION IS A CHECK HERE, NEVER A SOURCE, and that distinction is the
        whole of §B.1. The tables are LISTED by the model because the evidence call
        is the wrong query: a designated `sql` is deliberately not required to be
        one the agent ran, *because the executed query usually carries a LIMIT the
        agent chose for its own reading and paging needs the un-capped shape*.
        Deriving the tables from the evidence would page that capped query and
        silently truncate every grid — a new silent failure, introduced to avoid a
        payload field. (`getTableSchema` evidence has no pageable SQL at all, and a
        `blocked` intent's evidence is a denial; both would need hand-enumerated
        exclusions, which is the defect class this release keeps finding.)

        NOT A REFUSAL. A scalar part of a multi-part answer correctly belongs in the
        prose, so this counts a signal, not an error. Best-effort by construction:
        an intent whose evidence call ran in an EARLIER window contributes nothing,
        because `result_sql_by_call_id` is window-local.
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

    @staticmethod
    def _accumulate_answer_tables(
        tool_name: str,
        tool_result: ToolResult,
        *,
        answer_tables: list[AnswerTable],
        resolved: Sequence[AnswerTable],
    ) -> list[AnswerTable]:
        """Fold one SUCCESSFUL `answerWithTable` call into the turn's answer tables
        (mirrors `_accumulate_assumptions`: read from the call ARGUMENTS, never from
        the result). Returns the new list; a no-op returning *answer_tables*
        unchanged for any non-`ok` / non-`answerWithTable` call, so it is safe to
        call unconditionally.

        *resolved* is the output of `_resolve_answer_tables`, computed ONCE by the
        caller and passed in — resolving here as well would fire the
        `hooks/answer_table.py` seams TWICE per designation, which a registered hook
        would see as two events for one model decision.

        LAST designation wins, and it wins over the WHOLE SET. A second
        `answerWithTable` means the model changed its mind about which query is the
        answer — the later choice is the current one; that recorded rationale is
        exactly as true of a set as of a string, and appending instead would make
        "changed its mind" unexpressible. (`recordAssumptions` accumulates because
        assumptions are additive; an answer is one answer.) A designation that
        resolves to nothing (blank args, or a refused call) leaves the previous set
        intact rather than clearing it, so a malformed retry cannot silently drop a
        good table."""
        if tool_result.status != "ok" or tool_name != ANSWER_TABLE_TOOL_NAME:
            return answer_tables
        return list(resolved) if resolved else answer_tables

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
        seed_answer_tables: Sequence[AnswerTable] | None = None,
        seed_blueprint_runs: Mapping[str, BlueprintRun] | None = None,
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

        THE MIRROR HAD A HOLE. `seed_blueprint_terminal_sql` (this parameter's
        predecessor) was accepted here and then simply NOT forwarded below, so the
        blueprint-approval resume's carefully-built map was discarded on every
        resume and a blueprint that completed before the pause was never
        designatable after it — silently, since the explicit mirror only catches a
        typo at a CALL site, never an omission at this one. Forwarded now; the
        regression test lives with the resume tests.
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
                seed_answer_tables=seed_answer_tables,
                seed_blueprint_runs=seed_blueprint_runs,
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
        seed_answer_tables: Sequence[AnswerTable] | None = None,
        seed_blueprint_runs: Mapping[str, BlueprintRun] | None = None,
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
        # `signature -> tool_call_id` for the emulated pairs, merged into
        # `served_read_call_ids` below (see the loop that fills it for why).
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

        guard = new_budget_window(
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
        # Repeated-idempotent-read guard (generalizes D94): the set of already-
        # served idempotent-read signatures for THIS turn. Turn-window-local like
        # the memos above, BUT seeded from the persisted trail so it survives both
        # the D45 per-round-trip rebuild (the set would otherwise reset every
        # `send_turn`) AND a budget-window `continue` resume (a fresh `_run_loop`
        # window starts here with an empty in-memory set). Seeding from every prior
        # `ok` idempotent-read entry of this turn is what lets the guard recognize a
        # repeat it did not itself serve in the current window.
        seen_read_calls: set[tuple[str, str]] = set()
        # The blueprint-definition gate: every blueprint id this turn has already
        # EXPANDED with a successful `getBlueprint`. `runBlueprint` for an id that is
        # NOT in here is refused before the executor runs
        # (`_blueprint_definition_not_read`).
        #
        # Seeded from the persisted trail for the same two reasons `seen_read_calls`
        # is: the D45 per-round-trip rebuild would otherwise reset it on every
        # `send_turn`, and a budget-window `continue` resume starts a fresh
        # `_run_loop` window with an empty in-memory set — a model that expanded the
        # blueprint before the cap would then be refused for work it had done.
        #
        # TURN-SCOPED, like every other memory in this release. A `getBlueprint` from
        # an EARLIER turn does not satisfy the gate: context is rebuilt per turn and
        # trimmed by `fit_request_to_budget`, so a definition fetched in turn 1 may
        # have been trimmed out by turn 5, and the rule is about what the model
        # can read RIGHT NOW. The cost is real and accepted — a follow-up turn that
        # re-runs the same blueprint with a different slot value ("now just
        # Engineering") pays one extra `getBlueprint` per turn.
        blueprint_definitions_read: set[str] = set()
        # `read signature -> the tool_call_id of the entry that SERVED it` (latest
        # wins), for every guarded idempotent read — the pointer the trim-aware
        # re-fetch exemption below tests for readability. Seeded from the same trail
        # walk as `seen_read_calls`, so the two can never disagree about what was
        # served. A guard-marker entry is never recorded as a pointer: it carries no
        # data, so it is not where the result lives.
        served_read_call_ids: dict[tuple[str, str], str] = {}
        # How many trim-aware re-fetch exemptions each signature has been GRANTED in
        # this budget window (the oscillation cap, `_MAX_TRIMMED_READ_REFETCHES`).
        read_refetch_exemptions: dict[tuple[str, str], int] = {}
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
        # --- ANSWER-SHAPE GATE state (05 §J) --------------------------------
        #
        # How many SUCCESSFUL multi-row `runQuery`/`runBlueprint` calls this TURN has
        # made, and whether any `answerWithTable` has succeeded in it. Both are
        # TURN-scoped facts held in window-locals, so both are seeded from the
        # persisted trail for the reason `seen_read_calls` is: a budget-cap
        # `continue`, an `askUser` resume and a mid-DAG blueprint resume each start a
        # fresh `_run_loop_body` with empty in-memory sets, and a gate that forgot the
        # rows the model already has would go silent on exactly the long turns that
        # produce several tables.
        #
        # THE TRAIL WALK IS ALREADY TURN-FILTERED (`prior_entry.turn_index !=
        # turn_index` skips above), which is also the cross-turn replay protection: a
        # multi-row query from turn 3 cannot make turn 4's prose answer a defect, and
        # the `claim_finalization_block` key is `(turn_index, window)` too, so a stale
        # refusal cannot be replayed onto a later turn.
        multi_row_answer_calls = 0
        # `seed_answer_tables` covers the blueprint approval-resume path, whose
        # designation was made before the pause; the trail walk covers everything else.
        answer_table_succeeded = bool(seed_answer_tables)
        for prior_entry in session_doc.tool_trail:
            if prior_entry.turn_index != turn_index or prior_entry.status != "ok":
                continue
            if prior_entry.tool_name in IDEMPOTENT_READ_TOOLS:
                prior_sig = idempotent_read_signature(
                    prior_entry.tool_name, prior_entry.args
                )
                seen_read_calls.add(prior_sig)
                # A guard-marker entry is data-free — the READ it deduped is where
                # the result lives, so the marker must not become the pointer the
                # readability test follows.
                if prior_entry.error_code != IDEMPOTENT_READ_ALREADY_SERVED_CODE:
                    served_read_call_ids[prior_sig] = prior_entry.tool_call_id
            # NOT an `elif`: `getBlueprint` is BOTH a guarded idempotent read and the
            # thing the blueprint-definition gate is keyed on, so it seeds both.
            if prior_entry.tool_name == "getBlueprint":
                # `status == "ok"` is the whole predicate — no `found` check. The
                # result body sits behind a D46 KV pointer, so reading it here would
                # cost a store round-trip per entry, and a `{found: false}` expansion
                # cannot make a `runBlueprint` succeed anyway (the executor re-fetches
                # the definition and fails on its own merits). The gate's job is
                # "did you look", not "did you find".
                expanded_id = clean_blueprint_id(prior_entry.args.get("id"))
                if expanded_id is not None:
                    blueprint_definitions_read.add(expanded_id)
            # ANSWER-SHAPE GATE (05 §J), seeded from the same walk. `status == "ok"`
            # is guaranteed by the skip above, and is passed explicitly anyway so the
            # predicate reads the same at both of its call sites.
            if _is_multi_row_answer_call(
                prior_entry.tool_name, prior_entry.status, prior_entry.result_preview
            ):
                multi_row_answer_calls += 1
            # NAME + STATUS, DELIBERATELY ASYMMETRIC with the live flag site, which
            # since 08 §O requires a designation to have actually resolved.
            #
            # This walk reads PERSISTED entries and would have to re-resolve `args`
            # to know whether one designated anything — a second reading of the
            # designation in a third place, which is the divergence
            # `resolve_designation` was extracted to prevent, and it would need this
            # window's `blueprint_runs` (a D46 KV de-reference per blueprint) to
            # answer correctly for the blueprint form. The cheap wrong answer would
            # be to treat an unresolvable id as "no table" and re-arm the gate on a
            # turn that HAD one.
            #
            # The asymmetry is safe in the direction that matters. This is the
            # FALSE-NEGATIVE side: it can only leave the gate disarmed on a turn
            # whose `answerWithTable` succeeded in an earlier window, and a
            # successful entry that designated nothing is now itself refused at the
            # live site, so it never becomes a persisted `ok` entry in the first
            # place. Pre-§O entries all carried a designation in practice. Erring the
            # other way — re-arming — would refuse turns that already showed their
            # table, which is the false positive 05 §J is most exposed to.
            if prior_entry.tool_name == ANSWER_TABLE_TOOL_NAME:
                answer_table_succeeded = True
        # Seed the guard with the emulated-discovery signatures swept above (outside
        # the budget window) so a model re-call of listDatabases/listTables is served
        # locally, not re-dispatched to the MCP. Empty when the feature is off/degraded.
        #
        seen_read_calls |= emulation_read_signatures
        # ...and point those signatures at the synthetic entries that serve them, or
        # the trim-aware exemption below would find no readable source and re-dispatch
        # to the MCP the very calls the sweep exists to avoid. The emulated pairs are
        # pinned by `fit_request_to_budget` (invariant 7), so they stay readable and
        # the guard keeps deduping a model re-call exactly as it did before.
        served_read_call_ids.update(emulated_served_call_ids)
        # UI Slice 1 (docs/decisions/ui-slice1-enriched-result-contract.md §3):
        # turn-window-local accumulators for the enriched `result` event, same
        # lifecycle as the memos above (fresh per window, not persisted).
        # Populated on each successful runQuery/runBlueprint entry below, read at
        # every `TurnOutcome(...)` return site. Seeded (Fix 1) on the blueprint
        # approval-resume path so a resumed verified answer keeps its enrichment.
        turn_sql: list[str] = list(seed_sql) if seed_sql else []
        # EVERY table the model has designated so far this turn (08). Last
        # `answerWithTable` wins over the WHOLE set, never appends.
        answer_tables: list[AnswerTable] = list(seed_answer_tables or ())
        # `blueprint_id -> BlueprintRun` (terminal SQL + D56 verification + slots)
        # for every blueprint that ran SUCCESSFULLY this turn, captured at dispatch
        # (the result is in hand here, so this needs no D46 KV de-reference). It is
        # what lets `answerWithTable(blueprint_id=…)` resolve to a concrete pageable
        # query — with its own badge — without re-running the DAG. Seeded on the
        # approval-resume path so a blueprint that completed BEFORE the pause is
        # still designatable after it.
        blueprint_runs: dict[str, BlueprintRun] = dict(seed_blueprint_runs or {})
        blueprint_use: dict[str, Any] | None = seed_blueprint_use
        verification: dict[str, Any] | None = seed_verification
        # `tool_call_id -> the query whose rows that call produced`, for the
        # intent-coverage CHECK below (08 §B.1). Window-local and best-effort: it is
        # a telemetry signal that an intent's result went untabled, never a refusal,
        # so an evidence call from an earlier window simply does not contribute.
        result_sql_by_call_id: dict[str, str] = {}
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
            )
            canonical_messages = request.messages
            # ONE ROUND-TRIP ONLY (05 §D). `discovery_canonical` is computed once
            # per window and re-spliced into every rebuild; copying THAT lifetime
            # would repeat the nudge forever — including after the intents are
            # closed — and, because it is ephemeral and sits at the tail, would
            # migrate it to be the newest message on every rebuild, appearing
            # after tool results it predates.
            finalization_nudge = None
            # Every tool result the model can actually READ this round-trip — after
            # `fit_request_to_budget` has had its say, and with data-free sentinels
            # excluded (see `_CanonicalRequest`). The trim-aware re-fetch exemption
            # below asks this set whether an already-served read is still legible.
            readable_tool_call_ids = request.readable_tool_call_ids
            # Set by a SUCCESSFUL answerWithTable in this iteration's batch; drives
            # terminal exit #2 below. Reset per iteration — a designation only ends
            # the turn it was made in.
            designated_answer_text: str | None = None
            # Blueprint ids expanded by a SUCCESSFUL `getBlueprint` in THIS response.
            # Held apart from `blueprint_definitions_read` until the batch drains
            # (folded in below the dispatch loop) — see the fold site for why a
            # same-response `[getBlueprint(x), runBlueprint(x)]` pair must NOT pass
            # the gate. Reset per iteration, beside `designated_answer_text`.
            expanded_this_round: set[str] = set()
            # Read signatures SERVED EARLIER IN THIS RESPONSE. The trim-aware
            # exemption must skip them: `readable_tool_call_ids` was computed from the
            # window as it stood BEFORE this batch ran, so a read dispatched moments
            # ago is necessarily absent from it — and treating that as "trimmed away"
            # would re-dispatch the second of two identical calls in one batch, which
            # is precisely the duplicate the guard exists to collapse. Nothing can
            # have been trimmed between two calls of the same batch (no rebuild has
            # happened), so "served this round" means "will be readable next round".
            served_this_round: set[tuple[str, str]] = set()
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
                    kind="intents",
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
            elif (
                not result.tool_calls
                and multi_row_answer_calls
                and not answer_table_succeeded
            ):
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
                if await self._grant_forced_reround(
                    session_id=session_id,
                    turn_index=turn_index,
                    window_count=window_count,
                    kind="answer_shape",
                    already_refused_this_round=finalization_refused_this_round,
                ):
                    finalization_refused_this_round = True
                    refused_finalization = True
                    self._observer(
                        ANSWER_SHAPE_REFUSED_EVENT,
                        {"multi_row_calls": multi_row_answer_calls},
                    )
                    finalization_nudge = _answer_shape_nudge(
                        result.assistant_text, multi_row_answer_calls
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
                envelope = _answer_envelope(
                    answer_tables, blueprint_use=blueprint_use, verification=verification
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
                    answer_sql=envelope.answer_sql,
                    blueprint_use=envelope.blueprint_use,
                    verification=envelope.verification,
                    answer_tables=envelope.answer_tables,
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
                #   - `idempotent_read_signature` is computed below, and two
                #     identical `getTableSchema` fetches tagged for different
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
                    idempotent_read_signature(tool_call.name, call_args)
                    if is_idempotent_read
                    else None
                )
                guarded_repeat = is_idempotent_read and read_sig in seen_read_calls
                # --- TRIM-AWARE RE-FETCH EXEMPTION -----------------------------
                #
                # ⚠ THE GUARD'S PREMISE HAS A HOLE. The premise is "the already-served
                # result is in the history above" — true of the persisted TRAIL (which
                # is what `seen_read_calls` is seeded from), but NOT of the RENDERED
                # window: `context/budget.py::fit_request_to_budget` pins only the K
                # most recent current-turn tool pairs and drops older ones under real
                # budget pressure, and `context/assembly.py` replaces a D44-stranded
                # entry with a data-free sentinel. Either way the model is told "you
                # already have this" about something it demonstrably cannot read, and
                # nothing it can do recovers the result — the exact shape of an
                # unrecoverable turn.
                #
                # It also made the base prompt lie: its re-fetch escape ("if it is NO
                # LONGER above … fetch it again") described a door the guard had
                # welded shut. `prompts.py` now states the escape positively BECAUSE
                # this exemption makes it true; the two are a pair and must move
                # together.
                #
                # So: a repeat whose SERVING RESULT IS NO LONGER READABLE is exempted
                # and re-dispatched for real. When the result IS readable the guard
                # fires exactly as before — that is D94's protection against the
                # observed dozens-of-re-fetches spin, and it is untouched.
                #
                # UNIFORM ACROSS `IDEMPOTENT_READ_TOOLS`, deliberately. The predicate
                # is a property of the CONTEXT, not of any tool: whatever the read
                # was, if its answer is gone the model needs it again. A per-tool
                # carve-out would leave the prompt's escape silently working for some
                # reads and not others, which is the class of contradiction this
                # release has already paid for twice.
                #
                # BOUNDED, because "correct each time" and "wasteful in aggregate" are
                # both true here: a read that is fetched, trimmed, re-fetched, trimmed
                # is re-adding bulk the budget has already judged droppable, and past
                # a point the cheap nudge is strictly better than paying an MCP
                # round-trip to re-add it. `_MAX_TRIMMED_READ_REFETCHES` exemptions
                # per signature per window, then the guard resumes.
                if guarded_repeat and read_sig is not None and read_sig not in served_this_round:
                    served_by = served_read_call_ids.get(read_sig)
                    if served_by is None or served_by not in readable_tool_call_ids:
                        granted = read_refetch_exemptions.get(read_sig, 0)
                        event = (
                            "loop_trimmed_read_refetch_allowed"
                            if granted < _MAX_TRIMMED_READ_REFETCHES
                            else "loop_trimmed_read_refetch_capped"
                        )
                        if granted < _MAX_TRIMMED_READ_REFETCHES:
                            read_refetch_exemptions[read_sig] = granted + 1
                            guarded_repeat = False
                        self._observer(
                            event,
                            _trimmed_read_refetch_event(
                                tool_call.name,
                                call_args,
                                granted=granted,
                                reason=(
                                    "result_not_readable_in_window"
                                    if served_by is not None
                                    else "no_readable_source"
                                ),
                            ),
                        )
                if guarded_repeat:
                    # A guarded `getBlueprint` STILL SATISFIES the blueprint-definition
                    # gate. Being deduped means the definition is already in the
                    # model's context (the exemption above is what makes that true),
                    # which is exactly what the gate asks. Without this a deadlock is
                    # reachable: expand, run refused for an unrelated reason (a missing
                    # slot), re-expand defensively, get a data-free marker, and never
                    # satisfy the gate again.
                    #
                    # NOTE this is the OPPOSITE of 04 condition 5, which REJECTS the
                    # same marker as completion evidence. The two gates ask different
                    # questions — "does the model have the definition?" versus "did
                    # work actually happen?" — and a dedup answers yes to the first
                    # and no to the second.
                    if tool_call.name == "getBlueprint":
                        deduped_id = clean_blueprint_id(call_args.get("id"))
                        if deduped_id is not None:
                            expanded_this_round.add(deduped_id)
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
                        _repeated_read_guard_event(
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
                # measures something else entirely. See
                # `_blueprint_definition_not_read` for the full rationale.
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
                if (
                    tool_call.name == "runBlueprint"
                    and handler is not None
                    and isinstance(call_args, dict)
                ):
                    gated_blueprint_id = clean_blueprint_id(call_args.get("id"))
                    if (
                        gated_blueprint_id is not None
                        and gated_blueprint_id not in blueprint_definitions_read
                    ):
                        gate_refusal = _blueprint_definition_not_read(gated_blueprint_id)
                        self._observer(
                            "loop_blueprint_definition_not_read",
                            {
                                "tool_name": "runBlueprint",
                                # D25: corpus-authored, never user content. No SQL and
                                # no question text is placed on the span.
                                "blueprint_id": gated_blueprint_id,
                                "reason": "no_get_blueprint_this_turn",
                            },
                        )

                # LLM-generated progress summary (opt-in, `progress_summary_enabled`):
                # fire the value-rich present-tense line CONCURRENTLY, BEFORE dispatch
                # and WITHOUT awaiting it, so it never adds latency to the tool. The
                # instant `tool_dispatch_start` template label still fires as today
                # (inside the dispatcher / the runtime tools); this line is additive,
                # arriving when ready. No-op when the feature is off. Skipped for a
                # gated call: nothing is about to run, so narrating it would be a lie.
                if gate_refusal is None:
                    self._maybe_start_summary(tool_call.name, call_args)

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
                    tool_result = await self._tool_dispatcher.dispatch(
                        tool_call.name, call_args, credentials
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
                    and isinstance(call_args, dict)
                    and clean_answer_text(call_args.get("answer")) is not None
                ):
                    pending_at_answer = _pending_intents(analysis_state)
                    if pending_at_answer:
                        if await self._grant_forced_reround(
                            session_id=session_id,
                            turn_index=turn_index,
                            window_count=window_count,
                            kind="intents",
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
                            # `ok`, so neither `_resolve_answer_tables` below nor
                            # the terminal-exit check fires for this call.
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
                        blueprint_runs=blueprint_runs,
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
                        and multi_row_answer_calls
                        and clean_answer_text(call_args.get("answer")) is not None
                    ):
                        # THE EMPTY DESIGNATION (08 §O). The model called the table
                        # tool, named no table, and — because the call carries prose
                        # — would TERMINATE the turn right here, through the exit the
                        # 05 §J shape gate does not watch. Measured live as
                        # `status=done`, no table, no event, no log: the user asked
                        # for a breakdown, the turn held six rows of it, and the
                        # answer was prose. See `_answer_table_no_table_designated`.
                        #
                        # `multi_row_answer_calls` SCOPES IT, and the scope is the
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
                        if await self._grant_forced_reround(
                            session_id=session_id,
                            turn_index=turn_index,
                            window_count=window_count,
                            kind="answer_shape",
                            already_refused_this_round=finalization_refused_this_round,
                        ):
                            finalization_refused_this_round = True
                            _logger.info(
                                "answerWithTable designated no table while %d "
                                "multi-row result(s) went untabled — nudging "
                                "(session=%s)",
                                multi_row_answer_calls,
                                session_id,
                            )
                            self._observer(
                                ANSWER_SHAPE_REFUSED_EVENT,
                                {"multi_row_calls": multi_row_answer_calls},
                            )
                            tool_result = _answer_table_no_table_designated()
                        else:
                            self._observer(ANSWER_SHAPE_EXHAUSTED_EVENT, {})
                    else:
                        self._observe_uncovered_intents(
                            analysis_state,
                            tables=resolved_answer_tables,
                            result_sql_by_call_id=result_sql_by_call_id,
                        )

                # §2.5 pausing-runtime-tool seam: a runtime tool may signal a
                # pause (today only `runBlueprint`, on a slot-resolution
                # `askUser`). This GENERALIZES the terminal `askUser` branch
                # above — the loop writes the checkpoint and returns
                # `paused_ask_user` exactly as for `askUser`, before persisting a
                # trail entry or counting the call (a paused tool did not
                # complete, mirroring `askUser`). A dispatched MCP tool never
                # sets `.pause`, so this is inert on the normal path.
                envelope = _answer_envelope(
                    answer_tables, blueprint_use=blueprint_use, verification=verification
                )
                if tool_result.pause is not None:
                    return await self._pause_from_runtime_tool(
                        session_id=session_id,
                        pause=tool_result.pause,
                        window_count=window_count,
                        assistant_text=result.assistant_text,
                        tool_calls_made=tool_calls_made,
                        # Fix 2: surface whatever succeeded earlier in this window.
                        sql_executed=turn_sql or None,
                        envelope=envelope,
                        assumptions=turn_assumptions or None,
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
                # blueprint approval-resume seed path via `_accumulate_enrichment`.
                blueprint_use, verification = self._accumulate_enrichment(
                    tool_call.name,
                    call_args,
                    tool_result,
                    turn_sql=turn_sql,
                    blueprint_use=blueprint_use,
                    verification=verification,
                )
                _capture_terminal_sql(
                    tool_call.name,
                    tool_result,
                    into=blueprint_runs,
                    arguments=call_args if isinstance(call_args, dict) else None,
                )
                # The intent-coverage CHECK's raw material (08 §B.1): which query's
                # rows this call produced, keyed by the id an intent cites as its
                # evidence. NOT a source for the tables themselves — deriving those
                # from the evidence call would page the agent's own LIMIT-ed reading
                # query and silently truncate every grid.
                if tool_result.status == "ok":
                    if tool_call.name == "runQuery" and isinstance(call_args, dict):
                        produced = call_args.get("sql")
                        if isinstance(produced, str) and produced:
                            result_sql_by_call_id[tool_call.id] = produced
                    elif tool_call.name == "runBlueprint":
                        captured = blueprint_run_from_result(tool_result.result_full)
                        if captured is not None:
                            result_sql_by_call_id[tool_call.id] = captured[1].terminal_sql
                # ANSWER-SHAPE GATE (05 §J): count this call if it is a successful
                # data-returning call with more than one row. Read from the same
                # `result_preview` that was just persisted on the entry above, so the
                # in-window count and the trail seed can never disagree about what
                # happened. Counted AFTER the finalization/blueprint-not-run rewrites
                # of `tool_result`, so a refused call (now non-`ok`) is not counted.
                if _is_multi_row_answer_call(
                    tool_call.name, tool_result.status, tool_result.result_preview
                ):
                    multi_row_answer_calls += 1
                # recordAssumptions (docs/decisions/ui-assumptions-contract.md):
                # fold a SUCCESSFUL call's plain-English assumptions into the
                # turn accumulator, same discipline as the enrichment above.
                self._accumulate_assumptions(
                    tool_call.name,
                    call_args,
                    tool_result,
                    turn_assumptions=turn_assumptions,
                )
                # answerWithTable (composite/answer_with_table.py): the
                # model-designated answer tables. Same discipline again — read from
                # the call ARGUMENTS on success — except LAST designation wins,
                # over the whole SET, since a turn has one answer.
                answer_tables = self._accumulate_answer_tables(
                    tool_call.name,
                    tool_result,
                    answer_tables=answer_tables,
                    resolved=resolved_answer_tables,
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
                    # `answer_tables` (the accumulator, folded just above) rather
                    # than `resolved_answer_tables` alone, because a LATER call that
                    # designates nothing deliberately leaves an EARLIER good set
                    # intact (`_accumulate_answer_tables`: "a malformed retry cannot
                    # silently drop a good table"). Reading only this call's
                    # resolution would re-arm the gate on that retry and refuse a
                    # turn that has its table.
                    #
                    # Still set for the blank-`answer` call that does not terminate,
                    # PROVIDED it designated something: those tables reach the user
                    # through the envelope, which is what the gate protects.
                    if resolved_answer_tables or answer_tables:
                        answer_table_succeeded = True
                    designated_answer_text = (
                        clean_answer_text(call_args.get("answer"))
                        if isinstance(call_args, dict)
                        else None
                    )

                # Record a SUCCESSFUL idempotent read so an identical repeat later
                # this turn is caught by the guard above. Only `ok` reads are
                # "already served" — a denied/errored read is NOT recorded, so a
                # legitimate retry after a transient failure is never suppressed.
                if is_idempotent_read and tool_result.status == "ok" and read_sig is not None:
                    seen_read_calls.add(read_sig)
                    # THIS entry is now where the result lives — the pointer the
                    # trim-aware exemption tests for readability next round-trip. It
                    # is overwritten on a re-fetch, so the pointer always names the
                    # freshest serving entry rather than a stale trimmed one.
                    served_read_call_ids[read_sig] = tool_call.id
                    served_this_round.add(read_sig)

                # Record a SUCCESSFUL `getBlueprint` so the blueprint-definition gate
                # lets that id run. Staged in the per-response set, not folded into
                # `blueprint_definitions_read` until the batch drains (see the fold
                # below the loop).
                if (
                    tool_call.name == "getBlueprint"
                    and tool_result.status == "ok"
                    and isinstance(call_args, dict)
                ):
                    expanded_id = clean_blueprint_id(call_args.get("id"))
                    if expanded_id is not None:
                        expanded_this_round.add(expanded_id)

                # S3: also check the budget INSIDE the per-tool-call loop (not
                # only once per outer iteration) so a slow batch of capped
                # calls that blows the wall-clock window mid-dispatch stops
                # cleanly instead of finishing the whole batch regardless.
                if guard.exceeded:
                    break

            # BLUEPRINT-DEFINITION GATE, the fold. Ids expanded in THIS response
            # become runnable from the NEXT one — deliberately not mid-batch. The
            # rule is that the model has READ the definition, and the result of a
            # `getBlueprint` issued in this response does not reach the model until
            # the next round-trip: a `[getBlueprint(x), runBlueprint(x)]` pair in one
            # message would satisfy a mid-batch fold while the model was still blind
            # to the SQL, which is the whole failure the gate exists to stop. The
            # refusal costs exactly the round-trip the model owed anyway, and the
            # BATCHED shape is unaffected — `[getBlueprint(a), getBlueprint(b)]` then
            # `[runBlueprint(a), runBlueprint(b)]` is still 2 round-trips for 2
            # deliverables, not 2 per deliverable.
            blueprint_definitions_read |= expanded_this_round

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
                envelope = _answer_envelope(
                    answer_tables, blueprint_use=blueprint_use, verification=verification
                )
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
                    answer_sql=envelope.answer_sql,
                    blueprint_use=envelope.blueprint_use,
                    verification=envelope.verification,
                    answer_tables=envelope.answer_tables,
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
                envelope = _answer_envelope(
                    answer_tables, blueprint_use=blueprint_use, verification=verification
                )
                self._observer("loop_turn_done", {"tool_calls_made": tool_calls_made})
                return TurnOutcome(
                    status="done",
                    assistant_text=designated_answer_text,
                    pending_question=None,
                    tool_calls_made=tool_calls_made,
                    sql_executed=turn_sql or None,
                    answer_sql=envelope.answer_sql,
                    blueprint_use=envelope.blueprint_use,
                    verification=envelope.verification,
                    answer_tables=envelope.answer_tables,
                    provenance=turn_provenance,
                    assumptions=turn_assumptions or None,
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
                    envelope = _answer_envelope(
                        answer_tables, blueprint_use=blueprint_use, verification=verification
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
                        answer_sql=envelope.answer_sql,
                        blueprint_use=envelope.blueprint_use,
                        verification=envelope.verification,
                        answer_tables=envelope.answer_tables,
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
                if finalization_refused_this_round:
                    # `finalization_refused_this_round` is now set by the ANSWER-SHAPE
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
                await self._session_store.write_pause_checkpoint(session_id, checkpoint)
                envelope = _answer_envelope(
                    answer_tables, blueprint_use=blueprint_use, verification=verification
                )
                self._observer("loop_paused_budget_cap", {"window": window_count})
                return TurnOutcome(
                    status="paused_budget_cap",
                    assistant_text=last_assistant_text,
                    pending_question=checkpoint.pending_question,
                    tool_calls_made=tool_calls_made,
                    # Best-effort partial (§1): whatever succeeded before the cap;
                    # `provenance` stays `None` (done-only).
                    sql_executed=turn_sql or None,
                    answer_sql=envelope.answer_sql,
                    blueprint_use=envelope.blueprint_use,
                    verification=envelope.verification,
                    answer_tables=envelope.answer_tables,
                    assumptions=turn_assumptions or None,
                )
            # Under budget — loop back to 3a within the same window.


__all__ = [
    "ANSWER_SHAPE_EXHAUSTED_EVENT",
    "ANSWER_SHAPE_REFUSED_EVENT",
    "AgentLoop",
    "EmulatedDiscoveryProvider",
    "RuntimeTool",
    "ToolsProvider",
    "TurnContext",
    "TurnOutcome",
]
