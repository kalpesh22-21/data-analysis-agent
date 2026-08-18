"""Unit tests for dispatch/schema_preview.py — the two-tier wide-schema fit (ISSUES C5).

The bug being fixed: the old head-cut kept the first ~36 of `employee`'s 124
columns WHOLE and dropped the rest ENTIRELY, so `annual_salary` (index ~87) was
not merely undocumented to the model — it did not appear to EXIST. These tests
pin the replacement contract: every column is named for as long as any budget
remains, per-column DETAIL is what degrades, the question only ORDERS the detail,
and the marker is inside the fit and promises nothing the runtime cannot do.

`_employee_schema()` below rebuilds the real MCP `getTableSchema` response for
`dbpcm_warehouse.employee` from the committed catalog export fixture. The fixture
is the CATALOG side of the response (`{column: {type, description, ...}}`); the
merge here mirrors `clickhouse-api semantic_catalog/overlay.py::_merge_columns`
+ `build_schema_response` (the 10-key column entry with the overlay half defaulted
to None/False, hidden columns stripped), which is exactly the shape whose ~38%
null/false boilerplate the compaction tier exists to remove.
"""

from __future__ import annotations

import json
from typing import Any

from data_agent.runtime.dispatch.schema_preview import (
    MARKER_KEY,
    _estimate_tokens,
    _rank_columns,
    fit_schema_under_cap,
)
from tests._catalog_fixture import fixture_catalog

# The overlay half of a merged column entry (overlay.py::_COLUMN_OVERLAY_FIELDS).
_OVERLAY_FIELDS = ("description", "synonyms", "unit", "values", "observed_values")

_SALARY_QUESTION = "average annual salary by department for active employees"


def _employee_schema() -> dict[str, Any]:
    """The MCP getTableSchema response for `dbpcm_warehouse.employee`.

    DRIFT: this hand-mirrors `overlay.py::_merge_columns`, so a new overlay field
    added upstream goes quietly missing here (the fixture just gets smaller) — the
    LOUD tripwire is `assert _size(schema) > 4_000` in the production-cap test
    below, which fails the moment this stops being an over-cap schema.
    """
    entry = fixture_catalog()["dbpcm_warehouse.employee"]
    hidden = set((entry.get("mcp_projection") or {}).get("hidden_columns") or [])
    columns: list[dict[str, Any]] = []
    for name, overlay in entry["columns"].items():
        if name in hidden:
            continue
        column: dict[str, Any] = {
            "name": name,
            "type": overlay.get("type", "String"),
            "comment": "",
        }
        for field in _OVERLAY_FIELDS:
            column[field] = overlay.get(field)
        column["client_defined"] = bool(overlay.get("client_defined", False))
        column["sensitive"] = bool(overlay.get("sensitive", False))
        columns.append(column)
    return {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "catalogued": True,
        "description": entry.get("description"),
        "grain": entry.get("grain"),
        "temporal": entry.get("temporal"),
        "primary_key": entry.get("primary_key"),
        "join_keys": entry.get("join_keys"),
        "columns": columns,
        "measures": None,
        "rules": entry.get("rules"),
        "ambiguities": entry.get("ambiguities"),
    }


def _size(payload: Any) -> int:
    return _estimate_tokens(json.dumps(payload, default=str))


def _names(fitted: dict[str, Any]) -> list[str]:
    return [column["name"] for column in fitted["columns"]]


def _detailed_names(fitted: dict[str, Any]) -> list[str]:
    return [column["name"] for column in fitted["columns"] if set(column) - {"name", "type"}]


# --- tier 2: existence survives ---------------------------------------------


def test_every_column_is_named_at_the_production_cap() -> None:
    """THE REGRESSION. At the shipped 4,000-token cap the employee schema (130
    columns in the fixture, ~12k tokens) used to keep 35 columns and drop 95
    names. Now every single column is present, and the whole payload — marker
    included — is still under the cap."""
    schema = _employee_schema()
    total = len(schema["columns"])
    assert _size(schema) > 4_000, "fixture must actually be over the cap"

    fitted, report = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)

    assert _names(fitted) == [column["name"] for column in schema["columns"]]
    assert report.listed_columns == total
    assert report.omitted_columns == 0
    assert report.fits is True
    assert _size(fitted) <= 4_000


def test_the_marker_is_inside_the_fit() -> None:
    """The old marker was appended AFTER the fit was measured, so the stored cell
    was always the cap plus an unbudgeted ~120 chars. The marker is now part of
    every trial render: removing it can only make the payload smaller."""
    schema = _employee_schema()
    fitted, report = fit_schema_under_cap(schema, 4_000)

    assert report.marker_added is True
    assert MARKER_KEY in fitted
    assert _size(fitted) <= 4_000  # tightened: cap, not cap*2
    assert len(fitted[MARKER_KEY]) > 100  # the marker is real, not a token


