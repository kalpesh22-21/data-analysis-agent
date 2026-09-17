"""Execution-scoped measurement decisions and their real-loop wiring."""

import json
from types import SimpleNamespace

import pytest

from data_agent.runtime.loop.measurement import (
    MeasurementReviewer,
    MeasurementVerdict,
    execution_review_scope,
)
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from tests.runtime.test_harness_improvements import (
    CREDS,
    SQL,
    Judge,
    batch,
    build,
    call,
    discovery,
    finish,
    run,
)

CONTRACT = dict(
    metric="salary",
    population="Smith",
    period="current",
    units="USD",
    grain="employee",
    join_cardinality="none",
)


def verdict(*codes, alignment="matches_requested_part"):
    return dict(
        request_alignment=alignment,
        findings=[{"code": c, "detail": c} for c in codes],
        contract=CONTRACT,
    )


@pytest.mark.parametrize(
    "codes,alignment,approved",
    [
        ([], "matches_requested_part", True),
        (["other_deliverable_missing"], "matches_requested_part", True),
        (["population_mismatch"], "matches_requested_part", False),
        (["other_deliverable_missing", "population_mismatch"], "matches_requested_part", False),
        (["metric_mismatch"], "matches_requested_part", False),
        (["period_mismatch"], "matches_requested_part", False),
        (["units_mismatch"], "matches_requested_part", False),
        (["grain_mismatch"], "matches_requested_part", False),
        (["aggregation_fanout"], "matches_requested_part", False),
        (["request_mismatch"], "matches_requested_part", False),
        (["other_deliverable_missing"], "unrelated", False),
        ([], "uncertain", False),
    ],
)
def test_only_measurement_or_alignment_findings_block(codes, alignment, approved):
    result = MeasurementVerdict.model_validate(verdict(*codes, alignment=alignment)).decision()
    assert result["approved"] is approved
    assert result["reviewed"]
    assert "other_deliverable_missing" not in result["feedback"]


@pytest.mark.parametrize(
    "bad",
    [
        {"approved": True},
        verdict("unknown_code"),
        {**verdict(), "approved": True},
        {**verdict(), "request_alignment": "yes"},
    ],
)
async def test_invalid_review_is_not_certified(bad):
    model = ScriptedModelClient([batch(call("record_measurement_review", "r", **bad))])
    result = await MeasurementReviewer(model).review("Salary and SSN for Smith", {}, [])
    assert result["reviewed"] is False
    # Preserve the existing unavailable-review policy, not a semantic approval badge.
    assert result["contract"] == {}


def receipt(*, turn=2, status="ok", prepared=True):
    return {
        "role": "tool",
        "content": json.dumps(
            {
                "turn_index": turn,
                "status": status,
                "result_id": "card-result",
                "result_preview": {
                    "preview_rows": [
                        [
                            {
                                "prepared": prepared,
                                "capability_ref": "identifier_card",
                                "arguments": {"unresolved_entities": {"employees": ["Smith"]}},
                                "_agent_evidence": {
                                    "parameters": [{"name": "employees", "type": "employee"}],
                                    "description": "Employee SSN",
                                    "kind": "data_widget",
                                    "activation": "user_interaction_required",
                                },
                            }
                        ]
                    ]
                },
            }
        ),
    }


def test_scope_includes_only_received_current_successful_capabilities():
    state = SimpleNamespace(
        intents=[
            SimpleNamespace(intent_id="salary", description="Salary for Smith"),
            SimpleNamespace(intent_id="ssn", description="SSN for Smith"),
        ]
    )
    scope = execution_review_scope(
        [
            receipt(),
            receipt(turn=1),
            receipt(status="error"),
            receipt(prepared=False),
            {"role": "assistant", "content": receipt()["content"]},
            {"role": "tool", "content": "not JSON"},
        ],
        state,
        ["salary", "invented"],
        2,
    )
    assert scope["assigned_deliverables"] == [
        {"intent_id": "salary", "description": "Salary for Smith"}
    ]
    assert scope["other_declared_deliverables"] == [
        {"intent_id": "ssn", "description": "SSN for Smith"}
    ]
    assert len(scope["received_capabilities"]) == 1
    assert scope["received_capabilities"][0]["activation"] == "user_interaction_required"
    assert scope["received_capabilities"][0]["arguments"] == {
        "unresolved_entities": {"employees": ["Smith"]}
    }
    assert scope["received_capabilities"][0]["parameters"] == [
        {"name": "employees", "type": "employee"}
    ]
    assert execution_review_scope([], state, ["invented"], 2)["binding_status"] == "unbound"


