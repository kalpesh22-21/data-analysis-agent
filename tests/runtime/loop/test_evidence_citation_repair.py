"""A redundant lookup citation must not turn a supported draft into a fabricated gap."""

import pytest

from data_agent.runtime.context.scope_filter import filter_trail
from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.proposal import assess_deliverable_evidence, evidence_kind
from data_agent.runtime.session.models import AnalysisState, TrackedIntent
from tests.runtime.loop.test_repository_regressions import entry
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

DRAFT = "Sales has 120 employees and Operations has 80. The department breakdown is shown."


def setup(
    judge,
    *,
    ledger=True,
    bad_ref="resolve:10",
    first_capability=False,
    rounds=2,
    valid_first=False,
):
    steps = [
        discovery(),
        batch(call("resolveValues", "resolve:10", concept="employee status")),
        query(),
    ]
    if ledger:
        steps += [
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    intents=[{"description": "Department headcounts"}],
                )
            ),
            batch(
                call(
                    "updateAnalysisState",
                    "complete",
                    intents=[{"intent_id": "i1", "status": "completed", "result_id": "q"}],
                )
            ),
        ]
    steps += [
        batch(
            call(
                "recordAssumptions",
                "assumptions",
                assumptions=["Counts cover accessible employee records."],
            )
        )
    ]
    for index in range(rounds):
        steps += [
            batch(
                call(
                    "finalizeAnswer",
                    f"final:{index}",
                    answer=DRAFT,
                    tables=[{"result_id": "q", "caption": "Department headcounts"}],
                    capability_refs=["functions.runQuery"]
                    if first_capability and index == 0
                    else [],
                    evidence=["q"] + ([] if valid_first and index == 0 else [bad_ref]),
                )
            )
        ]
    return build(
        steps,
        judge,
        extra={"resolveValues": Tool("resolveValues", {"values": ["Active"]})},
        rows=[
            {
                "columns": ["Department", "n"],
                "rows": [["Sales", 120], ["Operations", 80]],
                "row_count": 2,
                "truncated": False,
            }
        ],
    )


class RejectFabricatedGap(Judge):
    async def review(self, brief):
        self.briefs.append(brief)
        if "I could not verify every requested part" in brief.draft:
            return JudgeVerdict(
                False,
                "unexplained_gap",
                "Present the available result.",
                True,
                repair_type="presentation",
            )
        return JudgeVerdict(True, reviewed=True)


@pytest.mark.parametrize("ledger", [False, True])
@pytest.mark.parametrize("first_capability", [False, True])
async def test_only_pivotal_evidence_can_block_original_draft_review(ledger, first_capability):
    judge = RejectFabricatedGap()
    loop, store, model, _, events = setup(judge, ledger=ledger, first_capability=first_capability)
    out = await run(loop)
    assert out.assistant_text == DRAFT
    assert len(out.answer_tables) == 1
    assert out.assumptions == ["Counts cover accessible employee records."]
    assert out.review["status"] == "approved"
    assert len(judge.briefs) == 1
    brief = judge.briefs[0]
    assert brief.draft == DRAFT
    assert brief.referenced_result_ids == ("q",)
    assert brief.deliverables[0]["evidence"] == [{"result_id": "q", "kind": "warehouse"}]
    assert brief.selected_components[0]["result_id"] == "q"
    messages = [
        m["content"]
        for request in model.calls
        for m in request.messages
        if isinstance(m.get("content"), str)
    ]
    assert not any("Remove it from evidence" in text for text in messages)
    if first_capability:
        assert any("A requested UI option is not prepared" in text for text in messages)
    assert sum(e == "loop_finalization_block_spent" for e, _ in events) == int(first_capability)
    assert not any(e == "loop_finalization_block_exhausted" for e, _ in events)
    assert ("loop_answer_evidence_extras_ignored", {"dropped_count": 1}) in events
    assert (await store.get_or_create_session(CREDS.session_id)).messages[-1].content == DRAFT


async def test_clean_evidence_uses_normal_review_without_exhaustion():
    judge = Judge()
    loop, _, _, _, events = setup(judge, valid_first=True, rounds=1)
    out = await run(loop)
    assert out.assistant_text == DRAFT
    assert not any(e == "loop_answer_evidence_extras_ignored" for e, _ in events)
    assert not any(e == "loop_finalization_block_exhausted" for e, _ in events)


