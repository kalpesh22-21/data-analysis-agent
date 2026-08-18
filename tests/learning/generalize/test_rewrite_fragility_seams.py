"""THE SEAMS the rewrite's failures escaped through — ISSUES H3/H5/H6, end to end.

The rewrite bugs were bad; what made them expensive is that they did not stay inside the
rewrite. `generalize_blueprint` catches `RewriteError` and nothing else, and the two
things it does AFTER the rewrite (`canonical_ast_norm`, which re-parses the template)
re-raise a `sqlglot.ParseError` from a module whose stated contract is that it never
raises for a bad candidate. Measured on this base, driving the live deductions-ratio
candidate through the REAL `GeneralizeStage.process`:

    sqlglot.errors.ParseError: Required keyword: 'this' missing for
      <class 'sqlglot.expressions.query.Where'>. Line 1, Col: 143.
      ... sumIf(p.amount) AS ratio FROM dbpcm_warehouse.payroll AS p WHERE  GROUP BY ...

That exception has two destinations, and neither is "this candidate is reviewed":

  * the CONSUMER never ACKs the message (`consumer.py`, the broad `except Exception`
    leaves it for reclaim), so the whole SESSION redelivers until it dead-letters —
    every OTHER candidate in it lost with it;
  * the reviewer COMPLETION path (`inbox/service.py`) maps `InboxTransitionError`,
    `CompletionRaceError` and `CompletionInputError`, so a `ParseError` is a 500 —
    permanently, on a form whose only fault is the shape of its SQL.

THE ROOT CAUSE IS GONE: `role=rule` no longer deletes anything
(`test_rewrite_rule_role_keeps.py` has the argument and the shape matrix). Every shape
below therefore GENERALIZES now — the assertions in this file changed from "reaches
review" to "produces the accepted SQL as its template", which is the outcome the
candidates always deserved.

The SEAMS are what this file still exists for, and they are kept deliberately. Removing
the bug does not remove the fact that an exception thrown after the rewrite dead-letters a
session and 500s a reviewer's form — it only removes today's way of throwing one. So the
builder's post-rewrite hash input is still fail-soft, the stage's "S4 never raises"
docstring is still enforced rather than asserted, and both are tested here against
injected faults rather than against the bug that happened to reveal them.
"""

from __future__ import annotations

import pytest

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.candidate.models import build_envelope
from data_agent.learning.extractor.grounding import (
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.extractor.models import ExtractedCandidate
from data_agent.learning.extractor.validation import to_candidate
from data_agent.learning.generalize import builder as builder_module
from data_agent.learning.generalize import stage as stage_module
from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.stage import GeneralizeStage
from data_agent.learning.generalize.validate import REASON_UNREWRITABLE
from data_agent.learning.stage import StageContext
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    make_summary,
    make_tool_call,
)
from .helpers import CATALOG, load_plan, single_sql_by_ref

# The live query, verbatim from `extractor/test_totality_hint.py` — a per-employee
# deductions-to-earnings ratio with THREE literal predicates: the `IN` list in the WHERE
# and one inside each `sumIf`.
_RATIO_SQL = (
    "SELECT p.employee_code, "
    "sumIf(p.amount, p.register_type = 'DDUCT') / sumIf(p.amount, p.register_type = 'EARN') "
    "AS ratio "
    "FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.register_type IN ('DDUCT','EARN') "
    "GROUP BY p.employee_code"
)
_PAYROLL = "dbpcm_warehouse.payroll"

_FIXTURE_CATALOG = fixture_catalog()
_SCHEMA = build_sqlglot_schema_from_catalog(_FIXTURE_CATALOG)
_KNOWN = known_rule_ids_from_catalog(_FIXTURE_CATALOG)
_INDEX = rule_index_from_catalog(_FIXTURE_CATALOG)


def _entry(column: str, value: str, **rest) -> dict:
    return {"locator": {"table": _PAYROLL, "column": column, "value": value}, **rest}


# The plan the totality hint teaches a model to write, and which S3 ACCEPTS: every
# predicate covered, the `IN` list inline, each `sumIf` condition attributed to the
# catalog rule that IS that filter.
_COVERED = blueprint_raw(
    intent="ratio of deductions to earnings per employee",
    parameterization=[
        _entry("register_type", "DDUCT,EARN", role="inline",
               why="the ratio is defined over these two register types"),
        _entry("register_type", "DDUCT", role="rule", rule_id="employee_deductions"),
        _entry("register_type", "EARN", role="rule", rule_id="gross_earnings"),
    ],
    source_refs=("tc1",),
)


