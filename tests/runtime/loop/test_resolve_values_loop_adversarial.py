"""Adversarial loop-level tests for the D77 resolveValues interception.

Covers gaps beyond the happy-path loop tests in `test_agent_loop.py`:
  - resolveValues mixed with a normal tool call in ONE model response
    (ordering, per-entry provenance, no double-count of the inner runQuery);
  - resolveValues at / past the per-iteration cap boundary;
  - the resolveValues tool result is appended to the NEXT model call in the
    canonical tool-message shape the model client expects;
  - D44 replay: a resolveValues trail entry is dropped on a later turn whose
    scope excludes a resolved column, kept under allow-all / full scope.
"""

from __future__ import annotations

import json

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.scope_filter import filter_trail
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_T = "dbpcm_warehouse.accrual_events"
CATALOG = CatalogHandle(
    {_T: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}}
)

SECRET_JWT = "eyJhbGciOi.super-secret-jwt-body.sig"
SESSION_ID = "sess-rv-loop-adv"

TOOLS_SCHEMA = [
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "resolveValues", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=scope)


def _rq_result() -> dict:
    return {
        "columns": ["EarnCode", "EarnDescription", "freq"],
        "rows": [["PTO", "paid time off", 10], ["OT", "overtime", 3]],
        "row_count": 2,
        "truncated": False,
    }


def _loop(
    *,
    model_client: ScriptedModelClient,
    mcp_client: FakeMCPClient,
    max_tool_calls_per_iteration: int = 8,
) -> tuple[AgentLoop, InMemorySessionStore]:
    from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
    from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp_client, CATALOG)
    assembler = ContextAssembler(store, history_token_budget=100_000)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=CATALOG,
        embedding_client=FakeEmbeddingClient(dim=2),
    )
    loop = AgentLoop(
        model_client=model_client,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        max_tool_calls_per_iteration=max_tool_calls_per_iteration,
        resolve_values=composite,
    )
    return loop, store


# ---------------------------------------------------------------------------
# resolveValues + a normal tool call in the SAME model response
# ---------------------------------------------------------------------------


async def test_resolve_values_mixed_with_run_query_same_response() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _T, "column": "EarnCode", "concept": "leave"},
                    ),
                    ToolCallRequest(
                        id="rq_1",
                        name="runQuery",
                        arguments={"sql": "SELECT EarnCode FROM dbpcm_warehouse.accrual_events"},
                    ),
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    # Two runQuery responses: [0] the composite's inner query, [1] the direct one.
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                _rq_result(),
                {"columns": ["EarnCode"], "rows": [["PTO"]], "row_count": 1, "truncated": False},
            ]
        }
    )
    loop, store = _loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")
    assert outcome.status == "done"
    # Both calls counted; the inner runQuery is NOT double-counted.
    assert outcome.tool_calls_made == 2
    # Ordering preserved: resolveValues' inner query fires before the direct one.
    assert [c.tool_name for c in mcp.calls] == ["runQuery", "runQuery"]

    trail = await store.load_trail(SESSION_ID)
    assert [e.tool_name for e in trail] == ["resolveValues", "runQuery"]
    # Each entry carries its OWN provenance.
    assert trail[0].provenance == frozenset({(_T, "EarnCode"), (_T, "EarnDescription")})
    assert trail[1].provenance == frozenset({(_T, "EarnCode")})


# ---------------------------------------------------------------------------
# Per-iteration cap boundary
# ---------------------------------------------------------------------------


async def test_resolve_values_as_eighth_call_is_dispatched_at_cap() -> None:
    calls = [
        ToolCallRequest(
            id=f"rq_{i}",
            name="runQuery",
            arguments={"sql": "SELECT EarnCode FROM dbpcm_warehouse.accrual_events"},
        )
        for i in range(7)
    ]
    calls.append(
        ToolCallRequest(
            id="rv_8",
            name="resolveValues",
            arguments={"table": _T, "column": "EarnCode", "concept": "leave"},
        )
    )
    model = ScriptedModelClient(
        [ModelTurnResult(tool_calls=calls), ModelTurnResult(assistant_text="done")]
    )
    # 7 direct runQuery + 1 inner runQuery from resolveValues = 8 responses.
    plain = {"columns": ["EarnCode"], "rows": [["PTO"]], "row_count": 1, "truncated": False}
    mcp = FakeMCPClient(scripted={"runQuery": [plain] * 7 + [_rq_result()]})
    loop, store = _loop(model_client=model, mcp_client=mcp, max_tool_calls_per_iteration=8)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")
    assert outcome.tool_calls_made == 8
    trail = await store.load_trail(SESSION_ID)
    assert trail[-1].tool_name == "resolveValues"
    assert trail[-1].status == "ok"


