"""Repeated-idempotent-read guard (generalizes D94 to "identical repeat of an
already-served read") — driven end-to-end through the real `AgentLoop`.

The cold-start deadlock this fixes: a model re-issues an identical idempotent read
(`getTableSchema(employee)` dozens of times, zero `runQuery`) and spins to the
budget hard ceiling. D94's ok+None sentinel never fires because `getTableSchema`
has DETERMINED provenance. This guard declines to re-dispatch such a repeat and
injects a data-free "you already have this, proceed" nudge into the dangling
tool-slot — reusing the exact D94 withheld-sentinel machinery, only the sentinel
TEXT branches.

Mirrors `tests/runtime/loop/test_withheld_provenance_loop_break_d94.py`: a real
`ToolDispatcher` + `FakeMCPClient` + `ContextAssembler`, and a model double.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import (
    _REPEATED_IDEMPOTENT_READ_NUDGE,
    IDEMPOTENT_READ_ALREADY_SERVED_CODE,
    ContextAssembler,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop, _repeated_read_guard_event
from data_agent.runtime.mcp.client import MCPToolError
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

CATALOG = CatalogHandle(
    {
        "dbpcm_warehouse.employee": {"EmployeeCode": "String"},
        "dbpcm_warehouse.payroll": {"Amount": "Float64"},
    }
)

SESSION_ID = "sess-repeated-read"
JWT = "jwt-not-under-test"

# Assert against the actual nudge constant so wording tweaks never break these tests.
_NUDGE_FRAGMENT = _REPEATED_IDEMPOTENT_READ_NUDGE
_WITHHELD_FRAGMENT = "result withheld: provenance could not be determined"

TOOLS_SCHEMA = [
    {"type": "function", "name": "getTableSchema", "description": "", "parameters": {}},
    {"type": "function", "name": "runQuery", "description": "", "parameters": {}},
    {"type": "function", "name": "sampleRows", "description": "", "parameters": {}},
]


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return list(TOOLS_SCHEMA)


def _credentials() -> RuntimeCredentials:
    # Empty scope == "no restriction": a determined-provenance served read stays
    # in scope (visible), while an ok+None entry is always dropped/stranded.
    return RuntimeCredentials(session_id=SESSION_ID, jwt=JWT, column_scope=frozenset())


def _sees_nudge(messages: list[dict[str, Any]]) -> bool:
    return any(
        m.get("role") == "tool"
        and isinstance(m.get("content"), str)
        and _NUDGE_FRAGMENT in m["content"]
        for m in messages
    )


def _schema_response() -> dict[str, Any]:
    return {"database": "dbpcm_warehouse", "table": "employee", "columns": ["EmployeeCode"]}


def _query_response() -> dict[str, Any]:
    return {"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False}


def _build_loop(
    model: Any,
    mcp: FakeMCPClient,
    *,
    max_loop_iterations: int = 6,
    max_budget_windows: int = 2,
) -> tuple[AgentLoop, InMemorySessionStore]:
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=max_loop_iterations,
        max_wall_clock_seconds=60,
        max_budget_windows=max_budget_windows,
    )
    return loop, store


def _mcp_call_count(mcp: FakeMCPClient, tool_name: str) -> int:
    return sum(1 for c in mcp.calls if c.tool_name == tool_name)


def _guard_entries(trail: list[Any]) -> list[Any]:
    return [
        e
        for e in trail
        if e.status == "ok" and e.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE
    ]


class _RepeatSchemaModel:
    """Re-emit `getTableSchema(employee)` each round-trip UNTIL the repeated-read
    nudge for the repeat call is visible, then stop with a final answer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if _sees_nudge(messages):
            return ModelTurnResult(
                assistant_text="Using the schema I already have.", usage={"total_tokens": 1}
            )
        self._n += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=f"gts_{self._n}",
                    name="getTableSchema",
                    arguments={"database": "dbpcm_warehouse", "table": "employee"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _RepeatSchemaModel:
        return self


async def test_repeat_read_is_not_redispatched_and_nudge_terminates_the_loop() -> None:
    model = _RepeatSchemaModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response() for _ in range(20)]})
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe employee."
    )

    # Terminated via the NORMAL done path — not the budget cap / hard ceiling.
    assert outcome.status == "done"

    # The repeat was NEVER re-dispatched: the MCP saw getTableSchema exactly once.
    assert _mcp_call_count(mcp, "getTableSchema") == 1

    # Exactly one data-free guard entry persisted for the repeat.
    trail = await store.load_trail(SESSION_ID)
    guards = _guard_entries(trail)
    assert len(guards) == 1
    assert guards[0].result_preview is None
    assert guards[0].provenance is None

    # The model actually received the nudge (that is what let it stop): emit,
    # repeat (guarded), then see nudge and answer.
    assert len(model.calls) == 3
    assert _sees_nudge(model.calls[-1]["messages"])


