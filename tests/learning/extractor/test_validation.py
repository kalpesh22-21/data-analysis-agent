"""Deterministic post-emit validation (`to_candidate`) — evidence-mandatory
(D31), lift-not-generate (D34), totality/roles (D97), unrewritable (D52).
Matrix rows 1/8/9/6/5/11 (task items 1,2,3,4,5,6).
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.models import BlueprintPayload, Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import (
    REASON_BAD_ROLE,
    REASON_MALFORMED,
    REASON_MISSING_RULE,
    REASON_NO_ACCEPTANCE,
    REASON_NO_EVIDENCE,
    REASON_TOTALITY,
    REASON_UNREWRITABLE,
    to_candidate,
)

from .helpers import (
    PAYROLL_SQL,
    blueprint_raw,
    evidence_item,
    make_answer_sql,
    make_summary,
    make_tool_call,
    param_inline,
    param_rule,
    param_slot,
    payroll_parameterization,
)


def _validate(raw, *, summary=None, known_rules=frozenset()):
    return to_candidate(raw, summary or make_summary(), known_rules=known_rules)


# --- item 1: valid blueprint extraction -------------------------------------


def test_valid_blueprint_extraction_yields_candidate_with_plan():
    out = _validate(blueprint_raw())
    assert isinstance(out, ExtractedCandidate)
    payload = out.payload
    assert isinstance(payload, BlueprintPayload)

    # The parameterization plan carries all three role kinds (slot|rule|inline).
    roles = {p.locator.column: p.role for p in payload.parameterization}
    assert roles["department"] == "slot"
    assert roles["record_type"] == "inline"
    assert roles["region"] == "slot"
    region = next(p for p in payload.parameterization if p.locator.column == "region")
    assert region.slot.required is False
    assert region.slot.optional_pattern == "TRUE"  # optional slot, NOT deleted (no-drop)

    # Mandatory evidence present (D31).
    assert len(out.header.evidence) >= 1

    # The extractor emits a PLAN, never SQL (D35): no sql_template anywhere.
    assert not hasattr(payload, "sql_template")
    assert "sql_template" not in payload.to_doc()


# --- item 2: evidence mandatory (D31) ---------------------------------------


def test_empty_evidence_is_declined_no_evidence():
    out = _validate(blueprint_raw(evidence=[]))
    assert isinstance(out, Decline)
    assert out.reason == REASON_NO_EVIDENCE


def test_missing_evidence_key_is_declined_no_evidence():
    raw = blueprint_raw()
    del raw["evidence"]
    out = _validate(raw)
    assert isinstance(out, Decline)
    assert out.reason == REASON_NO_EVIDENCE


# --- item 3: lift-not-generate / accepted_signal (D34) ----------------------


def test_session_without_acceptance_is_declined_no_acceptance():
    out = _validate(blueprint_raw(), summary=make_summary(accepted_signal=None))
    assert isinstance(out, Decline)
    assert out.reason == REASON_NO_ACCEPTANCE


def test_payload_without_accepted_signal_is_declined():
    raw = blueprint_raw()
    raw["payload"]["accepted_signal"] = ""
    out = _validate(raw)
    assert isinstance(out, Decline)
    assert out.reason == REASON_NO_ACCEPTANCE


# --- item 4: D97 totality (the WHERE-eq case works) -------------------------


def test_unplanned_where_predicate_is_declined_totality():
    # A plan that omits the region='NA' WHERE predicate → silent dropped filter.
    plan = [p for p in payroll_parameterization() if p["locator"]["column"] != "region"]
    out = _validate(blueprint_raw(parameterization=plan))
    assert isinstance(out, Decline)
    assert out.reason == REASON_TOTALITY


def test_fully_planned_predicates_pass_totality():
    out = _validate(blueprint_raw())
    assert isinstance(out, ExtractedCandidate)


def _function_horizon_param(**locator_overrides):
    locator = {
        "kind": "function_argument",
        "function": "numbers",
        "argument_index": 0,
        "occurrence": 0,
        "context": "table_source",
        "value": "5",
    }
    locator.update(locator_overrides)
    return {
        "locator": locator,
        "role": "slot",
        "slot": {
            "name": "forecast_months",
            "type": "positive_integer",
            "binds_to": None,
            "required": True,
        },
    }


def test_function_argument_horizon_is_a_valid_additive_locator():
    raw = blueprint_raw(
        parameterization=[_function_horizon_param()], source_refs=("tc1",)
    )
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql="SELECT number FROM numbers(5)"),)
    )
    out = _validate(raw, summary=summary)
    assert isinstance(out, ExtractedCandidate)
    locator = out.payload.parameterization[0].locator
    assert locator.kind == "function_argument"
    assert locator.to_doc() == _function_horizon_param()["locator"]


@pytest.mark.parametrize(
    "change",
    [
        {"function": "range"},
        {"argument_index": 1},
        {"context": "expression"},
        {"value": "0"},
    ],
)
def test_function_argument_horizon_rejects_broader_structural_edits(change):
    raw = blueprint_raw(
        parameterization=[_function_horizon_param(**change)], source_refs=("tc1",)
    )
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql="SELECT number FROM numbers(5)"),)
    )
    out = _validate(raw, summary=summary)
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE


def test_function_argument_stale_value_is_correctable_before_rewrite():
    raw = blueprint_raw(
        parameterization=[_function_horizon_param(value="6")], source_refs=("tc1",)
    )
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql="SELECT number FROM numbers(5)"),)
    )

    out = _validate(raw, summary=summary)

    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert out.correctable is True
    assert ".locator.value='6'" in out.detail
    assert "occurrence 0 has value '5'" in out.detail
    assert "Change only locator.value" in out.detail


def test_function_argument_missing_occurrence_is_correctable_before_rewrite():
    raw = blueprint_raw(
        parameterization=[_function_horizon_param(occurrence=1)], source_refs=("tc1",)
    )
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql="SELECT number FROM numbers(5)"),)
    )

    out = _validate(raw, summary=summary)

    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert out.correctable is True
    assert ".locator.occurrence=1" in out.detail
    assert "has 1 occurrence(s)" in out.detail
    assert "from 0 to 0" in out.detail
    assert "Change only locator.occurrence" in out.detail


def test_function_argument_with_no_matching_sql_site_is_terminal():
    raw = blueprint_raw(
        parameterization=[_function_horizon_param()], source_refs=("tc1",)
    )
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql="SELECT 5 AS fixed_horizon"),)
    )

    out = _validate(raw, summary=summary)

    assert isinstance(out, Decline)
    assert out.reason == REASON_UNREWRITABLE
    assert out.correctable is False
    assert "contains no matching structural site" in out.detail
    assert "0 to -1" not in out.detail


def _interval_param(value: str = "5", **locator_overrides):
    locator = {
        "kind": "interval_argument",
        "unit": "YEAR",
        "occurrence": 0,
        "context": "interval",
        "value": value,
    }
    locator.update(locator_overrides)
    return {
        "locator": locator,
        "role": "slot",
        "slot": {
            "name": "historical_years",
            "type": "relative_window",
            "binds_to": None,
            "required": True,
        },
    }


def test_interval_argument_is_located_and_accepted():
    raw = blueprint_raw(parameterization=[_interval_param()], source_refs=("tc1",))
    summary = make_summary(tool_calls=(make_tool_call(
        ref="tc1", sql="SELECT today() - INTERVAL 5 YEAR AS cutoff"
    ),))
    out = _validate(raw, summary=summary)
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.parameterization[0].locator.to_doc() == _interval_param()["locator"]


def test_interval_window_predicate_needs_only_the_structural_locator():
    raw = blueprint_raw(parameterization=[_interval_param()], source_refs=("tc1",))
    summary = make_summary(tool_calls=(make_tool_call(
        ref="tc1",
        sql=("SELECT count(*) FROM dbpcm_warehouse.employee "
             "WHERE hire_date >= today() - INTERVAL 5 YEAR"),
    ),))
    out = _validate(raw, summary=summary)
    assert isinstance(out, ExtractedCandidate)


def test_interval_argument_stale_value_gets_exact_correction():
    raw = blueprint_raw(parameterization=[_interval_param("4")], source_refs=("tc1",))
    summary = make_summary(tool_calls=(make_tool_call(
        ref="tc1", sql="SELECT today() - INTERVAL 5 YEAR AS cutoff"
    ),))
    out = _validate(raw, summary=summary)
    assert isinstance(out, Decline)
    assert out.correctable is True
    assert ".locator.value='4'" in out.detail
    assert "occurrence 0 has value '5'" in out.detail


def test_interval_argument_unit_is_structural_and_exact():
    raw = blueprint_raw(parameterization=[_interval_param(unit="MONTH")], source_refs=("tc1",))
    summary = make_summary(tool_calls=(make_tool_call(
        ref="tc1", sql="SELECT today() - INTERVAL 5 YEAR AS cutoff"
    ),))
    out = _validate(raw, summary=summary)
    assert isinstance(out, Decline)
    assert out.reason == REASON_UNREWRITABLE
    assert "contains no matching structural site" in out.detail


# --- item 6: unrewritable SQL → fail-to-review (D52) ------------------------


def test_unparseable_accepted_sql_is_declined_unrewritable():
    summary = make_summary(tool_calls=(make_tool_call(sql="SELCT (( bad from"),))
    out = _validate(blueprint_raw(parameterization=[]), summary=summary)
    assert isinstance(out, Decline)
    assert out.reason == REASON_UNREWRITABLE


def test_an_answer_designation_ref_resolves_like_a_runquery_ref():
    """Release 1: the cited ref is the `answerWithTable` call and its query was never
    dispatched, so `tc.sql` is None everywhere. Resolution goes through
    `summary/refs.py::sql_by_ref`, so the plan is checked against the real SQL
    instead of declining as if the session carried none."""
    summary = make_summary(
        tool_calls=(make_tool_call(ref="ans", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(make_answer_sql(PAYROLL_SQL, ref="ans"),),
    )
    assert isinstance(
        _validate(blueprint_raw(source_refs=("ans",)), summary=summary), ExtractedCandidate
    )


def test_every_query_behind_a_multi_table_ref_is_checked_for_totality():
    """One ref, two designated queries: a literal predicate in the SECOND that the
    plan does not cover is still a filter this blueprint would silently drop.
    Checking only the first would let it through the D97 gate unexamined."""
    summary = make_summary(
        tool_calls=(make_tool_call(ref="ans", sql=None, tool_name="answerWithTable"),),
        answer_sqls=(
            make_answer_sql(PAYROLL_SQL, ref="ans"),
            make_answer_sql(
                "SELECT count(*) FROM payroll.payroll_fact WHERE country = 'IE'",
                ref="ans",
            ),
        ),
    )
    out = _validate(blueprint_raw(source_refs=("ans",)), summary=summary)
    assert isinstance(out, Decline)
    assert out.reason == REASON_TOTALITY
    assert "country" in out.detail


def test_no_accepted_sql_for_refs_is_declined_unrewritable():
    # source ref points at a tool call the summary does not contain.
    out = _validate(blueprint_raw(source_refs=("tc_missing",), parameterization=[]))
    assert isinstance(out, Decline)
    assert out.reason == REASON_UNREWRITABLE


# --- item 5: role consistency (one test each) -------------------------------


def _plan_with(replacement: dict, *, column: str) -> list[dict]:
    """Payroll plan with the entry for *column* replaced (coverage preserved)."""
    return [
        replacement if p["locator"]["column"] == column else p for p in payroll_parameterization()
    ]


def test_slot_needs_a_valid_type():
    bad = param_slot("department", slot_type="banana", value="0420")
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="department")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE


def test_slot_missing_required_field_defaults_to_required_true():
    # A real model sometimes omits `required`; it must default to True (a required
    # slot) rather than crash the payload build.
    bad = param_slot("department", value="0420")
    del bad["slot"]["required"]
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="department")))
    assert isinstance(out, ExtractedCandidate)
    dept = next(p for p in out.payload.parameterization if p.locator.column == "department")
    assert dept.slot.required is True


def test_enum_slot_without_enum_values_is_declined_role_inconsistent():
    # An enum slot with no enum_values is un-landable (runtime SlotSpec.parse rejects
    # it) — the extractor must decline it, not let it crash at landing.
    bad = param_slot("department", slot_type="enum", value="0420")
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="department")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "enum_values" in out.detail


def test_enum_slot_with_enum_values_is_accepted():
    good = param_slot("department", slot_type="enum", value="0420")
    good["slot"]["enum_values"] = ["0420", "0500"]
    out = _validate(blueprint_raw(parameterization=_plan_with(good, column="department")))
    assert isinstance(out, ExtractedCandidate)


def test_optional_slot_needs_optional_pattern_no_silent_drop():
    # region as optional but WITHOUT optional_pattern → would silently drop.
    bad = param_slot("region", required=False, optional_pattern=None, value="NA")
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="region")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "optional_pattern" in out.detail


@pytest.mark.parametrize(
    "bad_pattern",
    [
        "region = ((( bad sql",  # unparseable SQL
        "region = {region}",  # carries a {placeholder}
        "1 + 1",  # a non-boolean (arithmetic) fragment
    ],
)
def test_optional_slot_with_malformed_optional_pattern_is_declined(bad_pattern):
    # A PRESENT-but-malformed optional_pattern must fail-to-review at extraction
    # (Slice C), not slip through to blow up at landing/runtime.
    bad = param_slot("region", required=False, optional_pattern=bad_pattern, value="NA")
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="region")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "malformed optional_pattern" in out.detail


def test_required_slot_with_malformed_optional_pattern_is_also_declined():
    # A REQUIRED slot (or one whose `required` the model omitted → defaults True) that
    # STILL carries a malformed optional_pattern is caught at extraction too — matching
    # the corpus loader, which validates whenever a pattern is present regardless of
    # `required`. Otherwise it would pass extraction and only fail at load.
    bad = param_slot("region", required=True, optional_pattern="region = ((( bad sql", value="NA")
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="region")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE
    assert "malformed optional_pattern" in out.detail


def test_optional_slot_with_wellformed_true_pattern_is_accepted():
    # The well-formed 'TRUE' pattern (the corpus default) still passes untouched.
    good = param_slot("region", required=False, optional_pattern="TRUE", value="NA")
    out = _validate(blueprint_raw(parameterization=_plan_with(good, column="region")))
    assert isinstance(out, ExtractedCandidate)


def test_rule_needs_an_existing_rule_id_missing_rule():
    bad = param_rule("record_type", "rule_does_not_exist", value="EARNING")
    out = _validate(
        blueprint_raw(parameterization=_plan_with(bad, column="record_type")),
        known_rules=frozenset({"some_other_rule"}),
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_MISSING_RULE  # → §7 fail-to-review / pairing hook


def test_rule_without_rule_id_is_role_inconsistent():
    bad = param_rule("record_type", None, value="EARNING")
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="record_type")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE


def test_inline_needs_a_why():
    bad = param_inline("record_type", why="", value="EARNING")
    # why="" → _param_plans stores why=None → inline without 'why'.
    out = _validate(blueprint_raw(parameterization=_plan_with(bad, column="record_type")))
    assert isinstance(out, Decline)
    assert out.reason == REASON_BAD_ROLE


def test_rule_with_existing_rule_id_passes_and_carries_no_slot():
    good = param_rule("record_type", "earning_record", value="EARNING")
    out = _validate(
        blueprint_raw(parameterization=_plan_with(good, column="record_type")),
        known_rules=frozenset({"earning_record"}),
    )
    assert isinstance(out, ExtractedCandidate)
    rule_plan = next(p for p in out.payload.parameterization if p.role == "rule")
    assert rule_plan.rule_id == "earning_record"
    assert rule_plan.slot is None  # rule → no literal slot in the plan


# =====================================================================
# REGRESSION GUARDS (formerly strict-xfail FINDINGS, now FIXED): an un-planned
# JOIN-ON / value-side-fn literal predicate is now enumerated → declines totality.
# =====================================================================

_JOIN_SQL = (
    "SELECT sum(pay) AS total FROM db.fact f "
    "JOIN db.dim d ON f.dk = d.dk AND d.region = 'NA' "
    "WHERE f.dept = '0420'"
)
_FN_LITERAL_SQL = (
    "SELECT sum(pay) AS total FROM db.t WHERE dept = '0420' AND pay_period = toDate('2025-01-01')"
)


def test_unplanned_join_on_predicate_declines_totality():
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_JOIN_SQL),))
    plan = [param_slot("dept", value="0420", table="db.fact", binds_to="db.fact.dept")]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)),
        summary,
        known_rules=frozenset(),
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_TOTALITY


def test_unplanned_value_side_fn_literal_declines_totality():
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_FN_LITERAL_SQL),))
    plan = [param_slot("dept", value="0420", table="db.t", binds_to="db.t.dept")]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)),
        summary,
        known_rules=frozenset(),
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_TOTALITY


def test_join_on_predicate_planned_passes_totality():
    # The corrective plan — a ParamPlan for the JOIN-ON region → valid.
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_JOIN_SQL),))
    plan = [
        param_slot("dept", value="0420", table="db.fact", binds_to="db.fact.dept"),
        param_slot(
            "region",
            value="NA",
            required=False,
            optional_pattern="TRUE",
            table="db.dim",
            binds_to="db.dim.region",
        ),
    ]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)),
        summary,
        known_rules=frozenset(),
    )
    assert isinstance(out, ExtractedCandidate)


# --- LOW-1: per-locator (per-value) coverage --------------------------------

_OR_SQL = "SELECT count(*) AS total FROM db.t WHERE region = 'NA' OR region = 'EU'"


def test_or_two_values_with_only_one_plan_entry_declines():
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_OR_SQL),))
    # Only the NA predicate is planned → the EU predicate is un-covered.
    plan = [param_slot("region", value="NA", table="db.t", binds_to="db.t.region")]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)), summary, known_rules=frozenset()
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_TOTALITY


def test_or_two_values_with_a_plan_per_value_passes():
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_OR_SQL),))
    plan = [
        param_slot("region", name="region_na", value="NA", table="db.t", binds_to="db.t.region"),
        param_slot("region", name="region_eu", value="EU", table="db.t", binds_to="db.t.region"),
    ]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)), summary, known_rules=frozenset()
    )
    assert isinstance(out, ExtractedCandidate)


def test_sql_quoted_locator_values_normalize_to_the_bare_totality_value():
    """Live extractors copy SQL tokens; quoting alone must not invent a missing predicate."""
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_OR_SQL),))
    plan = [
        param_slot("region", name="region_na", value="'NA'", table="db.t", binds_to="db.t.region"),
        param_slot("region", name="region_eu", value="'EU'", table="db.t", binds_to="db.t.region"),
    ]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)),
        summary,
        known_rules=frozenset(),
    )
    assert isinstance(out, ExtractedCandidate)
    assert [p.locator.value for p in out.payload.parameterization] == ["NA", "EU"]


def test_sql_quoted_empty_string_normalizes_to_empty():
    sql = "SELECT count(*) FROM db.t WHERE region != ''"
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=sql),))
    plan = [param_inline("region", value="''", table="db.t")]
    out = to_candidate(
        blueprint_raw(parameterization=plan, source_refs=("tc1",)),
        summary,
        known_rules=frozenset(),
    )
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.parameterization[0].locator.value == ""


def test_result_columns_aliases_normalize_and_are_audited():
    raw = blueprint_raw(
        result_signature={
            "columns": [
                {"name": "region", "semantic_type": "dimension"},
                {"name": "employee_count", "semantic_type": "measure"},
            ],
            "grain": {"columns": ["region"], "verifiable": True},
            "invariants": [],
        }
    )

    out = _validate(raw)

    assert isinstance(out, ExtractedCandidate)
    signature = out.payload.result_signature
    assert signature is not None
    assert [(column.column, column.type) for column in signature.shape] == [
        ("region", "dimension"),
        ("employee_count", "measure"),
    ]
    assert signature.normalizations == (
        "columns_to_shape",
        "name_to_column",
        "semantic_type_to_type",
    )


def test_missing_result_shape_type_names_canonical_key_and_gives_example():
    raw = blueprint_raw(
        result_signature={
            "shape": [{"column": "projected_month"}],
            "grain": {"columns": ["projected_month"], "verifiable": True},
            "invariants": [],
        }
    )

    out = _validate(raw)

    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert out.correctable is True
    assert "result_signature.shape[0].type is required" in out.detail
    assert '"column": "projected_month", "type": "date"' in out.detail
    assert "shape[0].semantic_type" not in out.detail


def test_missing_result_shape_column_names_canonical_key_not_alias():
    raw = blueprint_raw(
        result_signature={
            "shape": [{"type": "date"}],
            "grain": {"columns": ["projected_month"], "verifiable": True},
            "invariants": [],
        }
    )

    out = _validate(raw)

    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert "result_signature.shape[0].column is required" in out.detail
    assert "shape[0].name" not in out.detail


def test_verifiable_grain_requires_a_nonempty_result_shape():
    raw = blueprint_raw(
        result_signature={
            "shape": [],
            "grain": {"columns": ["region"], "verifiable": True},
            "invariants": [],
        }
    )

    out = _validate(raw)

    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert "non-empty array" in out.detail


def test_bare_alias_happy_path_still_matches_no_false_decline():
    # The payroll example uses unqualified/aliased columns; a correct full plan
    # must NOT falsely decline on table qualification.
    out = _validate(blueprint_raw())
    assert isinstance(out, ExtractedCandidate)


# --- MEDIUM-2: known_rules wired from the real catalog ----------------------


def test_rule_id_from_real_catalog_is_accepted():
    from data_agent.learning.extractor.grounding import known_rule_ids_from_catalog
    from tests._catalog_fixture import fixture_catalog

    known = known_rule_ids_from_catalog(fixture_catalog())
    assert "active_employee" in known  # a real semantic-catalog rule id (D67)
    good = param_rule("record_type", "active_employee", value="EARNING")
    out = _validate(
        blueprint_raw(parameterization=_plan_with(good, column="record_type")),
        known_rules=known,
    )
    assert isinstance(out, ExtractedCandidate)


def test_unknown_rule_id_against_real_catalog_declines_missing_rule():
    from data_agent.learning.extractor.grounding import known_rule_ids_from_catalog
    from tests._catalog_fixture import fixture_catalog

    known = known_rule_ids_from_catalog(fixture_catalog())
    bad = param_rule("record_type", "not_a_real_catalog_rule_xyz", value="EARNING")
    assert "not_a_real_catalog_rule_xyz" not in known
    out = _validate(
        blueprint_raw(parameterization=_plan_with(bad, column="record_type")),
        known_rules=known,
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_MISSING_RULE


# --- LOW-2: accepted_signal must be in the D34 enum domain ------------------


def test_out_of_domain_accepted_signal_is_declined():
    raw = blueprint_raw()
    raw["payload"]["accepted_signal"] = "banana"
    out = _validate(raw)
    assert isinstance(out, Decline)
    assert out.reason == REASON_NO_ACCEPTANCE


@pytest.mark.parametrize("signal", ["no_correction", "thumbs_up", "explicit_confirm"])
def test_valid_accepted_signal_values_are_accepted(signal):
    raw = blueprint_raw(accepted_signal=signal)
    # The session must also have carried acceptance (summary default no_correction).
    out = _validate(raw, summary=make_summary(accepted_signal="no_correction"))
    assert isinstance(out, ExtractedCandidate)


# --- payload-shape robustness: a malformed model payload DECLINES, never crashes ---


def test_resolves_as_a_list_is_coerced_not_a_crash():
    # Real models sometimes emit `resolves` as a LIST instead of a {term: col} map.
    # It must be tolerated (advisory field) — a valid plan still extracts.
    raw = blueprint_raw()
    raw["payload"]["resolves"] = [{"term": "earnings", "column": "gross_pay"}]
    out = _validate(raw)
    assert isinstance(out, ExtractedCandidate)
    assert out.payload.resolves == {}  # non-dict coerced to an empty map


def test_non_object_payload_is_declined_malformed_not_a_crash():
    raw = blueprint_raw()
    raw["payload"] = ["not", "an", "object"]
    out = _validate(raw)
    assert isinstance(out, Decline)
    assert out.reason == "malformed_candidate"


# --- the NON-blueprint payloads: checked against what LANDING reads -----------
#
# The regression these pin: a `global_knowledge` candidate whose payload read
# {definition, fact_type, intent, scope} passed intake (which asked only that the
# payload BE an object), passed the leakage gate (which scans a fixed four-field
# list and so never read `definition` or `intent`), reached a reviewer, and died on
# APPROVE inside `knowledge_seed_from_candidate` — reported as a landing failure,
# i.e. as infra, so the approve 503'd and retried forever.


def _typed_raw(ctype: str, payload: dict) -> dict:
    """One non-blueprint candidate envelope carrying *payload* (evidence present, so
    the D31 gate is not what declines)."""
    return {
        "type": ctype,
        "confidence": 0.9,
        "evidence": [evidence_item()],
        "rationale": "worth learning",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "payload": payload,
    }


def test_global_knowledge_without_statement_is_declined_naming_statement():
    out = _validate(_typed_raw("global_knowledge", {"knowledge_type": "business_rule"}))
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert out.correctable
    assert "candidate.payload.statement" in out.detail


def test_the_stuck_candidates_exact_payload_is_declined():
    """The shape that actually reached the inbox and jammed it (2026-09). It must
    decline at INTAKE, where the model can still be re-asked — not on approve."""
    out = _validate(
        _typed_raw(
            "global_knowledge",
            {
                "definition": "an active employee is one with no termination date",
                "fact_type": "business_rule",
                "intent": "define active employee",
                "scope": "employee",
            },
        )
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert out.correctable
    # The message names the offending keys AND why they are refused, because it is
    # the corrective prompt.
    assert "'definition'" in out.detail and "'intent'" in out.detail
    assert "statement" in out.detail


def test_well_formed_global_knowledge_payload_passes():
    out = _validate(
        _typed_raw(
            "global_knowledge",
            {
                "statement": "an active employee is one with no termination date",
                "knowledge_type": "business_rule",
                "related_terms": ["active", "headcount"],
                "scope": "employee",
            },
        )
    )
    assert isinstance(out, ExtractedCandidate)
    # The payload is forwarded UNCHANGED — these readers check, they never rewrite.
    assert out.payload["statement"].startswith("an active employee")


def test_global_knowledge_unknown_key_is_declined_as_an_unscanned_surface():
    """An off-contract key ALONGSIDE a valid statement is still refused: the leakage
    gate scans four named fields, so anything else is a text surface nothing scans."""
    out = _validate(
        _typed_raw(
            "global_knowledge",
            {
                "statement": "an active employee is one with no termination date",
                "definition": "employees in dept 0420 are active",
            },
        )
    )
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert "'definition'" in out.detail
    assert "leakage gate" in out.detail
    # The refused key's VALUE is never echoed back (it is the thing nothing scanned).
    assert "0420" not in out.detail


def test_global_knowledge_related_terms_as_a_string_is_declined():
    # `knowledge_seed_from_candidate` walks this for string leaves; a bare string
    # would iterate as characters.
    out = _validate(
        _typed_raw("global_knowledge", {"statement": "a fact", "related_terms": "active"})
    )
    assert isinstance(out, Decline)
    assert "candidate.payload.related_terms" in out.detail


def test_global_knowledge_keys_match_the_leakage_gate_surfaces():
    """The closed key set is EXACTLY what the S5 gate scans — every key intake permits
    is a scanned surface, with no exception (`knowledge_type` was the one delta until
    the gate grew it). Spelled in two modules to avoid the import; pinned here so a
    change to either is a failing test rather than a silent unscanned surface."""
    from data_agent.learning.extractor.validation import _GLOBAL_KNOWLEDGE_KEYS
    from data_agent.learning.leakage.gate import _ENTITY_FREE_SURFACES

    assert set(_ENTITY_FREE_SURFACES["global_knowledge"]) == _GLOBAL_KNOWLEDGE_KEYS


def test_every_non_blueprint_type_has_a_payload_reader():
    """A fifth candidate type must not silently re-open the accept-any-object hole."""
    from data_agent.learning.extractor.validation import _PAYLOAD_READERS, CANDIDATE_TYPES

    assert set(_PAYLOAD_READERS) == set(CANDIDATE_TYPES) - {"blueprint"}


def test_user_knowledge_without_statement_is_declined():
    """`UserKnowledgeRecord.from_candidate` DEFAULTS `statement` to "" — so without
    this check a malformed payload commits a blank per-user fact, silently."""
    out = _validate(_typed_raw("user_knowledge", {"fact_type": "preference"}))
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert "candidate.payload.statement" in out.detail


def test_user_knowledge_with_an_extra_key_still_passes():
    """No closed key set on the entity-BEARING, per-user target — and `user_id` in
    particular is accepted here and then discarded by the commit (R6/D17)."""
    out = _validate(
        _typed_raw(
            "user_knowledge",
            {"statement": "I mean the NA region", "scope": "user", "user_id": "user-1"},
        )
    )
    assert isinstance(out, ExtractedCandidate)


def _schema_edit_payload(**overrides) -> dict:
    payload = {
        "edit_kind": "add_rule",
        "target": {"database": "payroll"},
        "patch": "rules:\n  - id: active_employee",
        "statement": "define active_employee",
    }
    payload.update(overrides)
    return payload


def test_schema_edit_without_a_patch_is_declined():
    """An absent patch opens an EMPTY pull request — `SchemaEditPatch.from_payload`
    defaults `proposed_yaml` to "" and nothing downstream objects."""
    payload = _schema_edit_payload()
    del payload["patch"]
    out = _validate(_typed_raw("schema_edit", payload))
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    # BOTH accepted names are offered: declining on `patch` alone would ask a model
    # that sent `proposed_yaml` to add a field it had not omitted.
    assert "candidate.payload.patch" in out.detail
    assert "candidate.payload.proposed_yaml" in out.detail


def test_schema_edit_without_a_target_catalog_is_declined():
    payload = _schema_edit_payload()
    del payload["target"]
    out = _validate(_typed_raw("schema_edit", payload))
    assert isinstance(out, Decline)
    assert "candidate.payload.target_catalog" in out.detail
    assert "candidate.payload.target.database" in out.detail


def test_valid_schema_edit_passes():
    out = _validate(_typed_raw("schema_edit", _schema_edit_payload()))
    assert isinstance(out, ExtractedCandidate)


def test_schema_edit_passes_under_the_alias_field_names():
    """The Locked names and the fixture names are both live (`from_payload` reads
    `edit_kind or edit_type`, `proposed_yaml or patch`, `target_catalog or
    target.database`), so intake must accept the same disjunction."""
    out = _validate(
        _typed_raw(
            "schema_edit",
            {
                "edit_type": "add_rule",
                "target_catalog": "payroll",
                "proposed_yaml": "rules:\n  - id: active_employee",
                "statement": "define active_employee",
            },
        )
    )
    assert isinstance(out, ExtractedCandidate)


def test_schema_edit_wrong_typed_proposed_yaml_beside_a_valid_patch_is_declined():
    """Intake checks the aliases in the WRITER'S precedence (`proposed_yaml or patch`).
    A wrong-typed `proposed_yaml` next to a valid `patch` must decline: the writer
    would SELECT the wrong-typed value, so letting the pair pass on the strength of
    `patch` waves through exactly the payload `from_payload` mis-reads."""
    out = _validate(_typed_raw("schema_edit", _schema_edit_payload(proposed_yaml=123)))
    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED
    assert "candidate.payload.proposed_yaml" in out.detail