async def _through_the_stage(raw: dict, sql: str = _RATIO_SQL) -> tuple[dict, str]:
    """The REAL S3 validation + the REAL S4 stage. Returns `(generalization, control)`."""
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=sql),))
    candidate = to_candidate(raw, summary, known_rules=_KNOWN, rule_index=_INDEX)
    assert isinstance(candidate, ExtractedCandidate), candidate
    env = build_envelope(candidate, summary, candidate_id="c1", evidence_refs=("e1",))
    result = await GeneralizeStage(catalog_schema=_SCHEMA).process(
        env, StageContext(summary=summary, verdict=KEEP_VERDICT)
    )
    return result.envelope.payload["generalization"], result.control


# --- THE regression ----------------------------------------------------------------


async def test_the_deductions_ratio_candidate_now_generalizes_cleanly() -> None:
    """H5/H6's own reproduction case, driven the way the consumer drives it — and it is
    an `ok` now, not a refusal.

    It was always an ORDINARY candidate: S3 accepts it (asserted below — the plan is not
    malformed). On the base it raised `sqlglot.ParseError` out of
    `GeneralizeStage.process`, because deleting its two `sumIf` conditions and its WHERE
    left `sumIf(p.amount) ... WHERE  GROUP BY`. The first fix made that a `fail_to_review`.
    Keeping the predicates makes it a BLUEPRINT: both metrics intact and distinct, the
    `IN` filter still there, and the two catalog rules recorded beside them.

    That is the capability half of this slice. The deleting rewrite could not learn a
    conditional-aggregate ratio at all — the single most ordinary shape in payroll
    analytics."""
    generalization, control = await _through_the_stage(_COVERED)

    assert control == "continue"
    assert generalization["static_validation"]["outcome"] == "ok"
    assert generalization["static_validation"]["reason"] is None
    template = generalization["sql_template"]
    # Both metrics survive, and they are DIFFERENT — the H6 failure was that the
    # deleting rewrite made them byte-identical one-argument calls.
    assert "sumIf(p.amount, p.register_type = \'DDUCT\')" in template
    assert "sumIf(p.amount, p.register_type = \'EARN\')" in template
    assert "WHERE" in template  # the IN filter is not silently dropped
    # And the rules are recorded next to the predicates they describe.
    assert set(generalization["uses_rules"]) == {"employee_deductions", "gross_earnings"}
    # A real template has a real hash input (contrast the fail-soft test below).
    assert generalization["canonical_ast_norm"] != ""


async def test_the_same_candidate_still_passes_s3() -> None:
    """The half that made this a REWRITE bug rather than a plan bug: nothing upstream
    refuses this candidate, so whatever S4 did to it, it did to a plan every other layer
    had approved."""
    summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=_RATIO_SQL),))
    assert isinstance(
        to_candidate(_COVERED, summary, known_rules=_KNOWN, rule_index=_INDEX),
        ExtractedCandidate,
    )


# --- the builder's post-rewrite seam -----------------------------------------------


def test_a_canonical_norm_failure_degrades_to_the_documented_fail_soft(monkeypatch) -> None:
    """`canonical_ast_norm` is a HASH INPUT, computed after the verdict is made — so a
    template it cannot normalize must cost the candidate its dedup key, not its life.
    The empty string is the documented S6 fail-soft (`_fail_to_review` stamps exactly
    that, and `test_unrewritable` pins it), so the degrade is to a value the plane
    already understands."""

    def _boom(*args, **kwargs):
        raise ValueError("normalization exploded")

    monkeypatch.setattr(builder_module, "canonical_ast_norm", _boom)
    gen = generalize_blueprint(load_plan()["single"], single_sql_by_ref(), CATALOG)

    assert gen.canonical_ast_norm == ""
    # And ONLY the hash input degrades — the verdict this candidate earned is intact.
    assert gen.static_validation.outcome == "ok"
    assert gen.sql_template is not None


# --- the stage's own belt ----------------------------------------------------------


async def test_an_unanticipated_builder_fault_is_stamped_in_band_not_raised(
    monkeypatch,
) -> None:
    """The stage docstring says "S4 never raises". It is now ENFORCED there rather than
    inherited from the builder's care, because the cost of it being false is not
    proportional to the fault: one candidate's odd SQL dead-lettered a whole session's
    extraction and 500ed a reviewer's form.

    Deliberately NOT done by widening `builder`'s `except RewriteError` — the builder's
    derivation table depends on programming errors staying visible there."""

    def _boom(*args, **kwargs):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(stage_module, "generalize_blueprint", _boom)
    generalization, control = await _through_the_stage(_COVERED)

    assert control == "continue"
    assert generalization["static_validation"]["outcome"] == "fail_to_review"
    assert generalization["static_validation"]["reason"] == REASON_UNREWRITABLE
    assert generalization["sql_template"] is None
    # The candidate this is driven with generalizes cleanly on its own (see the top of
    # this file) — so the verdict here is the BELT's, not the candidate's.