async def test_seeding_catches_repeat_across_a_budget_window_resume() -> None:
    """The guard's `seen_read_calls` is turn-window-local, so a fresh budget
    window starts with an EMPTY in-memory set — only seeding it from the persisted
    trail lets it recognize a read served in a prior window. With
    max_loop_iterations=1, window 1 serves the read and pauses BEFORE any repeat,
    so the window-2 catch can ONLY come from seeding."""
    model = _RepeatSchemaModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response() for _ in range(20)]})
    loop, store = _build_loop(model, mcp, max_loop_iterations=1, max_budget_windows=3)

    # Window 1: serve exactly one getTableSchema, then hit the per-window cap.
    first = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe employee."
    )
    assert first.status == "paused_budget_cap"
    assert _mcp_call_count(mcp, "getTableSchema") == 1
    assert _guard_entries(await store.load_trail(SESSION_ID)) == []

    # Window 2 (fresh `_run_loop`, fresh in-memory set): the model repeats the
    # identical read. Only trail-seeding can catch it.
    second = await loop.resume(
        session_id=SESSION_ID, credentials=_credentials(), answer="continue"
    )
    assert second.status == "paused_budget_cap"

    # Still exactly one MCP dispatch — the window-2 repeat was guarded via seeding.
    assert _mcp_call_count(mcp, "getTableSchema") == 1
    assert len(_guard_entries(await store.load_trail(SESSION_ID))) == 1


class _TwoTablesModel:
    """Emit `getTableSchema(employee)` then `getTableSchema(payroll)` — a
    DIFFERENT-args call, which must be dispatched normally (no false positive)."""

    _SEQ = (("employee", "gts_x"), ("payroll", "gts_y"))

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._i = 0

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if self._i >= len(self._SEQ):
            return ModelTurnResult(assistant_text="Got both schemas.", usage={"total_tokens": 1})
        table, call_id = self._SEQ[self._i]
        self._i += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=call_id,
                    name="getTableSchema",
                    arguments={"database": "dbpcm_warehouse", "table": table},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _TwoTablesModel:
        return self


async def test_different_args_read_is_not_falsely_guarded() -> None:
    model = _TwoTablesModel()
    mcp = FakeMCPClient(
        scripted={"getTableSchema": [_schema_response(), _schema_response()]}
    )
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe both."
    )

    assert outcome.status == "done"
    # BOTH distinct reads were dispatched — no false-positive guard.
    assert _mcp_call_count(mcp, "getTableSchema") == 2
    tables = {c.args.get("table") for c in mcp.calls if c.tool_name == "getTableSchema"}
    assert tables == {"employee", "payroll"}
    assert _guard_entries(await store.load_trail(SESSION_ID)) == []


