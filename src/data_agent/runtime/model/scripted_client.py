"""ScriptedModelClient — cassette-style `ModelClient` double (Layer 1, design §8).

Scripted as a fixed, ordered sequence of `ModelTurnResult`s. Each `send_turn`
call consumes the next scripted result; calling it more times than scripted
raises `AssertionError` (a test-authoring bug, not a runtime condition — mirrors
`FakeMCPClient`'s own convention).

Every call is recorded (`self.calls`, a deep-ish copy of `(messages, tools)`)
so loop tests can scan every message payload actually handed to the model for
JWT/session_id leakage (D5) — see `tests/runtime/loop/test_agent_loop.py`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .client import ModelTurnResult


@dataclass(frozen=True)
class RecordedTurn:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]


class ScriptedModelClient:
    """Layer-1 `ModelClient` double with a scripted, ordered sequence of results."""

    def __init__(self, script: list[ModelTurnResult]) -> None:
        self._script: list[ModelTurnResult] = list(script)
        self._cursor = 0
        self.calls: list[RecordedTurn] = []

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append(RecordedTurn(messages=copy.deepcopy(messages), tools=copy.deepcopy(tools)))
        if self._cursor >= len(self._script):
            raise AssertionError(
                f"ScriptedModelClient has no more scripted turns "
                f"(consumed {self._cursor}/{len(self._script)})."
            )
        result = self._script[self._cursor]
        self._cursor += 1
        return result

    def begin_turn(self) -> ScriptedModelClient:
        """No per-turn state to isolate (Layer-1 double, always single-
        threaded/sequential per test) — returns `self` so `self.calls`/the
        scripted cursor keep accumulating across a whole test's `run()`/
        `resume()` sequence, per `model/client.py::begin_turn_client`."""
        return self

    @property
    def calls_made(self) -> int:
        return self._cursor
