"""QA adversarial tests: memoization, resume determinism, and empty/None parity.

Additive to `test_integration.py`. Covers gaps around the turn-local memo and
the D45 resume path (design §3.3/§6):
  - the memo is keyed by (question, scope_hash), so a scope change between
    round-trips busts it (re-embed) — no cross-scope stale reuse;
  - retrieval on RESUME re-runs on the ORIGINAL turn question, never the
    askUser answer (design §6);
  - the memo is per-turn-window state, never shared across AgentLoop instances
    (concurrent turns cannot poison each other's retrieval);
  - `_first_user_question` helper edge cases;
  - empty-retrieval assembly is BYTE-IDENTICAL to retrieval=None (parity).
"""

from __future__ import annotations

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.loop.agent_loop import AgentLoop, _first_user_question
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
from data_agent.runtime.session.models import TurnMessage

_Q = "sales overtime"
CATALOG = CatalogHandle({"dbpcm_warehouse.employee": {"Department": "String"}})


def _index() -> FakeVectorIndex:
    return FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-1",
                    kind="blueprint",
                    text="sales overtime rollup",
                    uses=frozenset(),  # empty uses => in every scope
                    payload={"intent": "sales overtime rollup", "slots_summary": "dept"},
                ),
                [1.0, 0.0],
            )
        ]
    )


def _pipeline(embedder: FakeEmbeddingClient, index: FakeVectorIndex | None = None) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=embedder,
        reranker=FakeRerankerClient(),
        vector_index=index if index is not None else _index(),
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )


# --------------------------------------------------------------------------
# Memo keying: scope change busts the memo (no cross-scope reuse)
# --------------------------------------------------------------------------


async def test_scope_change_busts_memo_and_re_embeds() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient({_Q: [1.0, 0.0]})
    assembler = ContextAssembler(store, retrieval=_pipeline(embedder))
    memo: dict = {}
    await assembler.assemble("s", frozenset({"db.t.a"}), user_message=_Q, retrieval_memo=memo)
    await assembler.assemble("s", frozenset(), user_message=_Q, retrieval_memo=memo)  # scope changed
    # Two DISTINCT scope hashes → two memo keys → embedded twice. The memo never
    # serves a result computed under a different scope.
    assert embedder.calls == [[_Q], [_Q]]
    assert len(memo) == 2


async def test_same_scope_same_question_hits_memo_once() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient({_Q: [1.0, 0.0]})
    assembler = ContextAssembler(store, retrieval=_pipeline(embedder))
    memo: dict = {}
    scope = frozenset({"db.t.a"})
    await assembler.assemble("s", scope, user_message=_Q, retrieval_memo=memo)
    await assembler.assemble("s", scope, user_message=_Q, retrieval_memo=memo)
    assert embedder.calls == [[_Q]]
    assert len(memo) == 1


# --------------------------------------------------------------------------
# Empty-retrieval vs retrieval=None parity (byte-identical assembled messages)
# --------------------------------------------------------------------------


async def test_empty_retrieval_is_byte_identical_to_none() -> None:
    store = InMemorySessionStore()
    # A pipeline over an EMPTY index returns an empty RetrievedContext → render
    # None → nothing prepended. Must byte-match the retrieval=None assembler.
    empty_pipeline = _pipeline(FakeEmbeddingClient({_Q: [1.0, 0.0]}), index=FakeVectorIndex([]))
    with_empty = ContextAssembler(store, retrieval=empty_pipeline)
    plain = ContextAssembler(store)
    empty_ctx = await with_empty.assemble("s", frozenset(), user_message=_Q, retrieval_memo={})
    none_ctx = await plain.assemble("s", frozenset(), user_message=_Q)
    assert empty_ctx.messages == none_ctx.messages
    assert empty_ctx.retrieved_counts == (0, 0)


async def test_index_failure_retrieval_is_byte_identical_to_none() -> None:
    store = InMemorySessionStore()
    failing = _pipeline(FakeEmbeddingClient({_Q: [1.0, 0.0]}), index=FakeVectorIndex(fail=True))
    with_fail = ContextAssembler(store, retrieval=failing)
    plain = ContextAssembler(store)
    fail_ctx = await with_fail.assemble("s", frozenset(), user_message=_Q, retrieval_memo={})
    none_ctx = await plain.assemble("s", frozenset(), user_message=_Q)
    assert fail_ctx.messages == none_ctx.messages