class _RepeatQueryModel:
    """Emit the identical `runQuery` twice — a NON-idempotent tool that the guard
    must NOT suppress (a repeated query may be a distinct legitimate step)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if self._n >= 2:
            return ModelTurnResult(assistant_text="Done.", usage={"total_tokens": 1})
        self._n += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(id=f"q_{self._n}", name="runQuery", arguments={"sql": "SELECT 1"})
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _RepeatQueryModel:
        return self


async def test_non_idempotent_repeat_is_not_guarded() -> None:
    model = _RepeatQueryModel()
    mcp = FakeMCPClient(scripted={"runQuery": [_query_response(), _query_response()]})
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Run it twice."
    )

    assert outcome.status == "done"
    # The identical runQuery was dispatched BOTH times — never guarded.
    assert _mcp_call_count(mcp, "runQuery") == 2
    assert _guard_entries(await store.load_trail(SESSION_ID)) == []


class _RetrySampleRowsModel:
    """The D94 regression double: re-emit `sampleRows(ghost_table)` until the
    WITHHELD-PROVENANCE sentinel (not the read-guard nudge) is visible."""

    _CALL_ID = "call_ghost"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        saw_withheld = any(
            m.get("role") == "tool"
            and m.get("tool_call_id") == self._CALL_ID
            and isinstance(m.get("content"), str)
            and _WITHHELD_FRAGMENT in m["content"]
            for m in messages
        )
        if saw_withheld:
            return ModelTurnResult(
                assistant_text="That result is withheld.", usage={"total_tokens": 1}
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

    def begin_turn(self) -> _RetrySampleRowsModel:
        return self


async def test_d94_withheld_provenance_sentinel_still_fires_unchanged() -> None:
    """Regression: branching `_build_withheld_sentinel_message` on the guard
    marker must not disturb the original D94 ok+None path — an uncatalogued
    sampleRows still surfaces the WITHHELD sentinel (not the read-guard nudge)."""
    model = _RetrySampleRowsModel()
    mcp = FakeMCPClient(
        scripted={"sampleRows": [_query_response() for _ in range(20)]}
    )
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Sample ghost."
    )

    assert outcome.status == "done"
    assert len(model.calls) == 2
    second_ctx = model.calls[1]["messages"]
    # The D94 withheld sentinel appears; the read-guard nudge does NOT.
    assert any(
        m.get("role") == "tool"
        and isinstance(m.get("content"), str)
        and _WITHHELD_FRAGMENT in m["content"]
        for m in second_ctx
    )
    assert not _sees_nudge(second_ctx)
    # It was a real stranded ok+None entry, never a guard entry.
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 1
    assert trail[0].status == "ok" and trail[0].provenance is None
    assert _guard_entries(trail) == []


# --------------------------------------------------------------------------- #
# Coverage-gap tests (QA additions): cross-turn seeding isolation, provenance-
# union exclusion, denied-then-retry, non-getTableSchema idempotent tools, and
# the OpenAI tool_call<->tool_result pairing after a guard entry.
# --------------------------------------------------------------------------- #


def _assert_valid_tool_pairing(messages: list[dict[str, Any]]) -> None:
    """Every assistant `tool_call.id` in a canonical message list has EXACTLY one
    matching `tool` message and vice-versa (the OpenAI contract). A guard entry's
    synthetic assistant/tool pair must not leave a dangling tool_call or emit a
    tool result with no originating call."""
    tool_call_ids: list[str] = []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tool_call_ids.append(tc["id"])
    tool_result_ids = [
        m["tool_call_id"] for m in messages if m.get("role") == "tool"
    ]
    assert sorted(tool_call_ids) == sorted(tool_result_ids)
    # No id appears twice on either side (would be an API 400).
    assert len(tool_call_ids) == len(set(tool_call_ids))
    assert len(tool_result_ids) == len(set(tool_result_ids))


class _OneSchemaPerTurnModel:
    """Emit exactly ONE `getTableSchema(employee)` per external turn, then answer.
    Detects a new turn by the count of user messages in the replayed context, so
    the SAME identical read is issued as the FIRST call of two consecutive turns."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._served_for_user_count = -1

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        user_count = sum(1 for m in messages if m.get("role") == "user")
        if user_count != self._served_for_user_count:
            self._served_for_user_count = user_count
            return ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(
                        id=f"gts_turn_{user_count}",
                        name="getTableSchema",
                        arguments={"database": "dbpcm_warehouse", "table": "employee"},
                    )
                ],
                usage={"total_tokens": 1},
            )
        return ModelTurnResult(assistant_text="Answered.", usage={"total_tokens": 1})

    def begin_turn(self) -> _OneSchemaPerTurnModel:
        return self


