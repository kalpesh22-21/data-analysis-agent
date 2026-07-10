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

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.record_assumptions import fold_assumptions
from data_agent.runtime.context import scope_filter
from data_agent.runtime.context.assembly import (
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
)
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    ToolPause,
    ToolResult,
)
from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.runtime.session.models import (
    PauseCheckpoint,
    ResultPreview,
    TrailEntry,
    TurnMessage,
)
from data_agent.runtime.session.store import SessionStore

from .budget_guard import new_budget_window

ToolsProvider = Callable[[RuntimeCredentials], Awaitable[list[dict[str, Any]]]]

_logger = logging.getLogger(__name__)

# A runtime tool that crashes or returns a contract-violating result is
# contained at the registry seam (read-tools-design §2 hardening, prep for
# runBlueprint): the loop returns this clean error rather than aborting the turn
# or leaking `str(exc)`. Distinct from the tools' own `_guarded` self-protection
# (defense in depth — both layers hold).
RUNTIME_TOOL_INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"
_RUNTIME_TOOL_INTERNAL_ERROR_MESSAGE = "That tool hit an internal error. Please try again."


class RuntimeTool(Protocol):
    """A model-facing tool implemented in the RUNTIME (not the MCP), intercepted
    in the loop and returning an inline `ToolResult` — the `resolveValues` shape
    (read-tools-design §2). `askUser` is NOT a `RuntimeTool`: it is TERMINAL (it
    pauses, it does not return a `ToolResult`), so it stays a hardcoded branch."""

    async def run(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials
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
    "done", "paused_ask_user", "paused_budget_cap", "stopped_hard_ceiling"
]

_BUDGET_CAP_QUESTION = "This is taking a while — continue, refine, or stop?"
_BUDGET_CAP_OPTIONS = ["continue", "refine", "stop"]

# Idempotent, side-effect-free read tools whose result depends ONLY on their
# arguments — an identical repeat within a turn is guaranteed to return the same
# already-served data (it is in the history above). The repeated-idempotent-read
# guard (generalizing D94) declines to re-dispatch such a repeat and injects a
# data-free "you already have this" nudge instead. runQuery/runBlueprint/askUser/
# resolveValues are deliberately EXCLUDED — a repeated runQuery may be a distinct
# legitimate step and is never guarded here.
_IDEMPOTENT_READ_TOOLS = frozenset(
    {"getTableSchema", "listTables", "listDatabases", "explainQuery"}
)


def _idempotent_read_signature(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, str]:
    """The content key that identifies an already-served idempotent read: the
    tool name plus its canonicalized arguments (stable key order, `str`-coerced
    for any non-JSON-native arg). Two calls with the same key return the same
    data by construction, so the second is a re-fetch."""
    return (tool_name, json.dumps(dict(arguments), sort_keys=True, default=str))


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
            f"duplicate {tool_name}({dedup_target}) — already served this turn; "
            "not re-dispatched"
        ),
    }
    if db:
        payload["database"] = db
    if tbl:
        payload["table"] = tbl
    return payload


def _now_iso() -> str:
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
    sql: list[str] | None = None
    result_table: ResultPreview | None = None
    blueprint_use: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    provenance: frozenset[tuple[str, str]] | None = None
    # recordAssumptions (docs/decisions/ui-assumptions-contract.md): the
    # model-declared, plain-English assumptions behind the answer — a first-class
    # result field mirroring `sql` in EVERY respect (additive, nullable, `[] ->
    # None` fork, accumulated across budget windows at every return site).
    assumptions: list[str] | None = None


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
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(
                {
                    "status": entry["status"],
                    "error_code": entry.get("error_code"),
                    # S4: the static, PII-safe denial message (never raw MCP error
                    # text) so the model can see WHY a retryable call failed and
                    # self-correct — see context/budget.py::_render_entry.
                    "user_message": entry.get("user_message"),
                    "result_preview": entry.get("result_preview"),
                },
                default=str,
            ),
        }
    return [assistant_message, tool_message]


