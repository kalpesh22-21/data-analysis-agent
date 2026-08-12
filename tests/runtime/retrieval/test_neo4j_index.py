"""Layer-1 tests for Neo4jVectorIndex (neo4j-corpus-design §2) — no live neo4j.

Drives `recall` by monkeypatching the single driver-touching seam (`_run`) so
the row→Candidate mapping, the never-raises degrade, the parity guard and the
model-mismatch observability are all locked without infrastructure. The
load-bearing assertion is `Candidate.uses` being a `frozenset[str]` of
byte-exact `"database.table.column"` strings (§0/§8 highest-risk contract).
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.retrieval import vector_index as vi
from data_agent.runtime.retrieval.vector_index import (
    Neo4jVectorIndex,
    map_blueprint_record,
    map_knowledge_record,
)


def _index(run: Any) -> Neo4jVectorIndex:
    """A Neo4jVectorIndex whose driver is a sentinel and whose `_run` is faked —
    no real driver is ever touched."""
    index = Neo4jVectorIndex(
        url="bolt://unused:7687",
        auth=("u", "p"),
        expected_model="all-mpnet-base-v2",
        driver=object(),  # sentinel — `_run` is monkeypatched, driver never used
    )
    index._run = run  # type: ignore[method-assign, assignment]
    return index


# --------------------------------------------------------------------------
# Row -> Candidate mapping (the whole point of the class)
# --------------------------------------------------------------------------


def test_map_blueprint_record_uses_is_frozenset_of_byte_exact_strings() -> None:
    record = {
        "id": "bp-overtime",
        "text": "Total overtime pay by department",
        "slots_summary": "department, pay_period",
        "uses": [
            "dbpcm_warehouse.payroll.Amount",
            "dbpcm_warehouse.employee.Department",
        ],
        "score": 0.91,
    }
    candidate = map_blueprint_record(record)

    assert candidate.id == "bp-overtime"
    assert candidate.kind == "blueprint"
    assert candidate.text == "Total overtime pay by department"
    assert isinstance(candidate.uses, frozenset)
    assert all(isinstance(u, str) for u in candidate.uses)
    assert candidate.uses == frozenset(
        {"dbpcm_warehouse.payroll.Amount", "dbpcm_warehouse.employee.Department"}
    )
    # release-1 §02: the payload gained the three card-enrichment keys. This row
    # stores no DAG, so all three decode to `None` — the card then omits them and
    # serialises byte-identically to before enrichment.
    assert candidate.payload == {
        "intent": "Total overtime pay by department",
        "slots_summary": "department, pay_period",
        "resolves": None,
        "slots": None,
        "result_grain": None,
    }
    assert candidate.score == 0.91


def test_map_blueprint_record_null_uses_becomes_none_fail_closed() -> None:
    # B1: undetermined uses -> None (scope filter DROPs), NOT frozenset() (which
    # would be a subset of every scope -> silent fail-OPEN).
    record = {"id": "bp", "text": "t", "slots_summary": None, "uses": None, "score": 0.5}
    candidate = map_blueprint_record(record)
    assert candidate.uses is None
    assert candidate.payload["slots_summary"] == ""


def test_map_knowledge_record_uses_is_none() -> None:
    record = {
        "id": "kn-1",
        "text": "Overtime is paid at 1.5x.",
        "title": "Overtime rule",
        "doc_id": "hr-policy",
        "score": 0.8,
    }
    candidate = map_knowledge_record(record)
    assert candidate.kind == "knowledge"
    assert candidate.uses is None
    assert candidate.payload == {
        "title": "Overtime rule",
        "chunk": "Overtime is paid at 1.5x.",
        "doc_id": "hr-policy",
    }


# --------------------------------------------------------------------------
# recall — mapping through the faked driver, parity params, ordering
# --------------------------------------------------------------------------


async def test_recall_maps_blueprint_rows_and_preserves_uses_exactly() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": "bp-overtime",
                "text": "overtime rollup",
                "slots_summary": "dept",
                "uses": ["dbpcm_warehouse.payroll.Amount"],
                "score": 0.9,
            }
        ]

    got = await _index(fake_run).recall(
        query_vector=[0.1] * 768, kind="blueprint", k=30
    )
    assert [c.id for c in got] == ["bp-overtime"]
    assert got[0].uses == frozenset({"dbpcm_warehouse.payroll.Amount"})


async def test_recall_passes_expected_model_and_index_name() -> None:
    captured: dict[str, Any] = {}

    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        captured.update(parameters)
        captured["query"] = query
        return []

    await _index(fake_run).recall(query_vector=[0.0] * 768, kind="knowledge", k=7)
    assert captured["expected_model"] == "all-mpnet-base-v2"
    assert captured["index_name"] == "knowledge_text_vec"
    assert captured["k"] == 7


def test_parity_filter_is_in_both_recall_queries() -> None:
    # The parity guard is a WHERE clause IN the Cypher (design §2.2/§2.3): a
    # corpus embedded with a different model never comes back.
    for query in (vi._BLUEPRINT_RECALL_QUERY, vi._KNOWLEDGE_RECALL_QUERY):
        assert "WHERE node.embedding_model = $expected_model" in query


# --------------------------------------------------------------------------
# Never-raises degrade (D86)
# --------------------------------------------------------------------------


async def test_recall_returns_empty_when_run_raises() -> None:
    async def boom(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("neo4j is down / timed out")

    assert await _index(boom).recall(query_vector=[0.0] * 768, kind="blueprint", k=30) == []


async def test_recall_unknown_kind_returns_empty_without_touching_driver() -> None:
    async def fail(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("_run must not be called for an unknown kind")

    assert await _index(fail).recall(query_vector=[0.0] * 768, kind="mystery", k=30) == []


# --------------------------------------------------------------------------
# Model-mismatch observability (design §2.3)
# --------------------------------------------------------------------------


async def test_empty_recall_with_populated_corpus_flags_model_mismatch() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        # Vector query drops everything (parity WHERE); the count probe shows the
        # corpus is NOT empty -> total model mismatch.
        if "queryNodes" in query:
            return []
        return [{"c": 3}]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    index = _index(fake_run)
    with tracer.start_as_current_span("recall"):
        got = await index.recall(query_vector=[0.0] * 768, kind="blueprint", k=30)

    assert got == []
    spans = exporter.get_finished_spans()
    assert spans[0].attributes is not None
    assert spans[0].attributes.get("retrieval.model_mismatch") is True


async def test_empty_recall_with_empty_corpus_does_not_flag_mismatch() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        if "queryNodes" in query:
            return []
        return [{"c": 0}]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("recall"):
        await _index(fake_run).recall(query_vector=[0.0] * 768, kind="blueprint", k=30)

    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert "retrieval.model_mismatch" not in span.attributes


def test_current_span_helper_is_safe_without_a_tracer() -> None:
    # Sanity: get_current_span with no active span is a no-op span whose
    # set_attribute never raises (the mismatch flag path stays crash-proof).
    trace.get_current_span().set_attribute("retrieval.model_mismatch", True)


# --------------------------------------------------------------------------
# graph_ready — the /ready probe signal (singleton-hydrator redesign)
# --------------------------------------------------------------------------


async def test_graph_ready_true_when_corpus_sha_present() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"corpus_sha": "abc123"}]

    assert await _index(fake_run).graph_ready() is True


async def test_graph_ready_false_when_corpus_meta_absent() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        return []  # no :CorpusMeta singleton yet (graph not seeded)

    assert await _index(fake_run).graph_ready() is False


async def test_graph_ready_false_when_corpus_sha_is_null() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"corpus_sha": None}]

    assert await _index(fake_run).graph_ready() is False


async def test_graph_ready_degrades_driver_exception_to_false_not_500() -> None:
    async def fake_run(query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("neo4j unreachable")

    # A driver/query failure must degrade to not-ready (→ 503), never propagate (→ 500).
    assert await _index(fake_run).graph_ready() is False
