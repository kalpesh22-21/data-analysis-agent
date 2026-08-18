"""Unit tests for dispatch/schema_preview.py — the model-facing schema shape
(ISSUES C5, refined by the C5b user spec).

The bug C5 fixed: the old head-cut kept the first ~35 of `employee`'s 130 columns
WHOLE and dropped the rest ENTIRELY, so `annual_salary` (index ~93) was not
merely undocumented to the model — it did not appear to EXIST.

What C5b changed on top, and what these tests now pin:
  * TENANCY COLUMNS (`client_code` / proc-center) are removed on EVERY path,
    including the under-budget one — they are pre-applied by the platform and the
    model must never write a predicate on one;
  * NULL/EMPTY KEYS are stripped on EVERY path too (user spec 2026-08-18 point 5,
    which overrides the earlier byte-identity deviation): the under-budget
    guarantee is COMPACTED-STABLE, not byte-identical, and carries no marker;
  * TABLE-LEVEL SECTIONS ARE NEVER TRUNCATED. The C5 largest-first base-drop
    ladder is gone; `rules`/`ambiguities` ride complete however pathological they
    are (the two old base-degradation tests became the two never-truncate pins
    below);
  * THE BUDGET IS THE COLUMNS SECTION'S ALONE (6,000 by default, not the generic
    `max_tool_result_tokens`), so base size can never buy or cost column detail;
  * THE EMITTED ORDER IS GROUPED: [detailed group, relevance-ordered, with the
    structural key columns pinned in] + [the rest in ORIGINAL schema order], and
    the marker says so;
  * the question is still an ORDERING input and nothing else (D25).

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

# The shipped default for the columns section (RuntimeSettings.
# schema_columns_token_budget / _DEFAULT_SCHEMA_COLUMNS_TOKEN_BUDGET).
_PRODUCTION_BUDGET = 6_000


def _employee_schema() -> dict[str, Any]:
    """The MCP getTableSchema response for `dbpcm_warehouse.employee`.

    DRIFT: this hand-mirrors `overlay.py::_merge_columns`, so a new overlay field
    added upstream goes quietly missing here (the fixture just gets smaller) — the
    LOUD tripwire is `assert _size(schema["columns"]) > _PRODUCTION_BUDGET` in the
    production-budget test below, which fails the moment this stops being an
    over-budget schema.
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


def _columns_size(fitted: dict[str, Any]) -> int:
    """What the budget actually governs: the columns section plus the marker."""
    return _estimate_tokens(
        json.dumps(fitted["columns"], default=str) + fitted.get(MARKER_KEY, "")
    )


def _names(fitted: dict[str, Any]) -> list[str]:
    return [column["name"] for column in fitted["columns"]]


def _detailed_names(fitted: dict[str, Any]) -> list[str]:
    return [column["name"] for column in fitted["columns"] if set(column) - {"name", "type"}]


# --- tier 2: existence survives ---------------------------------------------


def test_every_column_is_named_at_the_production_budget() -> None:
    """THE C5 REGRESSION, re-pinned at the C5b budget. At the shipped columns
    budget the employee schema (130 columns, ~10.8k tokens of columns) keeps every
    single column present, and the columns section — marker included — is under
    the budget."""
    schema = _employee_schema()
    total = len(schema["columns"])
    assert _size(schema["columns"]) > _PRODUCTION_BUDGET, "fixture must be over budget"

    fitted, report = fit_schema_under_cap(
        schema, _PRODUCTION_BUDGET, question=_SALARY_QUESTION
    )

    assert sorted(_names(fitted)) == sorted(c["name"] for c in schema["columns"])
    assert report.listed_columns == total
    assert report.omitted_columns == 0
    assert report.fits is True
    assert _columns_size(fitted) <= _PRODUCTION_BUDGET


def test_the_production_budget_shows_nearly_every_column_in_full() -> None:
    """THE C5b PAYOFF, measured. Compacting employee's columns gets them to ~6.7k
    tokens — just over the 6,000 budget — so almost every column keeps its
    documentation instead of the ~17 that fitted beside the base sections under
    the old shared 4,000-token cap."""
    schema = _employee_schema()
    fitted, report = fit_schema_under_cap(
        schema, _PRODUCTION_BUDGET, question=_SALARY_QUESTION
    )

    assert report.detailed_count >= int(0.85 * report.total_columns)
    assert report.reduced_count <= 20
    # And the whole payload, base included, stays a small fraction of the ~89.6k
    # request budget: three schemas of this size are ~24% of it.
    assert _size(fitted) < 8_000


