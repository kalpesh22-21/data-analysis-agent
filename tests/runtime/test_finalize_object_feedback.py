"""A rejected answer object says exactly what to change; an empty field is not a rejection.

Each case is a shape from tool_model_object_issues.md. The model repeated most of those
mistakes because the error named the allowed fields but never the wrong one (#22 -> #23,
#34 -> #35), or said only "expected type: array" for text that was broken JSON (#48-50).
"""

import json

import pytest

from data_agent.runtime.composer import SECTION_SCHEMA, PresentationError, proposal_to_internal
from data_agent.runtime.mcp.tool_schema import FINALIZE_ANSWER_SCHEMA
from tests.runtime.test_answer_document import section

BARS = {"category_column": "Department", "measure_column": "n", "label": "Headcount"}


def answer(*sections, **extra):
    return {"takeaway": "Counts.", "sections": list(sections) or [section()], "evidence": ["q"], **extra}


def rejection(raw):
    with pytest.raises(PresentationError) as caught:
        proposal_to_internal(raw)
    return str(caught.value)


# --- row 2 / 7: an undeclared field is named, with where it belongs ----------


@pytest.mark.parametrize(
    ("field", "home"),
    [("caveats", "is a section field: put it in the section it belongs to"),
     ("table_reason", "is a section field"),
     ("summary_note", "is not a declared field: remove it")],
)
def test_an_undeclared_answer_field_is_named_with_where_it_belongs(field, home):
    message = rejection(answer(**{field: "The roster is capped at 10,000 rows."}))
    assert f"undeclared field(s): {field} ({home}" in message
    assert "Use only declared fields: takeaway, sections, evidence, deliverables, assumptions" in message


@pytest.mark.parametrize(
    ("field", "home"),
    [("title", "is a section field, not a presentation field"), ("composite", "is not a field: use composition")],
)
def test_an_undeclared_presentation_field_is_named(field, home):
    message = rejection(answer(section("table", **{field: "Gross wages by pay date"})))
    assert f"sections.0.presentation: undeclared field(s): {field} ({home})" in message


def test_a_presentation_field_sent_on_the_section_is_pointed_inside_presentation():
    proposed = {**section(), "label": "Headcount"}
    assert "label (belongs inside presentation)" in rejection(answer(proposed))


def test_a_field_name_that_is_not_an_identifier_is_never_echoed():
    message = rejection(answer(**{"SELECT Salary FROM employee": "x"}))
    assert "SELECT" not in message and "an unnamed field (remove it)" in message


@pytest.mark.parametrize("value", [None, "", [], {}])
def test_an_empty_undeclared_field_is_dropped_at_every_level(value):
    """#5, #6, #10, #29, #37, #40 (answer level) and #46 (`markdown_note: ""`)."""
    proposed = {**section(), "evidence_refs": [],
                "presentation": {"kind": "text", "markdown": "No rows matched.", "markdown_note": value}}
    raw = answer(proposed, sections_note=value, recordAssumptions=value, actions=value)
    internal = proposal_to_internal(raw)
    assert {"sections_note", "recordAssumptions", "actions"}.isdisjoint(internal)
    assert "markdown_note" not in internal["sections"][0]["presentation"]
    assert raw["sections"][0]["presentation"]["markdown_note"] == value  # input not mutated


def test_a_declared_null_is_not_treated_as_absent():
    """An explicit null `evidence` is not an omitted one: omission lets the runtime infer it."""
    assert "evidence: expected type: array, got: null" in rejection(answer(evidence=None))


# --- row 8: an empty selector is a field that does not apply -----------------


def test_an_empty_selector_on_a_chart_is_absent():
    """#8: bars with `"column": ""` (a figure-only field)."""
    raw = answer(section("bars", **BARS, column="", direction="descending", ties="dense"))
    assert "column" not in proposal_to_internal(raw)["sections"][0]["presentation"]


def test_a_required_selector_left_empty_says_it_is_missing():
    raw = answer(section("bars", **{**BARS, "measure_column": ""}))
    assert "missing required field(s): measure_column" in rejection(raw)


# --- row 3: text that should be an array says it is broken JSON ---------------


def test_sections_sent_as_broken_json_text_say_where_it_broke():
    """#4, #6, #24, #36, #48-50 closed the presentation object with ']'."""
    text = json.dumps([section("text", markdown="No rows.")])
    broken = text.replace('"No rows."}', '"No rows."]', 1)
    message = rejection({"takeaway": "Counts.", "sections": broken})
    assert "sections: expected type: array, got: string (text that is not valid JSON: Expecting" in message
    assert f"of {len(broken)})" in message
    assert "Send the array itself, not a string, and close each { with } and each [ with ]" in message


def test_sections_sent_as_json_text_of_the_wrong_shape_say_what_it_decodes_to():
    assert "(text that decodes to object)" in rejection({"takeaway": "Counts.", "sections": "{}"})


# --- row 4: every table needs table_reason, the first included ----------------


