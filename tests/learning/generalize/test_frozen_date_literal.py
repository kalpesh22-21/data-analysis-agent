"""The fifth static check: a frozen absolute date OUTSIDE a comparison (S4, D97).

THE INCIDENT (live inbox): the extractor pasted the session's run date into the template
body — `DATE_DIFF(DAY, seniority_date, toDateTime64('2026-08-28 00:00:00', 6))` — and S4
stamped the candidate `ok`. A blueprint like that answers a DIFFERENT question every day it
ages, silently, which is exactly the wrong-answer class the stage exists to catch. The same
statement's `seniority_date > toDateTime64('1900-01-01 00:00:00', 6)` sentinel is legitimate
and must keep passing.

THE DISCRIMINATOR IS WHETHER S3 ACTUALLY ADJUDICATED THE LITERAL, not a list of blessed
functions — and the question is put to S3 itself
(`extractor/sql_predicates.py::literal_predicate_of`, the enumerator's own decision) rather
than re-derived here. Two conditions: an ancestor the ENUMERATOR RECOGNIZES as a literal
predicate, AND a column-free side under it. Neither alone is enough, and each has its own
test: `coalesce(a, b) < '<date>'` has a comparison ancestor but enumerates nothing
(`test_frozen_date_check_scope_qa.py`), while `WHERE 30 = DATE_DIFF(DAY, hire,
toDateTime64('2026-08-28 00:00:00', 6))` enumerates `hire = 30` and the run date rides along
inside the column side (`test_a_frozen_date_on_a_column_bearing_side_fails`). These tests pin
that boundary from both sides — the sentinel, the `countIf` threshold buried in an aggregate
and the `BETWEEN` range pass BECAUSE S3 enumerated them, not because of their spelling.
"""

from __future__ import annotations

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.extractor.sql_predicates import literal_predicates
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.validate import (
    REASON_DATE_LITERAL,
    REASON_READ_ONLY,
    check_no_frozen_date_literal,
    decide_outcome,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate

from ..extractor.helpers import KEEP_VERDICT, make_summary, make_tool_call
from .helpers import CATALOG, composite_sql_by_ref, load_plan

# The incident's table, as the D69 provenance extractor needs to see it.
_EMPLOYEE_CATALOG: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.employee": {
        "employee_code": "String",
        "employee_name": "String",
        "seniority_date": "Nullable(DateTime64(6))",
        "employee_status": "String",
    }
}

# The template S4 stamped `ok`, verbatim from the inbox.
_INCIDENT_SQL = (
    "SELECT employee_code, employee_name, seniority_date, "
    "DATE_DIFF(DAY, seniority_date, toDateTime64('2026-08-28 00:00:00', 6)) AS tenure_days "
    "FROM dbpcm_warehouse.employee "
    "WHERE employee_status <> 'Not Hired' "
    "AND NOT (seniority_date IS NULL) "
    "AND seniority_date > toDateTime64('1900-01-01 00:00:00', 6) "
    "ORDER BY tenure_days DESC LIMIT 1"
)

# The same question asked WITHOUT freezing today: the sentinel floor stays.
_SENTINEL_ONLY_SQL = _INCIDENT_SQL.replace(
    "toDateTime64('2026-08-28 00:00:00', 6)) AS tenure_days",
    "now()) AS tenure_days",
)


def _single_payload() -> dict:
    return {
        "kind": "single",
        "intent": "the longest-tenured active employee",
        "source_tool_call_refs": ["tc1"],
        "parameterization": [],
        "result_signature": {"grain": {"columns": [], "verifiable": True}},
    }


# --- the check itself ---------------------------------------------------------


def test_the_incident_template_fails_the_check():
    assert check_no_frozen_date_literal(_INCIDENT_SQL) is False


def test_the_sentinel_floor_alone_passes():
    """The SAME statement minus the pasted run date. `'1900-01-01 00:00:00'` sits under a
    `GT`, so S3 enumerated it as a predicate literal and a role adjudicated it."""
    assert check_no_frozen_date_literal(_SENTINEL_ONLY_SQL) is True


def test_a_threshold_inside_an_aggregate_passes():
    """`countIf(hire_date >= '2020-01-01')`: the comparison ancestor is INSIDE a function
    argument, which is why the walk goes up the whole chain rather than looking at the
    literal's immediate parent."""
    assert check_no_frozen_date_literal(
        "SELECT countIf(hire_date >= '2020-01-01') AS hired FROM dbpcm_warehouse.employee"
    ) is True


def test_a_between_range_passes():
    """`BETWEEN` is not in the rewrite's `_COMPARISONS` (it has no slot form) but the S3
    enumerator reads it as a range predicate — so it is adjudicated, and exempt."""
    assert check_no_frozen_date_literal(
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        "WHERE seniority_date BETWEEN '2020-01-01' AND '2020-12-31'"
    ) is True