def test_the_marker_is_inside_the_fit() -> None:
    """The old marker was appended AFTER the fit was measured, so the stored cell
    was always the budget plus an unbudgeted ~120 chars. The marker is now part of
    every trial render: removing it can only make the payload smaller."""
    schema = _employee_schema()
    fitted, report = fit_schema_under_cap(schema, 4_000)

    assert report.marker_added is True
    assert MARKER_KEY in fitted
    assert _columns_size(fitted) <= 4_000
    assert len(fitted[MARKER_KEY]) > 100  # the marker is real, not a token


def test_the_marker_makes_no_promise_the_runtime_cannot_keep() -> None:
    """The marker states what was withheld and (C5b) that the list is not the
    table's physical order, so the model cannot read the order as one.

    It no longer ends at "ask the user rather than assuming" (2026-08-18): the MCP
    took an optional `columns` argument, so the recoverable case has a RECOVERY —
    see the narrowing test below."""
    schema = _employee_schema()
    fitted, _ = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)
    marker = fitted[MARKER_KEY]

    assert "re-fetch" not in marker.lower()
    assert "name and type ONLY" in marker
    assert "was NOT shown to you here" in marker
    # The ordering sentence (C5b point 1).
    assert "RELEVANCE order, not the table's physical column order" in marker
    # ...and the promise that nothing else was touched (C5b point 3).
    assert "rules, ambiguities — is complete and was not touched" in marker


def test_the_marker_tells_the_model_how_to_get_the_withheld_documentation() -> None:
    """2026-08-18: `getTableSchema` NOW HAS a narrowing argument (`columns`), built
    concurrently in clickhouse-api. The old marker's dead end — "ask the user
    rather than assuming" — turned a recoverable gap into an interruption of the
    user; the marker now names the call that recovers it.

    The advice is followable: `read_guard.idempotent_read_signature` keys on the
    tool name plus the CANONICALIZED ARGUMENTS, so a second fetch carrying
    `columns` is a different signature from the bare one that produced this payload
    and is dispatched rather than declined as a repeated read."""
    from data_agent.runtime.loop.read_guard import idempotent_read_signature

    schema = _employee_schema()
    fitted, report = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)
    marker = fitted[MARKER_KEY]

    assert report.reduced_count > 0  # the clause is only emitted when it applies
    assert "call getTableSchema again for this table with" in marker
    assert 'columns: ["<name>", ...]' in marker
    assert "their full documentation is returned" in marker
    # The dead end is gone.
    assert "ask the user" not in marker
    # And the guard really does let that call through.
    bare = {"database": "dbpcm_warehouse", "table": "employee"}
    assert idempotent_read_signature("getTableSchema", bare) != idempotent_read_signature(
        "getTableSchema", {**bare, "columns": ["annual_salary"]}
    )


def test_the_marker_claims_relevance_order_only_when_a_question_ranked_it() -> None:
    """Two orderings, two sentences. With a question the emitted order really is
    relevance order. WITHOUT one `_rank_columns` returns list order, so the leading
    columns were chosen by the table and the structural pins — not by the request —
    and a marker calling that "the ones most likely to matter for the current
    request" would be telling the model something nobody computed."""
    schema = _employee_schema()

    ranked, _ = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)
    unranked, _ = fit_schema_under_cap(schema, 4_000)
    # A question of pure stopwords ranks nothing either — same gate as _rank_columns.
    stopwords_only, _ = fit_schema_under_cap(schema, 4_000, question="what are the")

    assert (
        "RELEVANCE order, not the table's physical column order: the ones most "
        "likely to matter for the current request — and the key columns — come "
        "first, with the remainder after them in the table's own order."
    ) in ranked[MARKER_KEY]

    for marker in (unranked[MARKER_KEY], stopwords_only[MARKER_KEY]):
        assert "relevance" not in marker.lower()
        assert "most likely to matter" not in marker
        assert (
            "The columns are NOT listed in the table's physical column order: the "
            "key columns and the columns the table lists first come first, with the "
            "remainder after them in the table's own order."
        ) in marker
    # Both variants still carry the rest of the marker.
    assert "name and type ONLY" in unranked[MARKER_KEY]


