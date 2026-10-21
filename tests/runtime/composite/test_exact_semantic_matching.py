"""Exact identities stay intact; semantic matching uses business descriptions."""

import pytest

from data_agent.runtime.composite.ranking import RowValue, exact_match, rank
from data_agent.runtime.composite.resolve_values import _validate_embed_shape
from tests.runtime.composite.test_resolve_values import _composite, _credentials, _run_query_result


@pytest.mark.parametrize(
    "concept,expected",
    [
        (" vac_ctx ", "exact_code"),
        ("VAC_CTX", "exact_code"),
        ("  paid\tTIME   off ", "exact_description"),
        ("VAC-CTX", None),
        ("VACCTX", None),
        ("paid time", None),
        ("", None),
    ],
)
def test_exact_matching_is_equality_not_fuzzy_code_rewriting(concept, expected):
    assert exact_match(RowValue("VAC_CTX", "Paid Time Off", 1), concept) == expected


def test_rare_exact_match_beats_popular_semantic_match_and_survives_top_k():
    rows = [RowValue("OTHER", "Something else", 100000), RowValue("VAC_CTX", "Vacation", 1)]
    values = rank(
        rows=rows,
        row_vectors=[[1.0, 0.0], [0.0, 1.0]],
        concept_vector=[1.0, 0.0],
        similarity_weight=0.7,
        top_k=1,
        concept="vac_ctx",
    )
    assert values[0].value == "VAC_CTX"
    assert values[0].score == 1
    assert values[0].match_type == "exact_code"


def test_shared_exact_descriptions_keep_multiple_codes():
    rows = [
        RowValue("VAC1", "Vacation", 1),
        RowValue("VAC2", " vacation ", 20),
        RowValue("OTHER", "Not vacation", 1000),
    ]
    values = rank(
        rows=rows,
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=3,
        concept="Vacation",
    )
    assert {v.value for v in values[:2]} == {"VAC1", "VAC2"}
    assert all(v.match_type == "exact_description" for v in values[:2])
    assert values[-1].match_type == "frequency"


def test_semantic_text_uses_description_and_falls_back_for_blank_description():
    assert RowValue("OPAQUE_991", " Vacation\nleave ", 1).embedding_text() == "Vacation leave"
    assert RowValue("VAC_CTX", "  ", 1).embedding_text() == "VAC_CTX"
    assert RowValue("VAC_CTX", None, 1).embedding_text() == "VAC_CTX"


@pytest.mark.parametrize("embedding", [True, False])
async def test_exact_matching_survives_embedding_outage_without_more_queries(embedding):
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"],
        [["OTHER", "Other", 100000], ["VAC_CTX", "Vacation", 1]],
    )
    composite, mcp = _composite(
        scripted={"runQuery": [result]}, embedding=embedding, embed_fail=True
    )
    response = await composite.run(
        {"table": "accrual_events", "column": "EarnCode", "concept": "vac_ctx"}, _credentials()
    )
    assert response.status == "ok"
    assert response.result_full["degraded"] is True
    best = response.result_full["values"][0]
    assert best["value"] == "VAC_CTX" and best["match_type"] == "exact_code"
    assert [c.tool_name for c in mcp.calls] == ["runQuery"]
    assert "vac_ctx" not in mcp.calls[0].args["sql"]


async def test_related_concept_uses_description_semantics():
    result = _run_query_result(
        ["EarnCode", "EarnDescription", "freq"],
        [["OPAQUE_991", "Vacation leave", 1], ["POPULAR", "Overtime", 100]],
    )
    composite, _ = _composite(
        scripted={"runQuery": [result]},
        vectors={"holiday time": [1.0, 0.0], "Vacation leave": [1.0, 0.0], "Overtime": [0.0, 1.0]},
    )
    response = await composite.run(
        {"table": "accrual_events", "column": "EarnCode", "concept": "holiday time"}, _credentials()
    )
    best = response.result_full["values"][0]
    assert best["value"] == "OPAQUE_991" and best["match_type"] == "semantic"
    assert response.result_full["degraded"] is False


def test_zero_embedding_is_unavailable_not_semantic_evidence():
    assert _validate_embed_shape([[0.0, 0.0], [1.0, 0.0]], expected=2) is None
