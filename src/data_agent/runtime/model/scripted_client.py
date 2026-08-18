"""ScriptedModelClient — cassette-style `ModelClient` double (Layer 1).

Consumes a fixed, ordered sequence of `ModelTurnResult`s; consuming past the end raises
`AssertionError` (a test-authoring bug, not a runtime condition). Every call is recorded
in `self.calls` so loop tests can scan the messages actually handed to the model for
JWT/session_id leakage (D5).
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
        """Returns `self` — no per-turn state to isolate, so `self.calls` and the scripted
                cursor keep accumulating across a whole test's run/resume sequence.
        """
        return self

    @property
    def calls_made(self) -> int:
        return self._cursor
