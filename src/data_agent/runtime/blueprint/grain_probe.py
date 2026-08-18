"""blueprint/grain_probe.py — the SHARED D56 grain-probe builder (one oracle).

The grain-integrity probe (`SELECT count(), count(DISTINCT <grain cols>)` over the final
SQL) is the fan-out canary the D56 gate relies on, and it is needed in TWO places that MUST
stay byte-identical: the live `runBlueprint` fast path (`executor._verify`) and the offline
golden-replay probe (`learning/promotion/warehouse_probe.py`). Any drift — a different
grain-column mapping, probe shape or unpack — would let an offline replay pass while the
live run failed, silently breaking the golden replay's whole premise that offline green
implies live green over the exact same enforced path. So the three pieces live here once
and both callers import them: `map_grain_columns`, `build_grain_probe_sql` and
`unpack_grain_probe`.

Pure and I/O-free (the caller does the dispatch); fail-closed on every ambiguity.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from .template import TemplateBindError, assert_read_only_select, parse_template


def map_grain_columns(
    template_or_sql: str, grain_columns: tuple[str, ...]
) -> list[str] | None:
    """Map each DECLARED grain column to an OUTPUT column name (a template aliases
    `Department AS department`, but the declared grain is `Department`). Parsed via
    `parse_template` so a `{slot}` template parses too — V1: the caller passes the
    PRE-BIND template, so an output name can never be a bound slot VALUE.

    Matching is fail-closed (V1): an EXACT output-name match wins; otherwise a
    case-insensitive match is accepted ONLY when it resolves to a SINGLE output
    column. An ambiguous casefold COLLISION (declared "Dept" vs outputs "dept" AND
    "DEPT") → `None` (the verify gate must not `COUNT(DISTINCT)` a GUESSED column);
    a declared grain column with NO output match → `None` too. Either `None` makes
    the caller pass `distinct=None` → verify.py withholds the result."""
    try:
        tree = parse_template(template_or_sql)
    except TemplateBindError:
        return None
    outputs = list(getattr(tree, "named_selects", []) or [])
    exact = set(outputs)
    # casefold key → the DISTINCT output names that collapse to it (order-preserved).
    casefold_candidates: dict[str, list[str]] = {}
    for name in outputs:
        bucket = casefold_candidates.setdefault(name.casefold(), [])
        if name not in bucket:
            bucket.append(name)
    mapped: list[str] = []
    for col in grain_columns:
        if col in exact:
            mapped.append(col)
            continue
        candidates = casefold_candidates.get(col.casefold(), [])
        if len(candidates) == 1:
            mapped.append(candidates[0])  # unambiguous case-insensitive match
        else:
            return None  # ambiguous collision OR no match → fail-closed
    return mapped


def build_grain_probe_sql(final_sql: str, grain_output_cols: list[str]) -> str:
    """Build the scope-enforceable `SELECT COUNT(*), COUNT(DISTINCT <grain>) FROM
    (<final SQL>)` probe (§4.2) — the fan-out canary. Built via the AST so the
    inner SQL is embedded structurally, never string-spliced."""
    inner = sqlglot.parse_one(final_sql, dialect="clickhouse")
    assert_read_only_select(inner)  # defense-in-depth: the probe subquery is a read
    subquery = exp.Subquery(
        this=inner, alias=exp.TableAlias(this=exp.to_identifier("__bp_sub"))
    )
    count_star = exp.Count(this=exp.Star())
    distinct = exp.Count(
        this=exp.Distinct(expressions=[exp.column(col) for col in grain_output_cols])
    )
    select = exp.select(
        exp.alias_(count_star, "__bp_n"), exp.alias_(distinct, "__bp_d")
    ).from_(subquery)
    return select.sql(dialect="clickhouse")


def unpack_grain_probe(raw: Any) -> tuple[int | None, int | None]:
    """Pull `(total, distinct)` from the single-row grain-probe result. Any
    structural surprise → `(None, None)` (fail-closed at the caller)."""
    rows = _grain_probe_rows(raw)
    if not rows or len(rows[0]) < 2:
        return None, None
    total, distinct = rows[0][0], rows[0][1]
    try:
        return int(total), int(distinct)
    except (TypeError, ValueError):
        return None, None


def _grain_probe_rows(raw: Any) -> list[list[Any]]:
    """Structurally pull the `rows` out of a `{columns, rows, …}` runQuery result,
    normalizing exactly as `executor._unpack_result` does (tuple→list, drop non-list
    rows) so the byte-behavior of the two callers is identical."""
    if not isinstance(raw, dict):
        return []
    rows = raw.get("rows")
    if not isinstance(rows, list):
        return []
    return [list(r) for r in rows if isinstance(r, list | tuple)]


__all__ = ["build_grain_probe_sql", "map_grain_columns", "unpack_grain_probe"]
