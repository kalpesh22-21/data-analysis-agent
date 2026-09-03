"""Shared deterministic resolution for non-predicate SQL locator sites.

S3 validates a model-authored locator and S4 rewrites the accepted SQL. Both stages must
identify the same AST node: duplicating the traversal would let preflight approve a site
that generalization later rejects. This module owns that shared, deliberately narrow walk.
"""

from __future__ import annotations

from typing import Any

import sqlglot.expressions as exp


def function_name(node: exp.Expression) -> str:
    """Return a stable authored/SQL name for recognized and anonymous functions."""
    this = node.args.get("this")
    if isinstance(this, str) and this:
        return this
    sql_name = getattr(node, "sql_name", None)
    if callable(sql_name):
        try:
            return str(sql_name())
        except Exception:  # pragma: no cover - defensive third-party AST behavior
            pass
    return type(node).__name__.upper()


def table_source_function_calls(ast: exp.Expression, function: str) -> list[exp.Func]:
    """Matching function calls used directly as FROM/JOIN table sources, in AST order."""
    calls: list[exp.Func] = []
    for func in ast.find_all(exp.Func):
        if function_name(func).lower() != function.lower():
            continue
        table = func.parent
        if isinstance(table, exp.Table) and isinstance(table.parent, (exp.From, exp.Join)):
            calls.append(func)
    return calls


def function_argument(
    ast: exp.Expression, locator: dict[str, Any], *, require_value_match: bool = True
) -> exp.Literal | None:
    """Resolve the supported table-source function argument described by *locator*."""
    function = locator.get("function")
    argument_index = locator.get("argument_index")
    occurrence = locator.get("occurrence", 0)
    if (
        not isinstance(function, str)
        or locator.get("context") != "table_source"
        or not isinstance(argument_index, int)
        or isinstance(argument_index, bool)
        or argument_index < 0
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 0
    ):
        return None
    calls = table_source_function_calls(ast, function)
    if occurrence >= len(calls):
        return None
    args = calls[occurrence].args.get("expressions") or []
    if argument_index >= len(args):
        return None
    argument = args[argument_index]
    if not isinstance(argument, exp.Literal) or not argument.is_int:
        return None
    if require_value_match and str(argument.this) != str(locator.get("value")):
        return None
    return argument