def test_the_marker_makes_no_promise_the_runtime_cannot_keep() -> None:
    """`getTableSchema` has no narrowing argument and the repeated-read guard
    declines an identical re-fetch, so the old "re-fetch getTableSchema or narrow"
    advice was unfollowable. The marker states what was withheld and tells the
    model to ASK instead of assuming."""
    schema = _employee_schema()
    fitted, _ = fit_schema_under_cap(schema, 4_000)
    marker = fitted[MARKER_KEY]

    assert "re-fetch" not in marker.lower()
    assert "narrow" not in marker.lower()
    assert "name and type ONLY" in marker
    assert "was NOT shown to you here" in marker
    assert "ask the user" in marker


def test_detail_is_what_degrades_not_existence() -> None:
    """Every column is named at every cap that can hold the names; what shrinks as
    the cap shrinks is how many of them keep their documentation."""
    schema = _employee_schema()
    total = len(schema["columns"])

    generous, generous_report = fit_schema_under_cap(schema, 6_000)
    tight, tight_report = fit_schema_under_cap(schema, 2_600)

    assert generous_report.listed_columns == tight_report.listed_columns == total
    assert generous_report.detailed_count > tight_report.detailed_count
    assert _size(generous) <= 6_000
    assert _size(tight) <= 2_600


# --- tier 1: compaction is lossless -----------------------------------------


def test_compaction_never_loses_a_truthy_field() -> None:
    """Property over EVERY column of the real employee schema: whatever survives
    into the fitted payload, no key with a truthy value may have been dropped or
    altered — compaction may only remove null/False/""/[]. (Reduced columns are
    the skeleton tier, checked separately; this asserts over the detailed ones.)"""
    schema = _employee_schema()
    source = {column["name"]: column for column in schema["columns"]}

    fitted, _ = fit_schema_under_cap(schema, 6_000)

    checked = 0
    for column in fitted["columns"]:
        if not set(column) - {"name", "type"}:
            continue  # skeleton tier — nothing claimed about it here
        original = source[column["name"]]
        expected = {k: v for k, v in original.items() if v not in (None, False, "", [])}
        assert column == expected
        checked += 1
    assert checked > 0


def test_compaction_keeps_zero_and_drops_only_the_information_free() -> None:
    """`0` is a measurement, not an absence — `0 == False` in Python, and a naive
    falsiness filter would have eaten it."""
    schema = {
        "database": "d",
        "table": "t",
        "columns": [
            {
                "name": "c",
                "type": "Int64",
                "comment": "",
                "description": None,
                "synonyms": [],
                "unit": None,
                "values": None,
                "observed_values": None,
                "client_defined": False,
                "sensitive": False,
                "min_observed": 0,
                "flagged": True,
            }
        ],
        "filler": "x" * 40_000,
    }
    fitted, report = fit_schema_under_cap(schema, 4_000)

    assert fitted["columns"] == [
        {"name": "c", "type": "Int64", "min_observed": 0, "flagged": True}
    ]
    assert report.compacted is True
    assert report.detailed_count == 1
    assert report.reduced_count == 0


def test_a_column_with_no_documentation_counts_as_detailed_not_reduced() -> None:
    """Honesty of the counts: showing `{name, type}` for a column the catalog never
    documented withholds NOTHING, so it must not be counted (or reported to the
    operator) as detail that was dropped."""
    schema = {
        "database": "d",
        "table": "t",
        "columns": [{"name": f"c{i}", "type": "String", "description": None} for i in range(50)],
        "filler": "x" * 40_000,
    }
    _, report = fit_schema_under_cap(schema, 4_000)

    assert report.total_columns == 50
    assert report.detailed_count == 50
    assert report.reduced_count == 0


# --- tier 3: question relevance ordering ------------------------------------


