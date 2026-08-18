"""ModelClient Protocol + DTOs — the model-provider abstraction seam.

`send_turn` is the only method on the seam: one round-trip over the loop's canonical
message history plus the tool schemas, returning free text (turn complete) or tool
calls. The canonical shape is this runtime's own convention, Chat-Completions-flavored;
`OpenAIModelClient` translates it to/from the Responses API:

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str | None,
     "tool_calls": [{"id": str, "type": "function",
                      "function": {"name": str, "arguments": str}}] | None}
    {"role": "tool", "tool_call_id": str, "content": str}

D5: that message list and the tool schemas are the ONLY things `send_turn` ever sees —
`RuntimeCredentials` must never be serialized into either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCallRequest:
    """One model-requested tool call."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurnResult:
    """The outcome of one `ModelClient.send_turn` round-trip.

        Empty `tool_calls` means the turn is complete and `assistant_text` is the final
        answer. `usage` carries whatever token counters the provider reports;
        `loop/budget_guard.py` reads `total_tokens` if present.
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

                Must not mutate *messages* or *tools* in place, and must never transmit
                anything beyond what those two arguments already contain (D5).
        """
        ...

    def begin_turn(self) -> ModelClient:
        """Optional: return a per-turn-scoped `ModelClient` handle.

                One `ModelClient` instance is shared across concurrent `/turn` requests, so any
                implementation carrying per-turn state MUST return a fresh, independent handle
                here, and every `send_turn` in that turn must go through it. Stateless
                implementations may `return self`; callers go through `begin_turn_client()`,
                which degrades for doubles that omit this method entirely.
        """
        ...


def begin_turn_client(model_client: ModelClient) -> ModelClient:
    """Return a per-turn-scoped handle for *model_client*.

        Calls `begin_turn()` if the client implements it (duck-typed), else returns
        *model_client* unchanged. The single place the loop obtains a turn handle.
    """
    begin_turn = getattr(model_client, "begin_turn", None)
    if callable(begin_turn):
        handle = begin_turn()
        if handle is not None:
            return handle
    return model_client
