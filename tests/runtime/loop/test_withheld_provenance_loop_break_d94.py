"""D94 Part 1 (end-to-end) — the `ok`+`None` provenance sentinel actually BREAKS
the retry-until-budget-cap loop, driven through the real `AgentLoop`.

Mirrors the adversarial loop harnesses
(`tests/runtime/loop/test_current_turn_scope_leak_adversarial.py`,
`tests/runtime/loop/test_budget_termination_adversarial.py`): a real
`ToolDispatcher` + `FakeMCPClient` + `ContextAssembler`, and a model double.

The stranded condition is produced by a REAL dispatch path (not a hand-built
trail entry): a `sampleRows` against a table absent from the `CatalogHandle`.
The MCP itself does not column-scope `sampleRows`, so the call returns
`status="ok"`; the runtime's declarative provenance capture then finds the table
uncatalogued and records `provenance=None` (`provenance/capture.py:92-94`). That
is exactly the catalog/extractor skew D94 addresses.

The model double faithfully MODELS the production hang: on every round-trip it
re-emits the identical `sampleRows` call UNLESS it can see, in the messages it
was handed, a tool result for that call id carrying the withheld sentinel — in
which case it stops with a final answer. Before D94, the sentinel would never
appear, so this model spins to the budget cap; with D94 it terminates via the
normal `done` path. This is the load-bearing "loop breaks" assertion.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

# The catalog knows `employee` but NOT `ghost_table` — a sampleRows against the
# latter succeeds at the MCP yet yields undetermined (None) provenance.
_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String"}})

SESSION_ID = "sess-d94-loop-break"
JWT = "jwt-not-under-test"

_SENTINEL_FRAGMENT = "result withheld: provenance could not be determined"
_PII = "PII_ROW_VALUE_do_not_surface"

TOOLS_SCHEMA = [
    {"type": "function", "name": "sampleRows", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


class _RetryUntilSentinelModel:
    """Models the real hang: re-emit the identical `sampleRows(ghost_table)`
    call every round-trip UNTIL a tool result carrying the withheld sentinel for
    that call id is visible in the handed-in messages, then stop with an answer.

    Without the D94 sentinel this NEVER stops on its own — the loop's budget cap
    is the only thing that would (which is precisely the bug). With the sentinel,
    it terminates via the normal `done` path.
    """

    _CALL_ID = "call_ghost"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        # Did the model receive a withheld sentinel for its dangling call?
        saw_sentinel = any(
            m.get("role") == "tool"
            and m.get("tool_call_id") == self._CALL_ID
            and isinstance(m.get("content"), str)
            and _SENTINEL_FRAGMENT in m["content"]
            for m in messages
        )
        if saw_sentinel:
            return ModelTurnResult(
                assistant_text="That result is withheld; I'll ask the user instead.",
                usage={"total_tokens": 1},
            )
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=self._CALL_ID,
                    name="sampleRows",
                    arguments={"database": "dbpcm_warehouse", "table": "ghost_table"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _RetryUntilSentinelModel:
        return self


def _build_loop(model: Any, mcp: FakeMCPClient) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=10,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
    )
    return loop, store


def _ghost_mcp() -> FakeMCPClient:
    # Enough scripted responses that, absent the fix, the loop could spin for a
    # long time; the sentinel must stop it well before these run out.
    return FakeMCPClient(
        scripted={
            "sampleRows": [
                {
                    "columns": ["EmployeeCode", "Name"],
                    "rows": [["E1", _PII]],
                    "row_count": 1,
                    "truncated": False,
                }
                for _ in range(20)
            ]
        }
    )


async def test_sentinel_breaks_retry_loop_and_terminates_normally() -> None:
    model = _RetryUntilSentinelModel()
    loop, store = _build_loop(model, _ghost_mcp())

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Sample the ghost table."
    )

    # Terminated via the NORMAL done path — NOT the budget cap / hard ceiling.
    assert outcome.status == "done"
    assert outcome.status not in {"paused_budget_cap", "stopped_hard_ceiling"}

    # The stranded entry really is ok+None (the skew condition under test).
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    assert trail[0].status == "ok"
    assert trail[0].provenance is None

    # It broke fast: exactly two model round-trips (emit call, then see sentinel
    # and stop) — nowhere near max_budget_windows * max_loop_iterations.
    assert len(model.calls) == 2


async def test_second_round_trip_context_contains_the_sentinel_for_the_dangling_call() -> None:
    """The dangling `tool_call` now has a matching tool result — the sentinel —
    in the exact slot the model re-inferred a missing result before."""
    model = _RetryUntilSentinelModel()
    loop, _ = _build_loop(model, _ghost_mcp())
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    assert len(model.calls) >= 2
    second_ctx = model.calls[1]["messages"]
    sentinel_tool_msgs = [
        m
        for m in second_ctx
        if m.get("role") == "tool"
        and m.get("tool_call_id") == "call_ghost"
        and isinstance(m.get("content"), str)
        and _SENTINEL_FRAGMENT in m["content"]
    ]
    assert len(sentinel_tool_msgs) == 1

    # And the PII rows the stranded (ok+None) result carried never leaked.
    blob = json.dumps(second_ctx, default=str, ensure_ascii=False)
    assert _PII not in blob


async def test_every_assistant_tool_call_has_a_matching_tool_result() -> None:
    """The OpenAI message-pairing invariant the sentinel exists to preserve:
    across every model round-trip, each assistant `tool_call` id has exactly one
    matching `tool` result and there are no orphan tool results."""
    model = _RetryUntilSentinelModel()
    loop, _ = _build_loop(model, _ghost_mcp())
    await loop.run(session_id=SESSION_ID, credentials=_credentials(), user_message="go")

    for recorded in model.calls:
        messages = recorded["messages"]
        assistant_ids: list[str] = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assistant_ids.extend(tc["id"] for tc in m["tool_calls"])
        tool_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        assert sorted(assistant_ids) == sorted(tool_ids), messages
        assert len(tool_ids) == len(set(tool_ids))
