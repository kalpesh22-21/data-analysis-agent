"""ModelClient Protocol + DTOs — the model-provider abstraction seam (design §1/§4).

`send_turn` is deliberately the *only* method on the seam: one model round-trip,
given the loop's current canonical message history and the tool schema list,
returns either free-text (turn complete) or a batch of tool calls for the
`AgentLoop` to dispatch. Two implementations exist:

  - `scripted_client.ScriptedModelClient` (Layer 1, cassette-style double).
  - `openai_client.OpenAIModelClient` (Layer 2/3, Responses-primary /
    Chat-fallback per D71).

Canonical message shape (this runtime's own convention, not necessarily any
one provider's wire format — `OpenAIModelClient` translates it to/from the
Responses API `input` items and the Chat Completions API `messages`):

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str | None,
     "tool_calls": [{"id": str, "type": "function",
                      "function": {"name": str, "arguments": str}}] | None}
    {"role": "tool", "tool_call_id": str, "content": str}

D5 (load-bearing): this canonical message list, and the `tools` schema list,
are the *only* things `send_turn` ever sees — `RuntimeCredentials` (jwt,
session_id, raw column_scope) must never be serialized into either. Every
`ModelClient` implementation (real or fake) must honor this; Pass B's loop
tests scan every message dict handed to `ScriptedModelClient.send_turn` for
the JWT/session_id substrings to enforce it end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCallRequest:
    """One model-requested tool call (design §4.1 step 3c)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurnResult:
    """The outcome of one `ModelClient.send_turn` round-trip.

    `tool_calls` empty means the turn is complete (design §4.1 termination
    condition #1) — `assistant_text` is then the final answer to persist and
    stream to the user. `usage` carries whatever token/latency counters the
    provider reports (`prompt_tokens`/`completion_tokens`/`total_tokens` at a
    minimum); `loop/budget_guard.py` reads `total_tokens` if present.
    """

    assistant_text: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    """The provider seam `loop/agent_loop.py` depends on."""

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        """One model round-trip: canonical *messages* + *tools* -> `ModelTurnResult`.

        Must never mutate *messages* or *tools* in place, and must never
        transmit anything beyond what those two arguments already contain
        (D5 — credentials are never passed to this method by any caller).
        """
        ...

    def begin_turn(self) -> ModelClient:
        """Optional: return a per-turn-scoped `ModelClient` handle (B3 fix,
        2026-07-01).

        `OpenAIModelClient` tracks Responses/Chat fallback stickiness (D71
        §4.2); when ONE `ModelClient` instance is shared across concurrent
        `/turn` requests (as `app.py`'s composition root does — and across
        the context-summarizer's background calls, `context/llm_summarizer.py`),
        mutating that stickiness as *shared instance state* lets one turn's
        fallback stomp another's. `begin_turn()` must therefore return a
        FRESH, independent handle whenever the implementation carries any
        such per-turn state (`OpenAIModelClient.begin_turn()` returns a new
        lightweight `OpenAIModelClient` wrapping the same underlying
        transport client); every `send_turn` call for the rest of that
        external turn must go through the returned handle, never the
        original shared instance.

        Implementations with no per-turn state (e.g. `ScriptedModelClient`,
        which is itself Layer-1-only and always single-threaded/sequential
        per test) may simply `return self`. This method is genuinely
        optional on the Protocol — callers must go through
        `begin_turn_client()` below, which degrades gracefully for any
        `ModelClient` double that omits it entirely.
        """
        ...


def begin_turn_client(model_client: ModelClient) -> ModelClient:
    """Return a per-turn-scoped handle for *model_client* (B3).

    Calls `model_client.begin_turn()` if the client implements it (duck-typed
    — not every `ModelClient` double needs to; a minimal test stub with only
    `send_turn` is a valid degenerate case), else returns *model_client*
    unchanged. The single call site both `loop/agent_loop.py` and
    `context/llm_summarizer.py` use, so the "never share per-turn state"
    contract is enforced identically everywhere a `ModelClient` is invoked.
    """
    begin_turn = getattr(model_client, "begin_turn", None)
    if callable(begin_turn):
        handle = begin_turn()
        if handle is not None:
            return handle
    return model_client
