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


@pytest.fixture(autouse=True)
def scripted_api_provenance(monkeypatch):
    """Upgrade legacy scripted payloads to the API's versioned response contract.

    Existing loop tests describe data and use the dispatcher's fixture catalog as
    their fake service schema. Derive receipts ONLY inside this test adapter; the
    runtime never receives this catalog-derived fallback. Explicit receipts are
    preserved, and real/custom clients are untouched for protocol failure tests.
    Unscripted explainQuery is a metadata validation in this service double.
    """
    from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, resolve_catalog
    from data_agent.runtime.mcp.client import MCPToolError
    from data_agent.runtime.mcp.fake_client import FakeMCPClient
    from data_agent.sqlparse import ProvenanceExtractionError, extract_column_provenance

    dispatch = ToolDispatcher.dispatch

    async def dispatch_with_service_catalog(self, name, args, credentials, **kwargs):
        if isinstance(self._mcp_client, FakeMCPClient):
            client = self._mcp_client
            client._test_api_catalog = await resolve_catalog(self._catalog, credentials)
            if not hasattr(client, "_test_api_original_call"):
                client._test_api_original_call = client.call_tool

                async def wrapped(name, args, **kw):
                    return await api_call(client, name, args, **kw)

                monkeypatch.setattr(client, "call_tool", wrapped)
        return await dispatch(self, name, args, credentials, **kwargs)

    async def api_call(self, name, args, **kwargs):
        catalog = getattr(self, "_test_api_catalog", None)
        if name == "explainQuery" and not self._scripted.get(name) and catalog is not None:
            result = {"columns": ["explain"], "rows": [["Plan"]], "row_count": 1}
        else:
            result = await self._test_api_original_call(name, args, **kwargs)
        if name not in {"runQuery", "sampleRows", "explainQuery"} or catalog is None:
            return result
        if not isinstance(result, dict) or "provenance" in result:
            return result
        sql = args.get("sql", "")
        if name == "sampleRows":
            sql = f"SELECT * FROM {args.get('database', '')}.{args.get('table', '')}"
        try:
            uses = extract_column_provenance(
                sql,
                {t: dict(c) for t, c in catalog.schema.items()},
                session_id=kwargs["session_id"],
            )
        except ProvenanceExtractionError as exc:
            raise MCPToolError("PARSE_FAILED_CLOSED", str(exc)) from exc
        return {**result, "provenance": {"version": 1, "columns": [list(p) for p in sorted(uses)]}}

    monkeypatch.setattr(ToolDispatcher, "dispatch", dispatch_with_service_catalog)