async def test_resolve_values_past_cap_is_dropped_not_dispatched() -> None:
    calls = [
        ToolCallRequest(
            id=f"rq_{i}",
            name="runQuery",
            arguments={"sql": "SELECT EarnCode FROM dbpcm_warehouse.accrual_events"},
        )
        for i in range(8)
    ]
    calls.append(
        ToolCallRequest(
            id="rv_9",
            name="resolveValues",
            arguments={"table": _T, "column": "EarnCode", "concept": "leave"},
        )
    )
    model = ScriptedModelClient(
        [ModelTurnResult(tool_calls=calls), ModelTurnResult(assistant_text="done")]
    )
    plain = {"columns": ["EarnCode"], "rows": [["PTO"]], "row_count": 1, "truncated": False}
    # Only the 8 direct runQuery responses — the 9th (resolveValues) is capped
    # out, so its inner runQuery must NEVER fire (no scripted response for it).
    mcp = FakeMCPClient(scripted={"runQuery": [plain] * 8})
    loop, store = _loop(model_client=model, mcp_client=mcp, max_tool_calls_per_iteration=8)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")
    assert outcome.tool_calls_made == 8
    trail = await store.load_trail(SESSION_ID)
    assert all(e.tool_name == "runQuery" for e in trail)
    assert len(mcp.calls) == 8  # the capped resolveValues never issued its inner query


# ---------------------------------------------------------------------------
# Result appended to the next model call in the expected shape
# ---------------------------------------------------------------------------


async def test_resolve_values_result_appended_to_next_model_call() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _T, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="PTO is the code."),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq_result()]})
    loop, _ = _loop(model_client=model, mcp_client=mcp)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")

    # The SECOND model call must include the resolveValues tool result, in the
    # canonical assistant-tool_calls / tool-result pair shape.
    second_call_messages = model.calls[1].messages
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any(m.get("tool_call_id") == "rv_1" for m in tool_messages)
    rv_tool_message = next(m for m in tool_messages if m["tool_call_id"] == "rv_1")
    payload = json.loads(rv_tool_message["content"])
    assert payload["status"] == "ok"
    # The ranked contract-shaped preview rides on result_preview.
    assert payload["result_preview"] is not None
    preview_blob = json.dumps(payload["result_preview"])
    assert "PTO" in preview_blob


async def test_resolve_values_inner_denial_persists_denied_entry_no_pause() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _T, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="I could not access that."),
        ]
    )
    mcp = FakeMCPClient(
        scripted={"runQuery": [MCPToolError("COLUMN_SCOPE_VIOLATION", "[COLUMN_SCOPE_VIOLATION] no")]}
    )
    loop, store = _loop(model_client=model, mcp_client=mcp)

    outcome = await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")
    assert outcome.status == "done"  # inline, not a pause
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    assert trail[0].tool_name == "resolveValues"
    assert trail[0].status == "denied"
    assert trail[0].error_code == "COLUMN_SCOPE_VIOLATION"
    assert trail[0].provenance is None  # fail-closed for D44


# ---------------------------------------------------------------------------
# D44 replay drop on a later turn whose scope excludes a resolved column
# ---------------------------------------------------------------------------


async def test_resolve_values_entry_dropped_by_d44_when_scope_narrows() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id="rv_1",
                        name="resolveValues",
                        arguments={"table": _T, "column": "EarnCode", "concept": "leave"},
                    )
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"runQuery": [_rq_result()]})
    loop, store = _loop(model_client=model, mcp_client=mcp)

    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="q")
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    entry = trail[0]
    assert entry.provenance == frozenset({(_T, "EarnCode"), (_T, "EarnDescription")})

    full_scope = frozenset({f"{_T}.EarnCode", f"{_T}.EarnDescription"})
    narrow_scope = frozenset({f"{_T}.EarnCode"})  # excludes the resolved desc col

    # Allow-all and full scope replay the entry; a scope missing EarnDescription
    # drops it entirely (D44 — resolveValues provenance is the inner query's).
    assert filter_trail(trail, frozenset()) == trail
    assert filter_trail(trail, full_scope) == trail
    assert filter_trail(trail, narrow_scope) == []