# --------------------------------------------------------------------------
# _first_user_question helper (drives resume retrieval, design §6)
# --------------------------------------------------------------------------


def _msg(turn: int, role: str, content: str) -> TurnMessage:
    return TurnMessage(turn_index=turn, role=role, content=content, ts="t")


def test_first_user_question_returns_first_user_message_of_turn() -> None:
    messages = [
        _msg(0, "user", "old turn"),
        _msg(1, "user", "the original question"),
        _msg(1, "assistant", "which team?"),
        _msg(1, "user", "the sales team"),  # askUser answer, same turn
    ]
    assert _first_user_question(messages, 1) == "the original question"


def test_first_user_question_none_when_turn_absent() -> None:
    assert _first_user_question([_msg(0, "user", "q")], 5) is None


def test_first_user_question_ignores_other_turns() -> None:
    messages = [_msg(2, "assistant", "a"), _msg(2, "user", "q2")]
    assert _first_user_question(messages, 2) == "q2"


# --------------------------------------------------------------------------
# Loop-level resume: retrieval re-runs on the ORIGINAL question, not the answer
# --------------------------------------------------------------------------


async def _ask_user_tools(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "askUser", "description": "", "parameters": {}}]


def _ask_user_loop(store, embedder) -> AgentLoop:
    assembler = ContextAssembler(store, retrieval=_pipeline(embedder))
    model = ScriptedModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCallRequest(id="c1", name="askUser", arguments={"question": "which team?"})
                ],
                usage={"total_tokens": 5},
            ),
            ModelTurnResult(assistant_text="done"),
        ]
    )
    return AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=assembler,
        session_store=store,
        tools_provider=_ask_user_tools,
        max_loop_iterations=15,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )


async def test_resume_re_embeds_original_question_not_the_answer() -> None:
    store = InMemorySessionStore()
    embedder = FakeEmbeddingClient()
    loop = _ask_user_loop(store, embedder)
    creds = RuntimeCredentials(session_id="s", jwt="j", column_scope=frozenset())
    original = "original overtime question"

    paused = await loop.run(session_id="s", credentials=creds, user_message=original)
    assert paused.status == "paused_ask_user"
    assert embedder.calls == [[original]]

    done = await loop.resume(session_id="s", credentials=creds, answer="the sales team")
    assert done.status == "done"
    # Resume re-embeds (fresh budget window = fresh memo) but on the ORIGINAL
    # question — the askUser answer is NEVER what gets embedded (design §6).
    assert embedder.calls == [[original], [original]]
    assert all(call == [original] for call in embedder.calls)


# --------------------------------------------------------------------------
# Memo isolation across concurrent AgentLoop instances
# --------------------------------------------------------------------------


async def _plain_tools(_c: RuntimeCredentials) -> list[dict]:
    return [{"type": "function", "name": "listDatabases", "description": "", "parameters": {}}]


def _done_loop(store, embedder) -> AgentLoop:
    assembler = ContextAssembler(store, retrieval=_pipeline(embedder))
    model = ScriptedModelClient([ModelTurnResult(assistant_text="done")])
    return AgentLoop(
        model_client=model,
        tool_dispatcher=ToolDispatcher(FakeMCPClient(), CATALOG),
        context_assembler=assembler,
        session_store=store,
        tools_provider=_plain_tools,
        max_loop_iterations=15,
        max_wall_clock_seconds=999,
        max_budget_windows=3,
    )


async def test_two_agentloops_do_not_share_a_memo() -> None:
    # Each _run_loop_body creates its own memo dict; two independent loops (distinct
    # sessions) each embed exactly once — no cross-turn memo leakage.
    store = InMemorySessionStore()
    emb_a = FakeEmbeddingClient()
    emb_b = FakeEmbeddingClient()
    creds_a = RuntimeCredentials(session_id="sa", jwt="j", column_scope=frozenset())
    creds_b = RuntimeCredentials(session_id="sb", jwt="j", column_scope=frozenset())
    out_a = await _done_loop(store, emb_a).run(session_id="sa", credentials=creds_a, user_message="qa")
    out_b = await _done_loop(store, emb_b).run(session_id="sb", credentials=creds_b, user_message="qb")
    assert out_a.status == "done" and out_b.status == "done"
    assert emb_a.calls == [["qa"]]
    assert emb_b.calls == [["qb"]]
