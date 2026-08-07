"""Layer-1 integration: retrieval ↔ ContextAssembler ↔ AgentLoop (design §3.3).

Covers: pre-injection prepend; byte-identical assembly when unconfigured / when
no user_message; the turn-local memo (exactly one embed per turn across loop
iterations).
"""

from __future__ import annotations

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_Q = "sales overtime"
_COL = ("dbpcm_warehouse.employee", "Department")
CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"Department": "String"}})
SESSION_ID = "sess-retr"


async def _seed_history(store: InMemorySessionStore, *, n: int = 40) -> None:
    """Seed enough in-scope ok TrailEntries (with a real preview + provenance)
    that assembly is non-trivial — enough to also exercise compaction — so the
    byte-parity check compares real rendered history, not two empty lists (M3)."""
    for i in range(n):
        await store.append_trail_entry(
            SESSION_ID,
            TrailEntry(
                turn_index=i,
                tool_call_id=f"call_{i}",
                tool_name="runQuery",
                args={"sql": f"SELECT Department FROM dbpcm_warehouse.employee LIMIT {i}"},
                status="ok",
                error_code=None,
                provenance=frozenset({_COL}),
                result_preview=ResultPreview(
                    columns=["Department"],
                    row_count=100 + i,
                    truncated=True,
                    preview_rows=[["Sales"], ["Eng"]],
                ),
                result_full_ref=None,
                ts="2026-07-01T00:00:00+00:00",
            ),
        )


def _index() -> FakeVectorIndex:
    return FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-1",
                    kind="blueprint",
                    text="sales overtime rollup",
                    uses=frozenset({"w.t.c"}),
                    payload={"intent": "sales overtime rollup", "slots_summary": "dept"},
                ),
                [1.0, 0.0],
            )
        ]
    )


def _pipeline(embedder: FakeEmbeddingClient) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=embedder,
        reranker=FakeRerankerClient(),
        vector_index=_index(),
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )


async def test_assemble_prepends_retrieval_user_message() -> None:
    store = InMemorySessionStore()
    assembler = ContextAssembler(
        store, history_token_budget=100_000, retrieval=_pipeline(FakeEmbeddingClient())
    )
    assembled = await assembler.assemble(
        SESSION_ID, frozenset(), user_message=_Q, retrieval_memo={}
    )
    # No base prompt configured here, so the retrieval block leads — as a
    # NON-system (`user`) message, never `system`.
    assert assembled.messages[0]["role"] == "user"
    assert "sales overtime rollup" in assembled.messages[0]["content"]
    assert assembled.retrieved_counts == (1, 0)


async def test_unconfigured_retrieval_is_byte_identical() -> None:
    # Non-vacuous (M3): seed real in-scope history (enough to trigger compaction
    # at a small budget) so this compares actual rendered messages, not [] == [].
    store = InMemorySessionStore()
    await _seed_history(store, n=40)
    scope = frozenset({f"{_COL[0]}.{_COL[1]}"})
    plain = ContextAssembler(store, history_token_budget=200)  # retrieval=None
    with_user_msg = await plain.assemble(SESSION_ID, scope, user_message=_Q, user_id="u1")
    without = await plain.assemble(SESSION_ID, scope)
    assert len(without.messages) > 1  # guard: history is genuinely present
    assert with_user_msg.messages == without.messages  # passing new params changes nothing
    assert with_user_msg.retrieved_counts == (0, 0)


async def test_configured_retrieval_but_no_user_message_does_not_run() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient()
    assembler = ContextAssembler(
        store, history_token_budget=100_000, retrieval=_pipeline(embedder)
    )
    assembled = await assembler.assemble(SESSION_ID, frozenset())  # no user_message
    assert embedder.calls == []  # retrieval never ran
    assert assembled.retrieved_counts == (0, 0)


async def test_memo_prevents_re_embedding_within_a_turn() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient()
    assembler = ContextAssembler(
        store, history_token_budget=100_000, retrieval=_pipeline(embedder)
    )
    memo: dict = {}
    await assembler.assemble(SESSION_ID, frozenset(), user_message=_Q, retrieval_memo=memo)
    await assembler.assemble(SESSION_ID, frozenset(), user_message=_Q, retrieval_memo=memo)
    assert embedder.calls == [[_Q]]  # embedded exactly once despite two assembles


async def test_no_memo_re_embeds_each_call() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient()
    assembler = ContextAssembler(
        store, history_token_budget=100_000, retrieval=_pipeline(embedder)
    )
    await assembler.assemble(SESSION_ID, frozenset(), user_message=_Q)
    await assembler.assemble(SESSION_ID, frozenset(), user_message=_Q)
    assert embedder.calls == [[_Q], [_Q]]


# --- Loop-level: exactly one embed per turn across multiple round-trips ---


async def _tools_provider(_credentials: RuntimeCredentials) -> list[dict]:
    return [
        {"type": "function", "name": "listDatabases", "description": "", "parameters": {}},
    ]


async def test_loop_embeds_once_per_turn_across_iterations() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient()
    assembler = ContextAssembler(
        store, history_token_budget=100_000, retrieval=_pipeline(embedder)
    )
    # Two round-trips: a tool call, then a tool-call-free response.
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[ToolCallRequest(id="c1", name="listDatabases", arguments={})],
                usage={"total_tokens": 10},
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    mcp = FakeMCPClient(scripted={"listDatabases": [[{"name": "dbpcm_warehouse"}]]})
    loop = AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(mcp, CATALOG),
        context_assembler=assembler,
        session_store=store,
        tools_provider=_tools_provider,
        max_loop_iterations=15,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )

    outcome = await loop.run(
        session_id=SESSION_ID,
        credentials=RuntimeCredentials(session_id=SESSION_ID, jwt="j", column_scope=frozenset()),
        user_message=_Q,
    )
    assert outcome.status == "done"
    assert len(model.calls) == 2  # two round-trips (two assembles)
    assert embedder.calls == [[_Q]]  # but embedded exactly once (memoized)