def test_the_question_named_column_keeps_its_detail() -> None:
    """`annual_salary` is index 93 of 130 and carries the median-on-even-rows
    guidance a salary question depends on. Under the head-cut it was not even
    named; with the question it now ranks FIRST and its documentation survives at
    the production cap."""
    schema = _employee_schema()
    order = _rank_columns(schema["columns"], _SALARY_QUESTION)

    assert schema["columns"][order[0]]["name"] == "annual_salary"
    # PLURAL PHRASING on the real table (`_stem`: "salaries" -> "salary"; the
    # trailing-`s` strip alone produced "salarie", which matches no column here).
    # The two `department_*` columns legitimately outrank it — the question names
    # "department" and that token is rarer across this table than "salary" — but
    # `annual_salary` is top-3 and, crucially, EVERY top-5 slot is now a salary or
    # department column. Under the broken stem the word "salaries" contributed
    # nothing at all and slots 4-6 went to prose noise whose descriptions merely
    # repeat "average"/"department" (dol_status_description, employee_code,
    # clock_sequence), which is the failure mode this ranker exists to avoid.
    plural = _rank_columns(schema["columns"], "what are the average salaries by department")
    plural_names = [schema["columns"][index]["name"] for index in plural]
    assert "annual_salary" in plural_names[:3]
    assert all("salary" in name or "department" in name for name in plural_names[:5])

    fitted, _ = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)
    detailed = _detailed_names(fitted)
    assert "annual_salary" in detailed
    salary_entry = next(c for c in fitted["columns"] if c["name"] == "annual_salary")
    assert "quantileExactLow" in salary_entry["description"]
    assert salary_entry["unit"] == "USD"
    # The rest of the question's subject matter is up there too...
    assert {"department_code", "department_name", "employee_status"} <= set(detailed)
    # ...and WITHOUT the question that same cap spends its detail budget on the
    # leading columns instead, which is how the naive prototype floated
    # badge_number and lost the salary column.
    unranked, _ = fit_schema_under_cap(schema, 4_000)
    assert "annual_salary" not in _detailed_names(unranked)
    assert "badge_number" in _detailed_names(unranked)


def test_rare_tokens_outrank_common_prose_words() -> None:
    """The naive prototype scored description words as heavily as names, so
    columns whose prose repeats the table's most common words ("employee", "the
    unique identifier associated with…") floated to the top. IDF over the table's
    own columns is what demotes them."""
    columns = [
        {
            "name": "badge_number",
            "type": "String",
            "description": "The unique employee badge number for the employee record.",
        },
        {
            "name": "emergency_contact_name",
            "type": "String",
            "description": "The employee emergency contact for this employee record.",
        },
        {
            "name": "annual_salary",
            "type": "Decimal",
            "description": "Yearly pay for the employee record.",
        },
    ]
    order = _rank_columns(columns, "what is the average annual salary of an employee")
    assert columns[order[0]]["name"] == "annual_salary"
    # PLURAL PHRASING, the commonest way this question is actually asked. The
    # trailing-`s` strip alone made "salaries" -> "salarie", which matches nothing
    # at all, so every column scored 0 and the ranker silently degraded to list
    # order — putting badge_number first. `_stem`'s `ies -> y` rule is what makes
    # the two phrasings agree.
    plural = _rank_columns(columns, "what are the average salaries by department")
    assert columns[plural[0]]["name"] == "annual_salary"


def test_synonyms_are_weighted_like_the_name() -> None:
    """A column whose NAME is opaque but whose catalog `synonyms` say what the
    user said must rank as if it were named that."""
    columns = [
        {"name": "col_a1", "type": "String", "description": "Opaque internal code."},
        {
            "name": "col_b2",
            "type": "Decimal",
            "description": "Opaque internal amount.",
            "synonyms": ["headcount"],
        },
    ]
    order = _rank_columns(columns, "what is the headcount")
    assert columns[order[0]]["name"] == "col_b2"


def test_no_question_preserves_list_order() -> None:
    columns = [{"name": f"c{i}", "type": "String"} for i in range(10)]
    assert _rank_columns(columns, None) == list(range(10))
    assert _rank_columns(columns, "   ") == list(range(10))
    assert _rank_columns(columns, "the and of to") == list(range(10))  # stopwords only


# --- D25: the question is an ORDERING input and nothing else ----------------


def test_the_question_never_appears_in_the_output() -> None:
    """D25 (load-bearing, same posture as the credential scan in
    test_tool_dispatcher.py): the question is RAW USER TEXT. It may reorder the
    fit and must never be echoed into the schema, the marker, or the report —
    those all flow onward into the trail, the SSE stream and the spans."""
    schema = _employee_schema()
    nonce = "zqx-nonce-42"
    question = f"average annual salary for {nonce} in the north-east region"

    fitted, report = fit_schema_under_cap(schema, 4_000, question=question)

    blob = json.dumps({"fitted": fitted, "report": report.__dict__}, default=str)
    assert question not in blob
    assert nonce not in blob
    assert "north-east" not in blob
    # And the question DID do its one job.
    assert "annual_salary" in _detailed_names(fitted)


# --- degradation floors ------------------------------------------------------


def test_a_hostile_cap_keeps_the_head_of_the_skeleton_and_says_so() -> None:
    """Below the skeleton's own size there is nothing left to give: the head of
    the NAME list survives and the marker says plainly that this is not the full
    column list. No omitted-count that contradicts what is shown."""
    schema = _employee_schema()
    total = len(schema["columns"])

    fitted, report = fit_schema_under_cap(schema, 1_200)

    assert 0 < report.listed_columns < total
    assert report.omitted_columns == total - report.listed_columns
    assert len(fitted["columns"]) == report.listed_columns
    assert report.fits is True
    assert _size(fitted) <= 1_200
    marker = fitted[MARKER_KEY]
    assert f"{report.listed_columns} of {total} columns are listed" in marker
    assert f"{report.omitted_columns} could not be listed at all" in marker
    assert "NOT the full column list" in marker
    # Identity survives every floor — the model must know WHICH table this is.
    assert fitted["database"] == "dbpcm_warehouse"
    assert fitted["table"] == "employee"


