"""Explicit prerequisites for tests isolating later runtime stages."""

from types import SimpleNamespace

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
        original(
            self,
            question,
            [*trail, SimpleNamespace(turn_index=turn_index, tool_name="searchBlueprints")],
            turn_index,
        )

    monkeypatch.setattr(BlueprintSearchGate, "__init__", initialize)
