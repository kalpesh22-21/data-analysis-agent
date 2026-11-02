"""Delivered answers, not rejected drafts, teach the historical finalizer shape."""

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from data_agent.runtime.composer import PROPOSAL_SCHEMA
from data_agent.runtime.context.assembly import ContextAssembler
from data_agent.runtime.context.budget import fit_request_to_budget
from data_agent.runtime.context.delivered_answer import delivered_answer_entry
from data_agent.runtime.loop.agent_loop import _assembled_to_canonical
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.models import TurnMessage
from tests.runtime.context.test_assembly_interleave import _pairing_intact


def message(status="approved", completion="full"):
    return TurnMessage(
        turn_index=1,
        role="assistant",
        content="Corrected delivered takeaway.",
        ts="2026-11-02T01:00:00Z",
        review={"status": status, "completion": completion},
        answer_document={
            "schema_version": 2,
            "answer_id": "answer-1",
            "revision": 2,
            "takeaway": "Older text must not override delivered content.",
            "sections": [
                {
                    "id": "s1",
                    "title": "Corrected title",
                    "evidence_refs": ["q1"],
                    "caveats": [],
                    "actions": [],
                    "presentation": {
                        "kind": "line",
                        "result_id": "q1",
                        "x_column": "Month",
                        "y_column": "Pay",
                        "label": "Gross pay",
                    },
                }
            ],
            "sources": {"q1": {"sql": "SELECT Month, Pay FROM payroll"}},
        },
    )


@pytest.mark.parametrize("completion", ["full", "partial"])
@pytest.mark.parametrize("status", ["approved", "disabled"])
def test_delivered_corrections_and_recovery_use_finalizer_pair(status, completion):
    saved = message(status, completion)
    before = deepcopy(saved.to_doc())
    assembler = ContextAssembler(InMemorySessionStore())
    rendered = assembler._interleave(
        in_scope_messages=[saved],
        in_scope_trail=[],
        raw_trail=[],
        current_turn_index=2,
        withheld_call_ids=None,
        observer=None,
    )
    canonical = _assembled_to_canonical(rendered)
    assert _pairing_intact(canonical)
    call = canonical[0]["tool_calls"][0]
    assert call["function"]["name"] == "finalizeAnswer"
    args = json.loads(call["function"]["arguments"])
    Draft202012Validator(PROPOSAL_SCHEMA).validate(args)
    assert args["takeaway"] == saved.content
    assert args["sections"][0]["title"] == "Corrected title"
    assert args["sections"][0]["presentation"]["category_column"] == "Month"
    assert args["sections"][0]["presentation"]["measure_column"] == "Pay"
    assert "sources" not in args
    result = json.loads(canonical[1]["content"])
    assert result["sources"] == saved.answer_document["sources"]
    assert result["completion"] == completion
    assert canonical[2] == {"role": "assistant", "content": saved.content}
    assert saved.to_doc() == before


@pytest.mark.parametrize("status", ["exhausted", "rejected", "unavailable", None])
def test_failed_or_unreviewed_turn_never_invents_success(status):
    assert delivered_answer_entry(message(status)) is None


def test_multi_table_reasons_are_preserved_and_legacy_missing_reasons_not_invented():
    saved = message()
    section = saved.answer_document["sections"][0]
    section["presentation"] = {"kind": "table", "result_id": "q1"}
    section["table_reason"] = "Requested department detail."
    other = deepcopy(section)
    other.update(id="s2", table_reason="Requested employee detail.")
    saved.answer_document["sections"].append(other)
    assert delivered_answer_entry(saved)["args"]["sections"] == saved.answer_document["sections"]
    del other["table_reason"]
    assert delivered_answer_entry(saved) is None


def test_ranking_and_compound_categories_round_trip():
    saved = message()
    saved.answer_document["sections"][0]["presentation"] = {
        "kind": "bars",
        "result_id": "q1",
        "x_columns": ["Department", "Type"],
        "y_column": "Pay",
        "label": "Pay",
        "ranking": {"direction": "descending", "ties": "dense"},
    }
    p = delivered_answer_entry(saved)["args"]["sections"][0]["presentation"]
    assert p["category_columns"] == ["Department", "Type"]
    assert p["direction"] == "descending" and p["ties"] == "dense"


async def test_scope_filter_drops_historical_call_and_sources_together():
    from dataclasses import replace

    saved = replace(message(), provenance=frozenset({("payroll", "Pay")}))
    store = InMemorySessionStore()
    await store.append_message("s", saved)
    assembled = await ContextAssembler(store).assemble(
        "s", frozenset({"employee.Name"}), current_turn_index=2
    )
    assert "Corrected delivered" not in json.dumps(assembled.messages)
    assert "SELECT Month" not in json.dumps(assembled.messages)
    assert "history_final_" not in json.dumps(assembled.messages)


def test_budget_trims_historical_pair_atomically_and_keeps_current_question():
    entry = delivered_answer_entry(message())
    canonical = _assembled_to_canonical(
        [
            {"role": "user", "content": "Old question"},
            entry,
            {"role": "assistant", "content": message().content},
            {"role": "user", "content": "Current question"},
        ]
    )
    fitted = fit_request_to_budget(canonical, token_budget=40)
    assert fitted.dropped_messages > 0
    assert _pairing_intact(fitted.messages)
    assert fitted.messages[-1]["content"] == "Current question"


@pytest.mark.parametrize("kind", ["heatmap", "donut", "multi_series_line", "stacked_bars"])
def test_extended_chart_selector_projection(kind):
    saved = message()
    p = {"kind": kind, "result_id": "q1", "x_column": "Month", "label": "Pay"}
    if kind == "heatmap":
        p.update(y_column="Department", value_column="Pay")
    elif kind == "donut":
        p.update(y_column="Pay", composition=True)
    else:
        p["series"] = [{"y_column": "Pay", "label": "Pay"}, {"y_column": "Bonus", "label": "Bonus"}]
        if kind == "stacked_bars":
            p["additive"] = True
    saved.answer_document["sections"][0]["presentation"] = p
    entry = delivered_answer_entry(saved)
    assert entry is not None
    Draft202012Validator(PROPOSAL_SCHEMA).validate(entry["args"])


def test_rejected_legacy_proposal_does_not_compete_with_delivered_call():
    from data_agent.runtime.session.models import TrailEntry

    entry = TrailEntry(
        turn_index=1,
        tool_call_id="rejected",
        tool_name="finalizeAnswer",
        args={"takeaway": "REJECTED DRAFT", "sections": []},
        status="ok",
        error_code=None,
        provenance=frozenset(),
        result_preview=None,
        result_full_ref=None,
        ts="2026-11-02T00:00:00Z",
    )
    rendered = ContextAssembler(InMemorySessionStore())._interleave(
        in_scope_messages=[message()],
        in_scope_trail=[entry],
        raw_trail=[entry],
        current_turn_index=2,
        withheld_call_ids=None,
        observer=None,
    )
    canonical = _assembled_to_canonical(rendered)
    assert "REJECTED DRAFT" not in json.dumps(canonical)
    assert _pairing_intact(canonical)
    assert sum(len(m.get("tool_calls", [])) for m in canonical) == 1
