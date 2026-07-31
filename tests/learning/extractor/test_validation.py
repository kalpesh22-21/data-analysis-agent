"""Deterministic post-emit validation (`to_candidate`) — evidence-mandatory
(D31), lift-not-generate (D34), totality/roles (D97), unrewritable (D52).
Matrix rows 1/8/9/6/5/11 (task items 1,2,3,4,5,6).
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.models import BlueprintPayload, Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import (
    REASON_BAD_ROLE,
    REASON_MISSING_RULE,
    REASON_NO_ACCEPTANCE,
    REASON_NO_EVIDENCE,
    REASON_TOTALITY,
    REASON_UNREWRITABLE,
    to_candidate,
)

from .helpers import (
    blueprint_raw,
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


# --- item 6: unrewritable SQL → fail-to-review (D52) ------------------------


def test_unparseable_accepted_sql_is_declined_unrewritable():
    summary = make_summary(tool_calls=(make_tool_call(sql="SELCT (( bad from"),))
    out = _validate(blueprint_raw(parameterization=[]), summary=summary)
    assert isinstance(out, Decline)
    assert out.reason == REASON_UNREWRITABLE


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
