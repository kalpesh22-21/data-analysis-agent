"""blueprint/when.py — the declarative `when`-clause evaluator + validator.

A `when.expr` is a SMALL declarative predicate over typed node outputs: `count()`,
`empty()`, scalar comparisons, `and`/`or`/`not`, set membership. NO raw SQL and NO `eval()`
— the expression is parsed with Python's `ast` in `mode="eval"` (which NEVER executes) into
a tree walked against an explicit whitelist, and anything outside the grammar raises
`WhenClauseError`.

Node-output references use the `$N` / `$N.field` syntax. Since `$` is not a valid Python
identifier character, each reference is rewritten to a sentinel identifier BEFORE
`ast.parse`, and the same rewrite is applied to the `outputs` keys so lookups line up.

Entity-agnostic gate (D59): a comparison against a STRING literal
(`department = 'Warehouse'`) is a slot or filter, not a predicate, and `validate_when`
REJECTS it. Only thresholds and shape (`count > 50`, `empty($1)`) are allowed. Applied at
LOAD time, so an entity-valued `when` never ships.
"""

from __future__ import annotations

import ast
import re
from typing import Any

# `$1`, `$1.dept_actuals`  → a walkable sentinel identifier. Longest-match: the
# optional `.field` is captured so `$1.company_avg` and `$1` both normalize.
_REF = re.compile(r"\$(\d+)(?:\.([A-Za-z_][A-Za-z0-9_]*))?")
_REF_PREFIX = "__bpref_"

# The declarative call vocabulary (04-blueprints §Control-flow).
_ALLOWED_CALLS: frozenset[str] = frozenset({"empty", "count"})


class WhenClauseError(Exception):
    """A `when.expr` is outside the declarative grammar or is entity-valued."""


def _normalize_ref(match: re.Match[str]) -> str:
    node, field = match.group(1), match.group(2)
    return f"{_REF_PREFIX}{node}_{field}" if field else f"{_REF_PREFIX}{node}"


def _canonical_ref(key: str) -> str:
    """Normalize an `outputs` key (`$1.dept_actuals` or already-sentinel) to the
    sentinel identifier used inside the parsed expression."""
    if key.startswith(_REF_PREFIX):
        return key
    return _REF.sub(_normalize_ref, key)


def _parse(expr: str) -> ast.expr:
    rewritten = _REF.sub(_normalize_ref, expr)
    try:
        tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as exc:
        raise WhenClauseError(f"when.expr is not a valid expression: {exc}") from exc
    return tree.body


def _is_ref_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id.startswith(_REF_PREFIX)


def _check(node: ast.expr) -> None:
    """Recursively assert *node* is inside the declarative grammar (validate).

    Raises `WhenClauseError` on anything else — a call to a non-whitelisted
    function, a comparison against a string literal (entity-valued, D59), an
    attribute/subscript, arbitrary names, etc.
    """
    if isinstance(node, ast.BoolOp):  # and / or
        for value in node.values:
            _check(value)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check(node.operand)
        return
    if isinstance(node, ast.Compare):
        _check_compare(node)
        return
    if isinstance(node, ast.Call):
        _check_call(node)
        return
    if _is_ref_name(node):  # a bare `$N` used as a boolean (`not empty($1)` etc.)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return
    raise WhenClauseError(
        f"when.expr contains an unsupported construct: {ast.dump(node)}"
    )


def _check_compare(node: ast.Compare) -> None:
    if len(node.ops) != 1 or len(node.comparators) != 1:
        raise WhenClauseError("when.expr comparisons must be a single binary comparison")
    op = node.ops[0]
    if not isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
        raise WhenClauseError("when.expr uses an unsupported comparison operator")
    left, right = node.left, node.comparators[0]
    if isinstance(op, (ast.In, ast.NotIn)):
        # set membership: `$1.status in ('A','B')` — a tuple/list of constants is
        # a SHAPE membership test, allowed; the left side must be a ref/call.
        _check_operand(left, allow_string=False)
        _check_membership_set(right)
        return
    # Entity-agnostic gate (D59): neither side may be a string literal.
    _check_operand(left, allow_string=False)
    _check_operand(right, allow_string=False)


def _check_operand(node: ast.expr, *, allow_string: bool) -> None:
    if _is_ref_name(node):
        return
    if isinstance(node, ast.Call):
        _check_call(node)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
            return  # a numeric threshold
        if isinstance(node.value, str):
            raise WhenClauseError(
                "when.expr compares against a string literal — that is a slot/filter, "
                "not an entity-agnostic predicate (D59 leakage gate)"
            )
    raise WhenClauseError(f"when.expr has an unsupported operand: {ast.dump(node)}")