@pytest.mark.parametrize("prior_rejection", [False, True])
async def test_explicit_rejection_cannot_be_bypassed_by_redundant_citations(prior_rejection):
    rejection = JudgeVerdict(
        False, "contradicts_result", "The interpretation is wrong.", True, repair_type="prose"
    )
    judge = Judge([rejection, rejection])
    loop, _, _, _, _ = setup(judge, rounds=3, valid_first=prior_rejection)
    out = await run(loop)
    assert not out.answer_tables
    assert out.assistant_text != DRAFT
    assert out.review["status"] == "rejected"
    assert len(judge.briefs) == 2
    assert all(b.draft == DRAFT for b in judge.briefs)


async def test_unavailable_review_can_fail_open_without_fabricating_approval():
    judge = Judge([JudgeVerdict(True)] * 4)
    loop, _, _, _, _ = setup(judge)
    out = await run(loop)
    assert out.assistant_text == DRAFT
    assert out.answer_tables
    assert out.review["status"] != "approved"


async def test_missing_reference_blocks_before_judge():
    judge = RejectFabricatedGap()
    loop, _, _, _, events = setup(judge, bad_ref="missing:99")
    out = await run(loop)
    assert out.assistant_text != DRAFT
    assert not out.answer_tables
    assert not any(e == "loop_answer_evidence_extras_ignored" for e, _ in events)


@pytest.mark.parametrize("tool", ["resolveValues", "sampleRows"])
def test_known_ineligible_references_are_dropped_even_when_no_support_remains(tool):
    lookup = entry("lookup", tool)
    assert evidence_kind(lookup) is None
    redundant = assess_deliverable_evidence(
        None, {"evidence": ["q", "lookup"]}, [entry("q", "runQuery"), lookup]
    )
    assert redundant.ignored_references == ("lookup",)
    assert redundant.feedback is None
    assert redundant.references == ("q",)
    assessed = assess_deliverable_evidence(None, {"evidence": ["lookup"]}, [lookup])
    assert assessed.feedback is None
    assert assessed.ignored_references == ("lookup",)
    assert assessed.references == ()
    state = AnalysisState(
        turn_index=0,
        intents=(
            TrackedIntent(
                intent_id="i1",
                description="Count",
                status="completed",
                evidence_tool_call_id="lookup",
            ),
        ),
    )
    assessed = assess_deliverable_evidence(
        state, {"evidence": ["q", "lookup"]}, [lookup, entry("q", "runQuery")]
    )
    assert assessed.ignored_references == ("lookup",)
    assert not assessed.errors
    assert assessed.deliverables[0]["evidence"] == []


def test_reports_every_bad_reference_while_preserving_healthy_deliverable():
    state = AnalysisState(
        turn_index=0,
        intents=(
            TrackedIntent(
                intent_id="i1",
                description="Count",
                status="completed",
                evidence_tool_call_id="q",
            ),
        ),
    )
    trail = [
        entry("q", "runQuery"),
        entry("q2", "runQuery"),
        entry("lookup", "resolveValues"),
        entry("failed", "runQuery", status="error"),
    ]
    assessed = assess_deliverable_evidence(
        state, {"evidence": ["lookup", "missing", "runQuery", "failed", "q"]}, trail
    )
    assert len(assessed.errors) == 2
    assert all(f'"{ref}"' in assessed.feedback for ref in ("missing", "runQuery"))
    assert "ambiguous" in assessed.feedback
    assert set(assessed.ignored_references) == {"lookup", "failed"}
    assert assessed.deliverables[0]["evidence"] == [{"result_id": "q", "kind": "warehouse"}]
    assert assessed.feedback is not None


def test_scope_filtered_reference_does_not_reveal_existence_or_metadata():
    denied = entry("hidden", "resolveValues", provenance=frozenset({("private.people", "salary")}))
    trail = filter_trail([denied, entry("q", "runQuery")], frozenset({"hr.employee.Department"}))
    args = {"evidence": ["q", "hidden"]}
    assessed = assess_deliverable_evidence(None, args, trail)
    absent = assess_deliverable_evidence(None, args, [entry("q", "runQuery")])
    assert assessed.feedback == absent.feedback
    assert "private.people" not in assessed.feedback and "resolveValues" not in assessed.feedback
    assert assessed.feedback is not None