def _assembled_to_canonical(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`AssembledContext.messages` (Pass-A shape, `context/budget.py::render_messages`)
    -> the canonical `ModelClient.send_turn` message shape (design §1 `model/client.py`).

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
        else:  # pragma: no cover - render_messages only ever emits system/tool
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
        max_tool_calls_per_iteration: int = 8,
        clock: Callable[[], float] = time.monotonic,
        observer: ToolObserver = _default_observer,
        runtime_tools: Mapping[str, RuntimeTool] | None = None,
        blueprint_executor: Any = None,
    ) -> None:
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
        self._max_budget_windows = max_budget_windows
        self._token_budget = token_budget
        self._max_tool_calls_per_iteration = max_tool_calls_per_iteration
        self._clock = clock
        self._observer = observer

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
                return TurnOutcome(
                    status="done",
                    assistant_text=(
                        "Stopping here — here is what I found before the budget cap."
                    ),
                    pending_question=None,
                    tool_calls_made=0,
                    # UI Slice 1: a `done` return — surface the turn's lineage from
                    # the trail (the fail-closed source of truth). The in-loop sql/
                    # table/blueprint accumulators are gone with the prior window, so
                    # they stay `None` (best-effort partial, §1 nullability table).
                    provenance=await self._compute_turn_provenance_union(
                        session_id, turn_index
                    ),
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
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            seed_assumptions=seed_assumptions,
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
        doc = await self._session_store.get_or_create_session(session_id)
        # D44 (2026-07-01 clarification, B1): the same replay scope-filter
        # that gates the tool trail also gates conversational ASSISTANT
        # messages — user messages carry no warehouse data and are always
        # kept; see context/scope_filter.py::filter_messages.
        in_scope_messages = scope_filter.filter_messages(doc.messages, column_scope)
        for turn_message in in_scope_messages:
            canonical.append({"role": turn_message.role, "content": turn_message.content})
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
            if (
                entry.status == "ok"
                and entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE
            ):
                continue
            # A successful `recordAssumptions` entry carries NO warehouse data (it
            # only echoes the model's plain-English assumptions) and deliberately
            # has `None` provenance so it is dropped from replay by `filter_trail`
            # (assumption strings never re-enter model context under a narrowed
            # scope). Like the idempotent-read guard above, it must NOT collapse
            # this union to `None` — otherwise every turn that records an
            # assumption would tag its answer undetermined and lose it from history
            # + replay (docs/decisions/ui-assumptions-contract.md).
            if entry.status == "ok" and entry.tool_name == "recordAssumptions":
                continue
            if entry.provenance is None:
                return None
            union.update(entry.provenance)
        return frozenset(union)

    async def _compute_turn_assumptions(
        self, session_id: str, turn_index: int
    ) -> list[str]:
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

    async def _run_runtime_tool(
        self,
        handler: RuntimeTool,
        tool_name: str,
        arguments: dict[str, Any],
        credentials: RuntimeCredentials,
    ) -> ToolResult:
        """Run one registry handler with a B4-style crash guard + a returned-
        provenance-type validation (S2), so a misbehaving runtime tool cannot
        abort the turn or persist a replay-poisoning provenance. The three read
        tools + `resolveValues` already self-guard; this is defense in depth and
        the containment seam the future `runBlueprint` brick relies on."""
        try:
            result = await handler.run(arguments, credentials)
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

    async def _pause_from_runtime_tool(
        self,
        *,
        session_id: str,
        pause: ToolPause,
        window_count: int,
        assistant_text: str | None,
        tool_calls_made: int,
        sql: list[str] | None = None,
        result_table: ResultPreview | None = None,
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
            sql=sql,
            result_table=result_table,
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
            _logger.exception(
                "runBlueprint resume raised (session=%s)", credentials.session_id
            )
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
        # same sql/result_table/blueprint_use/verification a non-paused blueprint
        # answer would (a fresh `_run_loop` window would otherwise start empty and
        # drop it). Raw slots ride the checkpoint's `slot_bindings`. A no-op on a
        # non-`ok` (failed/degraded) resume → no seed, matching a raw-loop fallback.
        seed_sql: list[str] = []
        seed_preview, seed_blueprint_use, seed_verification = self._accumulate_enrichment(
            "runBlueprint",
            {"slot_bindings": slot_bindings},
            tool_result,
            turn_sql=seed_sql,
            primary_preview=None,
            blueprint_use=None,
            verification=None,
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
        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
            question=question,
            seed_sql=seed_sql,
            seed_preview=seed_preview,
            seed_blueprint_use=seed_blueprint_use,
            seed_verification=seed_verification,
            seed_assumptions=seed_assumptions,
        )

    def _blueprint_outcome_to_tool_result(self, outcome: Any) -> ToolResult:
        """Map a `BlueprintExecutor` `ExecOutcome` to a `ToolResult` — the SAME
        mapping `RunBlueprintTool._execute` uses, reused here for the resume path
        so a mid-DAG resume produces byte-identical results to a first call."""
        from data_agent.runtime.blueprint.executor import (
            ExecCompleted,
            ExecFailed,
            ExecPaused,
        )

        if isinstance(outcome, ExecCompleted):
            return ToolResult(
                status="ok",
                tool_name="runBlueprint",
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=outcome.provenance,
                result_preview=outcome.preview,
                result_full=outcome.result_full,
            )
        if isinstance(outcome, ExecPaused):
            return ToolResult(
                status="ok",
                tool_name="runBlueprint",
                error_code=None,
                retryable=None,
                user_message=None,
                provenance=frozenset(),
                result_preview=None,
                result_full=None,
                pause=ToolPause(
                    reason=outcome.reason,
                    pending_question=outcome.pending_question,
                    blueprint_id=outcome.blueprint_id,
                    slot_bindings_json=outcome.slot_bindings_json,
                    completed_nodes_json=outcome.completed_nodes_json,
                    awaiting_node=outcome.awaiting_node,
                ),
            )
        if isinstance(outcome, ExecFailed):
            return ToolResult(
                status="error",
                tool_name="runBlueprint",
                error_code=outcome.error_code,
                retryable=outcome.retryable,
                user_message=outcome.user_message,
                provenance=outcome.provenance,
                result_preview=None,
                result_full=None,
            )
        return _runtime_tool_internal_error("runBlueprint")

    @staticmethod
    def _accumulate_enrichment(
        tool_name: str,
        arguments: dict[str, Any],
        tool_result: ToolResult,
        *,
        turn_sql: list[str],
        primary_preview: ResultPreview | None,
        blueprint_use: dict[str, Any] | None,
        verification: dict[str, Any] | None,
    ) -> tuple[ResultPreview | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Fold one SUCCESSFUL runQuery/runBlueprint result into the turn-window
        enrichment accumulators (UI Slice 1, contract §3.2). `turn_sql` is mutated
        in place (deduped, first-occurrence order); the other three are RETURNED
        for the caller to reassign. A no-op for any non-`ok` / non-query tool call,
        so it is safe to call unconditionally. Shared by the in-loop dispatch path
        AND the blueprint approval-resume seed path so both produce identical
        enrichment (Fix 1: a resumed verified answer keeps its badge + chip)."""
        if tool_result.status != "ok":
            return primary_preview, blueprint_use, verification
        if tool_name == "runQuery":
            query_sql = arguments.get("sql")
            if query_sql and query_sql not in turn_sql:
                turn_sql.append(query_sql)
            return tool_result.result_preview, blueprint_use, verification
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
            return tool_result.result_preview, new_blueprint_use, new_verification
        return primary_preview, blueprint_use, verification

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
        # UI Slice 1 Fix 1: seed the turn-window enrichment accumulators from a
        # completed-before-this-window result (the blueprint approval-resume path)
        # so the FINAL `done` result event carries the same enrichment a non-paused
        # answer would. Default `None`/empty → byte-identical to a fresh window.
        seed_sql: list[str] | None = None,
        seed_preview: ResultPreview | None = None,
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
        for prior_entry in await self._session_store.load_trail(session_id):
            if (
                prior_entry.turn_index == turn_index
                and prior_entry.status == "ok"
                and prior_entry.tool_name in _IDEMPOTENT_READ_TOOLS
            ):
                seen_read_calls.add(
                    _idempotent_read_signature(prior_entry.tool_name, prior_entry.args)
                )
        # UI Slice 1 (docs/decisions/ui-slice1-enriched-result-contract.md §3):
        # turn-window-local accumulators for the enriched `result` event, same
        # lifecycle as the memos above (fresh per window, not persisted).
        # Populated on each successful runQuery/runBlueprint entry below, read at
        # every `TurnOutcome(...)` return site. Seeded (Fix 1) on the blueprint
        # approval-resume path so a resumed verified answer keeps its enrichment.
        turn_sql: list[str] = list(seed_sql) if seed_sql else []
        primary_preview: ResultPreview | None = seed_preview
        blueprint_use: dict[str, Any] | None = seed_blueprint_use
        verification: dict[str, Any] | None = seed_verification
        # recordAssumptions accumulator (mirrors `turn_sql`): the deduped,
        # first-occurrence list of plain-English assumptions the model recorded
        # this turn. Folded from each SUCCESSFUL recordAssumptions call's ARGUMENTS
        # (`_accumulate_assumptions`), read at every `TurnOutcome(...)` return site.
        turn_assumptions: list[str] = list(seed_assumptions) if seed_assumptions else []

        while True:
            canonical_messages = await self._build_canonical_messages(
                session_id,
                credentials.column_scope,
                turn_index,
                question=question,
                user_id=None,
                retrieval_memo=retrieval_memo,
                withheld_call_ids=withheld_call_ids,
            )
            self._observer("loop_model_call_start", {"window": window_count})
            result = await model_client.send_turn(canonical_messages, tools)
            last_assistant_text = result.assistant_text

            if not result.tool_calls:
                # B1/D44 (2026-07-01 clarification) AND UI Slice 1: the union of
                # this turn's tool-result provenance — the tag for the final
                # assistant message (so it is scope re-filtered on replay exactly
                # like the trail itself) AND the enriched `result` event's lineage.
                # Computed ONCE here (the single fail-closed source of truth; do
                # not re-derive in-loop).
                turn_provenance = await self._compute_turn_provenance_union(
                    session_id, turn_index
                )
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
                    sql=turn_sql or None,
                    result_table=primary_preview,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    provenance=turn_provenance,
                    # `[]` (no recordAssumptions this turn) -> `None`, same fork as
                    # `sql`: the UI treats "no assumptions" and "empty" identically.
                    assumptions=turn_assumptions or None,
                )

            ask_user_call = next(
                (tc for tc in result.tool_calls if tc.name == "askUser"), None
            )
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
                    sql=turn_sql or None,
                    result_table=primary_preview,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    assumptions=turn_assumptions or None,
                )

            # S3: never dispatch an unbounded number of tool calls from one
            # model response — cap per iteration (RuntimeSettings-configurable,
            # default 8). Any calls beyond the cap are simply not dispatched
            # this round (best-partial); nothing is persisted for them, so
            # they leave no trail entry and are not "silently denied" — the
            # model just does not see a response for them and may re-request
            # on the next round-trip if it still wants them.
            capped_tool_calls = result.tool_calls[: self._max_tool_calls_per_iteration]
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
                is_idempotent_read = tool_call.name in _IDEMPOTENT_READ_TOOLS
                read_sig = (
                    _idempotent_read_signature(tool_call.name, tool_call.arguments)
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

                # Runtime-tool registry (read-tools-design §2): a runtime tool
                # (`resolveValues` + the three read tools) is intercepted here —
                # it never reaches `dispatch` under its own name (only any inner
                # tool it issues does). It returns the SAME `ToolResult`
                # dataclass, so the trail/budget path below is unchanged and it
                # counts as exactly one `tool_calls_made`. An advertised runtime
                # tool that is not wired returns a clean local unavailable error
                # (§6), never an incoherent MCP unknown-tool denial.
                handler = self._runtime_tools.get(tool_call.name)
                if handler is not None:
                    tool_result = await self._run_runtime_tool(
                        handler, tool_call.name, tool_call.arguments, credentials
                    )
                elif tool_call.name in _RUNTIME_TOOL_UNAVAILABLE_CODE:
                    tool_result = _runtime_tool_unavailable(
                        tool_call.name, _RUNTIME_TOOL_UNAVAILABLE_CODE[tool_call.name]
                    )
                else:
                    tool_result = await self._tool_dispatcher.dispatch(
                        tool_call.name, tool_call.arguments, credentials
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
                    return await self._pause_from_runtime_tool(
                        session_id=session_id,
                        pause=tool_result.pause,
                        window_count=window_count,
                        assistant_text=result.assistant_text,
                        tool_calls_made=tool_calls_made,
                        # Fix 2: surface whatever succeeded earlier in this window.
                        sql=turn_sql or None,
                        result_table=primary_preview,
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
                )
                await self._session_store.append_trail_entry(session_id, entry)

                # UI Slice 1 (§3.2): accumulate the enriched-result fields from
                # this SUCCESSFUL tool call (runQuery arg SQL + preview; runBlueprint
                # `result_full` SQL/blueprint_id/verify + preview). Shared with the
                # blueprint approval-resume seed path via `_accumulate_enrichment`.
                primary_preview, blueprint_use, verification = self._accumulate_enrichment(
                    tool_call.name,
                    tool_call.arguments,
                    tool_result,
                    turn_sql=turn_sql,
                    primary_preview=primary_preview,
                    blueprint_use=blueprint_use,
                    verification=verification,
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

            guard.record_iteration(tokens_used=int(result.usage.get("total_tokens") or 0))

            if guard.exceeded:
                if window_count >= self._max_budget_windows:
                    self._observer("loop_hard_ceiling_stop", {"window": window_count})
                    return TurnOutcome(
                        status="stopped_hard_ceiling",
                        assistant_text=last_assistant_text,
                        pending_question=None,
                        tool_calls_made=tool_calls_made,
                        # Best-effort partial (§1): whatever succeeded before the
                        # hard ceiling; `provenance` stays `None` (done-only).
                        sql=turn_sql or None,
                        result_table=primary_preview,
                        blueprint_use=blueprint_use,
                        verification=verification,
                        assumptions=turn_assumptions or None,
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
                    sql=turn_sql or None,
                    result_table=primary_preview,
                    blueprint_use=blueprint_use,
                    verification=verification,
                    assumptions=turn_assumptions or None,
                )
            # Under budget — loop back to 3a within the same window.


__all__ = ["AgentLoop", "RuntimeTool", "ToolsProvider", "TurnOutcome"]