def test_a_frozen_date_on_a_column_bearing_side_fails():
    """Condition (b). S3 DOES enumerate this comparison — as `seniority_date = 30`. The
    adjudicated constant is the `30` on the other side; the run date rides along inside the
    column side, seen by nobody. "There is a predicate here" is not "this literal is it"."""
    sql = (
        "SELECT employee_code FROM dbpcm_warehouse.employee "
        "WHERE 30 = DATE_DIFF(DAY, seniority_date, toDateTime64('2026-08-28 00:00:00', 6))"
    )
    assert check_no_frozen_date_literal(sql) is False
    # The half that makes the point: a predicate WAS enumerated, about a different literal.
    assert [(p.column, p.value) for p in literal_predicates(sql) or []] == [
        ("seniority_date", "30")
    ]


def test_a_date_in_its_own_nested_comparison_passes():
    """The neighbouring shape, and the reason (b) tests the literal's SIDE rather than the
    predicate's value: here the date has its OWN comparison (`a > '2026-08-28'`) nested inside
    the outer one, and the S3 enumerator — which walks EVERY node, not just top-level
    predicates — reports it. A literal S3 enumerated is S3's to classify; refusing it here
    would be S4 overruling the enumerator it defers to."""
    sql = (
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        "WHERE if(hire_date > '2026-08-28', 1, 2) = 1"
    )
    # (The outer `= 1` enumerates too, as `hire_date = 1` — the enumerator over-enumerates
    # on purpose, §2.3. What matters is that the DATE is among what it reported.)
    assert ("hire_date", ">", "2026-08-28") in [
        (p.column, p.operator, p.value) for p in literal_predicates(sql) or []
    ]
    assert check_no_frozen_date_literal(sql) is True


def test_a_column_free_side_with_date_arithmetic_passes():
    """The column-free test is on the SIDE, not on the literal's immediate wrapper: a
    computed-but-constant bound is still the constant side S3 reads the literal out of."""
    assert check_no_frozen_date_literal(
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        "WHERE seniority_date >= date_sub(toDate('2024-01-01'), INTERVAL 1 DAY)"
    ) is True


def test_an_in_list_of_dates_passes():
    assert check_no_frozen_date_literal(
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        "WHERE toDate(seniority_date) IN ('2024-01-01', '2024-02-01')"
    ) is True


def test_a_slot_template_still_fails_on_the_frozen_date():
    """A `{slot}` is not SQL: the check re-parses in colon-form exactly as
    `rewrite._check_rewritten` does, so a template that has already been parameterized is
    still readable and the date is still caught."""
    assert check_no_frozen_date_literal(
        "SELECT DATE_DIFF(DAY, seniority_date, toDateTime64('2026-08-28 00:00:00', 6)) AS d "
        "FROM dbpcm_warehouse.employee WHERE employee_status = {status}"
    ) is False


def test_a_date_in_the_projection_fails():
    """No WHERE clause anywhere: a stamped report date ages exactly like the run date."""
    assert check_no_frozen_date_literal(
        "SELECT '2026-01-01' AS report_date, count() AS c FROM dbpcm_warehouse.employee"
    ) is False


def test_a_bare_year_and_a_plain_string_pass():
    """The shape regex is a FULL ISO date on purpose. `2024` is a number (and lives under a
    comparison anyway); matching bare numbers would flag every threshold in the corpus."""
    assert check_no_frozen_date_literal(
        "SELECT count() AS c FROM dbpcm_warehouse.employee "
        "WHERE toYear(seniority_date) = 2024 AND employee_status <> 'Not Hired'"
    ) is True


def test_an_unparseable_template_passes_here():
    """Parse failure is `_check_rewritten`'s report to make (`unrewritable_sql`). Failing it
    here too would move the reason tag S7 routes on for a fault this check did not find."""
    assert check_no_frozen_date_literal("SELECT FROM WHERE ((( ") is True


# --- the outcome mapping ------------------------------------------------------


def test_decide_outcome_maps_the_check_to_the_frozen_reason():
    outcome, reason = decide_outcome(
        explain_ok=True,
        binds_to_subset_uses=True,
        dag_ok=True,
        read_only_select=True,
        date_literal_ok=False,
    )
    assert (outcome, reason) == ("fail_to_review", REASON_DATE_LITERAL)


def test_the_new_check_is_ranked_last():
    """Contract-A order for the original four is pinned — an older failing check still wins
    the `reason`, so triage on the existing tags does not shift under this addition."""
    _, reason = decide_outcome(
        explain_ok=True,
        binds_to_subset_uses=True,
        dag_ok=True,
        read_only_select=False,
        date_literal_ok=False,
    )
    assert reason == REASON_READ_ONLY


# --- end to end through the builder -------------------------------------------


def test_the_incident_candidate_is_routed_to_review():
    gen = generalize_blueprint(_single_payload(), {"tc1": _INCIDENT_SQL}, _EMPLOYEE_CATALOG)
    sv = gen.static_validation
    assert sv.date_literal_ok is False
    assert sv.outcome == "fail_to_review"
    assert sv.reason == REASON_DATE_LITERAL
    # The OTHER four checks pass — this candidate was `ok` on every axis S4 had before.
    assert (sv.explain_ok, sv.binds_to_subset_uses, sv.dag_ok, sv.read_only_select) == (
        True, True, True, True,
    )
    assert gen.to_doc()["static_validation"]["date_literal_ok"] is False


