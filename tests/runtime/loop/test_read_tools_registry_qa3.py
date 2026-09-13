"""QA-3 adversarial Layer-1 tests for the runtime-tool REGISTRY in the loop.

ADDITIVE — never modifies test_agent_loop.py. Attacks the registry seam that
routes `resolveValues` + the three read tools (read-tools-design §2): crash
isolation, mixed runtime/MCP responses, the per-iteration cap boundary, the
askUser-vs-runtime precedence, duplicate tool-call ids, an MCP name collision
(shadowing), the unwired-tool clean-error path, and the D44 frozenset()
provenance replay survival that the design explicitly chose.

All fakes; the read tools run over a real `RetrievalPipeline`/`FakeVectorIndex`.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context import scope_filter
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.loop.agent_loop import AgentLoop, RuntimeTool
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import SearchBlueprintsTool
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_E = "dbpcm_warehouse.employee"
CATALOG = CatalogHandle(
    {_E: {"EarnCode": "Nullable(String)", "EarnDescription": "Nullable(String)"}}
)
SESSION_ID = "sess-qa3"
_A = "dbpcm_warehouse.payroll.Amount"
_Q = "overtime rollup"


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "listDatabases", "description": "", "parameters": {}}]


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id=SESSION_ID, jwt="SECRET-JWT", column_scope=scope)


def _build(
    *,
    model: ScriptedModelClient,
    mcp: FakeMCPClient,
    runtime_tools: dict[str, RuntimeTool],
    max_tool_calls_per_iteration: int = 8,
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
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        max_tool_calls_per_iteration=max_tool_calls_per_iteration,
        runtime_tools=runtime_tools,
    )
    return loop, store


def _real_search_tool(*, reranked: bool = True) -> SearchBlueprintsTool:
    idx = FakeVectorIndex([_bp("in", "overtime rollup", {_A})])
    reranker = FakeRerankerClient({"overtime rollup": 0.9}) if reranked else None
    pipeline = RetrievalPipeline(
        embedding_client=FakeEmbeddingClient({_Q: [1.0, 0.0]}),
        reranker=reranker,
        vector_index=idx,
        user_memory=NullUserMemoryProvider(),
        recall_k=10,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )
    return SearchBlueprintsTool(pipeline=pipeline, default_k=5, max_k=20)


def _bp(id: str, intent: str, uses: set[str]) -> tuple[Candidate, list[float]]:
    return (
        Candidate(
            id=id,
            kind="blueprint",
            text=intent,
            uses=frozenset(uses),
            payload={"intent": intent, "slots_summary": f"slots-of-{id}"},
        ),
        [1.0, 0.0],
    )


def _sb_call(call_id: str, query: str = _Q) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name="searchBlueprints", arguments={"query": query})


# ---------------------------------------------------------------------------
# Crash isolation — a read tool whose pipeline raises: the tool self-guards
# (B4), so the registry sees a clean error and the TURN SURVIVES to completion.
# ---------------------------------------------------------------------------


class _RaisingPipeline:
    async def search_blueprints(
        self, *, question: str, column_scope: frozenset[str], k: int
    ) -> Any:
        raise RuntimeError("internal boom")


async def test_read_tool_crash_is_isolated_turn_survives() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_sb_call("c1")]),
            ModelTurnResult(assistant_text="recovered."),
        ]
    )
    tool = SearchBlueprintsTool(pipeline=_RaisingPipeline(), default_k=5, max_k=20)  # type: ignore[arg-type]
    loop, store = _build(model=model, mcp=FakeMCPClient(), runtime_tools={"searchBlueprints": tool})

    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hi")

    assert outcome.status == "done"
    assert outcome.assistant_text == "recovered."
    assert outcome.tool_calls_made == 1
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.status, e.error_code) for e in trail] == [
        ("searchBlueprints", "error", "RETRIEVAL_TOOL_INTERNAL_ERROR")
    ]
    # frozenset() provenance persisted even on the crash path.
    assert trail[0].provenance == frozenset()


# ---------------------------------------------------------------------------
# Mixed response — runtime read tool + runtime resolveValues + MCP tool in ONE
# response: all execute IN ORDER, count = 3, searchBlueprints never hits the MCP.
# ---------------------------------------------------------------------------


async def test_mixed_runtime_and_mcp_calls_order_and_counts() -> None:
    from data_agent.runtime.composite.resolve_values import ResolveValuesComposite

    store = InMemorySessionStore()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [
                {
                    "columns": ["EarnCode", "EarnDescription", "freq"],
                    "rows": [["OT", "overtime", 3]],
                    "row_count": 1,
                    "truncated": False,
                }
            ],
            "listDatabases": [["db1"]],
        }
    )
    dispatcher = ToolDispatcher(mcp, CATALOG)
    assembler = ContextAssembler(store)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher, catalog=CATALOG, embedding_client=FakeEmbeddingClient(dim=2)
    )
    loop = AgentLoop(
        model_client=ScriptedModelClient(
            [
                ModelTurnResult(
                    tool_calls=[
                        _sb_call("c1"),
                        ToolCallRequest(
                            id="c2",
                            name="resolveValues",
                            arguments={"table": _E, "column": "EarnCode", "concept": "overtime"},
                        ),
                        ToolCallRequest(id="c3", name="listDatabases", arguments={}),
                    ]
                ),
                ModelTurnResult(assistant_text="done"),
            ]
        ),
        tool_dispatcher=dispatcher,
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        runtime_tools={"searchBlueprints": _real_search_tool(), "resolveValues": composite},
    )

    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_creds(frozenset({_A})), user_message="hi"
    )

    assert outcome.tool_calls_made == 3
    trail = await store.load_trail(SESSION_ID)
    assert [e.tool_name for e in trail] == ["searchBlueprints", "resolveValues", "listDatabases"]
    # searchBlueprints never reached the MCP under its own name; only the inner
    # runQuery (from resolveValues) + the real listDatabases did.
    assert [c.tool_name for c in mcp.calls] == ["runQuery", "listDatabases"]


# ---------------------------------------------------------------------------
# Per-iteration cap boundary — 9 runtime calls, default cap 8 → exactly 8 run.
# ---------------------------------------------------------------------------


async def test_per_iteration_cap_boundary_at_8() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_sb_call(f"c{i}") for i in range(9)]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, store = _build(
        model=model,
        mcp=FakeMCPClient(),
        runtime_tools={"searchBlueprints": _real_search_tool()},
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_creds(frozenset({_A})), user_message="hi"
    )
    assert outcome.tool_calls_made == 8  # the 9th is not dispatched this round
    trail = await store.load_trail(SESSION_ID)
    assert len(trail) == 8


# ---------------------------------------------------------------------------
# askUser vs runtime tool — pause WINS; the runtime tool is NOT executed.
# ---------------------------------------------------------------------------


class _RecordingTool:
    def __init__(self) -> None:
        self.ran = False

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn=None,
        tool_call_id=None,
    ) -> ToolResult:
        self.ran = True
        return ToolResult(
            status="ok",
            tool_name="searchBlueprints",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full={"count": 0},
        )


async def test_ask_user_short_circuits_runtime_tool_in_same_response() -> None:
    tool = _RecordingTool()
    # askUser appears AFTER the runtime tool in the response, yet still wins.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    _sb_call("c1"),
                    ToolCallRequest(id="c2", name="askUser", arguments={"question": "Which dept?"}),
                ]
            )
        ]
    )
    loop, store = _build(model=model, mcp=FakeMCPClient(), runtime_tools={"searchBlueprints": tool})
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hi")
    assert outcome.status == "paused_ask_user"
    assert tool.ran is False  # the runtime tool was never executed
    assert outcome.tool_calls_made == 0
    assert await store.load_trail(SESSION_ID) == []  # nothing dispatched


# ---------------------------------------------------------------------------
# Duplicate tool-call ids — both execute; two trail entries share the id.
# ---------------------------------------------------------------------------


async def test_duplicate_tool_call_ids_both_execute_and_trail() -> None:
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_sb_call("dup", "q1"), _sb_call("dup", "q2")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, store = _build(
        model=model,
        mcp=FakeMCPClient(),
        runtime_tools={"searchBlueprints": _real_search_tool()},
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_creds(frozenset({_A})), user_message="hi"
    )
    # PIN: the loop does not de-duplicate ids — both run, both are trailed under
    # the same tool_call_id. (Replay would synthesize two assistant/tool pairs
    # with a colliding id — see the report's ambiguity note for runBlueprint.)
    assert outcome.tool_calls_made == 2
    trail = await store.load_trail(SESSION_ID)
    assert [e.tool_call_id for e in trail] == ["dup", "dup"]


# ---------------------------------------------------------------------------
# Name collision — a runtime tool named "runQuery" SHADOWS the MCP tool. FLAG.
# ---------------------------------------------------------------------------


class _ShadowRunQuery:
    def __init__(self) -> None:
        self.ran = False

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn=None,
        tool_call_id=None,
    ) -> ToolResult:
        self.ran = True
        return ToolResult(
            status="ok",
            tool_name="runQuery",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=None,
            result_full={"shadowed": True},
        )


async def test_runtime_tool_named_runquery_shadows_the_mcp_tool() -> None:
    # PIN + FLAG: the registry lookup precedes MCP dispatch, so a runtime tool
    # whose name collides with an MCP tool WINS and the MCP tool never runs.
    # app.py only registers non-colliding names today (resolveValues + the 3
    # read tools), but nothing guards against a future collision (e.g. a
    # mis-named runBlueprint). See the report.
    shadow = _ShadowRunQuery()
    mcp = FakeMCPClient(
        scripted={
            "runQuery": [{"columns": ["x"], "rows": [[1]], "row_count": 1, "truncated": False}]
        }
    )
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="runQuery", arguments={"sql": "SELECT 1"})
                ]
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, store = _build(model=model, mcp=mcp, runtime_tools={"runQuery": shadow})
    await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hi")
    assert shadow.ran is True
    assert mcp.calls == []  # the real MCP runQuery was shadowed out


# ---------------------------------------------------------------------------
# Unwired advertised tool — a clean local UNAVAILABLE error, NOT an MCP denial.
# ---------------------------------------------------------------------------


async def test_advertised_but_unwired_read_tool_returns_unavailable() -> None:
    mcp = FakeMCPClient()
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_sb_call("c1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    # searchBlueprints is NOT wired into the registry (retrieval inactive).
    loop, store = _build(model=model, mcp=mcp, runtime_tools={})
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hi")
    assert outcome.tool_calls_made == 1
    trail = await store.load_trail(SESSION_ID)
    assert (trail[0].tool_name, trail[0].status, trail[0].error_code) == (
        "searchBlueprints",
        "error",
        "RETRIEVAL_TOOL_UNAVAILABLE",
    )
    # Never dispatched to the MCP under its own name (no such MCP tool).
    assert mcp.calls == []
    # The unwired error carries provenance=None (undetermined) — DIVERGENT from
    # the wired tool's frozenset(); benign because it is an error entry with no
    # result rows and the current-turn exemption keeps self-correction working.
    assert trail[0].provenance is None


# ---------------------------------------------------------------------------
# D44 replay — a read-tool entry (frozenset() provenance) SURVIVES a narrowed
# scope that drops a warehouse entry (the design's explicit choice, §3).
# ---------------------------------------------------------------------------


def test_frozenset_provenance_read_entry_survives_narrowed_scope_d44() -> None:
    read_entry = TrailEntry(
        turn_index=0,
        tool_call_id="r1",
        tool_name="searchBlueprints",
        args={"query": "x"},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=ResultPreview(
            columns=[], row_count=1, truncated=False, preview_rows=[[{"count": 0}]]
        ),
        result_full_ref="ref-1",
        ts="t",
    )
    warehouse_entry = TrailEntry(
        turn_index=0,
        tool_call_id="w1",
        tool_name="runQuery",
        args={"sql": "..."},
        status="ok",
        error_code=None,
        provenance=frozenset({(_E, "EarnCode")}),
        result_preview=None,
        result_full_ref="ref-2",
        ts="t",
    )
    narrowed = frozenset({"dbpcm_warehouse.other.Col"})  # excludes employee.EarnCode

    kept = scope_filter.filter_trail([read_entry, warehouse_entry], narrowed)

    kept_ids = [e.tool_call_id for e in kept]
    assert "r1" in kept_ids  # frozenset() → always in scope, KEPT (design choice)
    assert "w1" not in kept_ids  # warehouse entry dropped under the narrowed scope


# ---------------------------------------------------------------------------
# Loop-level robustness — HARDENED (S2): the registry seam now isolates a
# misbehaving runtime tool (B4-style crash guard + returned-provenance-type
# validation) so a future runBlueprint entry cannot abort the turn or poison the
# D44 replay. The tools' own `_guarded`/frozenset() self-protection stays as
# defense in depth (both layers hold).
# ---------------------------------------------------------------------------


class _UnguardedRaisingTool:
    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn=None,
        tool_call_id=None,
    ) -> ToolResult:
        raise RuntimeError("a runtime tool that does not self-guard")


async def test_loop_wraps_a_raising_runtime_tool_turn_survives() -> None:
    # HARDENED: the loop wraps `handler.run()` in a B4-style guard, so a registry
    # entry that raises (a tool that does NOT self-guard) yields a clean
    # RUNTIME_TOOL_INTERNAL_ERROR entry and the turn completes — it no longer
    # crashes the whole turn. (Was: test_loop_does_not_wrap_a_raising_runtime_tool_flag.)
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_sb_call("c1")]),
            ModelTurnResult(assistant_text="recovered."),
        ]
    )
    loop, store = _build(
        model=model,
        mcp=FakeMCPClient(),
        runtime_tools={"searchBlueprints": _UnguardedRaisingTool()},
    )
    outcome = await loop.run(session_id=SESSION_ID, credentials=_creds(), user_message="hi")
    assert outcome.status == "done"
    assert outcome.assistant_text == "recovered."
    assert outcome.tool_calls_made == 1
    trail = await store.load_trail(SESSION_ID)
    assert [(e.tool_name, e.status, e.error_code) for e in trail] == [
        ("searchBlueprints", "error", "RUNTIME_TOOL_INTERNAL_ERROR")
    ]
    # A crash-path entry carries provenance=None (undetermined, fail-closed).
    assert trail[0].provenance is None


class _MalformedProvenanceTool:
    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn=None,
        tool_call_id=None,
    ) -> ToolResult:
        # provenance declared frozenset[tuple[str,str]] | None, but a contract
        # violator returns a bare string.
        return ToolResult(
            status="ok",
            tool_name="searchBlueprints",
            error_code=None,
            retryable=None,
            user_message=None,
            provenance="not-a-frozenset",  # type: ignore[arg-type]
            result_preview=None,
            result_full={"count": 0},
        )


async def test_loop_validates_runtime_tool_provenance_type_coerces_to_none() -> None:
    # HARDENED: a malformed (wrong-type) provenance from a registry entry is
    # coerced to None (fail-closed) BEFORE the trail write, so it no longer
    # crashes the NEXT round-trip inside the D44 replay filter. The turn
    # completes and the poisoned value is never persisted. (Was:
    # test_loop_does_not_validate_runtime_tool_provenance_type_flag.)
    model = ScriptedModelClient(
        [
            ModelTurnResult(tool_calls=[_sb_call("c1")]),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    loop, store = _build(
        model=model,
        mcp=FakeMCPClient(),
        runtime_tools={"searchBlueprints": _MalformedProvenanceTool()},
    )
    outcome = await loop.run(
        session_id=SESSION_ID, credentials=_creds(frozenset({"a.b.c"})), user_message="hi"
    )
    assert outcome.status == "done"
    trail = await store.load_trail(SESSION_ID)
    assert trail[0].provenance is None  # coerced fail-closed, not the bad string
