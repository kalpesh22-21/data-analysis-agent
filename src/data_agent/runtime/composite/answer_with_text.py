"""Terminal prose-answer tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.session.models import ResultPreview

from .answer_with_table import clean_answer_text

if TYPE_CHECKING:
    from data_agent.runtime.loop.agent_loop import TurnContext

TOOL_NAME = "answerWithText"


def clean_evidence(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        dict.fromkeys(value.strip() for value in raw if isinstance(value, str) and value.strip())
    )


class AnswerWithTextTool:
    tool_name = TOOL_NAME

    async def run(
        self,
        arguments: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        args = arguments if isinstance(arguments, dict) else {}
        confirmation = ResultPreview(
            columns=["answered", "evidence_declared"],
            row_count=1,
            truncated=False,
            preview_rows=[[
                clean_answer_text(args.get("answer")) is not None,
                bool(clean_evidence(args.get("evidence"))),
            ]],
        )
        return ToolResult(
            status="ok",
            tool_name=TOOL_NAME,
            error_code=None,
            retryable=None,
            user_message=None,
            provenance=frozenset(),
            result_preview=confirmation,
            result_full=None,
        )


__all__ = ["TOOL_NAME", "AnswerWithTextTool", "clean_evidence"]
