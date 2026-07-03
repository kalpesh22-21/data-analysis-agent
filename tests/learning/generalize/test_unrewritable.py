"""S4-unrewritable-fails-to-review (D52/D97): un-rewritable / unparseable accepted
SQL ⇒ `static_validation.outcome == "fail_to_review"` — IN-BAND, never raised, never
a guessed template, never auto-promotable."""

from __future__ import annotations

from data_agent.learning.generalize.builder import generalize_blueprint

from .helpers import CATALOG, load_plan, single_sql_by_ref


def test_missing_accepted_sql_fails_to_review():
    plan = load_plan()["single"]
    # No SQL resolves for the source ref → nothing to rewrite.
    gen = generalize_blueprint(plan, {"tc1": None}, CATALOG)
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "unrewritable_sql"
    assert gen.sql_template is None
    assert gen.canonical_ast_norm == ""  # no hash input → S6 fail-soft skips the hard key


def test_unparseable_accepted_sql_fails_to_review():
    plan = load_plan()["single"]
    gen = generalize_blueprint(plan, {"tc1": "SELECT WHERE FROM ((("}, CATALOG)
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "unrewritable_sql"
    assert gen.sql_template is None


def test_declared_slot_literal_absent_fails_to_review():
    """A role=slot param whose literal is not in the accepted SQL cannot be rewritten
    deterministically (D35) — reject to review rather than guess."""
    plan = load_plan()["single"]
    # The accepted SQL has region='NA'; declare a slot expecting a different value.
    plan["parameterization"][3]["locator"]["value"] = "ZZZ"
    gen = generalize_blueprint(plan, single_sql_by_ref(), CATALOG)
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "unrewritable_sql"
    assert gen.sql_template is None


def test_uncatalogued_column_explain_fails_to_review():
    """A template referencing a table absent from the catalog can't dry-run explain
    (provenance fails closed, D63) ⇒ explain_ok False ⇒ fail_to_review."""
    plan = load_plan()["single"]
    gen = generalize_blueprint(plan, single_sql_by_ref(), catalog_schema={})
    assert gen.static_validation.explain_ok is False
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "explain_failed"
    # The rewrite still produced a template — the FAILURE is the schema dry-run, not
    # the rewrite; the un-hashable template is left for the reviewer, not promoted.
    assert gen.uses == ()


def test_fail_to_review_never_raises():
    """Every degenerate input returns a valid generalization (in-band), never an
    exception — the S7 router depends on the value, not a raise."""
    plan = load_plan()["single"]
    for sql_by_ref in ({}, {"tc1": ""}, {"tc1": "DROP TABLE payroll.payroll_fact"}):
        gen = generalize_blueprint(plan, sql_by_ref, CATALOG)
        assert gen.static_validation.outcome == "fail_to_review"