# --- H3: a hallucinated inline literal ---------------------------------------------


def _inline_plan(value: str) -> dict:
    return {
        "kind": "single",
        "intent": "earnings",
        "source_tool_call_refs": ["tc1"],
        "parameterization": [
            _entry("register_type", value, role="inline", why="defines the metric")
        ],
        "result_signature": {"grain": {"columns": [], "verifiable": False}},
    }


_IN_LIST_SQL = (
    "SELECT sum(p.amount) AS t FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.register_type IN ('DDUCT','EARN')"
)


@pytest.mark.parametrize(
    ("value", "outcome", "why"),
    [
        pytest.param(
            "DDUCT,EARN", "ok",
            "the IN-list value as the S3 totality enumerator writes it — members joined "
            "by a comma (`sql_predicates._in_predicate`). Splitting it and requiring "
            "each member is what keeps this legitimate entry from regressing",
            id="in-list-all-members-present",
        ),
        pytest.param(
            "DDUCT", "ok", "one member of the list, named on its own", id="one-member",
        ),
        pytest.param(
            "DDUCT,BONUS", "fail_to_review",
            "one member is not in the SQL — the plan claims a filter half of which does "
            "not exist",
            id="one-member-absent",
        ),
        pytest.param(
            "TAXES", "fail_to_review",
            "H3: the literal appears NOWHERE. S3 totality counts any entry as coverage "
            "and the rewrite leaves inline literals alone, so this used to ship "
            "`outcome: ok` with a plan that disagrees with its own template",
            id="hallucinated",
        ),
    ],
)
def test_a_strict_inline_literal_must_be_in_the_accepted_sql(
    value: str, outcome: str, why: str
) -> None:
    gen = generalize_blueprint(_inline_plan(value), {"tc1": _IN_LIST_SQL}, _SCHEMA)
    assert gen.static_validation.outcome == outcome, why


def test_an_inline_range_or_boolean_literal_is_still_accepted() -> None:
    """The false-refusal edge of the H3 fix, pinned in both directions.

    `BETWEEN a AND b` and `= TRUE` are literal predicates the S3 totality gate
    enumerates and therefore predicates a model MUST cover — but neither is a shape
    `_find_literal` can see (`Between` is not in `_COMPARISONS`; a boolean constant is an
    `exp.Boolean`, not an `exp.Literal`). Locatability is therefore decided against the
    totality enumerator as well, or this fix would newly fail every candidate whose
    inline entry covers a range or a boolean flag."""
    sql = (
        "SELECT sum(p.amount) AS t FROM dbpcm_warehouse.payroll AS p "
        "WHERE p.pay_date BETWEEN '2025-01-01' AND '2025-06-30' AND p.is_active = TRUE"
    )
    plan = {
        "kind": "single",
        "intent": "earnings in the first half",
        "source_tool_call_refs": ["tc1"],
        "parameterization": [
            _entry("pay_date", "2025-01-01,2025-06-30", role="inline", why="the H1 window"),
            _entry("is_active", "TRUE", role="inline", why="active rows only"),
        ],
        "result_signature": {"grain": {"columns": [], "verifiable": False}},
    }
    gen = generalize_blueprint(plan, {"tc1": sql}, _SCHEMA)

    assert gen.static_validation.reason != REASON_UNREWRITABLE
    assert gen.sql_template is not None
    assert "BETWEEN" in gen.sql_template and "TRUE" in gen.sql_template


def test_lenient_mode_is_unchanged_by_the_inline_check() -> None:
    """A composite node's SQL references only a SUBSET of the top-level params — an
    inline entry absent from THIS node is the normal case there, not a fault. The
    multi-table variant of H3 is closed at the stage's collapse instead
    (`_collapse_designations`), which is the layer that can see the OTHER query."""
    from data_agent.learning.generalize.rewrite import rewrite_sql_to_template

    template = rewrite_sql_to_template(
        "SELECT sum(p.amount) AS t FROM dbpcm_warehouse.payroll AS p",
        [_entry("register_type", "TAXES", role="inline", why="not in this node")],
        strict=False,
    )
    assert template == "SELECT sum(p.amount) AS t FROM dbpcm_warehouse.payroll AS p"


# --- the review blockers, end to end -----------------------------------------------
#
# Both pass S3 and both landed `outcome: ok` with a template that means something other
# than the accepted SQL. Neither is exotic: an OR of two department filters and an IN
# list of two register types are ordinary analyst SQL, and the rule roles are what the
# decline hints ask for.


_OR_ARM_SQL = (
    "SELECT sum(p.amount) AS total FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.department_code = '0420' "
    "OR (p.department_code = '0910' AND p.register_type = 'EARN')"
)

