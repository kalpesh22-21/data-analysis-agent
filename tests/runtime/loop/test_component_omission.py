"""Targeted omission preserves supported answers without reviving rejected UI payloads."""

import pytest

from data_agent.runtime.loop.answer_judge import JudgeVerdict
from data_agent.runtime.loop.proposal import (
    ReviewState,
    filter_excluded_components,
    omit_components,
)
from data_agent.runtime.session_history import project_history
from tests.runtime.test_harness_improvements import (
    CREDS,
    SQL,
    Judge,
    Tool,
    batch,
    build,
    call,
    discovery,
    query,
    run,
)

CARD = {
    "name": "identifier",
    "prepared": True,
    "capability_ref": "identifier",
    "metadata": {},
    "_agent_evidence": {"kind": "data_widget", "description": "Employee SSN"},
}


def proposal(ident, text):
    return batch(
        call(
            "finalizeAnswer",
            ident,
            answer=text,
            tables=[{"result_id": "q", "caption": "Employee count"}],
            capability_refs=["identifier"],
            evidence=["q", "p"],
        )
    )


def refusal(*refs):
    return JudgeVerdict(
        False,
        "unsupported_by_evidence",
        "The identifier card claim is unsupported.",
        True,
        result_ids=refs,
        repair_type="omit_component",
    )


@pytest.mark.parametrize("targets", [(), ("unknown",), ("p", "unknown"), ("finalizer",)])
def test_invalid_targets_do_not_remove_any_component(targets):
    state = ReviewState()
    assert not omit_components(
        state,
        refusal(*targets),
        [
            {"kind": "capability", "result_id": "p", "capability_ref": "identifier"},
        ],
    )
    assert state.excluded_components == ()


def test_ambiguous_target_is_not_guessed():
    state = ReviewState()
    assert not omit_components(
        state,
        refusal("p"),
        [
            {"kind": "capability", "result_id": "p", "capability_ref": "one"},
            {"kind": "capability", "result_id": "p", "capability_ref": "two"},
        ],
    )


def test_exclusions_roundtrip_without_leaking_across_scope_changes():
    state = ReviewState(scope_hash="original")
    component = {"kind": "capability", "result_id": "p", "capability_ref": "identifier"}
    assert omit_components(state, refusal("p"), [component])
    restored = ReviewState.restore(state.to_doc(), "original")
    assert restored.excluded_components == (component,)
    assert ReviewState.restore(state.to_doc(), "narrowed").excluded_components == ()


def test_excluded_table_cannot_return_by_legacy_sql_or_blueprint_alias():
    excluded = [{"kind": "table", "result_id": "q", "sql": SQL, "blueprint_id": "bp"}]
    for table in ({"result_id": "q"}, {"sql": SQL}, {"blueprint_id": "bp"}):
        assert filter_excluded_components({"tables": [table]}, excluded)["tables"] == []
        assert filter_excluded_components(table, excluded)["tables"] == []
    assert filter_excluded_components({"tables": [{"result_id": "other"}]}, excluded)["tables"] == [
        {"result_id": "other"}
    ]


async def test_card_omission_preserves_table_and_cannot_be_reintroduced_in_history():
    judge = Judge([refusal("p"), JudgeVerdict(True, reviewed=True)])
    loop, store, model, _, _ = build(
        [
            discovery(),
            query(),
            batch(call("identifier", "p")),
            proposal("initial", "There are 120 employees. The option shows their SSNs."),
            # Deliberately stale selection: the harness must strip the card, not trust the agent.
            proposal("fixed", "There are 120 employees. SSN information is unavailable."),
        ],
        judge=judge,
        extra={"identifier": Tool("identifier", CARD)},
    )
    outcome = await run(loop, "Count employees and provide SSN information.")
    assert outcome.assistant_text == "There are 120 employees. SSN information is unavailable."
    assert outcome.capability_cards is None
    assert len(outcome.answer_tables) == 1
    assert len(judge.briefs) == 2
    assert judge.briefs[0].selected_components == (
        {"kind": "table", "result_id": "q", "sql": SQL, "blueprint_id": None},
        {"kind": "capability", "result_id": "p", "capability_ref": "identifier"},
    )
    assert not judge.briefs[1].capability_presented
    assert all(r.get("tool_name") != "identifier" for r in judge.briefs[1].results)
    assert any(r.get("tool_name") == "runQuery" for r in judge.briefs[1].results)
    assert judge.briefs[1].excluded_components[0]["result_id"] == "p"
    assert "explicitly disclose" in str(model.calls[-1].messages)
    assert refusal("p").feedback not in model.calls[-1].messages[-1]["content"]
    doc = await store.get_or_create_session(CREDS.session_id)
    assert len([e for e in doc.tool_trail if e.tool_name == "runQuery"]) == 1
    history = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    turn = history["turns"][0]
    assert not turn.get("capability_cards")
    assert turn["answer_tables"] == outcome.answer_tables
    last = next(e for e in doc.tool_trail if e.tool_call_id == "fixed")
    assert last.args["capability_refs"] == []
    assert last.args["evidence"] == ["q"]
    assert doc.review_states["0"]["calls"] == 2
    assert await loop._compute_turn_capability_cards(CREDS.session_id, 0) == []


