"""Replace the current turn's model-declared assumptions; [] clears them.

The loop, resume and history use the same normalization and last-successful-write
fold. The tool is stateless. Assumptions are business interpretations, not data
facts or freshness claims.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase
from data_agent.runtime.session.models import ResultPreview

if TYPE_CHECKING:
    from data_agent.runtime.loop.agent_loop import TurnContext

TOOL_NAME = "recordAssumptions"

# Lenient safety caps (not a contract, just DoS/absurdity guards): keep at most
# this many assumptions, each at most this long. The prompt/schema drive the
# real shape; these only stop a pathological payload from being accumulated
# verbatim into the turn result / history.
_MAX_ASSUMPTIONS = 50
_MAX_ASSUMPTION_LEN = 2000


def assumption_key(text: str) -> str:
    """Ignore case, whitespace and a terminal period, never semantic differences."""
    return " ".join(text.split()).casefold().rstrip(".")


def clean_assumptions(raw: Any) -> list[str]:
    """Normalize a model-supplied `assumptions` value into a clean list of plain-English
    strings — the SINGLE cleaning used by BOTH the agent loop and `session_history`, so the
    two agree.

    Lenient by design: a non-list is `[]`; only non-blank `str` items survive (stored in
    stripped form); duplicates drop preserving FIRST-occurrence order; items past
    `_MAX_ASSUMPTIONS` are ignored and any single item longer than `_MAX_ASSUMPTION_LEN` is
    truncated.

    Does NOT attempt to detect or strip SQL or codes — that contract is enforced by the tool
    description and system prompt, never here.
    """
    if not isinstance(raw, list | tuple):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        if len(text) > _MAX_ASSUMPTION_LEN:
            text = text[:_MAX_ASSUMPTION_LEN]
        key = assumption_key(text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= _MAX_ASSUMPTIONS:
            break
    return cleaned


def fold_assumptions(target: list[str], raw: Any) -> None:
    """Replace in place so live, resume and history agree; [] explicitly clears."""
    target[:] = clean_assumptions(raw)



class RecordAssumptionsTool(RuntimeToolBase):
    _INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"
    _INTERNAL_ERROR_MESSAGE = "The tool could not complete. Please try again."

    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        return {}

    """The `recordAssumptions(assumptions)` runtime tool.

        Stateless: `run` never raises on malformed args (the loop's `_run_runtime_tool` also
        guards, defense in depth) and never holds the accumulated assumptions itself — it just
        returns a small confirmation. The loop reads `arguments["assumptions"]` via
        `clean_assumptions`.
    """

    tool_name = TOOL_NAME

    async def _execute(
        self,
        arguments: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        # *turn* (03 §C.1): the loop threads its own `TurnContext` to every
        # runtime tool. This one does not need it — accepted and ignored so the
        # `RuntimeTool` protocol has ONE signature rather than two shapes the
        # dispatch site has to tell apart.
        cleaned = clean_assumptions(
            arguments.get("assumptions") if isinstance(arguments, dict) else None
        )
        # A tiny confirmation the model sees as this call's result (the count of
        # assumptions accepted) — enough for the model to know the record landed
        # and move on to the final answer. No `result_full` (nothing to persist
        # behind a KV pointer); the assumptions themselves are folded into the
        # turn result from the call ARGUMENTS by the loop, not from here.
        confirmation = ResultPreview(
            columns=["recorded"],
            row_count=1,
            truncated=False,
            preview_rows=[[len(cleaned)]],
        )
        return ToolResult(
            status="ok",
            tool_name=TOOL_NAME,
            error_code=None,
            retryable=None,
            user_message=None,
            # DETERMINED-EMPTY (`frozenset()`), not `None`. This tool reads no
            # warehouse data at all — it echoes back the model's own plain-English
            # sentences — so it belongs to the same class as `listDatabases` /
            # `getTableSchema` / `searchKnowledge` in
            # `provenance/capture.py::_NO_PROVENANCE_TOOLS`: "exposes no
            # column-level data -> empty, not undetermined".
            #
            # It used to return `None`, which in this codebase means UNDETERMINED
            # (fail-closed), and that had a cost paid on EVERY successful call:
            # `scope_filter.filter_trail`'s current-turn exemption is status-gated
            # to `status != "ok"`, so an `ok`+`None` entry is never exempt — it was
            # dropped and re-materialised as the D94 stranded sentinel. The model
            # therefore saw "result withheld: provenance could not be determined …
            # Do not retry the identical call" in place of its confirmation, every
            # time it recorded assumptions, immediately before writing the final
            # answer. (The sentinel still rendered `args`, so the assumption text
            # was in context anyway — the withholding bought nothing in-turn.)
            #
            # The `None` was ALSO doing a second, unrelated job: keeping assumption
            # strings out of a LATER turn's context, whose `column_scope` may have
            # narrowed. That rule is real, but provenance is the wrong channel for
            # it — it now lives explicitly in
            # `context/assembly.py::_is_stale_model_text_entry`, which drops a
            # non-current-turn `recordAssumptions` entry by name.
            provenance=frozenset(),
            result_preview=confirmation,
            result_full=None,
        )


__all__ = ["TOOL_NAME", "RecordAssumptionsTool", "clean_assumptions", "fold_assumptions"]
