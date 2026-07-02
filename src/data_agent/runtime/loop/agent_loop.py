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

`resolveValues` (D77) is the SECOND intercepted tool, handled symmetrically:
it too never reaches `ToolDispatcher.dispatch` under its own name (only the
inner `runQuery` it issues does), but — unlike `askUser`, which pauses — it
returns an INLINE `ToolResult` so the loop's existing TrailEntry +
write_full_result + budget path handles it identically to a dispatched tool.
It therefore counts as exactly ONE `tool_calls_made` (the inner runQuery does
not double-count) and respects `max_tool_calls_per_iteration` + wall-clock like
any other tool call. Its TrailEntry carries the inner runQuery's provenance
(resolvevalues-design §8).

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
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.context import scope_filter
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    ToolResult,
)
from data_agent.runtime.model.client import ModelClient, begin_turn_client
from data_agent.runtime.session.models import PauseCheckpoint, TrailEntry, TurnMessage
from data_agent.runtime.session.store import SessionStore

from .budget_guard import new_budget_window

ToolsProvider = Callable[[RuntimeCredentials], Awaitable[list[dict[str, Any]]]]

TurnStatus = Literal[
    "done", "paused_ask_user", "paused_budget_cap", "stopped_hard_ceiling"
]

_BUDGET_CAP_QUESTION = "This is taking a while — continue, refine, or stop?"
_BUDGET_CAP_OPTIONS = ["continue", "refine", "stop"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_observer(event: str, payload: dict[str, Any]) -> None:
    return None


def _resolve_values_unavailable(tool_name: str) -> ToolResult:
    """A clean local error for a `resolveValues` call when the composite is not
    wired (L2) — never dispatched to the MCP under its own name."""
    return ToolResult(
        status="error",
        tool_name=tool_name,
        error_code="RESOLVE_VALUES_UNAVAILABLE",
        retryable=False,
        user_message="Value resolution is not available right now.",
        provenance=None,
        result_preview=None,
        result_full=None,
    )


@dataclass(frozen=True)
class TurnOutcome:
    """The result of one `AgentLoop.run()`/`resume()` call."""

    status: TurnStatus
    assistant_text: str | None
    pending_question: dict[str, Any] | None
    tool_calls_made: int


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
    -> the canonical `ModelClient.send_turn` message shape (design §1 `model/client.py`)."""
    canonical: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            canonical.append({"role": "system", "content": message["content"]})
        elif role == "tool":
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
        resolve_values: ResolveValuesComposite | None = None,
    ) -> None:
        self._model_client = model_client
        self._tool_dispatcher = tool_dispatcher
        self._context_assembler = context_assembler
        self._session_store = session_store
        self._tools_provider = tools_provider
        # D77: the `resolveValues` composite is intercepted here (never
        # dispatched under its own name). Optional so existing Layer-1 loop
        # tests that never exercise resolveValues need not wire it.
        self._resolve_values = resolve_values
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
                )
            window_count = prior_window_count + 1  # D55: "continue"/"refine" grants a fresh window
        else:
            window_count = prior_window_count  # askUser resume is not a new budget grant

        turn_model_client = self._begin_model_turn()
        return await self._run_loop(
            session_id=session_id,
            credentials=credentials,
            window_count=window_count,
            turn_index=turn_index,
            model_client=turn_model_client,
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
        self, session_id: str, column_scope: frozenset[str], current_turn_index: int
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
        """
        assembled = await self._context_assembler.assemble(
            session_id, column_scope, current_turn_index=current_turn_index
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
            if entry.provenance is None:
                return None
            union.update(entry.provenance)
        return frozenset(union)

    async def _run_loop(
        self,
        *,
        session_id: str,
        credentials: RuntimeCredentials,
        window_count: int,
        turn_index: int,
        model_client: ModelClient,
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

        while True:
            canonical_messages = await self._build_canonical_messages(
                session_id, credentials.column_scope, turn_index
            )
            self._observer("loop_model_call_start", {"window": window_count})
            result = await model_client.send_turn(canonical_messages, tools)
            last_assistant_text = result.assistant_text

            if not result.tool_calls:
                if result.assistant_text:
                    # B1/D44 (2026-07-01 clarification): tag this assistant
                    # message with the union of this turn's tool-result
                    # provenance so it is scope re-filtered on replay exactly
                    # like the trail itself.
                    provenance = await self._compute_turn_provenance_union(
                        session_id, turn_index
                    )
                    await self._session_store.append_message(
                        session_id,
                        TurnMessage(
                            turn_index=turn_index,
                            role="assistant",
                            content=result.assistant_text,
                            ts=_now_iso(),
                            provenance=provenance,
                        ),
                    )
                self._observer("loop_turn_done", {"tool_calls_made": tool_calls_made})
                return TurnOutcome(
                    status="done",
                    assistant_text=result.assistant_text,
                    pending_question=None,
                    tool_calls_made=tool_calls_made,
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
                # D77: `resolveValues` is intercepted here (symmetric to
                # `askUser` above) — it never reaches `dispatch` under its own
                # name; only the inner `runQuery` it issues does. It returns the
                # SAME `ToolResult` dataclass, so the trail/budget path below is
                # unchanged and it counts as exactly one `tool_calls_made`.
                if tool_call.name == "resolveValues":
                    # L2: `resolveValues` is a runtime composite — it must NEVER
                    # be dispatched to the MCP under its own name (there is no
                    # such MCP tool). If the composite is not wired, return a
                    # clean local error instead of an incoherent unknown-tool
                    # MCP denial.
                    if self._resolve_values is None:
                        tool_result = _resolve_values_unavailable(tool_call.name)
                    else:
                        tool_result = await self._resolve_values.run(
                            tool_call.arguments, credentials
                        )
                else:
                    tool_result = await self._tool_dispatcher.dispatch(
                        tool_call.name, tool_call.arguments, credentials
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
                )
            # Under budget — loop back to 3a within the same window.


__all__ = ["AgentLoop", "TurnOutcome", "ToolsProvider"]
