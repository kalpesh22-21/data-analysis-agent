"""P08/R9: malformed finalization bindings must not erase selected prepared work."""

import pytest

from data_agent.runtime.capabilities.client import CapabilityDefinition
from data_agent.runtime.capabilities.tools import GetCapabilityTool, PresentCapabilityCardTool
from data_agent.runtime.loop.answer_judge import APPROVED, JudgeVerdict
from tests.runtime.test_harness_improvements import (
    CREDS,
    Judge,
    Tool,
    batch,
    build,
    call,
    discovery,
    query,
    run,
)

NAMES = ("employees", "headcount", "compensation")
LABELS = ("Employees in position", "Department headcount", "Compensation history")


def finalize(ident, *, valid=False, table=False, refs=NAMES):
    return batch(
        call(
            "finalizeAnswer",
            ident,
            answer="The requested views are available.",
            tables=[{"result_id": "q", "caption": "Department totals"}] if table else [],
            capability_refs=list(refs),
            evidence=["prep:0", "prep:1", "prep:2"]
            if valid
            else [
                "load:0",
                "functions.employees",
                "functions.headcount",
                "resolve:3",
            ],
        )
    )


def setup(
    judge,
    *,
    table=False,
    leave_missing=False,
    prior_rejection=False,
    refs=NAMES,
    repair_evidence=False,
):
    class Client:
        async def get_definition(self, name, **kwargs):
            return CapabilityDefinition(
                name, "1", "data_widget", LABELS[NAMES.index(name)], (), {"widgetName": name}
            )

        async def hydrate(self, name, **kwargs):
            return {"name": name, "arguments": {}, "metadata": {"widgetName": name}}

    client = Client()
    steps = [
        batch(
            *(
                call("getCapabilityTool", f"load:{i}", tool_name=name)
                for i, name in enumerate(NAMES)
            )
        )
    ]
    if table:
        steps += [discovery(), query()]
    steps += [
        batch(
            call("resolveValues", "resolve:3", concept="position"),
            call(NAMES[0], "prep:0"),
            call(NAMES[1], "prep:1"),
        ),
        finalize("final:1", table=table, refs=refs),
    ]
    if not leave_missing:
        steps += [batch(call(NAMES[2], "prep:2"))]
    steps += [finalize("final:2", valid=prior_rejection or repair_evidence, table=table, refs=refs)]
    if prior_rejection:
        steps += [finalize("final:3", table=table, refs=refs)]
    loop, store, model, _, events = build(
        steps, judge, extra={"resolveValues": Tool("resolveValues", {"values": ["Position"]})}
    )

    def register(definition):
        loop._runtime_tools[definition.name] = PresentCapabilityCardTool(
            client=client, definition=definition
        )
        return True

    loop._runtime_tools["getCapabilityTool"] = GetCapabilityTool(
        client=client, hydrate=register, visible_names=set()
    )
    return loop, store, model, events


@pytest.mark.parametrize("table", [False, True])
async def test_p08_composed_repair_and_reviewed_partial_delivery(table):
    judge = Judge()
    loop, store, model, events = setup(judge, table=table)
    out = await run(loop, "Show compensation history and department headcount.")
    assert {c["name"] for c in out.capability_cards or []} == set(NAMES)
    assert bool(out.answer_tables) == table
    assert out.review == {"status": "approved", "attempts": 1}
    assert all(f"{label}: The prepared view is included." in out.assistant_text for label in LABELS)
    assert "could not be verified" in out.assistant_text
    repair = [
        message["content"]
        for request in model.calls
        for message in request.messages
        if isinstance(message.get("content"), str)
        and "A requested UI option is not prepared" in message["content"]
    ]
    assert repair and "evidence reference" in repair[0]
    assert sum(e == "loop_finalization_block_spent" for e, _ in events) == 1
    assert sum(e == "loop_finalization_block_exhausted" for e, _ in events) == 1
    (brief,) = judge.briefs
    assert brief.site == "exit_capability"
    assert {c["name"] for c in brief.capability_presented} == set(NAMES)
    assert len(brief.selected_components) == 3 + int(table)
    assert {
        c["capability_ref"]: c["result_id"]
        for c in brief.selected_components
        if c["kind"] == "capability"
    } == dict(zip(NAMES, ("prep:0", "prep:1", "prep:2"), strict=True))
    # Selections are explicit, but no intent associations are invented.
    assert brief.evidence_package["assignment_mode"] == "single_request_without_ledger"
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.messages[-1].content == out.assistant_text
    assert any(e == "loop_delivery_review" and a["site"] == "exit_capability" for e, a in events)


async def test_missing_preparation_does_not_get_promoted_on_exhaustion():
    loop, _, _, _ = setup(Judge(), leave_missing=True)
    out = await run(loop)
    assert {c["name"] for c in out.capability_cards} == set(NAMES[:2])
    assert "Compensation history: The prepared view is included." not in out.assistant_text


async def test_unselected_preparation_does_not_get_promoted_on_exhaustion():
    loop, _, _, _ = setup(Judge(), refs=NAMES[:2])
    out = await run(loop)
    assert {c["name"] for c in out.capability_cards} == set(NAMES[:2])


@pytest.mark.parametrize("prior_rejection", [False, True])
async def test_explicit_rejection_remains_binding(prior_rejection):
    judge = Judge(
        [JudgeVerdict(False, "unsupported_by_evidence", "Do not deliver these views.", True)]
    )
    loop, _, _, _ = setup(judge, prior_rejection=prior_rejection)
    out = await run(loop)
    assert not out.capability_cards and not out.answer_tables
    assert out.review["status"] == "rejected"
    assert len(judge.briefs) == 1


async def test_no_verdict_exhaustion_can_deliver_retained_components():
    judge = Judge([APPROVED, APPROVED])
    loop, _, _, _ = setup(judge)
    out = await run(loop)
    assert len(out.capability_cards) == 3
    assert out.review["status"] == "exhausted"


async def test_complete_repair_uses_normal_proposal_review():
    judge = Judge()
    loop, _, _, events = setup(judge, repair_evidence=True)
    out = await run(loop)
    assert len(out.capability_cards) == 3
    assert out.review["status"] == "approved"
    assert out.assistant_text == "The requested views are available."
    assert not any(e == "loop_finalization_block_exhausted" for e, _ in events)
    assert len(judge.briefs) == 1
