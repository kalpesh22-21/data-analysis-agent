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


def interval_arguments(ast: exp.Expression, unit: str) -> list[exp.Expression]:
    """Every INTERVAL magnitude for *unit*, in deterministic AST order.

    Unsupported expressions stay in the list so occurrence coordinates cannot slide
    to a later literal merely because an earlier site was not rewritable.
    """
    arguments: list[exp.Expression] = []
    for interval in ast.find_all(exp.Interval):
        interval_unit = interval.args.get("unit")
        if not isinstance(interval_unit, exp.Var) or interval_unit.name.upper() != unit.upper():
            continue
        arguments.append(interval.this)
    return arguments


def interval_argument(
    ast: exp.Expression, locator: dict[str, Any], *, require_value_match: bool = True
) -> exp.Literal | None:
    """Resolve an INTERVAL magnitude while keeping its unit fixed in SQL."""
    unit = locator.get("unit")
    occurrence = locator.get("occurrence", 0)
    if (
        not isinstance(unit, str)
        or locator.get("context") != "interval"
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 0
    ):
        return None
    arguments = interval_arguments(ast, unit)
    if occurrence >= len(arguments):
        return None
    argument = arguments[occurrence]
    if not isinstance(argument, exp.Literal) or not str(argument.this).isdigit():
        return None
    if require_value_match and str(argument.this) != str(locator.get("value")):
        return None
    return argument


def in_list_predicates(ast: exp.Expression, column: str) -> list[exp.In]:
    """Every literal-only IN predicate for *column*, in deterministic AST order."""
    matches: list[exp.In] = []
    for predicate in ast.find_all(exp.In):
        columns = list(predicate.this.find_all(exp.Column))
        if len(columns) != 1 or columns[0].name.lower() != column.lower():
            continue
        matches.append(predicate)
    return matches


def in_list(
    ast: exp.Expression, locator: dict[str, Any], *, require_value_match: bool = True
) -> exp.In | None:
    """Resolve one literal-only IN list as a single typed-list bind site."""
    column = locator.get("column")
    occurrence = locator.get("occurrence", 0)
    if (
        not isinstance(column, str)
        or not column
        or locator.get("context") != "in_predicate"
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 0
    ):
        return None
    predicates = in_list_predicates(ast, column)
    if occurrence >= len(predicates):
        return None
    predicate = predicates[occurrence]
    members = predicate.expressions
    if not members or not all(isinstance(member, exp.Literal) for member in members):
        return None
    value = ",".join(str(member.this) for member in members)
    if require_value_match and value != str(locator.get("value")):
        return None
    return predicate


def limit_arguments(ast: exp.Expression) -> list[exp.Expression]:
    """Every LIMIT expression in deterministic AST order, including unsupported ones."""
    return [limit.expression for limit in ast.find_all(exp.Limit)]


def limit_argument(
    ast: exp.Expression, locator: dict[str, Any], *, require_value_match: bool = True
) -> exp.Literal | None:
    """Resolve a positive integer LIMIT while excluding OFFSET and expression limits."""
    occurrence = locator.get("occurrence", 0)
    if (
        locator.get("context") != "limit"
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 0
    ):
        return None
    arguments = limit_arguments(ast)
    if occurrence >= len(arguments):
        return None
    argument = arguments[occurrence]
    if not isinstance(argument, exp.Literal) or not str(argument.this).isdigit():
        return None
    if require_value_match and str(argument.this) != str(locator.get("value")):
        return None
    return argument
