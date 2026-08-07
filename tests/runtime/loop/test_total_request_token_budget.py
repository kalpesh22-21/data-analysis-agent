"""Load-bearing regression (2026-08): the FULL assembled request handed to
`send_turn` is bounded by a TOTAL-request token budget, and the base prompt is
NEVER front-truncated out of the model window — even after many iterations that
pile MULTIPLE ~30k-token tool results into the trail so the RAW request exceeds
the model context window.

This is the scenario the prior base-prompt tests missed: they used tiny tool
results, so the raw request never actually exceeded the window and the missing
total-request budget was never exercised. Here each `runQuery` returns a fat
(~30k-token) result and the model never self-terminates, so the trail grows
unbounded across a turn's iterations/windows and the raw request blows past the
budget on every later round-trip — exactly the production trace (274k tokens at
80 turns vs a 128k window) that let the endpoint drop `messages[0]`.

Asserts, on EVERY main-loop `send_turn` payload (and specifically the deepest,
last one):
  1. messages[0] is the base prompt (system), byte-identical.
  2. total estimated tokens <= request_token_budget.
  3. the current (last) user question is present.
  4. no orphaned tool / assistant-tool_calls messages (pairing intact).
  5. trimming dropped the OLDEST entries (an early-turn result is gone; a recent
     one survives).
"""

from __future__ import annotations

import copy
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.budget import estimate_message_tokens
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle({_E: {"EmployeeCode": "String", "Department": "Nullable(String)"}})
SESSION_ID = "sess-total-request-budget"
SECRET_JWT = "jwt.body.sig"
_TOOLS = [{"type": "function", "name": "runQuery", "description": "", "parameters": {}}]

# A fat runQuery result: 20 wide rows so a single tool result is ~30k tokens —
# the "giant single tool result" the bug report measured, in the trail verbatim.
_WIDE_CELL = "lorem-ipsum-dolor-sit-amet-consectetur-" * 160  # ~6.2k chars
_REQUEST_TOKEN_BUDGET = 60_000
_ITERS_PER_WINDOW = 8
_MAX_WINDOWS = 3


def _fat_result() -> dict[str, Any]:
    return {
        "columns": ["EmployeeCode", "Department"],
        "rows": [[f"E{i}", _WIDE_CELL] for i in range(20)],
        "row_count": 20,
        "truncated": False,
    }


async def _tools_provider(_c: RuntimeCredentials) -> list[dict]:
    return list(_TOOLS)


def _creds() -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt=SECRET_JWT, column_scope=frozenset())


class _NeverTerminatingModel:
    """Records every `messages` payload and ALWAYS issues a fresh, distinct
    runQuery, so the turn runs to the hard ceiling and the trail piles up fat
    results the total-request budget must then trim."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self._n = 0

    async def send_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append(copy.deepcopy(messages))
        self._n += 1
        sql = f"SELECT EmployeeCode, Department FROM employee WHERE Department='D{self._n}'"
        return ModelTurnResult(
            tool_calls=[ToolCallRequest(id=f"c{self._n}", name="runQuery", arguments={"sql": sql})],
            usage={"total_tokens": 5000},
        )

    def begin_turn(self) -> _NeverTerminatingModel:
        return self


class _FatMCP:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, tool_name, args, *, jwt, session_id):
        self.calls.append(tool_name)
        return _fat_result()

    async def list_tools(self, *, jwt, session_id):
        return []


def _assert_pairing_intact(messages: list[dict[str, Any]]) -> None:
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                open_ids.add(tc["id"])
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_ids, (
                f"orphan tool message {m.get('tool_call_id')} without a preceding tool_calls"
            )
            open_ids.discard(m["tool_call_id"])
    assert not open_ids, f"assistant tool_calls left dangling without a tool result: {open_ids}"


def _tool_pair_sqls(messages: list[dict[str, Any]]) -> list[str]:
    """The SQL of every surviving assistant runQuery tool_call, in order."""
    sqls: list[str] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                sqls.append(tc["function"]["arguments"])
    return sqls


async def test_total_request_budget_pins_base_prompt_and_bounds_every_send_turn() -> None:
    model = _NeverTerminatingModel()
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(_FatMCP(), CATALOG)
    # history_token_budget HIGH so trail compaction does not pre-shrink the trail —
    # we are exercising the SEPARATE total-request fit, which must bound the whole
    # request even when the trail replays verbatim and grows unbounded.
    assembler = ContextAssembler(
        store,
        history_token_budget=10_000_000,
        base_system_prompt=AGENT_SYSTEM_PROMPT,
    )
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=_ITERS_PER_WINDOW,
        max_wall_clock_seconds=999,
        max_budget_windows=_MAX_WINDOWS,
        request_token_budget=_REQUEST_TOKEN_BUDGET,
    )

    statuses: list[str] = []
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="how many?")
    statuses.append(outcome.status)
    while outcome.status == "paused_budget_cap":
        outcome = await loop.resume(session_id=SESSION_ID, credentials=_creds(), answer="continue")
        statuses.append(outcome.status)

    # The turn really ran deep (cap -> resume -> ... -> hard ceiling).
    assert statuses[-1] == "stopped_hard_ceiling"
    assert len(model.calls) == _ITERS_PER_WINDOW * _MAX_WINDOWS

    # A single fat result is genuinely ~30k tokens — so a raw untrimmed request
    # with several of them dwarfs the budget (the scenario the fix must contain).
    assert estimate_message_tokens(
        {"role": "tool", "tool_call_id": "x", "content": str(_fat_result())}
    ) > 25_000

    for i, msgs in enumerate(model.calls):
        # (1) base prompt byte-identical at index 0 — NEVER dropped/front-truncated.
        assert msgs[0]["role"] == "system", f"call[{i}] messages[0] is not system"
        assert msgs[0]["content"] == AGENT_SYSTEM_PROMPT, (
            f"call[{i}] dropped/altered the base prompt at messages[0]"
        )
        # (2) total request within the budget.
        total = sum(estimate_message_tokens(m) for m in msgs)
        assert total <= _REQUEST_TOKEN_BUDGET, (
            f"call[{i}] total {total} exceeds budget {_REQUEST_TOKEN_BUDGET}"
        )
        # (3) the current question survives (pinned tail).
        assert any(
            m["role"] == "user" and m.get("content") == "how many?" for m in msgs
        ), f"call[{i}] dropped the current user question"
        # (4) pairing intact — no orphan tool / dangling tool_calls.
        _assert_pairing_intact(msgs)

    # (5) The DEEPEST call (last, largest raw trail) proves OLDEST-first trimming:
    # an early result is gone; a recent one survives. There were 24 fat results;
    # only a couple fit under the 60k budget alongside the base prompt + question.
    deepest = model.calls[-1]
    sqls = _tool_pair_sqls(deepest)
    assert sqls, "the deepest call kept no tool pairs at all"
    # The very first result (Department='D1') was dropped as oldest...
    assert not any("'D1'" in s for s in sqls), "oldest result was NOT trimmed"
    # ...and a recent result survived (the most recent runQuery the model issued
    # right before this rebuild).
    recent_marker = f"'D{len(model.calls) - 1}'"
    assert any(recent_marker in s for s in sqls), (
        f"the most recent result ({recent_marker}) did not survive the trim"
    )
    # Far fewer pairs survived than were produced — the request really was trimmed.
    assert len(sqls) < len(model.calls)