def test_detail_is_what_degrades_not_existence() -> None:
    """Every column is named at every budget that can hold the names; what shrinks
    as the budget shrinks is how many of them keep their documentation."""
    schema = _employee_schema()
    total = len(schema["columns"])

    generous, generous_report = fit_schema_under_cap(schema, 6_000)
    tight, tight_report = fit_schema_under_cap(schema, 2_600)

    assert generous_report.listed_columns == tight_report.listed_columns == total
    assert generous_report.detailed_count > tight_report.detailed_count
    assert _columns_size(generous) <= 6_000
    assert _columns_size(tight) <= 2_600


# --- C5b point 2: the tenancy columns are never model-facing ----------------


def _tenancy_schema(*, filler_columns: int = 0) -> dict[str, Any]:
    """A schema shaped like the seven catalogued tables that expose `client_code`
    as an ordinary column (accrual_events, performance_discussions, the
    applicant-tracking and candidate tables) — plus a `proc_center` for the RLS
    pair, and a CamelCase spelling from before the warehouse snake-migration."""
    return {
        "database": "dbpcm_warehouse",
        "table": "accrual_events",
        "primary_key": ["employee_code"],
        # Base sections ride COMPLETE even when they name a tenancy column: this
        # one describes filtering the PLATFORM applies (C5b point 2a).
        "rules": [
            {
                "id": "tenant_scope",
                "predicate": "client_code = getSetting('paycom_client_code')",
                "description": "Applied server-side by the row policy.",
            }
        ],
        "ambiguities": [{"term": "client", "note": "client_code is not a filter you write."}],
        "columns": [
            {"name": "client_code", "type": "String", "description": "Tenant key."},
            {"name": "proc_center", "type": "String", "description": "Processing centre."},
            {"name": "ClientCode", "type": "String", "description": "Pre-migration spelling."},
            {"name": "employee_code", "type": "String", "description": "Employee key."},
            {"name": "hours", "type": "Float64", "description": "Accrued hours."},
        ]
        + [
            {"name": f"filler_{i}", "type": "String", "description": "y" * 200}
            for i in range(filler_columns)
        ],
    }


def test_tenancy_columns_are_stripped_from_an_under_budget_schema() -> None:
    """One of the TWO unconditional strips. A small schema is still returned with
    no reordering and no marker — but the pre-applied tenancy columns are gone.
    These entries carry no null keys, so the other unconditional strip (§3a
    compaction) is a no-op here and the survivors come back verbatim."""
    schema = _tenancy_schema()

    fitted, report = fit_schema_under_cap(schema, _PRODUCTION_BUDGET)

    assert _names(fitted) == ["employee_code", "hours"]
    assert report.tenancy_hidden_count == 3
    assert report.total_columns == 2  # the model's universe, tenancy excluded
    assert report.marker_added is False
    assert report.compacted is False  # nothing to compact in these entries
    assert MARKER_KEY not in fitted
    assert fitted["columns"] == schema["columns"][3:]


def test_tenancy_columns_are_stripped_over_budget_too() -> None:
    """The same strip on the path where the fitter is actually working."""
    schema = _tenancy_schema(filler_columns=200)

    fitted, report = fit_schema_under_cap(schema, 1_000, question="accrued hours")

    assert "client_code" not in _names(fitted)
    assert "proc_center" not in _names(fitted)
    assert "ClientCode" not in _names(fitted)
    assert report.tenancy_hidden_count == 3
    assert json.dumps(fitted["columns"]).count("client_code") == 0


def test_the_tenancy_strip_is_silent_to_the_model_and_counted_for_the_operator() -> None:
    """No marker clause names them: a column the model must not use is one it must
    not be told about either, or the removal has re-created the predicate it
    exists to prevent. The operator still gets the count (D25-safe)."""
    schema = _tenancy_schema(filler_columns=200)

    fitted, report = fit_schema_under_cap(schema, 1_000)

    assert report.tenancy_hidden_count == 3
    marker = fitted[MARKER_KEY]
    assert "tenanc" not in marker.lower()
    assert "client_code" not in marker
    assert "proc_center" not in marker


