"""S4-uses-scope-key-subset (D69/D87): `uses` is byte-exact `database.table.column`
scope keys, and every slot `binds_to` ⊆ `uses` (the corpus_loader assertion)."""

from __future__ import annotations

from data_agent.learning.generalize.builder import generalize_blueprint

from .helpers import CATALOG, load_plan, single_sql_by_ref


def test_uses_are_byte_exact_scope_keys():
    plan = load_plan()["single"]
    gen = generalize_blueprint(plan, single_sql_by_ref(), CATALOG)
    assert gen.uses == (
        "payroll.payroll_fact.department",
        "payroll.payroll_fact.gross_pay",
        "payroll.payroll_fact.pay_period",
        "payroll.payroll_fact.record_type",
        "payroll.payroll_fact.region",
    )
    # Sorted, de-duplicated, joined from the D69 (database.table, column) pairs by '.'.
    assert list(gen.uses) == sorted(set(gen.uses))


def test_every_slot_binds_to_is_subset_of_uses():
    plan = load_plan()["single"]
    gen = generalize_blueprint(plan, single_sql_by_ref(), CATALOG)
    binds = [
        p["slot"]["binds_to"]
        for p in plan["parameterization"]
        if p["role"] == "slot"
    ]
    assert all(b in set(gen.uses) for b in binds)
    assert gen.static_validation.binds_to_subset_uses is True


def test_binds_to_not_subset_fails_to_review():
    """A slot binding to a column the template never touches ⇒ binds_to_subset_uses
    is False ⇒ fail_to_review with the stable `binds_to_not_subset` tag."""
    plan = load_plan()["single"]
    # Point a slot at a real catalog column that the accepted SQL does NOT reference.
    plan["parameterization"][0]["slot"]["binds_to"] = "payroll.payroll_fact.unrelated"
    gen = generalize_blueprint(plan, single_sql_by_ref(), CATALOG)
    assert gen.static_validation.binds_to_subset_uses is False
    assert gen.static_validation.outcome == "fail_to_review"
    assert gen.static_validation.reason == "binds_to_not_subset"