def test_a_missing_first_table_reason_says_every_table_needs_one():
    """#9, #13: the reason was on the second table only, as the description then said."""
    first, second = section(), {**section(), "id": "second", "table_reason": "Separate requested detail."}
    message = rejection(answer(first, second))
    assert "sections.0: missing required field(s): table_reason (with two or more table sections, " \
           "every table section needs a table_reason, the first included)" in message


def test_every_description_says_the_first_table_needs_a_reason_too():
    from data_agent.runtime.loop import salvage

    assert "the first included" in SECTION_SCHEMA["properties"]["table_reason"]["description"]
    assert "the first included" in FINALIZE_ANSWER_SCHEMA["description"]
    assert "every table section needs table_reason, the first included" in open(salvage.__file__).read()
    assert "extras require table_reason" not in open(salvage.__file__.replace("salvage.py", "agent_loop.py")).read()


# --- row 6: a result ID that does not match names both sides ------------------


def test_a_mistyped_result_id_names_the_section_and_both_ids():
    """#14 dropped two letters; #15 sent a placeholder."""
    proposed = {**section("table"), "id": "band-distribution", "evidence_refs": ["chatcmpl-tool-910d"]}
    proposed["presentation"] = {"kind": "table", "result_id": "chatcm-tool-910d"}
    message = rejection(answer(proposed))
    assert 'section "band-distribution" shows result_id "chatcm-tool-910d"' in message
    assert 'its evidence_refs are ["chatcmpl-tool-910d"]' in message


def test_an_unknown_kind_lists_the_choices():
    message = rejection(answer(section("pie")))
    assert "kind: must be one of: text, table, figure, facts, bars, line" in message


# --- row 10: the salvage outcome rules are on the field itself ---------------


async def test_the_salvage_outcome_field_states_its_rules():
    from data_agent.runtime.loop.answer_judge import AnswerJudge, JudgeBrief
    from tests.runtime.loop.test_salvage_contract import PART, RecoveryClient, batch, call

    response = {**PART, "outcome": "partial"}
    client = RecoveryClient([batch(call("salvageAnswer", "s", **response))])
    await AnswerJudge(client, token_budget=10000).salvage(JudgeBrief("exit_prose", "Headcount?"))
    outcome = client.calls[0].tools[0]["parameters"]["properties"]["outcome"]
    assert "outcome=none needs empty sections and empty evidence" in outcome["description"]
    assert "outcome=partial needs at least one unfinished item and at least one evidence reference" in outcome["description"]
    assert "A limitation with no supporting evidence is outcome=none" in outcome["description"]



def test_a_section_citing_a_refusal_receipt_is_told_which_reference_to_remove():
    """A refusal section cited its declineOutOfScope receipt twice and the turn fell to
    salvage: the error never said which reference was wrong."""
    from data_agent.runtime.composer import build_document

    refusal = {**section(), "id": "scope", "evidence_refs": ["call_decline"],
               "presentation": {"kind": "text", "markdown": "I can't terminate employees."}}
    with pytest.raises(PresentationError) as caught:
        build_document({"takeaway": "I can't do that.", "sections": [refusal]}, [], turn_index=0)
    message = str(caught.value)
    assert 'section "scope" cites ["call_decline"]' in message
    assert "evidence_refs: []" in message



@pytest.mark.parametrize("value", ["ok", "na", "omit", "single section", None])
def test_a_known_junk_answer_field_is_dropped_whatever_its_value(value):
    """#18, #30, #33, #41 (and #6, #37, #40 with null) sent `sections_note`, which never
    carries answer content."""
    raw = answer(sections_note=value)
    assert "sections_note" not in proposal_to_internal(raw)
    assert raw["sections_note"] == value  # input not mutated


def test_only_named_junk_is_dropped_so_real_content_is_never_lost_silently():
    message = rejection(answer(caveats=["The roster is capped at 10,000 rows."]))
    assert "undeclared field(s): caveats (is a section field" in message


@pytest.mark.parametrize(
    ("presentation", "expected"),
    [({**BARS, "category_columns": ["Department", "Location"]}, "send only one of: category_column, category_columns"),
     ({"measure_column": "n", "label": "Headcount"}, "send exactly one of: category_column, category_columns")],
)
def test_bars_with_both_or_neither_category_selector_say_which_to_send(presentation, expected):
    assert f"sections.0.presentation: {expected}" in rejection(answer(section("bars", **presentation)))


def test_a_selector_another_kind_does_not_use_is_named():
    raw = answer(section("line", **BARS, category_columns=["Department", "Location"]))
    assert "remove field(s) this kind does not use: category_columns" in rejection(raw)


def test_a_blank_table_reason_and_a_repeated_reference_are_named():
    blank = {**section(), "table_reason": "   "}
    second = {**section(), "id": "second", "table_reason": "Separate requested detail."}
    assert "sections.0.table_reason: must contain text, not only spaces" in rejection(answer(blank, second))
    assert "evidence: must not repeat an item" in rejection(answer(evidence=["q", "q"]))


def test_sections_nested_too_deeply_to_decode_are_rejected_not_raised():
    message = rejection({"takeaway": "Counts.", "sections": "[" * 100_000, "evidence": ["q"]})
    assert "sections: expected type: array, got: string (text nested too deeply to read)" in message
