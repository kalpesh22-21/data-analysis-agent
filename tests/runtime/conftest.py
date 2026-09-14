"""Explicit prerequisites for tests isolating later runtime stages."""

import json

import pytest

from data_agent.runtime.loop.dispatch_gates import BlueprintSearchGate


@pytest.fixture
def blueprint_consulted(monkeypatch):
    """Model a previous blueprint consultation in tests of downstream behavior.

    These suites exercise provenance, finalization, tracing, or result shapes and
    start their scripts at runQuery. Gate behavior with actual dispatch/trail writes
    is covered separately in test_remote_runtime_changes.py.
    """
    original = BlueprintSearchGate.__init__

    def initialize(self, question, trail, turn_index):
        original(self, question, trail, turn_index)
        self.observe_context(
            [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {"tool_name": "searchBlueprints", "turn_index": turn_index, "status": "ok"}
                    ),
                }
            ]
        )

    monkeypatch.setattr(BlueprintSearchGate, "__init__", initialize)


@pytest.fixture
def answer_tools(monkeypatch):
    """Wire the final-answer handlers, as create_app does in production."""
    from data_agent.runtime.composite.answer_with_table import AnswerWithTableTool
    from data_agent.runtime.composite.answer_with_text import AnswerWithTextTool
    from data_agent.runtime.loop.agent_loop import AgentLoop

    original = AgentLoop.__init__

    def initialize(self, *args, **kwargs):
        kwargs["runtime_tools"] = {
            "answerWithText": AnswerWithTextTool(),
            "answerWithTable": AnswerWithTableTool(),
            **(kwargs.get("runtime_tools") or {}),
        }
        original(self, *args, **kwargs)

    monkeypatch.setattr(AgentLoop, "__init__", initialize)
