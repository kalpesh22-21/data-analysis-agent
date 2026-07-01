"""Dev launcher: the Phase-0 agent runtime (`create_app`), wired to Layer-1
fakes only, so the minimal UI (`ui/`) can be exercised end-to-end WITHOUT an
OpenAI API key, Couchbase, or the real ClickHouse MCP.

Wiring (mirrors `tests/runtime/test_app.py::_build_client`, but as a live
uvicorn process rather than a `TestClient`):

    - `model_client`  -> `DemoModelClient` (below) — a small, content-routed
      `ModelClient` double. NOT `ScriptedModelClient` verbatim: that class is
      a single-use, strictly-ordered cassette (`AssertionError` once its
      script is exhausted) — perfect for one pytest assertion, but this
      launcher needs to serve an UNBOUNDED number of demo turns, from any
      number of browser sessions, in either order. `DemoModelClient`
      reproduces the same `ModelClient` Protocol (`send_turn`/`begin_turn`)
      and the same `ModelTurnResult`/`ToolCallRequest` DTOs, but decides its
      canned response by inspecting the canonical message shape the loop
      hands it (see its docstring) instead of popping a fixed queue — so it
      can be replayed indefinitely.
    - `mcp_client`    -> `data_agent.runtime.mcp.fake_client.FakeMCPClient`,
      scripted with a repeated `getTableSchema` response (also for the same
      "must survive many demo turns" reason — a `FakeMCPClient` scripted with
      exactly one response would raise `AssertionError` on the second call).
    - `session_store` -> `data_agent.runtime.session.memory_store.InMemorySessionStore`.
    - `catalog`       -> a small hand-built `CatalogHandle` with one table
      (`analytics.employees`) so `getTableSchema`'s provenance capture has
      something real to resolve.

JWT verification is NOT bypassed/monkeypatched: `RuntimeSettings.jwks_url` /
`jwt_issuer` / `jwt_audience` are pointed at the already-running `l2-token`
container's real JWKS endpoint (`docker-compose.integration.yml`, unmodified,
untouched) — so a JWT minted by `ui/server.py` via the real token service is
ALSO really verified here, exactly like production. Only the model provider
and the ClickHouse MCP are faked (no OpenAI key, no live ClickHouse needed).

Run:
    uv run python scripts/run_ui_runtime.py
    # listens on :8000 (same port/shape the real runtime uses)
"""

from __future__ import annotations

from typing import Any

import uvicorn

from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

# The real l2-token container (docker-compose.integration.yml), already
# running — see docker-compose.integration.yml's `token` service. We only
# READ its JWKS/issue endpoints here; nothing about the stack is modified.
_TOKEN_ISSUER = "http://token:8000/"  # must match the token the JWT's "iss" claim carries
_TOKEN_AUDIENCE = "clickhouse-api"
_JWKS_URL = "http://localhost:19000/.well-known/jwks.json"

_DEMO_DATABASE = "analytics"
_DEMO_TABLE = "employees"
_DEMO_SCHEMA_RESPONSE: dict[str, Any] = {
    "database": _DEMO_DATABASE,
    "table": _DEMO_TABLE,
    "columns": {
        "id": "UInt64",
        "name": "String",
        "department": "String",
        "salary": "Decimal64(2)",
    },
}


class DemoModelClient:
    """A repeatable, content-routed `ModelClient` double for interactive demos.

    Reads the ALREADY-BUILT canonical message list the loop hands it (same
    shape `ScriptedModelClient` receives — see `model/client.py`) and decides
    its canned response purely from that shape, so it needs no external
    per-session state and never runs out of "script":

      - If the ORIGINAL (first) user message of the turn contains "ask"
        (case-insensitive): demo the askUser/chip flow.
          * 1st call this turn (only one user message seen so far) -> emit an
            `askUser` tool call with a couple of chip `options`.
          * 2nd call (a resume already appended the user's answer -> two user
            messages now) -> final answer that echoes the chosen option.
      - Otherwise: demo an ordinary tool-call turn.
          * 1st call (no `tool` messages yet) -> emit a `getTableSchema`
            tool call.
          * 2nd call (a `tool` role message is now present, i.e. the
            dispatched result was replayed back) -> final free-text answer.
    """

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        user_messages = [m for m in messages if m.get("role") == "user"]
        tool_messages = [m for m in messages if m.get("role") == "tool"]

        if not user_messages:
            return ModelTurnResult(assistant_text="I didn't receive a message to respond to.")

        first_user_text = str(user_messages[0].get("content") or "")

        if "ask" in first_user_text.lower():
            if len(user_messages) == 1:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-ask-1",
                            name="askUser",
                            arguments={
                                "question": "Which department should I focus the analysis on?",
                                "options": ["Sales", "Engineering", "Support"],
                            },
                        )
                    ]
                )
            answer_text = str(user_messages[-1].get("content") or "").strip()
            return ModelTurnResult(
                assistant_text=(
                    f"Got it — focusing on {answer_text or 'that'}. "
                    f"Here is a placeholder summary for the {answer_text or 'chosen'} department."
                )
            )

        if not tool_messages:
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="call-schema-1",
                        name="getTableSchema",
                        arguments={"database": _DEMO_DATABASE, "table": _DEMO_TABLE},
                    )
                ]
            )

        return ModelTurnResult(
            assistant_text=(
                f"The `{_DEMO_DATABASE}.{_DEMO_TABLE}` table has columns "
                f"{', '.join(_DEMO_SCHEMA_RESPONSE['columns'])}. "
                "(Placeholder answer — scripted demo runtime, no real ClickHouse query ran.)"
            )
        )

    def begin_turn(self) -> DemoModelClient:
        # No per-turn state to isolate (mirrors ScriptedModelClient's own
        # degenerate case, model/client.py's `begin_turn` docstring).
        return self


def build_demo_app():
    settings = RuntimeSettings(
        jwks_url=_JWKS_URL,
        jwt_issuer=_TOKEN_ISSUER,
        jwt_audience=_TOKEN_AUDIENCE,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )

    catalog = CatalogHandle({f"{_DEMO_DATABASE}.{_DEMO_TABLE}": _DEMO_SCHEMA_RESPONSE["columns"]})

    mcp_client = FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="getTableSchema",
                description="Return the column schema of a database.table.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "database": {"type": "string"},
                        "table": {"type": "string"},
                    },
                    "required": ["database", "table"],
                },
            )
        ],
        # Repeated 200x so this launcher survives many demo turns/sessions
        # without exhausting FakeMCPClient's per-tool response queue (see
        # module docstring).
        scripted={"getTableSchema": [dict(_DEMO_SCHEMA_RESPONSE) for _ in range(200)]},
    )

    return create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=mcp_client,
        model_client=DemoModelClient(),
        catalog=catalog,
    )


app = build_demo_app()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