def _check_membership_set(node: ast.expr) -> None:
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        raise WhenClauseError("when.expr membership requires a literal set/tuple/list")
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, (int, float))):
            raise WhenClauseError(
                "when.expr membership sets must contain only numeric constants "
                "(a string set is an entity filter — D59 leakage gate)"
            )


def _check_call(node: ast.Call) -> None:
    if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
        raise WhenClauseError("when.expr may only call empty(…) or count(…)")
    if len(node.args) != 1 or node.keywords:
        raise WhenClauseError(f"{node.func.id}(…) takes exactly one output reference")
    if not _is_ref_name(node.args[0]):
        raise WhenClauseError(f"{node.func.id}(…) argument must be a node output reference")


def validate_when(expr: str) -> None:
    """Assert *expr* is a valid, entity-AGNOSTIC declarative predicate (load-time).

    Raises `WhenClauseError` on any grammar violation or an entity-valued
    comparison (D59). Pure; no evaluation.
    """
    _check(_parse(expr))


# --------------------------------------------------------------------------
# Evaluation (executor path, Slice B wires it; pure + Layer-1-testable now)
# --------------------------------------------------------------------------


def _is_empty(value: Any) -> bool:
    """`empty(x)` truth: None, empty collection/string, or a row-shaped result
    with zero rows (a `{rows: [...]}` / `{row_count: 0}` dict, or a bare list)."""
    if value is None:
        return True
    if isinstance(value, dict):
        if "row_count" in value:
            return not value.get("row_count")
        if "rows" in value:
            return not value.get("rows")
        return not value
    if isinstance(value, (list, tuple, str, set, frozenset)):
        return len(value) == 0
    return False


def _count(value: Any) -> float:
    if isinstance(value, dict):
        if "row_count" in value:
            return float(value.get("row_count") or 0)
        if "rows" in value:
            rows = value.get("rows") or []
            return float(len(rows))
    if isinstance(value, (list, tuple, set, frozenset, str)):
        return float(len(value))
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raise WhenClauseError(f"count(…) cannot be applied to a {type(value).__name__} output")


def evaluate_when(expr: str, outputs: dict[str, Any]) -> bool:
    """Evaluate *expr* over typed node *outputs* → bool (§2.6). Pure; safe.

    *outputs* keys may be the raw `$N.field` / `$N` references or already the
    normalized sentinel form. `evaluate_when` re-validates the grammar first, so
    an entity-valued or malformed expr raises `WhenClauseError` rather than
    silently mis-evaluating.
    """
    node = _parse(expr)
    _check(node)  # never evaluate an out-of-grammar / entity-valued expr
    env = {_canonical_ref(key): val for key, val in outputs.items()}
    return bool(_eval(node, env))


def _eval(node: ast.expr, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval(v, env) for v in node.values)
        return any(_eval(v, env) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, env)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, env)
    if isinstance(node, ast.Call):
        return _eval_call(node, env)
    if _is_ref_name(node):
        return _lookup(node, env)  # a bare ref used as a truth value
    if isinstance(node, ast.Constant):
        return node.value
    raise WhenClauseError(f"when.expr contains an unsupported construct: {ast.dump(node)}")


def _lookup(node: ast.Name, env: dict[str, Any]) -> Any:
    if node.id not in env:
        raise WhenClauseError(f"when.expr references an unknown node output: {node.id}")
    return env[node.id]


def _eval_operand(node: ast.expr, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return _lookup(node, env)
    if isinstance(node, ast.Call):
        return _eval_call(node, env)
    if isinstance(node, ast.Constant):
        return node.value
    raise WhenClauseError(f"when.expr has an unsupported operand: {ast.dump(node)}")


def _eval_compare(node: ast.Compare, env: dict[str, Any]) -> bool:
    left = _eval_operand(node.left, env)
    op = node.ops[0]
    if isinstance(op, (ast.In, ast.NotIn)):
        members = {el.value for el in node.comparators[0].elts}  # type: ignore[attr-defined]
        present = left in members
        return present if isinstance(op, ast.In) else not present
    right = _eval_operand(node.comparators[0], env)
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Eq):
        return left == right
    return left != right  # ast.NotEq


def _eval_call(node: ast.Call, env: dict[str, Any]) -> Any:
    name = node.func.id  # type: ignore[attr-defined]
    arg = _lookup(node.args[0], env)  # type: ignore[arg-type]
    if name == "empty":
        return _is_empty(arg)
    return _count(arg)


__all__ = ["WhenClauseError", "evaluate_when", "validate_when"]