def test_base_sections_that_mention_a_tenancy_column_ride_complete() -> None:
    """C5b point 2a, resolved by the user: a RULE describing the pre-applied
    tenant predicate is kept verbatim — it explains why the model does NOT write
    that filter. Section completeness (point 3) outranks name-hiding."""
    schema = _tenancy_schema(filler_columns=200)

    fitted, _ = fit_schema_under_cap(schema, 1_000)

    assert fitted["rules"] == schema["rules"]
    assert fitted["ambiguities"] == schema["ambiguities"]
    assert "client_code" in json.dumps(fitted["rules"])


# --- C5b point 3: the base sections are never truncated ---------------------


def test_rules_and_ambiguities_are_never_truncated_however_pathological() -> None:
    """REPLACES `test_base_keys_are_degraded_explicitly_and_named_in_the_marker`.

    That test pinned the OPPOSITE behaviour: a schema whose `rules`/`ambiguities`
    blew the cap had them dropped largest-first. The user decision is that they
    ride complete ALWAYS — they are the semantics the prompt tells the model to
    read, and a budget that can silently delete them buys column width with
    correctness. There is no longer any input that removes a base section."""
    schema = {
        "database": "dbpcm_warehouse",
        "table": "employee",
        "catalogued": True,
        "grain": ["employee_code"],
        "ambiguities": [{"term": f"t{i}", "note": "y" * 400} for i in range(20)],
        "rules": [{"rule": "z" * 12_000}],
        "columns": [{"name": f"c{i}", "type": "String"} for i in range(30)],
    }

    fitted, report = fit_schema_under_cap(schema, 300)

    assert fitted["rules"] == schema["rules"]
    assert fitted["ambiguities"] == schema["ambiguities"]
    assert fitted["grain"] == schema["grain"]
    assert fitted["catalogued"] is True
    assert fitted["database"] == "dbpcm_warehouse"
    assert fitted["table"] == "employee"
    # The columns were never squeezed on the base's behalf either: 30 skeletons
    # cost ~180 tokens, which is inside the 200-token COLUMNS budget.
    assert report.listed_columns == 30
    assert report.omitted_columns == 0
    # No "sections omitted" clause can exist any more.
    assert "omitted to fit" not in json.dumps(fitted)


def test_an_enormous_base_costs_the_columns_nothing() -> None:
    """THE BUDGET IS THE COLUMNS SECTION'S ALONE (C5b point 4). The same columns
    fit identically whether the base sections are empty or gigantic — under the
    C5 shared cap the big base would have eaten the column detail first."""
    columns = [
        {"name": f"c{i}", "type": "String", "description": "A documented column."}
        for i in range(40)
    ]
    lean = {"database": "d", "table": "t", "columns": json.loads(json.dumps(columns))}
    heavy = {
        "database": "d",
        "table": "t",
        "rules": ["z" * 60_000],
        "ambiguities": ["y" * 60_000],
        "columns": json.loads(json.dumps(columns)),
    }

    lean_fitted, lean_report = fit_schema_under_cap(lean, 400)
    heavy_fitted, heavy_report = fit_schema_under_cap(heavy, 400)

    assert lean_fitted["columns"] == heavy_fitted["columns"]
    assert lean_report.detailed_count == heavy_report.detailed_count
    assert heavy_fitted["rules"] == heavy["rules"]


def test_the_base_sections_ride_complete_on_the_real_schema() -> None:
    """REWORK of `test_base_is_kept_whenever_the_names_fit_beside_it`: the
    condition is gone, the guarantee is unconditional."""
    schema = _employee_schema()
    fitted, _ = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)

    assert fitted["rules"] == schema["rules"]
    assert fitted["ambiguities"] == schema["ambiguities"]
    assert fitted["join_keys"] == schema["join_keys"]
    assert fitted["temporal"] == schema["temporal"]
    assert fitted["primary_key"] == schema["primary_key"]
    assert fitted["description"] == schema["description"]


# --- C5b point 1: grouped presentation + structural pins --------------------


def test_the_detailed_group_leads_and_the_skeletons_keep_original_order() -> None:
    """The emitted list is [detailed group, relevance-ordered] + [skeleton
    remainder, ORIGINAL schema order]. The model is told this by the marker; these
    are the two halves of the promise."""
    schema = _employee_schema()
    original = [column["name"] for column in schema["columns"]]

    fitted, report = fit_schema_under_cap(schema, 4_000, question=_SALARY_QUESTION)

    emitted = _names(fitted)
    assert sorted(emitted) == sorted(original)
    detailed = _detailed_names(fitted)
    # The detailed group is a strict PREFIX of the emitted list...
    assert emitted[: len(detailed)] == detailed
    # ...it is question-ordered (the salary/department columns lead it)...
    assert detailed[0] == "annual_salary"
    # ...and the tail is in the table's own order.
    tail = emitted[len(detailed) :]
    assert tail == [name for name in original if name in set(tail)]