def test_base_keys_are_degraded_explicitly_and_named_in_the_marker() -> None:
    """Base keys are NOT free. A schema whose `ambiguities` alone blow the cap
    must not push the column NAMES out: the heavy sections are dropped
    largest-first, the identity keys stay, and the marker names what went."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "catalogued": True,
        "grain": ["employee_code"],
        # `rules` is the BIGGER of the two, so it is dropped FIRST — but the marker
        # must name them in the schema's own key order, not in drop order.
        "ambiguities": [{"term": f"t{i}", "note": "y" * 400} for i in range(20)],
        "rules": [{"rule": "z" * 12_000}],
        "columns": [{"name": f"c{i}", "type": "String"} for i in range(30)],
    }
    fitted, report = fit_schema_under_cap(schema, 700)

    assert report.listed_columns == 30  # the names survived the base
    assert report.omitted_columns == 0
    assert report.base_kept is False
    assert report.base_dropped_count == 2
    assert "ambiguities" not in fitted
    assert "rules" not in fitted
    assert fitted["database"] == "dbpcm_warehouse"
    assert fitted["table"] == "employee"
    assert report.fits is True
    assert _size(fitted) <= 700
    # Dropped largest-first (rules, then ambiguities) but NAMED in schema order.
    assert "Table-level sections omitted to fit: ambiguities, rules" in fitted[MARKER_KEY]
    # The cheap ones were never touched.
    assert fitted["grain"] == ["employee_code"]
    assert fitted["catalogued"] is True


def test_base_is_kept_whenever_the_names_fit_beside_it() -> None:
    """The converse: `rules`/`ambiguities` are exactly the semantics the system
    prompt tells the model to read, so they are only sacrificed when the column
    names would otherwise be lost — never merely to buy more per-column detail."""
    schema = _employee_schema()
    fitted, report = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)

    assert report.base_kept is True
    assert report.base_dropped_count == 0
    assert fitted["rules"] == schema["rules"]
    assert fitted["ambiguities"] == schema["ambiguities"]
    assert fitted["join_keys"] == schema["join_keys"]


def test_a_nonsense_cap_still_returns_a_coherent_dict() -> None:
    """The degenerate floor: not even the marker fits. The result is still a
    well-formed dict whose marker AGREES with what it contains (0 listed, N
    omitted) — the incoherent-at-zero text the card branch had to carve out
    cannot happen here, because the counts are computed from the payload."""
    schema = _employee_schema()
    total = len(schema["columns"])

    fitted, report = fit_schema_under_cap(schema, 1)

    assert isinstance(fitted, dict)
    assert fitted["columns"] == []
    assert report.listed_columns == 0
    assert report.omitted_columns == total
    assert report.fits is False
    marker = fitted[MARKER_KEY]
    assert f"0 of {total} columns are listed" in marker
    assert "name and type ONLY" not in marker  # nothing is listed to say it about


def test_a_schema_that_fits_whole_is_returned_whole_without_a_marker() -> None:
    """Compaction alone is LOSSLESS, so a schema that fits once the null/false
    boilerplate is gone carries no marker: there is nothing to warn about."""
    schema = {
        "database": "d",
        "table": "t",
        "columns": [
            {"name": "a", "type": "String", "description": "The a.", "unit": None},
            {"name": "b", "type": "Int64", "description": None, "sensitive": False},
        ],
    }
    fitted, report = fit_schema_under_cap(schema, 4_000)

    assert MARKER_KEY not in fitted
    assert report.marker_added is False
    assert report.reduced_count == 0
    assert report.detailed_count == 2
    assert fitted["columns"] == [
        {"name": "a", "type": "String", "description": "The a."},
        {"name": "b", "type": "Int64"},
    ]


def test_non_dict_column_entries_do_not_crash_the_fit() -> None:
    """Defensive: the branch is keyed on `columns` being a LIST, not on its items
    being the 10-key entry. A bare-string column list still comes back a valid,
    bounded dict."""
    schema = {"database": "d", "table": "t", "columns": [f"col_{i}" for i in range(2_000)]}
    fitted, report = fit_schema_under_cap(schema, 500)

    assert isinstance(fitted, dict)
    assert 0 < report.listed_columns < 2_000
    assert all(isinstance(c, str) for c in fitted["columns"])
    assert _size(fitted) <= 500