async def test_prior_turn_read_does_not_suppress_first_read_of_a_later_turn() -> None:
    """Seeding is per CURRENT turn_index: turn 0's `getTableSchema(employee)` must
    NOT be seeded into turn 1's `seen_read_calls`, so turn 1's FIRST identical read
    is dispatched normally (the highest-risk false-positive if seeding leaked
    across turns)."""
    model = _OneSchemaPerTurnModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response() for _ in range(5)]})
    loop, store = _build_loop(model, mcp)

    first = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Turn 0 describe."
    )
    assert first.status == "done"
    assert _mcp_call_count(mcp, "getTableSchema") == 1

    second = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Turn 1 describe."
    )
    assert second.status == "done"

    # Turn 1's first identical read WAS dispatched — cross-turn isolation holds.
    assert _mcp_call_count(mcp, "getTableSchema") == 2
    # And no guard ever fired (neither turn issued an intra-turn repeat).
    assert _guard_entries(await store.load_trail(SESSION_ID)) == []


class _SchemaQueryRepeatModel:
    """Serve `getTableSchema(employee)` then `runQuery` (both determined), then
    re-issue the identical `getTableSchema(employee)` (guarded), then answer once
    the nudge is visible — a turn mixing a guard entry (provenance=None) with real
    determined provenance."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._step = 0

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if _sees_nudge(messages):
            return ModelTurnResult(assistant_text="Here is your answer.", usage={"total_tokens": 1})
        self._step += 1
        if self._step == 1:
            call = ToolCallRequest(
                id="gts_1",
                name="getTableSchema",
                arguments={"database": "dbpcm_warehouse", "table": "employee"},
            )
        elif self._step == 2:
            call = ToolCallRequest(
                id="rq_1",
                name="runQuery",
                arguments={"sql": "SELECT EmployeeCode FROM dbpcm_warehouse.employee"},
            )
        else:
            call = ToolCallRequest(
                id="gts_repeat",
                name="getTableSchema",
                arguments={"database": "dbpcm_warehouse", "table": "employee"},
            )
        return ModelTurnResult(tool_calls=[call], usage={"total_tokens": 1})

    def begin_turn(self) -> _SchemaQueryRepeatModel:
        return self


async def test_turn_provenance_union_excludes_guard_entry() -> None:
    """A guard entry's `ok`+`None` provenance must NOT poison the turn's provenance
    union to `None` (which would drop the turn's answer from future-turn replay).
    The union is the determined lineage of the REAL served reads only."""
    model = _SchemaQueryRepeatModel()
    mcp = FakeMCPClient(
        scripted={
            "getTableSchema": [_schema_response() for _ in range(5)],
            "runQuery": [_query_response()],
        }
    )
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe then query."
    )

    assert outcome.status == "done"
    # Exactly one guard entry (the repeat) and one real getTableSchema dispatch.
    trail = await store.load_trail(SESSION_ID)
    assert len(_guard_entries(trail)) == 1
    assert _mcp_call_count(mcp, "getTableSchema") == 1

    # The union is DETERMINED (non-None) despite the guard entry's None provenance,
    # and carries the served read's real lineage.
    assert outcome.provenance is not None
    assert ("dbpcm_warehouse.employee", "EmployeeCode") in outcome.provenance
    # The final assistant TurnMessage carries the same determined tag (kept on replay).
    doc = await store.get_or_create_session(SESSION_ID)
    assistant_msgs = [m for m in doc.messages if m.role == "assistant"]
    assert assistant_msgs and assistant_msgs[-1].provenance == outcome.provenance


class _DeniedThenRetryModel:
    """Issue `getTableSchema(employee)` twice, then answer. The first dispatch is
    scripted to DENY; the retry must be dispatched (a denied read is never recorded
    as 'already served')."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if self._n >= 2:
            return ModelTurnResult(assistant_text="Got it on retry.", usage={"total_tokens": 1})
        self._n += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=f"gts_{self._n}",
                    name="getTableSchema",
                    arguments={"database": "dbpcm_warehouse", "table": "employee"},
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _DeniedThenRetryModel:
        return self


async def test_denied_idempotent_read_then_retry_is_dispatched_not_guarded() -> None:
    model = _DeniedThenRetryModel()
    mcp = FakeMCPClient(
        scripted={
            "getTableSchema": [
                MCPToolError("CLICKHOUSE_UNAVAILABLE", "[CLICKHOUSE_UNAVAILABLE] transient"),
                _schema_response(),
            ]
        }
    )
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe employee."
    )

    assert outcome.status == "done"
    # BOTH the denied call AND the retry reached the MCP — the retry was NOT guarded.
    assert _mcp_call_count(mcp, "getTableSchema") == 2
    trail = await store.load_trail(SESSION_ID)
    assert _guard_entries(trail) == []
    statuses = [e.status for e in trail if e.tool_name == "getTableSchema"]
    assert statuses == ["denied", "ok"]