def test_extra_type_hint_is_ignored_and_ineligible_binding_is_dropped():
    state = AnalysisState(
        turn_index=0,
        intents=(
            TrackedIntent(
                intent_id="i1",
                description="Count",
                status="completed",
                evidence_tool_call_id="q",
            ),
        ),
    )
    assessed = assess_deliverable_evidence(
        state,
        {
            "evidence": ["q", "lookup"],
            "deliverables": [
                {"intent_id": "i1", "result_ids": ["q", "lookup"], "evidence_type": "product"}
            ],
        },
        [entry("q", "runQuery"), entry("lookup", "resolveValues")],
    )
    assert assessed.feedback is None
    assert assessed.ignored_references == ("lookup",)
    assert assessed.deliverables[0]["evidence"] == [{"result_id": "q", "kind": "warehouse"}]


@pytest.mark.parametrize("ledger", [False, True])
async def test_extra_record_assumptions_citation_reaches_judge_without_repair(ledger):
    judge = Judge()
    loop, _, model, mcp, events = setup(judge, ledger=ledger, bad_ref="assumptions", rounds=1)
    out = await run(loop)
    assert out.assistant_text == DRAFT
    assert out.review["status"] == "approved"
    assert len(judge.briefs) == 1
    assert judge.briefs[0].referenced_result_ids == ("q",)
    assert not any(e == "loop_finalization_block_spent" for e, _ in events)
    assert len(mcp.calls) == 1


async def test_dropping_extra_refs_does_not_bypass_explicit_judge_rejection():
    rejected = JudgeVerdict(
        False, "unsupported_by_evidence", "No evidence supports this claim.", True
    )
    judge = Judge([rejected, rejected])
    loop, _, _, _, _ = setup(judge, bad_ref="assumptions")
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert not out.answer_tables


async def test_empty_support_after_dropping_control_receipts_still_reaches_judge():
    reject = JudgeVerdict(False, "unsupported_by_evidence", "The claim has no support.", True)
    judge = Judge([reject, reject])
    final = batch(
        call("finalizeAnswer", "final", answer="Everyone is on leave.", tables=[], evidence=["a"])
    )
    loop, _, _, _, _ = build(
        [
            batch(call("recordAssumptions", "a", assumptions=["Current period."])),
            final,
            final,
        ],
        judge,
    )
    out = await run(loop)
    assert out.review["status"] == "rejected"
    assert len(judge.briefs) == 2
    assert all(b.referenced_result_ids == () for b in judge.briefs)
    assert all(b.deliverables[0]["evidence"] == [] for b in judge.briefs)


async def test_completed_state_update_ignores_extras_and_preserves_bound_work():
    judge = Judge()
    loop, store, _, _, events = build(
        [
            discovery(),
            query(),
            batch(
                call(
                    "updateAnalysisState",
                    "declare",
                    mode="initialize",
                    intents=[{"description": "Count", "note": "extra"}],
                )
            ),
            batch(
                call(
                    "updateAnalysisState",
                    "complete",
                    progress="done",
                    intents=[
                        {
                            "intent_id": "i1",
                            "status": "completed",
                            "result_id": "q",
                            "description": "Changed ask",
                            "note": {"extra": True},
                        }
                    ],
                )
            ),
            batch(
                call(
                    "finalizeAnswer",
                    "final",
                    answer="There are 120 employees.",
                    tables=[{"result_id": "q"}],
                    evidence=["q", "complete"],
                )
            ),
        ],
        judge,
    )
    out = await run(loop)
    assert out.review["status"] == "approved"
    assert len(judge.briefs) == 1
    assert judge.briefs[0].referenced_result_ids == ("q",)
    doc = await store.get_or_create_session(CREDS.session_id)
    assert doc.analysis_state.intents[0].description == "Count"
    assert doc.analysis_state.intents[0].status == "completed"
    assert doc.analysis_state.intents[0].evidence_tool_call_id == "q"
    assert not any(name == "loop_analysis_state_rejected" for name, _ in events)
    assert not any(name == "loop_finalization_block_spent" for name, _ in events)