def test_the_sentinel_only_candidate_still_stamps_ok():
    gen = generalize_blueprint(
        _single_payload(), {"tc1": _SENTINEL_ONLY_SQL}, _EMPLOYEE_CATALOG
    )
    assert gen.static_validation.date_literal_ok is True
    assert gen.static_validation.outcome == "ok"
    assert gen.static_validation.reason is None


def test_a_composite_node_carrying_the_frozen_date_fails():
    """A composite's top-level `sql_template` is None, so the node templates are the only SQL
    the candidate carries — ONE frozen date in ONE node ages the whole blueprint."""
    plan = load_plan()["composite"]
    sql_by_ref = composite_sql_by_ref()
    sql_by_ref["tc2"] = sql_by_ref["tc2"].replace(
        "SELECT sum(gross_pay) AS company_total",
        "SELECT sum(gross_pay) AS company_total, toDate('2026-08-28') AS asof",
    )
    gen = generalize_blueprint(plan, sql_by_ref, CATALOG)
    assert gen.sql_template is None
    assert gen.static_validation.date_literal_ok is False
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == REASON_DATE_LITERAL


def test_the_clean_composite_is_unaffected():
    gen = generalize_blueprint(load_plan()["composite"], composite_sql_by_ref(), CATALOG)
    assert gen.static_validation.date_literal_ok is True
    assert gen.static_validation.outcome == "ok"


# --- end to end: the stage, and what the writer does with the stamp -----------
#
# The tests above prove the CHECK and the BUILDER. Neither shows that the stamp
# survives the stage seam (`payload["generalization"]`, filled from the session tool
# trail) or that S7 acts on it — and a check nobody routes on is a field, not a gate.
# These two run the same envelope twice, changing ONE literal, and assert the pipeline
# forks: review vs auto-land.


def _incident_env(payload: dict) -> CandidateEnvelope:
    """A blueprint envelope that is CLEAN on every axis the writer looks at except the
    static stamp: a settled `pass` leakage verdict, no dedup verdict, and the routing call
    below passes `sampled_for_inbox=False`. So `in_review` here can only have come from
    S4 — nothing else in the envelope can produce it."""
    return CandidateEnvelope(
        candidate_id="candidate::frozen::0",
        type="blueprint",
        status="extracted",
        payload=payload,
        source_session="sess-1",
        source_trace="trace-1",
        evidence_refs=(),
        extractor_rationale="the longest-tenured active employee",
        entity_scan=LeakageVerdict(result="pass", scanner="regex+ner+llm").to_doc(),
        confidence=0.9,
        proposed_action="new",
        depends_on=(),
        content_hash="h",
    )


async def _run_stage(sql: str) -> CandidateEnvelope:
    ctx = StageContext(
        summary=make_summary(tool_calls=(make_tool_call(ref="tc1", sql=sql),)),
        verdict=KEEP_VERDICT,
    )
    result = await GeneralizeStage(catalog_schema=_EMPLOYEE_CATALOG).process(
        _incident_env(_single_payload()), ctx
    )
    # The stamp is a VERDICT, not a stop: S4 always continues so the later stages still
    # run and the writer is the one place that decides (D102 §7.1).
    assert result.control == "continue"
    return result.envelope


async def test_the_incident_is_stamped_through_the_stage_and_routed_to_the_inbox():
    env = await _run_stage(_INCIDENT_SQL)

    sv = env.payload["generalization"]["static_validation"]
    assert sv["date_literal_ok"] is False
    assert sv["outcome"] == "fail_to_review"
    assert sv["reason"] == REASON_DATE_LITERAL
    # S3's own fields are untouched — the stamp is additive.
    assert env.payload["intent"] == "the longest-tenured active employee"

    decision = route_candidate(env, sampled_for_inbox=False)
    assert decision.status == CandidateStatus.IN_REVIEW
    assert decision.control == "route_inbox"
    assert decision.reason == "fail_to_review"
    # The writer's decision and the inbox label are read from the same rule.
    assert derive_inbox_reason(env) == "fail_to_review"


async def test_the_same_candidate_without_the_pasted_run_date_auto_lands():
    """THE COUNTERFACTUAL, and the reason the test above means anything: one literal
    apart (`now()` for `toDateTime64('2026-08-28 …')`), the identical envelope takes the
    auto-land branch. So the fifth check is what diverted the incident, not the leakage
    verdict, the dedup allowlist or the audit sample."""
    env = await _run_stage(_SENTINEL_ONLY_SQL)

    assert env.payload["generalization"]["static_validation"]["outcome"] == "ok"
    decision = route_candidate(env, sampled_for_inbox=False)
    assert decision.status == CandidateStatus.CANDIDATE
    assert decision.control == "continue"
    assert decision.reason is None