_OR_ARM_PLAN = blueprint_raw(
    intent="total pay for department 0420, plus 0910 earnings",
    parameterization=[
        {"locator": {"table": _PAYROLL, "column": "department_code", "value": "0420"},
         "role": "inline", "why": "the report pairs these two departments"},
        {"locator": {"table": _PAYROLL, "column": "department_code", "value": "0910"},
         "role": "inline", "why": "the report pairs these two departments"},
        _entry("register_type", "EARN", role="rule", rule_id="gross_earnings"),
    ],
    source_refs=("tc1",),
)


async def test_a_rule_conjunct_inside_an_or_arm_keeps_the_whole_condition() -> None:
    """REVIEW BLOCKER 1, end to end — now a blueprint rather than a refusal.

    The `role=rule` predicate's parent is an ordinary `And`, so the parent-only allowlist
    dropped it and shipped

        WHERE p.department_code = '0420' OR (p.department_code = '0910')

    — every 0910 row, not just its earnings, stamped `outcome: ok`. The ancestry check
    that review asked for turned that into a refusal; keeping turns it into a correct
    template. The candidate was never the problem."""
    generalization, control = await _through_the_stage(_OR_ARM_PLAN, sql=_OR_ARM_SQL)

    assert control == "continue"
    assert generalization["static_validation"]["outcome"] == "ok"
    template = generalization["sql_template"]
    assert "p.register_type = \'EARN\'" in template  # the conjunct that used to vanish
    assert "OR" in template


_IN_LIST_RULE_SQL = (
    "SELECT p.employee_code, sum(p.amount) AS total "
    "FROM dbpcm_warehouse.payroll AS p "
    "WHERE p.register_type IN ('DDUCT','EARN') "
    "GROUP BY p.employee_code"
)

# The entry pair that gets this past every S3 gate, which is what makes it reachable:
# the INLINE entry covers the `IN` predicate (totality is satisfied — its value is the
# comma-joined form the enumerator writes), and the RULE entry names ONE member, so it
# covers no predicate by value-equality and the `rule_predicate_mismatch` landing gate
# never gets a predicate to contradict it. A plan can therefore cite a rule for a list
# member and be accepted by everything upstream of the AST.
_IN_LIST_RULE_PLAN = blueprint_raw(
    intent="total of deductions and earnings per employee",
    parameterization=[
        _entry("register_type", "DDUCT,EARN", role="inline",
               why="the total is defined over both register types"),
        _entry("register_type", "EARN", role="rule", rule_id="gross_earnings"),
    ],
    source_refs=("tc1",),
)


async def test_a_rule_locator_naming_one_member_of_an_in_list_keeps_the_list() -> None:
    """REVIEW BLOCKER 2, end to end — the sharpest lesson of the slice.

    `_find_literal` matches individual literals, so the rule entry for `'EARN'` resolves
    to the whole `register_type IN ('DDUCT','EARN')` node — and dropping it deletes
    `'DDUCT'` too, which no rule covers and nothing re-applies. The template became

        SELECT p.employee_code, sum(p.amount) AS total FROM ... GROUP BY p.employee_code

    — every register type, summed under the same alias, stamped `outcome: ok`. Before
    this slice the emptied `WHERE` refused to parse and the candidate died loudly, so
    fixing H5 is precisely what made it quiet.

    NOTE what the INLINE entry proves. It declares `'DDUCT,EARN'` and the drop deleted
    the literal it names — and the H3 pre-pass cannot see that, because it reads the
    PRISTINE AST before any mutation. That is why `_recheck_inline_literals` survives
    into a rewrite that deletes nothing: the plan being honest about the SQL and the
    rewrite being honest about the plan are two different claims, and only the second
    one catches an edit."""
    generalization, control = await _through_the_stage(
        _IN_LIST_RULE_PLAN, sql=_IN_LIST_RULE_SQL
    )

    assert control == "continue"
    assert generalization["static_validation"]["outcome"] == "ok"
    template = generalization["sql_template"]
    # BOTH members survive — the drop took the uncovered one with it.
    assert "\'DDUCT\'" in template and "\'EARN\'" in template


async def test_both_shapes_pass_s3_which_is_why_s4_had_to_get_them_right() -> None:
    """Why these were S4's to get wrong. `_validate_totality` counts an entry per literal
    predicate and asks nothing about where the predicate SITS, so neither plan is
    malformed by any upstream rule — S4 is the only layer that sees the AST, and it was
    editing it."""
    for plan, sql in ((_OR_ARM_PLAN, _OR_ARM_SQL), (_IN_LIST_RULE_PLAN, _IN_LIST_RULE_SQL)):
        summary = make_summary(tool_calls=(make_tool_call(ref="tc1", sql=sql),))
        assert isinstance(
            to_candidate(plan, summary, known_rules=_KNOWN, rule_index=_INDEX),
            ExtractedCandidate,
        )