def test_structural_columns_are_pinned_into_the_detailed_group() -> None:
    """The table's primary-key and join-key columns keep their documentation
    WHATEVER the question was: a model that cannot see a key column's definition
    cannot join, de-duplicate or check grain, and no phrasing makes that less
    true. Read from the schema response's OWN primary_key/join_keys sections."""
    schema = _employee_schema()
    # A question that names none of them, at a budget too small to detail
    # everything by luck.
    fitted, report = fit_schema_under_cap(
        schema, 2_800, question="how many accrued vacation hours were taken"
    )

    detailed = _detailed_names(fitted)
    assert report.pinned_count == 6
    for key_column in ("employee_code", "department_code", "primary_supervisor_ee_code"):
        assert key_column in detailed, key_column
    # They are MEMBERS of the detailed group (which is relevance-ordered, so a
    # key column the question never mentions sits at the back of it) — not a
    # separate block ahead of it.
    assert set(detailed) >= {"employee_code", "department_code"}
    assert _names(fitted)[: len(detailed)] == detailed


def test_the_pins_survive_a_budget_that_cannot_list_every_column() -> None:
    """When even the names have to go, the tail of the EMISSION order goes — so
    the key columns and the question's columns are the last things dropped, not
    whatever happened to sit late in the physical order."""
    schema = _employee_schema()

    fitted, report = fit_schema_under_cap(schema, 1_200, question=_SALARY_QUESTION)

    assert 0 < report.listed_columns < report.total_columns
    names = _names(fitted)
    assert "employee_code" in names
    assert "department_code" in names
    assert "annual_salary" in names


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
    falsiness filter would have eaten it. (C5b: the trigger is now a COLUMNS
    budget the raw entry misses and the compacted one meets; a big base key can no
    longer force compaction, because the base is not budgeted.)"""
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
    fitted, report = fit_schema_under_cap(schema, 25)

    assert fitted["columns"] == [
        {"name": "c", "type": "Int64", "min_observed": 0, "flagged": True}
    ]
    assert report.compacted is True
    assert report.detailed_count == 1
    assert report.reduced_count == 0
    assert report.marker_added is False  # compaction alone is lossless
    assert fitted["filler"] == schema["filler"]  # the base is not budgeted


def test_a_column_with_no_documentation_counts_as_detailed_not_reduced() -> None:
    """Honesty of the counts: showing `{name, type}` for a column the catalog never
    documented withholds NOTHING, so it must not be counted (or reported to the
    operator) as detail that was dropped."""
    schema = {
        "database": "d",
        "table": "t",
        "columns": [{"name": f"c{i}", "type": "String", "description": None} for i in range(50)],
    }
    _, report = fit_schema_under_cap(schema, 500)

    assert report.total_columns == 50
    assert report.detailed_count == 50
    assert report.reduced_count == 0
    assert report.marker_added is False


# --- tier 3: question relevance ordering ------------------------------------


def test_the_question_named_column_keeps_its_detail() -> None:
    """`annual_salary` is index 93 of 130 and carries the median-on-even-rows
    guidance a salary question depends on. Under the head-cut it was not even
    named; with the question it now ranks FIRST and its documentation survives at
    a budget too small to document everything."""
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

    fitted, _ = fit_schema_under_cap(schema, 3_000, question=_SALARY_QUESTION)
    detailed = _detailed_names(fitted)
    assert "annual_salary" in detailed
    salary_entry = next(c for c in fitted["columns"] if c["name"] == "annual_salary")
    assert "quantileExactLow" in salary_entry["description"]
    assert salary_entry["unit"] == "USD"
    # The rest of the question's subject matter is up there too...
    assert {"department_code", "department_name", "employee_status"} <= set(detailed)
    # ...and WITHOUT the question that same budget spends its detail on the head of
    # the list instead, which is how the naive prototype floated badge_number and
    # lost the salary column.
    unranked, _ = fit_schema_under_cap(schema, 3_000)
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


def test_a_hostile_budget_keeps_the_front_of_the_list_and_says_so() -> None:
    """Below the skeletons' own size there is nothing left to give: the FRONT of
    the emission order survives and the marker says plainly that this is not the
    full column list. No omitted-count that contradicts what is shown."""
    schema = _employee_schema()
    total = len(schema["columns"])

    fitted, report = fit_schema_under_cap(schema, 1_200)

    assert 0 < report.listed_columns < total
    assert report.omitted_columns == total - report.listed_columns
    assert len(fitted["columns"]) == report.listed_columns
    assert report.fits is True
    assert _columns_size(fitted) <= 1_200
    marker = fitted[MARKER_KEY]
    assert f"{report.listed_columns} of {total} columns are listed" in marker
    assert f"{report.omitted_columns} could not be listed at all" in marker
    assert "NOT the full column list" in marker
    # Identity — and every other base section — survives every floor.
    assert fitted["database"] == "dbpcm_warehouse"
    assert fitted["table"] == "employee"
    assert fitted["rules"] == schema["rules"]


def test_a_nonsense_budget_still_returns_a_coherent_dict() -> None:
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
    # Even here the semantics the model is told to read are intact.
    assert fitted["rules"] == schema["rules"]


def test_a_schema_that_fits_is_returned_compacted_stable() -> None:
    """THE UNDER-BUDGET CONTRACT, restated by the user spec (2026-08-18 point 5).

    An earlier draft of C5b made this pin BYTE-IDENTITY — a schema that fit came
    back with its null keys intact, because compaction was framed as a budget
    device. The user's spec is that null keys are stripped from the response
    ALWAYS, to save tokens: `"unit": null` teaches the model nothing at any size.
    So the guarantee is COMPACTED-STABLE, and this test is what pins the
    difference between the two:
      * the tenancy columns and the information-free keys are gone;
      * every key that survives keeps its ORDER and its value verbatim;
      * `columns` keeps its position among the table-level sections;
      * and there is NO MARKER — nothing the model needed was withheld, so it is
        told nothing (a marker here would be a false claim of a degrade).
    """
    schema = {
        "database": "d",
        "table": "t",
        "columns": [
            {"name": "a", "type": "String", "description": "The a.", "unit": None},
            {"name": "b", "type": "Int64", "description": None, "sensitive": False},
        ],
        "rules": ["Keep it."],
    }
    fitted, report = fit_schema_under_cap(schema, 4_000)

    assert MARKER_KEY not in fitted
    assert report.marker_added is False
    assert report.compacted is True  # it reports what the payload IS, not a degrade
    assert report.reduced_count == 0
    assert report.detailed_count == 2
    assert report.tenancy_hidden_count == 0
    # Key order preserved on both axes — the schema's own keys and each column's.
    assert json.dumps(fitted) == json.dumps(
        {
            "database": "d",
            "table": "t",
            "columns": [
                {"name": "a", "type": "String", "description": "The a."},
                {"name": "b", "type": "Int64"},
            ],
            "rules": ["Keep it."],
        }
    )


def test_a_bare_string_column_named_like_a_tenancy_column_is_still_stripped() -> None:
    """FAIL CLOSED ON THE ENTRY SHAPE. `_merge_columns` emits dicts, so no live
    path produces a bare-string column entry today — but the tenancy strip must not
    be one `isinstance` away from handing the model `client_code`. The strip is
    keyed on the NAME however the entry spells itself; a non-dict, non-string entry
    has no name to match and is kept."""
    schema = {
        "database": "d",
        "table": "t",
        "columns": ["client_code", "ClientCode", "proc_center", "employee_code", 42],
    }
    fitted, report = fit_schema_under_cap(schema, 4_000)

    assert fitted["columns"] == ["employee_code", 42]
    assert report.tenancy_hidden_count == 3
    assert report.total_columns == 2
    assert "client_code" not in json.dumps(fitted["columns"])


def test_non_dict_column_entries_do_not_crash_the_fit() -> None:
    """Defensive: the branch is keyed on `columns` being a LIST, not on its items
    being the 10-key entry. A bare-string column list still comes back a valid,
    bounded dict."""
    schema = {"database": "d", "table": "t", "columns": [f"col_{i}" for i in range(2_000)]}
    fitted, report = fit_schema_under_cap(schema, 500)

    assert isinstance(fitted, dict)
    assert 0 < report.listed_columns < 2_000
    assert all(isinstance(c, str) for c in fitted["columns"])
    assert _columns_size(fitted) <= 500