async def test_invalid_omission_target_leaves_unrelated_table_and_card_selected():
    judge = Judge([refusal("initial"), JudgeVerdict(True, reviewed=True)])
    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(call("identifier", "p")),
            proposal("initial", "There are 120 employees. An identifier option is available."),
            proposal("fixed", "There are 120 employees. An identifier option is available."),
        ],
        judge=judge,
        extra={"identifier": Tool("identifier", CARD)},
    )
    outcome = await run(loop, "Count employees and provide SSN information.")
    assert outcome.capability_cards and outcome.answer_tables
    assert not (await store.get_or_create_session(CREDS.session_id)).review_states["0"][
        "excluded_components"
    ]


async def test_second_rejection_falls_back_without_third_review_or_rejected_card():
    judge = Judge(
        [refusal("p"), JudgeVerdict(False, "unsupported_by_evidence", "Still unsupported.", True)]
    )
    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(call("identifier", "p")),
            proposal("initial", "There are 120 employees. The option shows SSNs."),
            proposal("fixed", "There are 120 employees. SSN information is unavailable."),
        ],
        judge=judge,
        extra={"identifier": Tool("identifier", CARD)},
    )
    outcome = await run(loop, "Count employees and provide SSN information.")
    assert outcome.capability_cards is None
    assert len(judge.briefs) == 2
    assert (await store.get_or_create_session(CREDS.session_id)).review_states["0"]["calls"] == 2


def test_shared_selected_alias_is_not_removed_as_one_independent_component():
    state = ReviewState()
    assert not omit_components(
        state,
        refusal("p"),
        [
            {"kind": "table", "result_id": "p", "sql": SQL},
            {"kind": "table", "result_id": "other", "sql": SQL},
        ],
    )
    assert state.excluded_components == ()


async def test_table_omission_preserves_another_table_and_history():
    other_sql = "SELECT count() AS n FROM hr.employee"
    judge = Judge([refusal("q"), JudgeVerdict(True, reviewed=True)])

    def both(ident):
        return batch(
            call(
                "finalizeAnswer",
                ident,
                answer="There are 120 employees. The breakdown could not be verified.",
                tables=[{"result_id": "q"}, {"result_id": "other"}],
                capability_refs=[],
                evidence=["q", "other"],
            )
        )

    loop, store, *_ = build(
        [
            discovery(),
            query(),
            batch(call("runQuery", "other", sql=other_sql)),
            both("initial"),
            both("fixed"),
        ],
        judge=judge,
        rows=[
            {"columns": ["Department", "n"], "rows": [["Sales", 120]], "row_count": 1},
            {"columns": ["n"], "rows": [[120]], "row_count": 1},
        ],
    )
    out = await run(loop, "Give a total and a department breakdown.")
    assert [t["sql"] for t in out.answer_tables] == [other_sql]
    doc = await store.get_or_create_session(CREDS.session_id)
    history = project_history(doc.messages, doc.tool_trail, frozenset(), None)
    assert history["turns"][0]["answer_tables"] == out.answer_tables
    rebuilt, _ = await loop._compute_turn_answer_tables(CREDS.session_id, 0)
    assert [t.sql for t in rebuilt] == [other_sql]


def test_capability_rejection_cannot_remove_a_valid_table_id():
    state = ReviewState()
    verdict = JudgeVerdict(
        False,
        "capability_coverage_gap",
        "Omit the card.",
        True,
        result_ids=("salary",),
        repair_type="omit_component",
    )
    assert not omit_components(
        state, verdict, [{"kind": "table", "result_id": "salary", "sql": SQL}]
    )
    assert state.excluded_components == ()
