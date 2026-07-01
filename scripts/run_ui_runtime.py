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
    - `mcp_client`    -> `DemoMCPClient` (below), a thin subclass of
      `data_agent.runtime.mcp.fake_client.FakeMCPClient`, scripted with a
      repeated `getTableSchema` response (also for the same "must survive
      many demo turns" reason — a `FakeMCPClient` scripted with exactly one
      response would raise `AssertionError` on the second call). `runQuery`
      is NOT scripted through the normal FIFO queue: `FakeMCPClient` forks
      purely on tool name + call order, never on `args`, so it cannot itself
      express "the SAME tool, called with different SQL, denied for
      different reasons" — which the scope-denial (D57) and
      parser-fail-closed (D63) conformance scenarios both need from the one
      `runQuery` tool. `DemoMCPClient` overrides `call_tool` to content-route
      `runQuery` off the submitted `query` string instead (see its
      docstring); every other tool falls through to the base class unchanged.
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

Trigger phrases (content-routed on the FIRST user message of the turn,
case-insensitive substring match — see `DemoModelClient.send_turn`):

    | Phrase contains...      | Scenario                                    |
    |--------------------------|---------------------------------------------|
    | "ask"                    | clarify: askUser -> chip options -> resume   |
    | "keep going" / "forever" | budget-cap: never finishes -> paused_budget_cap |
    | "salaries" / "salary"    | scope-denial (D57): COLUMN_SCOPE_VIOLATION   |
    | "raw sql"                | parser-fail-closed (D63): PARSE_FAILED_CLOSED |
    | (anything else)          | normal: getTableSchema -> final answer       |

`RuntimeSettings.max_loop_iterations` is deliberately set LOW (3) below so
the budget-cap scenario is reachable in a handful of demo turns without a
real wall-clock wait; the normal/clarify/denial/parse-fail scenarios each
only ever consume a single loop iteration, so they complete well under that
cap regardless.

Run:
    uv run python scripts/run_ui_runtime.py
    # listens on :8000 (same port/shape the real runtime uses)
