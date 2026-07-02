"""Layer-1: the §2.6 declarative `when`-clause evaluator + entity-agnostic gate.

Truth table over typed outputs; an entity-VALUED predicate is REJECTED (D59
leakage gate); no `eval()`, no arbitrary code.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.when import (
    WhenClauseError,
    evaluate_when,
    validate_when,
)

# -- evaluation truth table -------------------------------------------------


def test_empty_and_not_empty() -> None:
    assert evaluate_when("empty($1)", {"$1": {"row_count": 0}}) is True
    assert evaluate_when("not empty($1)", {"$1": {"row_count": 3}}) is True
    assert evaluate_when("empty($1)", {"$1": []}) is True
    assert evaluate_when("empty($1)", {"$1": [1, 2]}) is False


def test_count_comparison() -> None:
    assert evaluate_when("count($1) > 2", {"$1": {"row_count": 3}}) is True
    assert evaluate_when("count($1) > 2", {"$1": {"rows": [[1]]}}) is False


def test_scalar_field_comparison() -> None:
    assert evaluate_when("$3.company_avg > 0", {"$3.company_avg": 5}) is True
    assert evaluate_when("$3.company_avg > 0", {"$3.company_avg": 0}) is False


def test_and_or_not_composition() -> None:
    outs = {"$1.dept_actuals": [[1]], "$3.company_avg": 5}
    assert evaluate_when("not empty($1.dept_actuals) and $3.company_avg > 0", outs) is True
    assert evaluate_when("empty($1.dept_actuals) or $3.company_avg > 0", outs) is True
    assert evaluate_when("empty($1.dept_actuals) and $3.company_avg > 0", outs) is False


def test_numeric_membership() -> None:
    assert evaluate_when("$1.level in (1, 2, 3)", {"$1.level": 2}) is True
    assert evaluate_when("$1.level in (1, 2, 3)", {"$1.level": 9}) is False


def test_bare_ref_truth_value() -> None:
    assert evaluate_when("not empty($1)", {"$1": {"row_count": 1}}) is True


# -- entity-agnostic gate (D59) ---------------------------------------------


def test_string_literal_comparison_is_rejected() -> None:
    with pytest.raises(WhenClauseError):
        validate_when("$1.department = 'Warehouse'")


def test_string_membership_set_is_rejected() -> None:
    with pytest.raises(WhenClauseError):
        validate_when("$1.status in ('A', 'D')")


def test_evaluate_also_rejects_entity_valued() -> None:
    with pytest.raises(WhenClauseError):
        evaluate_when("$1.department = 'Warehouse'", {"$1.department": "Warehouse"})


# -- grammar guards ---------------------------------------------------------


def test_disallowed_function_rejected() -> None:
    with pytest.raises(WhenClauseError):
        validate_when("sum($1) > 0")


def test_attribute_or_arbitrary_name_rejected() -> None:
    with pytest.raises(WhenClauseError):
        validate_when("os.system > 0")


def test_valid_thresholds_pass_validation() -> None:
    validate_when("count($1) > 50")
    validate_when("not empty($1) and $3.company_avg > 0")
    validate_when("$1.n >= 1")


def test_unknown_output_reference_raises_on_eval() -> None:
    with pytest.raises(WhenClauseError):
        evaluate_when("count($9) > 0", {"$1": []})