class _RepeatToolModel:
    """Re-issue one arbitrary idempotent read (name+args) each round-trip until its
    guard nudge is visible, then answer — parametrizes the guard over the non-
    getTableSchema idempotent tools."""

    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._tool_name = tool_name
        self._arguments = arguments
        self._n = 0

    async def send_turn(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurnResult:
        self.calls.append({"messages": messages})
        if _sees_nudge(messages):
            return ModelTurnResult(assistant_text="Using what I have.", usage={"total_tokens": 1})
        self._n += 1
        return ModelTurnResult(
            tool_calls=[
                ToolCallRequest(
                    id=f"c_{self._n}", name=self._tool_name, arguments=dict(self._arguments)
                )
            ],
            usage={"total_tokens": 1},
        )

    def begin_turn(self) -> _RepeatToolModel:
        return self


@pytest.mark.parametrize(
    ("tool_name", "arguments", "response"),
    [
        ("listTables", {"database": "dbpcm_warehouse"}, [{"table": "employee"}]),
        ("explainQuery", {"sql": "SELECT 1"}, _query_response()),
    ],
)
async def test_non_schema_idempotent_reads_are_also_guarded(
    tool_name: str, arguments: dict[str, Any], response: Any
) -> None:
    model = _RepeatToolModel(tool_name, arguments)
    mcp = FakeMCPClient(scripted={tool_name: [response for _ in range(20)]})
    loop, store = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message=f"Repeat {tool_name}."
    )

    assert outcome.status == "done"
    # The repeat of this idempotent read was guarded, not re-dispatched.
    assert _mcp_call_count(mcp, tool_name) == 1
    assert len(_guard_entries(await store.load_trail(SESSION_ID))) == 1
    assert _sees_nudge(model.calls[-1]["messages"])


