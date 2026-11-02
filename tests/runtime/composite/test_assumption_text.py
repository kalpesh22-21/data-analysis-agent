import pytest

from data_agent.runtime.composer import seal_document
from data_agent.runtime.composite.assumption_text import (
    assumption_key,
    business_caveat,
    clean_caveats,
)
from data_agent.runtime.composite.record_assumptions import clean_assumptions


@pytest.mark.parametrize(
    "instruction",
    [
        "Only the top 20 of 211 departments are shown in the chart; the full result is available in the table/CSV",
        "Use the table's paging/CSV to view all rows.",
        "The chart is limited to 25 rows.",
        "The full result is available in the table/CSV.",
        "Click Download to export the CSV.",
    ],
)
def test_ui_instructions_are_removed(instruction):
    assert clean_assumptions([instruction]) == []
    assert clean_caveats([instruction]) == []


def test_mixed_text_preserves_business_clause():
    assert (
        business_caveat(
            "Only active employees are counted. Use the table's paging to view all rows."
        )
        == "Only active employees are counted."
    )
    assert (
        business_caveat(
            "Only the first 20 rows are shown in the chart; salaries exclude zero values."
        )
        == "salaries exclude zero values."
    )


@pytest.mark.parametrize(
    "other",
    [
        "Headcount is current employees (Active plus On Leave).",
        "Headcount counts unique employees with status On Leave and Active.",
        "Headcount includes Active and On Leave employees.",
    ],
)
def test_known_population_paraphrases_keep_first_wording(other):
    first = "Headcount counts distinct employees with status Active or On Leave"
    assert clean_assumptions([first, other]) == [first]
    assert assumption_key(first) == assumption_key(other)


@pytest.mark.parametrize(
    "distinct",
    [
        "Headcount counts employees with status Active only.",
        "Headcount counts employee records with status Active or On Leave.",
        "Headcount counts distinct employees with status Active or On Leave in 2026.",
        "Headcount counts distinct employees with status Active or On Leave excluding contractors.",
        "Headcount counts distinct employees with status Active or On Leave in Sales.",
        "Headcount does not count employees with status Active or On Leave.",
        "Headcount is current employees.",
        "Only the top 20 departments are included in the calculation.",
        "The CSV source excludes terminated employees.",
        "The table shows employees whose salary is unavailable.",
    ],
)
def test_qualifications_unknown_meanings_and_source_limitations_survive(distinct):
    first = "Headcount counts distinct employees with status Active or On Leave"
    assert clean_caveats([first, distinct]) == [first, distinct]


def test_sealing_cleans_corrections_without_mutating_reviewed_input():
    first = "Headcount counts distinct employees with status Active or On Leave"
    candidate = {
        "sections": [
            {"caveats": [first]},
            {
                "caveats": [
                    "Headcount is current employees (Active plus On Leave).",
                    "Use the table's paging/CSV to view all rows.",
                    "Only 2025 records are counted.",
                ]
            },
        ]
    }
    sealed = seal_document(candidate, answer_id="a", generated_at="now")
    assert sealed["sections"][1]["caveats"] == ["Only 2025 records are counted."]
    assert len(candidate["sections"][1]["caveats"]) == 3
    assert seal_document(sealed, answer_id="a", generated_at="now") == sealed


@pytest.mark.parametrize("reverse", [False, True])
def test_deepseek_population_definition_is_subsumed_by_full_headcount(reverse):
    from data_agent.runtime.composite.assumption_text import clean_document_caveats

    full = "Headcount counts distinct employees with status Active or On Leave."
    sections = [{"caveats": ["Current means Active plus On Leave."]}, {"caveats": [full]}]
    if reverse:
        sections.reverse()
    doc = {"sections": sections}
    clean_document_caveats(doc)
    assert [c for section in sections for c in section["caveats"]] == [full]


def test_population_definition_is_preserved_without_an_equivalent_full_definition():
    from data_agent.runtime.composite.assumption_text import clean_document_caveats

    caveats = [
        "Current means Active plus On Leave.",
        "Headcount counts distinct employees with status Active or On Leave in Sales.",
    ]
    doc = {"sections": [{"caveats": list(caveats)}]}
    clean_document_caveats(doc)
    assert doc["sections"][0]["caveats"] == caveats


@pytest.mark.parametrize("field", ["takeaway", "title", "explanation", "markdown"])
def test_qualification_already_displayed_does_not_need_a_caveat(field):
    from data_agent.runtime.composite.assumption_text import clean_document_caveats

    text = "Median excludes zero salaries."
    section = {
        "caveats": [text, "Employees without hire dates are excluded."],
        "presentation": {"kind": "text"},
    }
    doc = {"sections": [section]}
    if field == "takeaway":
        doc[field] = "The median is $100,000. " + text
    elif field == "markdown":
        section["presentation"][field] = text
    else:
        section[field] = text
    clean_document_caveats(doc)
    assert section["caveats"] == ["Employees without hire dates are excluded."]


def test_a_negated_or_differently_scoped_statement_does_not_suppress_a_caveat():
    from data_agent.runtime.composite.assumption_text import clean_document_caveats

    doc = {
        "takeaway": "It is not true that median excludes zero salaries. Only 2026 records are included.",
        "sections": [
            {"caveats": ["Median excludes zero salaries.", "Only 2025 records are included."]}
        ],
    }
    clean_document_caveats(doc)
    assert len(doc["sections"][0]["caveats"]) == 2
