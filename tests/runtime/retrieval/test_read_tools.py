"""Layer-1 tests for the model-facing read tools (read-tools-design §7).

All fakes, no infra. Covers: `BlueprintDetail` record mapping; `get_blueprint`
on both the Fake and the Neo4j store (via the monkeypatched `_run` seam); the
pipeline's public `search_blueprints`/`search_knowledge` single-corpus methods;
and the three tools' behaviours — provenance == frozenset(), empty-ok,
malformed-args → RETRIEVAL_TOOL_INVALID_ARGS, degrade, scope pre-filter drop,
the getBlueprint out-of-scope==absent non-oracle, and `query` redaction.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context import scope_filter
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import (
    GetBlueprintTool,
    SearchBlueprintsTool,
    SearchKnowledgeTool,
)
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import (
    FakeVectorIndex,
    Neo4jVectorIndex,
    map_blueprint_detail_record,
)
from data_agent.runtime.session.models import ResultPreview, TrailEntry

_Q = "how much overtime did the sales team work"
_QVEC = [1.0, 0.0]
_A = "dbpcm_warehouse.payroll.Amount"
_B = "dbpcm_warehouse.employee.Department"


def _creds(scope: frozenset[str] = frozenset()) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s1", jwt="jwt-secret", column_scope=scope)


def _bp(id: str, intent: str, uses: set[str], vec: list[float]) -> tuple[Candidate, list[float]]:
    return (
        Candidate(
            id=id,
            kind="blueprint",
            text=intent,
            uses=frozenset(uses),
            payload={"intent": intent, "slots_summary": f"slots-of-{id}"},
        ),
        vec,
    )


def _kn(id: str, chunk: str, vec: list[float], title: str | None = None) -> tuple[Candidate, list[float]]:
    return (
        Candidate(id=id, kind="knowledge", text=chunk, uses=None, payload={"title": title}),
        vec,
    )


def _detail(id: str = "bp-x", uses: frozenset[str] | None = frozenset({_A, _B})) -> BlueprintDetail:
    return BlueprintDetail(
        id=id,
        intent="Total overtime pay by department",
        slots_summary="department, pay_period",
        uses=uses,
        status="validated",
        drift_status="clean",
        hit_count=0,
        catalog_sha="sha123",
    )


_DEFAULT_EMBEDDER = object()  # sentinel: distinguishes "omitted" from an explicit None


def _pipeline(
    *,
    index: FakeVectorIndex,
    embedder: Any = _DEFAULT_EMBEDDER,
    reranker: FakeRerankerClient | None = None,
    recall_k: int = 30,
    top_k_blueprints: int = 3,
    top_k_knowledge: int = 3,
) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=(
            FakeEmbeddingClient({_Q: _QVEC}) if embedder is _DEFAULT_EMBEDDER else embedder
        ),
        reranker=reranker,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=recall_k,
        top_k_blueprints=top_k_blueprints,
        top_k_knowledge=top_k_knowledge,
    )


# ---------------------------------------------------------------------------
# BlueprintDetail record mapping (locks the field set without infra)
# ---------------------------------------------------------------------------


def test_map_blueprint_detail_record_locks_projection_fields() -> None:
    record = {
        "id": "bp-overtime",
        "intent": "Total overtime pay by department",
        "slots_summary": "department, pay_period",
        "uses": [_A, _B],
        "status": "validated",
        "drift_status": "clean",
        "hit_count": 7,
        "catalog_sha": "sha-abc",
    }
    detail = map_blueprint_detail_record(record)
    assert detail.id == "bp-overtime"
    assert detail.intent == "Total overtime pay by department"
    assert detail.slots_summary == "department, pay_period"
    assert detail.uses == frozenset({_A, _B})
    assert detail.status == "validated"
    assert detail.drift_status == "clean"
    assert detail.hit_count == 7
    assert detail.catalog_sha == "sha-abc"


def test_map_blueprint_detail_record_uses_fail_closed_on_corrupt() -> None:
    # A bare string / null `uses` is UNDETERMINED → None (fail-closed), never a
    # char-exploded or empty frozenset (would fail-open on the scope check).
    assert map_blueprint_detail_record({"id": "b", "uses": "not-a-list"}).uses is None
    assert map_blueprint_detail_record({"id": "b", "uses": None}).uses is None
    # A non-int hit_count defaults to 0, never raises.
    assert map_blueprint_detail_record({"id": "b", "uses": [_A], "hit_count": None}).hit_count == 0


# ---------------------------------------------------------------------------
# get_blueprint — Fake + Neo4j store seam
# ---------------------------------------------------------------------------


async def test_fake_get_blueprint_hit_miss_and_fail() -> None:
    index = FakeVectorIndex(details={"bp-x": _detail("bp-x")})
    assert (await index.get_blueprint("bp-x")).id == "bp-x"  # type: ignore[union-attr]
    assert await index.get_blueprint("nope") is None  # a miss
    failing = FakeVectorIndex(details={"bp-x": _detail("bp-x")}, fail=True)
    assert await failing.get_blueprint("bp-x") is None  # store-unavailable degrade


async def test_neo4j_get_blueprint_maps_row_and_degrades() -> None:
    async def _run_hit(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert params == {"id": "bp-overtime"}  # parameterized, never interpolated
        return [
            {
                "id": "bp-overtime",
                "intent": "Total overtime pay",
                "slots_summary": "department",
                "uses": [_A, _B],
                "status": "validated",
                "drift_status": "clean",
                "hit_count": 0,
                "catalog_sha": "sha",
            }
        ]

    index = Neo4jVectorIndex(
        url="bolt://unused:7687", auth=("u", "p"), expected_model="m", driver=object()
    )
    index._run = _run_hit  # type: ignore[method-assign, assignment]
    detail = await index.get_blueprint("bp-overtime")
    assert detail is not None and detail.uses == frozenset({_A, _B})

    async def _run_empty(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    index._run = _run_empty  # type: ignore[method-assign, assignment]
    assert await index.get_blueprint("bp-overtime") is None  # miss → None

    async def _run_raise(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("neo4j down")

    index._run = _run_raise  # type: ignore[method-assign, assignment]
    assert await index.get_blueprint("bp-overtime") is None  # degrade → None, never raises


# ---------------------------------------------------------------------------
# pipeline.search_blueprints / search_knowledge (public single-corpus methods)
# ---------------------------------------------------------------------------


async def test_search_blueprints_reranks_and_scope_prefilters() -> None:
    index = FakeVectorIndex(
        [
            _bp("in", "sales overtime rollup", {_A}, [1.0, 0.0]),
            _bp("out", "headcount", {"other.t.c"}, [0.95, 0.0]),
        ]
    )
    reranker = FakeRerankerClient({"sales overtime rollup": 0.9})
    pipeline = _pipeline(index=index, reranker=reranker)
    cards, reranked = await pipeline.search_blueprints(
        question=_Q, column_scope=frozenset({_A}), k=5
    )
    assert [c.id for c in cards] == ["in"]  # `out` scope-dropped
    assert reranked is True


async def test_search_blueprints_no_embedder_degrades_empty() -> None:
    pipeline = _pipeline(index=FakeVectorIndex([_bp("a", "x", {_A}, [1.0, 0.0])]), embedder=None)
    cards, reranked = await pipeline.search_blueprints(
        question=_Q, column_scope=frozenset(), k=5
    )
    assert cards == []
    assert reranked is False


async def test_search_blueprints_no_reranker_recall_order_not_reranked() -> None:
    index = FakeVectorIndex(
        [_bp("near", "n", {_A}, [1.0, 0.0]), _bp("far", "f", {_A}, [0.0, 1.0])]
    )
    pipeline = _pipeline(index=index, reranker=None)
    cards, reranked = await pipeline.search_blueprints(
        question=_Q, column_scope=frozenset(), k=5
    )
    assert [c.id for c in cards] == ["near", "far"]
    assert reranked is False


async def test_search_blueprints_recall_pool_uses_max_recall_k_and_cuts_to_k() -> None:
    index = FakeVectorIndex([_bp(f"b{i}", f"i{i}", {_A}, [1.0, i / 100]) for i in range(8)])
    pipeline = _pipeline(index=index, reranker=None, recall_k=2)
    cards, _ = await pipeline.search_blueprints(question=_Q, column_scope=frozenset(), k=5)
    # Recall pool = max(recall_k=2, k=5) = 5; the reranked list is then cut to k.
    assert index.calls[-1] == ("blueprint", 5)
    assert len(cards) == 5


async def test_search_knowledge_not_scope_filtered_and_cuts_to_k() -> None:
    index = FakeVectorIndex([_kn(f"k{i}", f"chunk {i}", [1.0, i / 100]) for i in range(6)])
    pipeline = _pipeline(index=index, reranker=None)
    hits, _ = await pipeline.search_knowledge(question=_Q, k=2)
    # Even with a narrow scope, knowledge is never dropped (entity-agnostic).
    assert len(hits) == 2


# ---------------------------------------------------------------------------
# SearchBlueprintsTool
# ---------------------------------------------------------------------------


async def test_search_blueprints_tool_ok_shape_and_provenance() -> None:
    index = FakeVectorIndex([_bp("in", "overtime rollup", {_A}, [1.0, 0.0])])
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=index, reranker=FakeRerankerClient({"overtime rollup": 0.9})),
        default_k=5,
        max_k=20,
    )
    result = await tool.run({"query": _Q}, _creds(frozenset({_A})))
    assert result.status == "ok"
    assert result.provenance == frozenset()  # safe-empty, kept in D44 replay
    assert result.result_full["count"] == 1
    assert result.result_full["degraded"] is False
    assert result.result_full["blueprints"][0]["id"] == "in"


async def test_search_blueprints_tool_empty_is_ok() -> None:
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=FakeVectorIndex([])), default_k=5, max_k=20
    )
    result = await tool.run({"query": _Q}, _creds())
    assert result.status == "ok"
    assert result.result_full["count"] == 0
    assert result.result_full["blueprints"] == []


async def test_search_blueprints_tool_degraded_flag_true_without_reranker() -> None:
    index = FakeVectorIndex([_bp("a", "x", {_A}, [1.0, 0.0])])
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index, reranker=None), default_k=5, max_k=20)
    result = await tool.run({"query": _Q}, _creds())
    assert result.status == "ok"
    assert result.result_full["degraded"] is True


async def test_search_blueprints_tool_scope_drop_removes_unrunnable_card() -> None:
    index = FakeVectorIndex(
        [_bp("in", "a", {_A}, [1.0, 0.0]), _bp("out", "b", {"x.y.z"}, [1.0, 0.0])]
    )
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    result = await tool.run({"query": _Q}, _creds(frozenset({_A})))
    assert [b["id"] for b in result.result_full["blueprints"]] == ["in"]


async def test_search_blueprints_tool_malformed_query_and_k() -> None:
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=FakeVectorIndex([])), default_k=5, max_k=20
    )
    for bad_args in ({}, {"query": ""}, {"query": "   "}):
        r = await tool.run(bad_args, _creds())
        assert r.status == "error" and r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"
    for bad_k in ("5", -1, True):
        r = await tool.run({"query": _Q, "k": bad_k}, _creds())
        assert r.status == "error" and r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"


async def test_search_blueprints_tool_k_over_max_is_clamped_not_rejected() -> None:
    index = FakeVectorIndex([_bp(f"b{i}", f"i{i}", {_A}, [1.0, i / 100]) for i in range(30)])
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=index, reranker=None, recall_k=30), default_k=5, max_k=20
    )
    result = await tool.run({"query": _Q, "k": 100}, _creds())
    assert result.status == "ok"
    assert result.result_full["count"] == 20  # clamped to max_k, never an error


# ---------------------------------------------------------------------------
# GetBlueprintTool — the non-oracle
# ---------------------------------------------------------------------------


async def test_get_blueprint_tool_in_scope_hit_returns_projection() -> None:
    index = FakeVectorIndex(details={"bp-x": _detail("bp-x", frozenset({_A, _B}))})
    tool = GetBlueprintTool(vector_index=index)
    result = await tool.run({"id": "bp-x"}, _creds(frozenset({_A, _B})))
    assert result.status == "ok"
    # S1: provenance is the blueprint's scoped `uses` footprint (NOT frozenset())
    # so the entry drops from D44 replay under a later scope narrowing — split
    # "db.table.column" into the (db_table, column) tuples the filter consumes.
    assert result.provenance == frozenset(
        {("dbpcm_warehouse.payroll", "Amount"), ("dbpcm_warehouse.employee", "Department")}
    )
    full = result.result_full
    assert full["found"] is True
    assert full["id"] == "bp-x"
    assert full["uses"] == sorted([_A, _B])  # rendered as a sorted list
    assert full["status"] == "validated"
    assert full["hit_count"] == 0


async def test_get_blueprint_tool_out_of_scope_and_absent_are_indistinguishable() -> None:
    # The load-bearing non-oracle assertion (§3): a real miss and an
    # out-of-scope blueprint return the BYTE-IDENTICAL {found: false}.
    index = FakeVectorIndex(details={"secret": _detail("secret", frozenset({_A}))})
    tool = GetBlueprintTool(vector_index=index)
    scope = frozenset({_B})  # does NOT cover the blueprint's uses ({_A})

    out_of_scope = await tool.run({"id": "secret"}, _creds(scope))
    absent = await tool.run({"id": "does-not-exist"}, _creds(scope))

    assert out_of_scope.status == absent.status == "ok"
    assert out_of_scope.result_full == absent.result_full == {"found": False}


async def test_get_blueprint_tool_store_failure_is_not_found() -> None:
    index = FakeVectorIndex(details={"bp-x": _detail("bp-x")}, fail=True)
    tool = GetBlueprintTool(vector_index=index)
    result = await tool.run({"id": "bp-x"}, _creds(frozenset({_A, _B})))
    assert result.status == "ok"
    assert result.result_full == {"found": False}


async def test_get_blueprint_entry_drops_from_d44_replay_under_narrowed_scope() -> None:
    # S1: a getBlueprint FOUND entry carries the blueprint's `uses` footprint as
    # provenance, so under a later scope narrowing the D44 replay filter DROPS it
    # (the model can no longer re-surface a now-forbidden blueprint's existence +
    # footprint) — while a searchKnowledge entry (frozenset()) SURVIVES.
    call_scope = frozenset({_A, _B})
    gb = GetBlueprintTool(
        vector_index=FakeVectorIndex(details={"bp-x": _detail("bp-x", call_scope)})
    )
    sk = SearchKnowledgeTool(
        pipeline=_pipeline(index=FakeVectorIndex([_kn("kn-1", "overtime is 1.5x", [1.0, 0.0])])),
        knowledge_k=5,
    )
    gb_result = await gb.run({"id": "bp-x"}, _creds(call_scope))
    sk_result = await sk.run({"query": _Q}, _creds(call_scope))

    def _entry(call_id: str, tool_name: str, provenance: object) -> TrailEntry:
        return TrailEntry(
            turn_index=0,
            tool_call_id=call_id,
            tool_name=tool_name,
            args={},
            status="ok",
            error_code=None,
            provenance=provenance,  # type: ignore[arg-type]
            result_preview=ResultPreview(
                columns=[], row_count=1, truncated=False, preview_rows=[[{"x": 1}]]
            ),
            result_full_ref="ref",
            ts="t",
        )

    gb_entry = _entry("gb", "getBlueprint", gb_result.provenance)
    sk_entry = _entry("sk", "searchKnowledge", sk_result.provenance)
    # A narrowed scope that no longer covers the blueprint's uses.
    narrowed = frozenset({"dbpcm_warehouse.other.Col"})

    kept_ids = [e.tool_call_id for e in scope_filter.filter_trail([gb_entry, sk_entry], narrowed)]
    assert "gb" not in kept_ids  # getBlueprint footprint ⊄ narrowed scope → DROPPED
    assert "sk" in kept_ids  # searchKnowledge frozenset() → always kept


async def test_get_blueprint_tool_undetermined_uses_fail_closed() -> None:
    # A blueprint whose stored `uses` is undetermined (None) is fail-closed to
    # not-found, never fail-open — even under an empty (allow-all) scope.
    index = FakeVectorIndex(details={"bp-x": _detail("bp-x", uses=None)})
    tool = GetBlueprintTool(vector_index=index)
    result = await tool.run({"id": "bp-x"}, _creds(frozenset()))
    assert result.result_full == {"found": False}


async def test_get_blueprint_tool_malformed_id() -> None:
    tool = GetBlueprintTool(vector_index=FakeVectorIndex())
    for bad in ({}, {"id": ""}, {"id": "  "}):
        r = await tool.run(bad, _creds())
        assert r.status == "error" and r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"


# ---------------------------------------------------------------------------
# SearchKnowledgeTool — scope bypass
# ---------------------------------------------------------------------------


async def test_search_knowledge_tool_ok_and_scope_bypassed() -> None:
    index = FakeVectorIndex([_kn("kn-1", "overtime is 1.5x", [1.0, 0.0], title="OT rule")])
    tool = SearchKnowledgeTool(pipeline=_pipeline(index=index), knowledge_k=5)
    # A narrow scope must NOT filter knowledge (entity-agnostic).
    result = await tool.run({"query": _Q}, _creds(frozenset({"x.y.z"})))
    assert result.status == "ok"
    assert result.provenance == frozenset()
    assert result.result_full["count"] == 1
    assert result.result_full["knowledge"][0]["id"] == "kn-1"
    assert result.result_full["knowledge"][0]["title"] == "OT rule"


async def test_search_knowledge_tool_malformed_query() -> None:
    tool = SearchKnowledgeTool(pipeline=_pipeline(index=FakeVectorIndex([])), knowledge_k=5)
    r = await tool.run({}, _creds())
    assert r.status == "error" and r.error_code == "RETRIEVAL_TOOL_INVALID_ARGS"


# ---------------------------------------------------------------------------
# Redaction (D25) — `query` fully redacted; `id`/`k` preserved
# ---------------------------------------------------------------------------


def test_redact_tool_args_redacts_query_keeps_id_and_k() -> None:
    # Imported here (not at module top) to keep this file's first data_agent
    # import off the pre-existing observability.redaction ⇄ dispatch import
    # cycle (context loads first via retrieval.tools above).
    from data_agent.runtime.observability.redaction import redact_tool_args

    sb = redact_tool_args("searchBlueprints", {"query": "salary of Jane Doe", "k": 5})
    assert sb["query"] == "<redacted>"
    assert sb["k"] == 5  # structural, kept
    gb = redact_tool_args("getBlueprint", {"id": "bp-overtime"})
    assert gb["id"] == "bp-overtime"  # non-PII structural id, kept
    sk = redact_tool_args("searchKnowledge", {"query": "how is overtime defined"})
    assert sk["query"] == "<redacted>"
