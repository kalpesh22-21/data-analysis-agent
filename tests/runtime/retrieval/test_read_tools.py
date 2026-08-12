"""Layer-1 tests for the model-facing read tools (read-tools-design §7).

All fakes, no infra. Covers: `BlueprintDetail` record mapping; `get_blueprint`
on both the Fake and the Neo4j store (via the monkeypatched `_run` seam); the
pipeline's public `search_blueprints`/`search_knowledge` single-corpus methods;
and the three tools' behaviours — provenance == frozenset(), empty-ok,
malformed-args → RETRIEVAL_TOOL_INVALID_ARGS, degrade, scope pre-filter drop,
the getBlueprint out-of-scope==absent non-oracle, and `query` redaction.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.context import scope_filter
from data_agent.runtime.dispatch.tool_dispatcher import _DEFAULT_MAX_TOOL_RESULT_TOKENS
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate, ThinCard
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import (
    GetBlueprintTool,
    SearchBlueprintsTool,
    SearchKnowledgeTool,
    _cards_to_provenance,
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


def _bp(
    id: str,
    intent: str,
    uses: set[str],
    vec: list[float],
    *,
    resolves: dict[str, Any] | None = None,
    slots: list[dict[str, Any]] | None = None,
    result_grain: Any = None,
) -> tuple[Candidate, list[float]]:
    """Seed one blueprint candidate.

    `FakeVectorIndex` builds no candidates of its own — tests seed `Candidate`s
    directly — so FAKE-INDEX PARITY with the release-1 §02 enrichment is a
    FIXTURE change: the payload carries the same RAW decoded keys
    `map_blueprint_record` now writes (`resolves`/`slots`/`result_grain`).
    Omitted → `None`, i.e. a blueprint that stored no DAG.
    """
    return (
        Candidate(
            id=id,
            kind="blueprint",
            text=intent,
            uses=frozenset(uses),
            payload={
                "intent": intent,
                "slots_summary": f"slots-of-{id}",
                "resolves": resolves,
                "slots": slots,
                "result_grain": result_grain,
            },
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
    # release-1 §02 ⚠Provenance: NO LONGER the safe-empty frozenset(). An
    # enriched card names columns, so the entry carries the union of the returned
    # cards' `uses` and drops from D44 replay under a later scope narrowing.
    assert result.provenance == frozenset({("dbpcm_warehouse.payroll", "Amount")})
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
    # No cards → an empty UNION → the determined safe-empty frozenset(), i.e.
    # byte-identical D44 behaviour to before enrichment for the empty result.
    assert result.provenance == frozenset()


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
# SearchBlueprintsTool — card enrichment (release-1 §02)
# ---------------------------------------------------------------------------


_AUTHORED_SLOTS = [
    {
        "name": "department",
        "type": "string",
        "required": True,
        # The three fields that must NEVER reach a card.
        "binds_to": "dbpcm_warehouse.employee.department_name",
        "enum_values": ["Sales", "Ops"],
        "optional_pattern": "TRUE",
        "min_value": 1,
        "max_value": 12,
    },
    {"name": "pay_period", "type": "period", "required": False},
]


def _enriched_index() -> FakeVectorIndex:
    return FakeVectorIndex(
        [
            _bp(
                "in",
                "overtime rollup",
                {_A},
                [1.0, 0.0],
                resolves={"salary": "annual_salary"},
                slots=_AUTHORED_SLOTS,
                result_grain=["Department"],
            )
        ]
    )


async def _enriched_card(index: FakeVectorIndex | None = None) -> dict[str, Any]:
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=index or _enriched_index(), reranker=None),
        default_k=5,
        max_k=20,
    )
    result = await tool.run({"query": _Q}, _creds(frozenset({_A})))
    assert result.status == "ok"
    return result.result_full["blueprints"][0]


async def test_search_blueprints_card_carries_the_enrichment_keys() -> None:
    card = await _enriched_card()
    assert card["resolves"] == {"salary": "annual_salary"}
    assert card["result_grain"] == ["Department"]
    assert card["slots"] == [
        {"name": "department", "type": "string", "required": True},
        {"name": "pay_period", "type": "period", "required": False},
    ]
    # The pre-existing four keys are untouched.
    assert card["id"] == "in"
    assert card["intent"] == "overtime rollup"
    assert card["slots_summary"] == "slots-of-in"
    assert card["score"] == 1.0  # no reranker → recall similarity is the score


async def test_search_blueprints_card_slots_never_carry_execution_detail() -> None:
    # THE projection assertion. `slots_json` stores fully-qualified column paths
    # in `binds_to`; a card in a list of k must carry {name,type,required} ONLY.
    card = await _enriched_card()
    for slot in card["slots"]:
        assert set(slot) == {"name", "type", "required"}
    serialised = str(card)
    for leaked in ("binds_to", "enum_values", "optional_pattern", "min_value", "max_value"):
        assert leaked not in serialised
    assert "department_name" not in serialised  # the column path itself


async def test_search_blueprints_card_omits_status() -> None:
    # Deliberately absent: recall filters status='validated', so the field is a
    # constant on every search card and would imply a distinction that cannot
    # occur here. It stays on getBlueprint, where a keyed fetch can return one.
    assert "status" not in await _enriched_card()


async def test_search_blueprints_card_without_a_dag_is_byte_identical_to_before() -> None:
    # The strict back-compat guarantee: a blueprint that stored no DAG omits all
    # the new keys, so its serialisation is exactly the pre-enrichment four.
    card = await _enriched_card(FakeVectorIndex([_bp("in", "overtime rollup", {_A}, [1.0, 0.0])]))
    assert card == {
        "id": "in",
        "intent": "overtime rollup",
        "slots_summary": "slots-of-in",
        "score": 1.0,
    }


async def test_search_blueprints_card_omits_individually_absent_keys() -> None:
    # Partial DAGs are normal (most seeds have no `resolves`): each key is
    # omitted on its own, not all-or-nothing.
    card = await _enriched_card(
        FakeVectorIndex([_bp("in", "i", {_A}, [1.0, 0.0], result_grain=["Department"])])
    )
    assert card["result_grain"] == ["Department"]
    assert "resolves" not in card
    assert "slots" not in card
    assert "slots_omitted" not in card


async def test_search_blueprints_card_caps_slots_at_six_with_an_overflow_count() -> None:
    many = [{"name": f"slot{i}", "type": "string", "required": True} for i in range(12)]
    card = await _enriched_card(
        FakeVectorIndex([_bp("in", "i", {_A}, [1.0, 0.0], slots=many)])
    )
    assert len(card["slots"]) == 6
    assert [s["name"] for s in card["slots"]] == [f"slot{i}" for i in range(6)]
    # Without this the model would believe the blueprint has 6 slots and
    # under-fill runBlueprint. Absent when nothing was dropped (asserted above).
    assert card["slots_omitted"] == 6


async def test_search_blueprints_card_coerces_dict_shaped_result_grain() -> None:
    card = await _enriched_card(
        FakeVectorIndex(
            [
                _bp(
                    "in",
                    "i",
                    {_A},
                    [1.0, 0.0],
                    result_grain={"columns": ["Department"], "verifiable": False},
                )
            ]
        )
    )
    # The column list only — `verifiable` is a D56 statement about the runtime's
    # own checking and is dropped rather than invite the model to reason about it.
    assert card["result_grain"] == ["Department"]


async def test_search_blueprints_card_degrades_on_corrupt_payload_without_failing() -> None:
    # A corrupt stored prop reaches the payload as a wrong-typed value; the card
    # must degrade to "absent", never raise and never emit a broken shape.
    card = await _enriched_card(
        FakeVectorIndex(
            [
                _bp(
                    "in",
                    "i",
                    {_A},
                    [1.0, 0.0],
                    resolves={"salary": 7},  # non-str value → dropped
                    slots=["not-a-slot", {"no_name": True}],  # type: ignore[list-item]
                    result_grain=[7, None],  # no usable strings
                )
            ]
        )
    )
    assert "resolves" not in card
    assert "slots" not in card
    assert "result_grain" not in card


async def test_search_blueprints_entry_drops_from_d44_replay_under_narrowed_scope() -> None:
    # release-1 §02 ⚠Provenance. Enriched cards NAME columns (`resolves` is
    # term → column name, `result_grain` a column/alias list), so the entry can
    # no longer carry the safe-empty frozenset() that would keep it in replay
    # forever. Its provenance is the UNION of the returned cards' `uses`, so it
    # drops the moment scope narrows past ANY of that union — while a
    # searchKnowledge entry (genuinely column-free) survives.
    call_scope = frozenset({_A, _B})
    index = FakeVectorIndex(
        [
            _bp("bp-a", "overtime", {_A}, [1.0, 0.0], resolves={"salary": "Amount"}),
            _bp("bp-b", "headcount", {_B}, [1.0, 0.01], result_grain=["Department"]),
        ]
    )
    sb = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    sk = SearchKnowledgeTool(
        pipeline=_pipeline(index=FakeVectorIndex([_kn("kn-1", "overtime is 1.5x", [1.0, 0.0])])),
        knowledge_k=5,
    )
    sb_result = await sb.run({"query": _Q}, _creds(call_scope))
    sk_result = await sk.run({"query": _Q}, _creds(call_scope))

    assert {b["id"] for b in sb_result.result_full["blueprints"]} == {"bp-a", "bp-b"}
    # The union of BOTH cards' footprints, not just the first.
    assert sb_result.provenance == frozenset(
        {("dbpcm_warehouse.payroll", "Amount"), ("dbpcm_warehouse.employee", "Department")}
    )

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

    sb_entry = _entry("sb", "searchBlueprints", sb_result.provenance)
    sk_entry = _entry("sk", "searchKnowledge", sk_result.provenance)

    # Still in scope at the call scope: the union is a subset by construction.
    assert {e.tool_call_id for e in scope_filter.filter_trail([sb_entry], call_scope)} == {"sb"}

    # Narrow past ONE column of the union — whole-entry granularity is coarse but
    # fail-closed, so the entry drops rather than replaying a card that names a
    # now-forbidden column.
    narrowed = frozenset({_A})
    kept_ids = [e.tool_call_id for e in scope_filter.filter_trail([sb_entry, sk_entry], narrowed)]
    assert "sb" not in kept_ids
    assert "sk" in kept_ids

    # And past ALL of it.
    unrelated = frozenset({"dbpcm_warehouse.other.Col"})
    assert "sb" not in [
        e.tool_call_id for e in scope_filter.filter_trail([sb_entry], unrelated)
    ]


async def test_search_blueprints_provenance_fails_closed_on_a_malformed_uses_key() -> None:
    # A malformed stored key ("no dots") makes the WHOLE union undetermined →
    # None → dropped from replay. Never a partial set that could fail open.
    index = FakeVectorIndex([_bp("in", "i", {"malformed"}, [1.0, 0.0])])
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    result = await tool.run({"query": _Q}, _creds())  # empty scope = allow-all
    assert result.status == "ok"
    assert result.provenance is None
    assert not scope_filter.is_provenance_in_scope(result.provenance, frozenset({_A}))


# ---------------------------------------------------------------------------
# The printed-column guard (release-1 §02 ⚠Provenance — the QA residue)
# ---------------------------------------------------------------------------
#
# §02 set the entry's provenance to the union of the cards' `uses` and stopped
# there. But `uses` is the transitive set the DAG READS, while `resolves` is
# AUTHORED disambiguation metadata — nothing makes them the same set, so a card
# could print a column OUTSIDE the footprint the entry claims, and then survive a
# narrowing to exactly that footprint while naming a column the caller had just
# lost. The guard derives the printed identifiers from the SERIALISED card and
# fails closed to `None` when the claimed union does not cover them.


async def test_provenance_fails_closed_when_a_card_prints_a_column_outside_uses() -> None:
    # THE leak: uses={payroll.Amount} but the card pins "salary" to a column of a
    # different table. Claiming {payroll.Amount} would keep the entry in scope
    # under a narrowing to exactly {payroll.Amount} — replaying "annual_salary"
    # after the caller lost access to it.
    index = FakeVectorIndex(
        [_bp("in", "i", {_A}, [1.0, 0.0], resolves={"salary": "annual_salary"})]
    )
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    result = await tool.run({"query": _Q}, _creds())
    assert result.status == "ok"
    assert "annual_salary" in json.dumps(result.result_full)  # it IS printed
    assert result.provenance is None
    assert not scope_filter.is_provenance_in_scope(result.provenance, frozenset({_A}))


async def test_covered_resolves_and_grain_stay_determined_despite_the_qualification_gap() -> None:
    # The other half, and the reason the comparison cannot be a naive string
    # equality: `uses` is fully qualified and the card prints BARE names. Both
    # printed columns here ARE the footprint's columns, spelled the only way a
    # card can spell them — the entry must stay DETERMINED, or the release's
    # primary route would lose its own search results one round-trip later.
    index = FakeVectorIndex(
        [
            _bp(
                "in",
                "i",
                {_A, _B},
                [1.0, 0.0],
                resolves={"pay": "Amount", "team": "employee.Department"},
                result_grain=["Department"],
            )
        ]
    )
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    result = await tool.run({"query": _Q}, _creds(frozenset({_A, _B})))
    assert result.provenance == frozenset(
        {("dbpcm_warehouse.payroll", "Amount"), ("dbpcm_warehouse.employee", "Department")}
    )
    # In scope at the call scope; drops the moment scope narrows past the union.
    assert scope_filter.is_provenance_in_scope(result.provenance, frozenset({_A, _B}))
    assert not scope_filter.is_provenance_in_scope(result.provenance, frozenset({_A}))


async def test_slot_labels_and_display_grain_labels_do_not_fail_closed() -> None:
    # Over-strictness is the other failure mode, and it is the silent one: a slot
    # NAME is an authored bind-site label ("department"), and a grain entry is an
    # output-column display label for a `SELECT … AS x` — the real corpus declares
    # `[Department]` over `employee.department_name` on 6 of its 11 seeds. Neither
    # is a column identifier, and requiring them to be covered would delete most
    # search entries from replay.
    index = FakeVectorIndex(
        [
            _bp(
                "in",
                "i",
                {"dbpcm_warehouse.employee.department_name"},
                [1.0, 0.0],
                slots=[{"name": "department", "type": "string", "required": True}],
                result_grain=["Department", "month"],
            )
        ]
    )
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    result = await tool.run({"query": _Q}, _creds())
    assert result.provenance == frozenset({("dbpcm_warehouse.employee", "department_name")})


async def test_a_qualified_column_anywhere_on_a_card_must_be_covered() -> None:
    # A dotted path is a column identifier under any reading — a display label it
    # is not. This is what catches `binds_to` (or any authored qualified path) if
    # it ever reaches a card through a field nobody re-classified.
    index = FakeVectorIndex(
        [
            _bp(
                "in",
                "i",
                {_A},
                [1.0, 0.0],
                slots=[
                    {
                        "name": "department",
                        "type": "string",
                        "required": True,
                        "binds_to": "dbpcm_warehouse.employee.department_name",
                    }
                ],
                result_grain=["dbpcm_warehouse.employee.department_name"],
            )
        ]
    )
    tool = SearchBlueprintsTool(pipeline=_pipeline(index=index), default_k=5, max_k=20)
    assert (await tool.run({"query": _Q}, _creds())).provenance is None


async def test_a_future_card_field_carrying_a_column_name_fails_closed_by_construction() -> None:
    # The point of deriving the guard from the SERIALISED card rather than from a
    # list of field names: an UNCLASSIFIED key is walked whole and everything it
    # carries must be covered. Called directly because no such field exists yet —
    # which is exactly the case that must already be safe.
    card = ThinCard(id="in", intent="i", slots_summary="", score=1.0, uses=frozenset({_A}))
    covered = _cards_to_provenance([card], [{"id": "in", "sort_column": "Amount"}])
    assert covered == frozenset({("dbpcm_warehouse.payroll", "Amount")})
    assert _cards_to_provenance([card], [{"id": "in", "sort_column": "annual_salary"}]) is None


async def test_an_undetermined_footprint_is_never_silent() -> None:
    # Degrade-not-fail, never silently: the entry is about to be dropped from every
    # later replay, so the failure is logged server-side and emitted as shape-only
    # telemetry (counts + tool name, never a card id, column or the query).
    events: list[tuple[str, dict[str, Any]]] = []
    index = FakeVectorIndex([_bp("in", "i", {_A}, [1.0, 0.0], resolves={"s": "annual_salary"})])
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=index),
        default_k=5,
        max_k=20,
        observer=lambda name, payload: events.append((name, payload)),
    )
    result = await tool.run({"query": _Q}, _creds())
    assert result.provenance is None
    assert ("retrieval_provenance_undetermined", {
        "tool_name": "searchBlueprints",
        "blueprints": 1,
    }) in events
    assert all("annual_salary" not in repr(payload) for _name, payload in events)


# ---------------------------------------------------------------------------
# The trail-replay truncation hazard (release-1 §02 §Pre-injected cards)
# ---------------------------------------------------------------------------
#
# With per-deliverable search as the default path, one turn persists several
# `searchBlueprints` entries of up to `k` cards, each replayed on every later
# round-trip. `_build_preview` sends the result to `_cap_nontabular_result` with
# a 4,000-token cap, and because the result dict has NO top-level `columns` key
# the over-cap path USED TO BE the STRINGIFY-AND-TRUNCATE branch — the model
# received a mangled JSON string instead of a card list, silently, on the
# release's primary route.
#
# FIXED: `_cap_nontabular_result` now has a cards-aware branch that drops whole
# low-scoring cards from the TAIL and marks the drop, so the model always gets a
# well-formed card list. The tests below pin all three regimes: under the cap
# (byte-identical passthrough), over the cap (graceful drop, head intact), and
# the operator lever that avoids the drop entirely.

_MAX_INTENT = (
    "Earnings by department and cost centre for a given pay period, filtered to a "
    "specific pay type (type_code) such as overtime, shift differential or bonus, "
    "restricted to active employees and reconciled against the payroll register"
)
_MAX_SLOTS_SUMMARY = (
    "department, cost_centre, pay_period, type_code, register_type, employee_status, "
    "location, job_family, pay_group, union_code, reason_code, currency"
)
_MAX_SLOT_NAMES = [
    "department_name", "cost_centre_code", "pay_period_end_date", "type_code",
    "register_type", "employee_status", "location_name", "job_family_name",
    "pay_group_code", "union_code", "reason_code", "currency_code",
]
_MAX_RESOLVES = {
    "salary": "annual_salary", "overtime": "type_code", "headcount": "employee_code",
    "department": "department_name", "cost centre": "cost_centre_code",
    "pay period": "pay_period_end_date", "status": "employee_status",
    "location": "location_name",
}
_MAX_GRAIN = [
    "Department", "CostCentre", "PayPeriod", "TypeCode",
    "Earnings", "Headcount", "Currency", "Location",
]


def _sized_index(count: int, *, maximal: bool) -> FakeVectorIndex:
    """`count` blueprint candidates, either corpus-scale (the shape of the real
    seeds in `tests/fixtures/corpus/blueprints.yaml`) or maximally enriched."""
    if maximal:
        return FakeVectorIndex(
            [
                _bp(
                    f"bp-earnings-by-department-and-cost-centre-{i:02d}",
                    _MAX_INTENT,
                    {_A},
                    [1.0, i / 100],
                    resolves=_MAX_RESOLVES,
                    slots=[
                        {"name": n, "type": "string", "required": True}
                        for n in _MAX_SLOT_NAMES
                    ],
                    result_grain=_MAX_GRAIN,
                )
                for i in range(count)
            ]
        )
    return FakeVectorIndex(
        [
            _bp(
                f"bp-overtime-by-department-{i:02d}",
                "Earnings by department for a given pay period, filtered to a specific "
                "pay type (type_code) such as overtime",
                {_A},
                [1.0, i / 100],
                resolves={"salary": "annual_salary"},
                slots=[
                    {"name": "department", "type": "string", "required": True},
                    {"name": "pay_period", "type": "period", "required": True},
                    {"name": "type_code", "type": "string", "required": True},
                ],
                result_grain=["Department"],
            )
            for i in range(count)
        ]
    )


async def _preview_at_max_k(
    index: FakeVectorIndex,
    *,
    max_result_tokens: int | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[Any, int]:
    kwargs: dict[str, Any] = {}
    if max_result_tokens is not None:
        kwargs["max_result_tokens"] = max_result_tokens
    if events is not None:
        kwargs["observer"] = lambda event, payload: events.append((event, payload))
    tool = SearchBlueprintsTool(
        pipeline=_pipeline(index=index, reranker=None, recall_k=30),
        default_k=5,
        max_k=20,
        **kwargs,
    )
    result = await tool.run({"query": _Q, "k": 20}, _creds())
    assert result.result_full["count"] == 20
    return result, len(json.dumps(result.result_full, default=str))


async def test_preview_not_truncated_at_max_k_for_corpus_scale_cards() -> None:
    # At k=max_k, cards shaped like the real seed corpus stay well under the
    # 4,000-token preview cap even fully enriched, so the model receives the card
    # LIST — and, being under the cap, the cell is the result dict UNTOUCHED.
    result, size = await _preview_at_max_k(_sized_index(20, maximal=False))
    assert result.result_preview.truncated is False, f"{size} chars"
    assert isinstance(result.result_preview.preview_rows[0][0], dict)


async def test_corpus_scale_preview_cell_is_byte_identical_to_the_full_result() -> None:
    # The no-regression case for the cards-aware cap: a normal corpus-scale result
    # must pass through the new branch UNCHANGED — same object, no dropped card, no
    # `_truncated` marker, no observer event. If the cap ever started rewriting the
    # under-cap path, this is what would catch it.
    events: list[tuple[str, dict[str, Any]]] = []
    result, size = await _preview_at_max_k(_sized_index(20, maximal=False), events=events)
    assert size <= _DEFAULT_MAX_TOOL_RESULT_TOKENS * 4
    cell = result.result_preview.preview_rows[0][0]
    assert cell is result.result_full  # not a copy, not a re-serialisation
    assert json.dumps(cell, default=str) == json.dumps(result.result_full, default=str)
    assert "_truncated" not in cell
    assert len(cell["blueprints"]) == 20
    assert [e for e in events if e[0] == "tool_dispatch_cards_dropped"] == []


async def test_preview_not_truncated_at_max_k_with_maximally_slotted_cards() -> None:
    # WAS a strict xfail (release-1 §02 recorded the measurement rather than fixing
    # it): 20 maximally-enriched cards serialise to ~25,600 chars against the
    # 4,000-token cap, and with no top-level `columns` key the over-cap path was
    # stringify-and-truncate — the model got mangled JSON, first at k=13.
    #
    # The cards-aware branch makes the DEGRADE the thing that gives, not the SHAPE:
    # the preview is still a well-formed card list of well-formed cards.
    result, size = await _preview_at_max_k(_sized_index(20, maximal=True))
    assert size > _DEFAULT_MAX_TOOL_RESULT_TOKENS * 4  # genuinely over the cap
    cell = result.result_preview.preview_rows[0][0]
    assert isinstance(cell, dict)
    assert isinstance(cell["blueprints"], list)
    assert cell["blueprints"]  # never emptied
    for card in cell["blueprints"]:
        # Every surviving card is WHOLE — this is the property stringify-and-
        # truncate destroyed (its last card was cut mid-token).
        assert set(card) >= {"id", "intent", "slots_summary", "score", "slots"}
        # 6 = the per-card slot projection cap (02), with the rest declared via
        # `slots_omitted` — i.e. the card is whole to ITS OWN contract.
        assert len(card["slots"]) == 6
        assert card["slots_omitted"] == len(_MAX_SLOT_NAMES) - 6
        assert card["result_grain"] == _MAX_GRAIN


async def test_max_k_maximal_cards_drop_whole_tail_cards_and_mark_the_drop() -> None:
    # Replaces `test_max_k_maximal_cards_currently_stringify_truncate`, which pinned
    # the stringify behaviour. Same fixture, new contract: cards are dropped from
    # the TAIL, the HEAD (highest-scoring) survives intact, the drop is marked for
    # the model and reported to the operator.
    events: list[tuple[str, dict[str, Any]]] = []
    full, _ = await _preview_at_max_k(_sized_index(20, maximal=True))
    result, size = await _preview_at_max_k(_sized_index(20, maximal=True), events=events)
    assert result.result_preview.truncated is True
    assert size > _DEFAULT_MAX_TOOL_RESULT_TOKENS * 4

    cell = result.result_preview.preview_rows[0][0]
    kept = cell["blueprints"]
    expected = full.result_full["blueprints"]
    assert 0 < len(kept) < len(expected)
    # The HEAD survives, in order, byte-identical — and it is the highest-scoring
    # end of the list, because the pipeline already returns cards score-ordered.
    assert kept == expected[: len(kept)]
    assert [c["score"] for c in kept] == sorted((c["score"] for c in kept), reverse=True)
    assert kept[0]["score"] >= max(c["score"] for c in expected[len(kept) :])

    # `result_full` is UNTOUCHED — the cap bounds the replayed preview only.
    assert len(result.result_full["blueprints"]) == 20

    # Marked for the model...
    dropped = 20 - len(kept)
    assert f"{dropped} lowest-scoring of 20 blueprint cards" in cell["_truncated"]
    # ...and reported to the operator, shape-only (D25: no card text/id/score).
    drops = [payload for event, payload in events if event == "tool_dispatch_cards_dropped"]
    assert drops == [
        {"tool_name": "searchBlueprints", "dropped_count": dropped, "total_count": 20}
    ]

    # The cell still fits the cap it was capped to.
    assert len(json.dumps(cell, default=str)) // 4 <= _DEFAULT_MAX_TOOL_RESULT_TOKENS


async def test_raising_max_result_tokens_keeps_every_maximal_card() -> None:
    # B2: the read tools used to hardcode `_build_preview`'s 4,000-token default,
    # so an operator raising `RuntimeSettings.max_tool_result_tokens` changed
    # nothing here. Now the lever reaches the tool: at a cap above the measured
    # ~25,600-char worst case, nothing is dropped at all.
    events: list[tuple[str, dict[str, Any]]] = []
    result, size = await _preview_at_max_k(
        _sized_index(20, maximal=True), max_result_tokens=10_000, events=events
    )
    assert size > _DEFAULT_MAX_TOOL_RESULT_TOKENS * 4  # over the DEFAULT cap
    assert result.result_preview.truncated is False
    cell = result.result_preview.preview_rows[0][0]
    assert len(cell["blueprints"]) == 20
    assert "_truncated" not in cell
    assert [e for e in events if e[0] == "tool_dispatch_cards_dropped"] == []


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
