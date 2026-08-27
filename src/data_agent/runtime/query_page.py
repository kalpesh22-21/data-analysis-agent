"""Paging for the model-designated answer table.

`POST /query/page` executes ONE read-only query with `LIMIT`/`OFFSET` so the UI can page
the answer table itself. The SQL is whatever the model designated; it is NOT required to
have been executed during the turn. It adds NO authority: it runs through the SAME
`ToolDispatcher.dispatch("runQuery", ...)` with the CALLER's own credentials, so column
scope (D57/D80), read-only enforcement, row caps, denial mapping and provenance capture
are the identical code path — a designated query can never read a column the same caller
could not read by asking the agent.

Paging is applied by WRAPPING (`SELECT * FROM (<sql>) LIMIT n OFFSET m`) through
sqlglot, never by splicing a `LIMIT` onto model text: that makes the page bounds OURS,
and a malformed or multi-statement payload fails at parse time, before dispatch.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import sqlglot
from sqlglot import exp

_logger = logging.getLogger(__name__)

# Paging bounds. `_MAX_PAGE_SIZE` is deliberately well under the MCP's own
# MAX_RESPONSE_ROWS cap so a page is always the smaller of the two limits and the
# server-side cap is never the thing a user runs into while scrolling.
_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_SIZE = 1000

__all__ = [
    "MAX_PAGE_SIZE",
    "QueryPageError",
    "build_page_sql",
    "clamp_page_params",
    "page_rows",
]

MAX_PAGE_SIZE = _MAX_PAGE_SIZE


class QueryPageError(ValueError):
    """The designated SQL could not be turned into a page query.

        Carries a SHORT, static reason only: `str(exc)` reaches the client, so it must never
        echo the offending SQL or a raw sqlglot message (which can quote a fragment back).
    """


def clamp_page_params(limit: Any, offset: Any) -> tuple[int, int]:
    """Coerce and clamp caller-supplied paging params.

        Total by design (never raises): a non-integer, negative or absent value falls back
        to the default rather than 400-ing. `limit` clamps to `[1, _MAX_PAGE_SIZE]`,
        `offset` to `>= 0`.
    """
    try:
        page_limit = int(limit)
    except (TypeError, ValueError):
        page_limit = _DEFAULT_PAGE_SIZE
    try:
        page_offset = int(offset)
    except (TypeError, ValueError):
        page_offset = 0
    page_limit = max(1, min(page_limit, _MAX_PAGE_SIZE))
    page_offset = max(0, page_offset)
    return page_limit, page_offset


def build_page_sql(sql: str, *, limit: int, offset: int) -> str:
    """Wrap *sql* as `SELECT * FROM (<sql>) LIMIT limit OFFSET offset`.

        Parsed and re-rendered through sqlglot (ClickHouse dialect) rather than concatenated,
        so a multi-statement payload and a non-SELECT top level are both rejected here,
        before dispatch, and a payload crafted to break out of a hand-built f-string has
        nothing to break out of. Raises `QueryPageError` (static message) on anything it
        cannot safely wrap.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise QueryPageError("No query was provided.")
    try:
        parsed = sqlglot.parse_one(sql, dialect="clickhouse")
    except Exception as exc:  # noqa: BLE001 — sqlglot raises several error types
        # Log the real parse failure server-side ONLY; the raised message is static
        # (a sqlglot error can echo a fragment of the statement).
        _logger.warning("query-page: designated SQL failed to parse: %s", exc)
        raise QueryPageError("The query could not be parsed.") from exc
    if parsed is None:
        raise QueryPageError("The query could not be parsed.")
    # Read-only at the door. The MCP enforces this too — this is the earlier, more
    # specific rejection so a write never reaches dispatch at all.
    if not isinstance(parsed, exp.Select | exp.Union | exp.Subquery):
        raise QueryPageError("Only read-only SELECT queries can be paged.")

    page_query = (
        exp.select(exp.Star())
        .from_(exp.Subquery(this=parsed, alias=exp.TableAlias(this=exp.to_identifier("page_src"))))
        .limit(limit)
        .offset(offset)
    )
    return page_query.sql(dialect="clickhouse")


def page_rows(
    result_full: Any, preview_rows: Sequence[Sequence[Any]], *, limit: int
) -> list[list[Any]]:
    """The rows to serve for ONE page, taken from the FULL result, not the preview.

        `ToolResult.result_preview` is truncated to `preview_row_count` — the cap on what
        reaches MODEL CONTEXT (default 20). Serving a page from it capped EVERY page at 20
        rows however large the requested `limit`, and made `has_more` (`len(rows) >= limit`)
        false for any page size above that, so the rest of the table was unreachable from
        the UI: the fixed-preview problem paging exists to solve, reintroduced one layer up.

        `result_full` is the same authorized MCP result the preview is derived from — column
        scope is enforced at dispatch, which DENIES the query rather than filtering rows, so
        the full result carries nothing the preview was hiding. Its size is already bounded
        by OUR OWN wrapped `LIMIT` (`build_page_sql`), never by what the model can hold.

        Falls back to *preview_rows* for any other shape: a non-tabular result (the
        size-capped dict `_build_preview` produces) has no page to serve beyond the one
        row the dispatcher already built, and this must not be the thing that unwraps it.

        Trimmed to *limit* on every path. The wrapped `LIMIT` already bounds this
        server-side, so the slice is a belt: dropping the preview cap removed the
        incidental ceiling that used to sit here, and a page is OURS to bound — a
        backend that over-returns must not turn into an unbounded response body.
    """
    if isinstance(result_full, dict) and isinstance(result_full.get("rows"), list):
        return [list(row) for row in result_full["rows"][:limit]]
    # A bare list result, shaped one-item-per-row exactly as `_build_preview` shapes it.
    if isinstance(result_full, list):
        return [[item] for item in result_full[:limit]]
    return [list(row) for row in preview_rows[:limit]]
