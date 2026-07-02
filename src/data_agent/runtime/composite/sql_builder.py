"""sql_builder.py — validated target resolution + injection-safe SQL (design §2). Pure.

`table`/`column`/`period.column` are model-supplied and are ALLOWLISTED against
the runtime catalog `{database.table: {column: type}}` before any SQL is built
(design §2.1) — an unknown/ambiguous target fails closed with
`TargetValidationError` and NEVER reaches the MCP. `concept` is never a SQL
input at all (D10) — it lives entirely in the ranking path.

The SQL is assembled with **sqlglot AST nodes** (identifiers + literal nodes),
never Python string concatenation of model input (design §2.2). Even though the
identifiers are already catalog-allowlisted, this stacks a second independent
guarantee, and `period` start/end become properly-escaped sqlglot string
literals (design §2.4) rather than interpolated text.

Description-column discovery (design §2.3): a convention-based, catalog-validated
candidate list with a value-only fallback when no sibling description column
exists (OQ-2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlglot import exp

from data_agent.runtime.context.scope_filter import is_provenance_in_scope
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

# Ordered description-column candidate transforms (design §2.3). `<Base>` is the
# code column with a trailing "Code" stripped (EarnCode -> Earn -> EarnDescription).
_DESCRIPTION_SUFFIXES = ("Description", "Name")


class TargetValidationError(Exception):
    """A model-supplied table/column/period target failed catalog validation.

    `message` is model/user-facing and names ONLY the specific target the model
    already supplied — never an enumeration of the catalog (no scope leak,
    design §2.1).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Period:
    """A concrete, structured period predicate (design §2.4).

    `column` is validated against the catalog exactly like the code column;
    `start`/`end` are optional inclusive bounds rendered as sqlglot string
    literals. Deictic/relative period resolution ("latest", "Q2") is out of
    scope (OQ-3) — this is an already-concrete window only.
    """

    column: str
    start: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class ResolvedTarget:
    """A fully catalog-validated resolution target (all identifiers allowlisted)."""

    db_table: str  # "database.table" (a real catalog key)
    column: str
    description_col: str | None
    period_col: str | None


def _candidate_description_columns(column: str) -> list[str]:
    base = column[: -len("Code")] if column.endswith("Code") else column
    candidates: list[str] = []
    for suffix in _DESCRIPTION_SUFFIXES:
        candidates.append(f"{base}{suffix}")
        if base != column:
            candidates.append(f"{column}{suffix}")
    # De-dup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _resolve_db_table(catalog: CatalogHandle, table: str) -> str:
    """Resolve *table* (fully-qualified or bare) to exactly one catalog key."""
    schema: Mapping[str, Mapping[str, str]] = catalog.schema
    if table in schema:
        return table
    # Bare name -> match any "<database>.<table>" key whose table part equals it.
    matches = [key for key in schema if key.split(".", 1)[-1] == table]
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        raise TargetValidationError(f"No table '{table}' is available.")
    raise TargetValidationError(
        f"Table '{table}' is ambiguous — qualify it as 'database.table'."
    )


def resolve_target(
    catalog: CatalogHandle,
    *,
    table: str,
    column: str,
    period: Period | None,
    column_scope: frozenset[str] = frozenset(),
) -> ResolvedTarget:
    """Validate + resolve the model's target against the catalog allowlist (design §2.1).

    *column_scope* is used ONLY as an availability pre-check for the discovered
    description column (M1): if the sibling description column is out of the
    caller's scope, it is skipped and the query falls back to value-only —
    otherwise every call would draw a non-retryable `COLUMN_SCOPE_VIOLATION`
    from the MCP and the tool would be permanently unusable for that column.
    This is a pre-check, NOT enforcement — the MCP still enforces D57 on the
    inner query. The value column and period column are deliberately NOT
    scope-pre-checked: if they are out of scope the MCP denies (design §7),
    which is the correct fail-closed behaviour for a directly-requested target.
    Empty scope == allow-all (matches `context/scope_filter` D80b semantics).

    Raises:
        TargetValidationError: unknown/ambiguous table, unknown column, or an
            unknown period column — all fail-closed, before any SQL is built.
    """
    db_table = _resolve_db_table(catalog, table)
    columns = catalog.schema[db_table]

    if column not in columns:
        raise TargetValidationError(
            f"No column '{column}' on table '{db_table}' is available."
        )

    description_col: str | None = None
    for candidate in _candidate_description_columns(column):
        if candidate in columns and is_provenance_in_scope(
            frozenset({(db_table, candidate)}), column_scope
        ):
            description_col = candidate
            break

    period_col: str | None = None
    if period is not None:
        if period.column not in columns:
            raise TargetValidationError(
                f"No column '{period.column}' on table '{db_table}' is available."
            )
        period_col = period.column

    return ResolvedTarget(
        db_table=db_table,
        column=column,
        description_col=description_col,
        period_col=period_col,
    )


def _period_predicate(period_col: str, period: Period) -> exp.Expression | None:
    col = exp.column(period_col)
    bounds: list[exp.Expression] = []
    if period.start is not None:
        bounds.append(exp.GTE(this=col.copy(), expression=exp.Literal.string(period.start)))
    if period.end is not None:
        bounds.append(exp.LTE(this=col.copy(), expression=exp.Literal.string(period.end)))
    if not bounds:
        return None
    predicate = bounds[0]
    for extra in bounds[1:]:
        predicate = exp.and_(predicate, extra)
    return predicate


def build_sql(
    target: ResolvedTarget,
    *,
    period: Period | None,
    limit: int,
) -> str:
    """Build the backing `runQuery` SQL (design §2.5) via sqlglot AST nodes.

        SELECT <column> [, <descriptionCol>], count() AS freq
        FROM <database>.<table>
        [WHERE <period.column> >= <start> AND <period.column> <= <end>]
        GROUP BY <column> [, <descriptionCol>]
        ORDER BY freq DESC
        LIMIT <limit>
    """
    database, table_name = target.db_table.split(".", 1)
    value_col = exp.column(target.column)
    group_cols: list[exp.Expression] = [value_col]
    select_cols: list[exp.Expression] = [value_col.copy()]

    if target.description_col is not None:
        desc_col = exp.column(target.description_col)
        select_cols.append(desc_col.copy())
        group_cols.append(desc_col)

    select_cols.append(exp.alias_(exp.Count(this=exp.Star()), "freq"))

    query = (
        exp.select(*select_cols)
        .from_(exp.table_(table_name, db=database))
        .group_by(*group_cols)
        .order_by(exp.column("freq").desc())
        .limit(limit)
    )

    if period is not None and target.period_col is not None:
        predicate = _period_predicate(target.period_col, period)
        if predicate is not None:
            query = query.where(predicate)

    return query.sql(dialect="clickhouse")


__all__ = [
    "Period",
    "ResolvedTarget",
    "TargetValidationError",
    "build_sql",
    "resolve_target",
]