"""

from __future__ import annotations

from typing import Any

import uvicorn

from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolError, MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient, RecordedCall
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


# Sentinel substrings `DemoModelClient` embeds into the `runQuery` SQL it
# emits, and `DemoMCPClient.call_tool` (below) matches on, to content-route
# the SAME tool name to two different denial codes (see `DemoMCPClient`'s
# docstring for why a plain FIFO-scripted `FakeMCPClient` cannot do this on
# its own).
_SCOPE_DENIAL_QUERY = f"SELECT AnnualSalary FROM {_DEMO_DATABASE}.{_DEMO_TABLE}"
_PARSE_FAIL_QUERY = "RAW_SQL_DEMO -- ; DROP TABLE employees; --"


class DemoModelClient:
    """A repeatable, content-routed `ModelClient` double for interactive demos.

    Reads the ALREADY-BUILT canonical message list the loop hands it (same
    shape `ScriptedModelClient` receives — see `model/client.py`) and decides
    its canned response purely from that shape, so it needs no external
    per-session state and never runs out of "script". Routed off the
    ORIGINAL (first) user message of the turn (case-insensitive substring
    match — see the module docstring's trigger-phrase table):

      - "ask" -> demo the askUser/chip flow.
          * 1st call this turn (only one user message seen so far) -> emit an
            `askUser` tool call with a couple of chip `options`.
          * 2nd call (a resume already appended the user's answer -> two user
            messages now) -> final answer that echoes the chosen option.
      - "keep going" / "forever" -> demo the budget-cap pause (D47).
          * Every call THIS window (still only one user message seen) -> emit
            another `getTableSchema` tool call, never a final answer, so the
            loop's `BudgetGuard` is the only thing that can end the window.
          * Once resumed with "continue"/"refine" (a second user message is
            now present, appended by `AgentLoop.resume`/D55's fresh window) ->
            a final answer immediately, so the turn ends cleanly on the very
            first call of the new window.
      - "salaries" / "salary" -> demo a column-scope denial (D57).
          * 1st call (no `tool` messages yet) -> emit a `runQuery` tool call
            whose SQL is `_SCOPE_DENIAL_QUERY` (matched by `DemoMCPClient`).
          * 2nd call (the denied tool result was replayed back) -> a graceful
            final answer that surfaces the denial in plain language — never
            raw data (there is none: the MCP denied the call before any rows
            existed).
      - "raw sql" -> demo a fail-closed parser rejection (D63).
          * 1st call (no `tool` messages yet) -> emit a `runQuery` tool call
            whose SQL is `_PARSE_FAIL_QUERY` (matched by `DemoMCPClient`).
          * 2nd call (the denied tool result was replayed back) -> a graceful
            "couldn't validate that query" final answer — rejected, not
            crashed.
      - Otherwise -> demo an ordinary tool-call turn.
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
        first_lower = first_user_text.lower()

        if "ask" in first_lower:
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

        if "keep going" in first_lower or "forever" in first_lower:
            if len(user_messages) >= 2:
                # Resumed into a fresh budget window (D55) -> finish cleanly
                # on the first call of the new window rather than looping
                # forever again.
                return ModelTurnResult(
                    assistant_text=(
                        "Wrapping up after the extra budget window — here is a "
                        "placeholder summary of what was found so far. "
                        "(Placeholder answer — scripted demo runtime.)"
                    )
                )
            call_id = f"call-keepgoing-{len(tool_messages) // 2 + 1}"
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id=call_id,
                        name="getTableSchema",
                        arguments={"database": _DEMO_DATABASE, "table": _DEMO_TABLE},
                    )
                ]
            )

        if "salaries" in first_lower or "salary" in first_lower:
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-scope-1",
                            name="runQuery",
                            arguments={"query": _SCOPE_DENIAL_QUERY},
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "I can't access those columns — that needs access outside your "
                    "current column scope. I can share a summary of the columns you "
                    "do have access to instead, if that helps."
                )
            )

        if "raw sql" in first_lower:
            if not tool_messages:
                return ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(
                            id="call-parsefail-1",
                            name="runQuery",
                            arguments={"query": _PARSE_FAIL_QUERY},
                        )
                    ]
                )
            return ModelTurnResult(
                assistant_text=(
                    "I couldn't validate that query safely, so I didn't run it. "
                    "Could you rephrase it as a normal question instead?"
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


class DemoMCPClient(FakeMCPClient):
    """`FakeMCPClient`, extended so `runQuery` denials are content-routed off
    the submitted SQL string rather than a fixed FIFO queue.

    `FakeMCPClient.call_tool` forks purely on `(tool_name, call order)` — it
    has no visibility into `args` at all — so it cannot express "the SAME
    tool, called with different SQL, must be denied for two different
    reasons depending on what the SQL says", which the scope-denial (D57)
    and parser-fail-closed (D63) conformance scenarios both need from one
    `runQuery` tool. This override intercepts `runQuery` specifically and
    matches its `query` argument against the two sentinel strings
    `DemoModelClient` emits (`_SCOPE_DENIAL_QUERY`/`_PARSE_FAIL_QUERY`);
    every other tool call (including any `runQuery` call that matches
    neither sentinel) falls straight through to the base class's normal
    scripted-queue behavior unchanged.

    This is demo-launcher plumbing ONLY — the real MCP derives these same
    denial codes from its actual scope-check/parser, never from string
    matching; nothing here is a substitute for `dispatch/denial_mapping.py`
    or the adopted MCP's own enforcement.
    """

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        jwt: str,
        session_id: str,
    ) -> dict[str, Any] | list[Any]:
        if tool_name == "runQuery":
            query = str(args.get("query") or "")
            if _SCOPE_DENIAL_QUERY in query:
                self.calls.append(
                    RecordedCall(tool_name=tool_name, args=dict(args), jwt=jwt, session_id=session_id)
                )
                raise MCPToolError(
                    "COLUMN_SCOPE_VIOLATION",
                    "[COLUMN_SCOPE_VIOLATION] column 'AnnualSalary' is outside the "
                    "caller's column scope",
                )
            if _PARSE_FAIL_QUERY in query:
                self.calls.append(
                    RecordedCall(tool_name=tool_name, args=dict(args), jwt=jwt, session_id=session_id)
                )
                raise MCPToolError(
                    "PARSE_FAILED_CLOSED",
                    "[PARSE_FAILED_CLOSED] could not parse/validate the submitted SQL",
                )
        return await super().call_tool(tool_name, args, jwt=jwt, session_id=session_id)


def build_demo_app():
    settings = RuntimeSettings(
        jwks_url=_JWKS_URL,
        jwt_issuer=_TOKEN_ISSUER,
        jwt_audience=_TOKEN_AUDIENCE,
        # Deliberately LOW (module docstring): makes the budget-cap
        # conformance scenario ("keep going forever") reachable in exactly 3
        # demo tool-call iterations instead of a real 15-round wait, while
        # every other scenario here (normal/clarify/denial/parse-fail) only
        # ever consumes a single iteration and so still completes well under
        # this cap.
        max_loop_iterations=3,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )

    catalog = CatalogHandle({f"{_DEMO_DATABASE}.{_DEMO_TABLE}": _DEMO_SCHEMA_RESPONSE["columns"]})

    mcp_client = DemoMCPClient(
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
            ),
            MCPToolSpec(
                name="runQuery",
                description="Run a read-only SQL query against the warehouse.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
        ],
        # Repeated 200x so this launcher survives many demo turns/sessions
        # without exhausting FakeMCPClient's per-tool response queue (see
        # module docstring). `runQuery` is NOT scripted here — DemoMCPClient
        # intercepts it directly (see its docstring).
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