async def test_unbound_partial_query_preserves_original_request_and_scope():
    model = ScriptedModelClient(
        [batch(call("record_measurement_review", "r", **verdict("other_deliverable_missing")))]
    )
    scope = execution_review_scope([receipt()], None, [], 2)
    result = await MeasurementReviewer(model).review(
        "Salary and SSN for Smith",
        {"sql": "SELECT salary WHERE last_name = 'Smith'"},
        [],
        review_scope=scope,
    )
    assert result["approved"] and result["reviewed"]
    payload = json.loads(model.calls[0].messages[1]["content"])
    assert payload["original_request"] == "Salary and SSN for Smith"
    assert payload["review_scope"] == scope


async def test_real_loop_allows_coverage_observation_and_passes_full_request_to_final_judge():
    reviewer_model = ScriptedModelClient(
        [batch(call("record_measurement_review", "r", **verdict("other_deliverable_missing")))]
    )
    final_judge = Judge()
    loop, store, _, mcp, _ = build(
        [
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[
                        {"description": "Employee count"},
                    ],
                )
            ),
            discovery(),
            batch(call("runQuery", "q", sql=SQL, serves_intents=["i1"])),
            batch(
                call(
                    "updateAnalysisState",
                    "close",
                    intents=[{"intent_id": "i1", "status": "completed"}],
                ),
                finish("There are 120 employees. SSN information is unavailable."),
            ),
        ],
        judge=final_judge,
    )
    loop._measurement_reviewer = MeasurementReviewer(reviewer_model)
    question = "Count employees and provide SSN information."
    await run(loop, question)
    doc = await store.get_or_create_session(CREDS.session_id)
    entry = next(e for e in doc.tool_trail if e.tool_call_id == "q")
    assert entry.status == "ok"
    payload = json.loads(reviewer_model.calls[0].messages[1]["content"])
    assert payload["original_request"] == question
    assert payload["review_scope"]["assigned_deliverables"] == [
        {"intent_id": "i1", "description": "Employee count"},
    ]
    assert final_judge.briefs and final_judge.briefs[-1].question == question


async def test_final_judge_can_require_disclosure_without_reexecuting_valid_query():
    from data_agent.runtime.loop.answer_judge import JudgeVerdict

    reviewer_model = ScriptedModelClient(
        [batch(call("record_measurement_review", "r", **verdict("other_deliverable_missing")))]
    )
    final_judge = Judge(
        [
            JudgeVerdict(
                False,
                "unexplained_gap",
                "Disclose that SSNs are unavailable.",
                True,
                repair_type="prose",
            ),
            JudgeVerdict(True, reviewed=True),
        ]
    )
    loop, store, *_ = build(
        [
            discovery(),
            batch(call("runQuery", "q", sql=SQL)),
            batch(finish("There are 120 employees.")),
            batch(
                call(
                    "finalizeAnswer",
                    "fixed",
                    answer="There are 120 employees. SSNs are unavailable.",
                    tables=[],
                    capability_refs=[],
                    evidence=["q"],
                )
            ),
        ],
        judge=final_judge,
    )
    loop._measurement_reviewer = MeasurementReviewer(reviewer_model)
    question = "Count employees and provide their SSNs."
    outcome = await run(loop, question)
    assert outcome.assistant_text == "There are 120 employees. SSNs are unavailable."
    assert len(final_judge.briefs) == 2
    assert all(b.question == question for b in final_judge.briefs)
    assert len(reviewer_model.calls) == 1
    doc = await store.get_or_create_session(CREDS.session_id)
    assert len([e for e in doc.tool_trail if e.tool_name == "runQuery"]) == 1
