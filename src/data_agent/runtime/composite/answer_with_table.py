"""answer_with_table.py — the `answerWithTable` runtime tool.

The model calls `answerWithTable(answer=..., sql=... | blueprint_id=...)` when its
answer IS a table. The call is TERMINAL: it carries the final prose AND designates
the query whose rows the user should see, so the turn ends there.

Why terminal. The loop's other exit is a model turn with NO tool calls. A non-
terminal designation tool therefore cost a whole extra round-trip: the model
designated the table, got back a trivial confirmation, and then re-sent the ENTIRE
conversation (4.7k-10.2k tokens on a measured turn) purely to emit one sentence it
could already have written. It learned nothing in between — it had the query
results in context at designation time. Folding the prose into the designation
removes a round-trip that bought nothing.

Why `answer` is REQUIRED. If it were optional, a model calling this out of habit
mid-turn would terminate the turn with an empty answer — a total, silent failure.
Requiring it makes the call self-describing: "this is my final answer, and here is
its table."

The safe default is unchanged. A SCALAR answer still ends the old way — a turn with
no tool calls — so a model that never calls this tool cannot hang; it falls through
to prose with no table, exactly today's degradation.

TWO WAYS TO DESIGNATE, one wire contract:

  * `sql=` — a raw read-only SELECT. NOT required to be one the agent already ran:
    the executed query usually carries a LIMIT the agent chose for its own reading,
    and paging needs the un-capped shape. Scope is still enforced where the query
    runs (the MCP, under the caller's own JWT), so this can never widen access.

  * `blueprint_id=` — the blueprint whose result IS the answer. The model should
    not have to copy rendered SQL it may only partly see. The runtime resolves the
    id to that blueprint's `terminal_sql` (`blueprint/executor.py`), captured when
    the blueprint RAN this turn.

    It must name a blueprint run SUCCESSFULLY THIS TURN. Unlike raw SQL there is
    nothing to resolve otherwise, and re-running it to find out would decouple the
    paged table from the D56 verification that gated the answer the user was given.
    An unmatched id resolves to nothing and is logged.

Either way the UI receives ONE field — `answer_sql` — and pages it through
`POST /query/page`. The blueprint path is resolved server-side precisely so the UI
never re-executes a DAG: for a composed blueprint that would re-run every node and
re-materialise scratch on every scroll.

KNOWN LIMIT: a scratch-backed composed blueprint's terminal SQL references a
session-scoped `scratch.*` table with a TTL. Paging works until that lapses, then
returns an ordinary query error. Fixing it properly means materialising the answer
separately, which is a larger change than this tool.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.session.models import ResultPreview

TOOL_NAME = "answerWithTable"

# Lenient safety caps (not a contract — DoS/absurdity guards). Over-long input is
# truncated rather than rejected, matching `record_assumptions.py`'s posture: the
# runtime does not second-guess model text, it only stops a pathological payload
# being stored verbatim.
_MAX_SQL_LEN = 20_000
_MAX_ANSWER_LEN = 20_000


def clean_answer_sql(raw: Any) -> str | None:
    """Normalize a model-supplied `sql` value into a single SQL string or `None`.

    Non-`str` → `None`; stripped; empty → `None`; truncated to `_MAX_SQL_LEN`.
    Does NOT parse, validate, or rewrite the SQL — validation happens where the
    query actually runs (`runtime/query_page.py` + the MCP), so this helper can
    never be the thing that silently changes what the user sees.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:_MAX_SQL_LEN] if text else None


def clean_answer_text(raw: Any) -> str | None:
    """Normalize the model's final prose. Same rules as `clean_answer_sql`; kept a
    separate function so the two caps can diverge without a shared-helper edit."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:_MAX_ANSWER_LEN] if text else None


def clean_blueprint_id(raw: Any) -> str | None:
    """Normalize a model-supplied `blueprint_id`. Resolution against THIS turn's
    successful runs happens in the loop (`_resolve_answer_sql`), not here."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text[:200] if text else None


class AnswerWithTableTool:
    """The `answerWithTable(answer, sql | blueprint_id)` runtime tool.

    Stateless: `run` never raises on malformed args (the loop's
    `_run_runtime_tool` also guards, defense in depth) and holds nothing itself.
    The loop reads the ARGUMENTS — the same discipline as `recordAssumptions` — to
    build the turn's `answer_sql`/`assistant_text`, and treats a SUCCESSFUL call as
    the turn's terminal event.
    """

    tool_name = TOOL_NAME

    async def run(
        self, arguments: dict[str, Any], credentials: RuntimeCredentials
    ) -> ToolResult:
        args = arguments if isinstance(arguments, dict) else {}
        answered = clean_answer_text(args.get("answer")) is not None
        designated = (
            clean_answer_sql(args.get("sql")) is not None
            or clean_blueprint_id(args.get("blueprint_id")) is not None
        )
        # A tiny confirmation. In the normal (terminal) case the model never sees
        # it — the turn ends on this call — but it is still persisted to the trail,
        # so it must be a real, honest result rather than a placeholder. It
        # deliberately carries NO rows: echoing the table back would re-create the
        # transcribe-the-rows behaviour this tool exists to stop.
        confirmation = ResultPreview(
            columns=["answered", "table_designated"],
            row_count=1,
            truncated=False,
            preview_rows=[[answered, designated]],
        )
        return ToolResult(
            status="ok",
            tool_name=TOOL_NAME,
            error_code=None,
            retryable=None,
            user_message=None,
            # DETERMINED-EMPTY, like `recordAssumptions` and every other data-free
            # tool in `provenance/capture.py::_NO_PROVENANCE_TOOLS`. This call reads
            # NO warehouse data — it echoes back the model's own text and a query
            # reference. `None` would mean UNDETERMINED and, because
            # `filter_trail`'s current-turn exemption is status-gated to
            # `status != "ok"`, would hand the model the D94 "result withheld"
            # sentinel (the bug fixed for recordAssumptions).
            provenance=frozenset(),
            result_preview=confirmation,
            result_full=None,
        )


__all__ = [
    "TOOL_NAME",
    "AnswerWithTableTool",
    "clean_answer_sql",
    "clean_answer_text",
    "clean_blueprint_id",
]
