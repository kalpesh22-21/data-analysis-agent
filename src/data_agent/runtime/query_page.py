"""query_page.py — paging for the model-designated answer table.

`POST /query/page` executes ONE read-only query with `LIMIT`/`OFFSET` and returns
a page of rows. It exists so the UI can render the answer table itself, with real
paging, instead of the model transcribing rows into its prose or the runtime
shipping a fixed ~20-row `ResultPreview` the user could not page past (the
`result_table` field this replaced).

The SQL is the `answer_sql` the model designated via `presentTable`. It is NOT
required to have been executed during the turn (see `composite/present_table.py`
for why: the agent's own query usually carries a `LIMIT` it picked for its own
reading, and paging needs the un-capped shape).

WHY THAT IS SAFE, precisely — this endpoint adds NO authority:

  * It runs through the SAME `ToolDispatcher.dispatch("runQuery", ...)` the model
    uses, with the CALLER'S OWN credentials taken from this request's headers.
    Column-scope (D57/D80), read-only enforcement, row caps, denial mapping,
    provenance capture and telemetry are therefore the identical code path — a
    designated query can never read a column the same caller could not read by
    asking the agent for it.
  * It is not an eval hatch: anything the MCP rejects (write statement, out-of-
    scope column, unparseable SQL) is rejected here in exactly the same way, and
    surfaces as an ordinary denial rather than a runtime error.

So the trust boundary is unchanged; this is a second doorway onto the same
enforced path, not a bypass of it.

PAGING is applied by WRAPPING, never by string-splicing a `LIMIT` onto model
text: the SQL is parsed with sqlglot's ClickHouse dialect (the dialect
`sqlparse/provenance.py` already standardizes on) and nested as
`SELECT * FROM (<sql>) LIMIT n OFFSET m`. Wrapping is what makes the page bounds
OURS rather than the model's — a `LIMIT` inside the designated query still
applies to the inner result, but it can never let a page exceed `limit`, and a
malformed or multi-statement payload fails at parse time, before dispatch.
"""

from __future__ import annotations

import logging
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
]

MAX_PAGE_SIZE = _MAX_PAGE_SIZE


class QueryPageError(ValueError):
    """The designated SQL could not be turned into a page query.

    Carries a SHORT, static reason only. `str(exc)` reaches the client, so it must
    never echo the offending SQL or a raw sqlglot message — a parse error can quote
    a fragment of the statement back (the same reasoning as
    `sqlparse/oracle.py`'s masking).
    """


def clamp_page_params(limit: Any, offset: Any) -> tuple[int, int]:
    """Coerce and clamp caller-supplied paging params.

    Total by design (never raises): a non-integer/negative/absent value falls back
    to the default rather than 400-ing, because paging params are UI plumbing, not
    a place to fail a user's scroll. `limit` is clamped to
    `[1, _MAX_PAGE_SIZE]`, `offset` to `>= 0`.
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

    Parsed and re-rendered through sqlglot (ClickHouse dialect) rather than string
    concatenation, so:
      * a multi-statement payload is rejected — `parse_one` raises on the trailing
        statement, closing the `…; DROP …` shape at the door rather than relying on
        the MCP's read-only mode as the only guard;
      * a non-SELECT top level (INSERT/ALTER/CREATE) is rejected here explicitly,
        before dispatch;
      * the emitted page query is whatever sqlglot renders, so a payload crafted to
        break out of a hand-built f-string has nothing to break out of.

    Raises `QueryPageError` (static message) on anything it cannot safely wrap.
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
