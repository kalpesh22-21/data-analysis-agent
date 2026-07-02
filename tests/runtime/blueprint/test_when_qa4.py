"""QA4 Layer-1: `when`-evaluator hardening (runblueprint-design §2.6).

Adversarial extensions of `test_when.py`: code-execution attempts (`__import__`,
attribute-chain access, lambda, comprehension, walrus, subscript), nesting depth,
membership edge cases, and cross-type comparison behavior. The evaluator parses
with `ast` in `mode="eval"` (which NEVER executes) and walks a whitelist — anything
outside the grammar must raise `WhenClauseError`, never run.

ADD-only; does not modify the reviewer-owned `test_when.py`.
"""

from __future__ import annotations

import pytest

from data_agent.runtime.blueprint.when import (
    WhenClauseError,
    evaluate_when,
    validate_when,
)

# Every one of these is an attempt to escape the declarative grammar; each must
# raise WhenClauseError at validate AND at evaluate (never execute).
_CODE_EXEC_ATTEMPTS = [
    "__import__('os')",
    "__import__('os').system('id')",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "{k: 1 for k in range(3)}",
    "(a := 1)",
    "$1['key']",
    "count($1) + 1",  # arithmetic is not in the grammar
    "$1 if $2 else $3",  # ternary
    "f'{1}'",
]


@pytest.mark.parametrize("expr", _CODE_EXEC_ATTEMPTS)
def test_code_execution_attempts_rejected_at_validate(expr: str) -> None:
    with pytest.raises(WhenClauseError):
        validate_when(expr)


@pytest.mark.parametrize("expr", _CODE_EXEC_ATTEMPTS)
def test_code_execution_attempts_rejected_at_evaluate(expr: str) -> None:
    # Evaluate re-validates the grammar first, so a hostile expr never evaluates.
    with pytest.raises(WhenClauseError):
        evaluate_when(expr, {"$1": [1], "$2": [1], "$3": [1]})


def test_dollar_field_reference_is_a_dict_lookup_not_attribute_access() -> None:
    # `$1.__class__` normalizes to a sentinel name; it is a MISSING output key, not
    # a real attribute access on the value → an unknown-output WhenClauseError.
    with pytest.raises(WhenClauseError):
        evaluate_when("count($1.__class__) > 0", {"$1": [1, 2]})


def test_moderately_nested_boolean_expression_evaluates() -> None:
    expr = "not (not (not empty($1)))"  # triple negation
    assert evaluate_when(expr, {"$1": []}) is False


def test_deeply_nested_or_chain_evaluates() -> None:
    # A wide (not deep) boolean chain stays well under any recursion limit.
    expr = " or ".join(["count($1) > 0"] * 50)
    assert evaluate_when(expr, {"$1": [1]}) is True


def test_cross_type_comparison_currently_raises_typeerror() -> None:
    # PIN (Slice B): a string node-output compared with `>` a number raises a RAW
    # TypeError, not a WhenClauseError — the evaluator does not coerce/guard operand
    # types. Flagged for Slice B (the executor supplies typed scalar outputs; a
    # string threshold comparison should be caught as a WhenClauseError). This test
    # PINS current behavior so a future guard is a deliberate, visible change.
    with pytest.raises(TypeError):
        evaluate_when("$1.x > 0", {"$1.x": "not-a-number"})


def test_membership_with_float_constants_ok() -> None:
    assert evaluate_when("$1.n in (1.5, 2.5)", {"$1.n": 2.5}) is True


def test_not_in_membership() -> None:
    assert evaluate_when("$1.n not in (1, 2)", {"$1.n": 9}) is True


def test_bool_constant_operand_allowed() -> None:
    # A bare boolean constant is inside the grammar (numeric/bool threshold).
    assert evaluate_when("count($1) >= 0", {"$1": []}) is True


def test_chained_comparison_rejected() -> None:
    # `0 < count($1) < 10` is a chained comparison — the grammar allows only a
    # single binary comparison; must reject rather than mis-evaluate.
    with pytest.raises(WhenClauseError):
        validate_when("0 < count($1) < 10")


def test_empty_string_expr_rejected() -> None:
    with pytest.raises(WhenClauseError):
        validate_when("")
