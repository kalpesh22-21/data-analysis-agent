"""`when`-bearing composite ⇒ fail_to_review (§1 / §11.6 / D102): a composite node
carrying a `when` precondition cannot promote 1:1 onto `Blueprint` (the S3 plan's
`when` is a bare string; `NodeTemplate` has no `when` field), so S4 rejects it to
review rather than emit a half-typed template. All Wave-0 fixtures carry when:null."""

from __future__ import annotations

from data_agent.learning.generalize.builder import generalize_blueprint

from .helpers import CATALOG, composite_sql_by_ref, load_plan


def test_when_bearing_composite_fails_to_review():
    plan = load_plan()["composite"]
    plan["composes"][0]["when"] = "dept_total > 0"  # a bare-string precondition
    gen = generalize_blueprint(plan, composite_sql_by_ref(), CATALOG)

    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "when_bearing_composite"
    # No half-typed template is emitted.
    assert gen.sql_template is None
    assert gen.node_templates == ()
    assert gen.canonical_ast_norm == ""


def test_when_null_composite_is_ok():
    """The frozen fixture (when:null throughout) is accepted — the guard fires ONLY
    on a present `when`, not on the plan carrying the field at all."""
    plan = load_plan()["composite"]
    assert all(n["when"] is None for n in plan["composes"])
    gen = generalize_blueprint(plan, composite_sql_by_ref(), CATALOG)
    assert gen.static_validation.outcome == "ok"
