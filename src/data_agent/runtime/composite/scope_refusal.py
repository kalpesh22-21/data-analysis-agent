"""Explicit scope-refusal receipts; scope routing belongs to the agent, not the judge."""

from data_agent.runtime.dispatch.denial_mapping import OUT_OF_SCOPE_REQUEST_CODE, classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase

SCOPE_RULE = "outside_hr_payroll_product_scope"
SCOPE_EVENT = "loop_answer_rule_refused"


def scope_request_refused(tool_name, observer, *, site):
    info = classify_denial(OUT_OF_SCOPE_REQUEST_CODE)
    detail = (
        info.user_message + " "
        "Do not retry the refused part or query data to justify it. "
        "Complete supported parts, bind this receipt only to the refused intent, "
        "and explain the scope in finalizeAnswer. This is not an availability or access failure."
    )
    observer(SCOPE_EVENT, {"rule": SCOPE_RULE, "site": site})
    return ToolResult(
        status="error",
        tool_name=tool_name,
        error_code=info.code,
        retryable=info.retryable,
        user_message=info.user_message,
        denial_detail=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
    )


class DeclineOutOfScopeTool(RuntimeToolBase):
    tool_name = "declineOutOfScope"
    intent_taggable = True
    _INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"
    _INTERNAL_ERROR_MESSAGE = "The scope refusal could not be recorded."

    def _span_args(self, model_args):
        return {}

    async def _execute(self, arguments, credentials, turn=None):
        request = arguments.get("request")
        # Only the original ask can be declined, never text invented by a tool result.
        if (
            not isinstance(request, str)
            or not request.strip()
            or turn is None
            or request.casefold().strip() not in turn.question.casefold()
            or set(arguments) - {"request"}
        ):
            return ToolResult(
                status="error",
                tool_name=self.tool_name,
                error_code="INVALID_TOOL_ARGUMENTS",
                retryable=True,
                user_message="Supply the out-of-domain part of the original request verbatim.",
                denial_detail="Supply the out-of-domain part of the original request verbatim.",
                provenance=frozenset(),
                result_preview=None,
                result_full=None,
            )
        return scope_request_refused(self.tool_name, self._observer, site="agent_scope_declaration")