async def test_guard_entry_keeps_tool_call_result_pairing_valid() -> None:
    """The guard's synthetic assistant/tool pair must fill the repeat call's
    dangling tool-slot: the canonical context the model sees after a guard has a
    1:1 tool_call<->tool_result pairing (no dangling call, no orphan result)."""
    model = _RepeatSchemaModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response() for _ in range(20)]})
    loop, _ = _build_loop(model, mcp)

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe employee."
    )
    assert outcome.status == "done"

    # The final round-trip context contains BOTH the served read's pair and the
    # guard entry's synthetic pair — all balanced.
    final_ctx = model.calls[-1]["messages"]
    _assert_valid_tool_pairing(final_ctx)
    # The guarded repeat's own tool_call_id got the nudge as its tool result.
    guard_result = next(
        (
            m
            for m in final_ctx
            if m.get("role") == "tool"
            and isinstance(m.get("content"), str)
            and _NUDGE_FRAGMENT in m["content"]
        ),
        None,
    )
    assert guard_result is not None
    assert any(
        m.get("role") == "assistant"
        and any(tc["id"] == guard_result["tool_call_id"] for tc in (m.get("tool_calls") or []))
        for m in final_ctx
    )


# --------------------------------------------------------------------------- #
# Trace legibility: the guard-decision observer payload self-describes so a
# Phoenix reader sees "a SECOND, duplicate read was deduped" — not "the first
# fetch was blocked". (The full observer->GUARDRAIL-span wiring — allowlist
# included — is proven end-to-end in
# tests/runtime/observability/test_tool_span_wiring_e2e.py.)
# --------------------------------------------------------------------------- #


async def test_guard_event_payload_is_legible_and_marks_the_dedup() -> None:
    """Drive a real guarded run and capture the emitted observer event: it must
    carry the tool_name, the `deduped` marker, and a human-readable note so the
    trace attributes it unambiguously to the duplicate call."""
    events: list[tuple[str, dict[str, Any]]] = []

    model = _RepeatSchemaModel()
    mcp = FakeMCPClient(scripted={"getTableSchema": [_schema_response() for _ in range(20)]})
    store = InMemorySessionStore()
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store)
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=6,
        max_wall_clock_seconds=60,
        max_budget_windows=2,
        observer=lambda event, payload: events.append((event, dict(payload))),
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_credentials(), user_message="Describe employee."
    )
    assert outcome.status == "done"

    guard_events = [p for name, p in events if name == "loop_repeated_idempotent_read_guarded"]
    # Emitted EXACTLY once (the loop-side guard-decision site is the sole owner —
    # no per-window / render-time double-emit).
    assert len(guard_events) == 1
    payload = guard_events[0]
    assert payload["tool_name"] == "getTableSchema"
    assert payload["deduped"] is True
    # UPDATED with the trim-aware exemption: "already served" is no longer on its
    # own sufficient for the guard to fire — the result must ALSO still be readable.
    # The reason string says so, because a trace that claimed the old reason would
    # misdescribe the decision the loop actually made.
    assert payload["guard_reason"] == "already_served_and_still_readable"
    assert payload["database"] == "dbpcm_warehouse"
    assert payload["table"] == "employee"
    assert payload["dedup_target"] == "dbpcm_warehouse.employee"
    assert "duplicate getTableSchema(dbpcm_warehouse.employee)" in payload["note"]
    assert "still readable above" in payload["note"]
    assert "not re-dispatched" in payload["note"]


def test_guard_event_never_places_free_form_args_like_sql_on_the_span() -> None:
    """`explainQuery`'s `sql` (which can carry PII literals) must NEVER reach the
    guard event — only catalog-safe identifiers do. The span still self-describes
    as a deduped explainQuery via the fixed note."""
    payload = _repeated_read_guard_event(
        "explainQuery", "eq_2", {"sql": "SELECT secret FROM employee WHERE name = 'PII_VALUE'"}
    )
    assert "sql" not in payload
    assert "table" not in payload and "database" not in payload
    assert payload["tool_name"] == "explainQuery"
    assert payload["deduped"] is True
    assert payload["dedup_target"] == ""
    assert "duplicate explainQuery()" in payload["note"]
    # No SQL text / literal leaks through ANY value.
    blob = json.dumps(payload)
    assert "PII_VALUE" not in blob
    assert "SELECT secret" not in blob
