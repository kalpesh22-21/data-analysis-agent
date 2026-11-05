"""Explicit scope-refusal receipts; scope routing belongs to the agent, not the judge."""

from data_agent.runtime.dispatch.denial_mapping import OUT_OF_SCOPE_REQUEST_CODE, classify_denial
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase

SCOPE_RULE = "outside_hr_payroll_product_scope"
ACTION_RULE = "action_not_supported"
DOCUMENTATION_RULE = "product_documentation_unavailable"
SCOPE_EVENT = "loop_answer_rule_refused"
# Why a part is declined: outside the domain, or an action this deployment cannot take
# (no UI options are enabled, so nothing can even be prepared for the user to click).
DECLINE_REASONS = ("outside_domain", ACTION_RULE, DOCUMENTATION_RULE)
_ACTION_MESSAGE = (
    "This assistant cannot perform or prepare actions or navigation here. "
    "I can answer HR, payroll and product questions instead."
)


def scope_request_refused(tool_name, observer, *, site, reason="outside_domain"):
    limited = reason in {ACTION_RULE, DOCUMENTATION_RULE}
    info = classify_denial("NOT_SUPPORTED_HERE" if limited else OUT_OF_SCOPE_REQUEST_CODE)
    action = reason == ACTION_RULE
    message = (_ACTION_MESSAGE if action else "Product documentation is not available in this deployment."
               if reason == DOCUMENTATION_RULE else info.user_message)
    detail = message + " " + (
        "Do not query data to stand in for the action or to pick who it applies to; "
        "choosing them is part of the action. Say in finalizeAnswer that it has to be done in Paycom. "
        if action else "Do not retry the refused part or query data to justify it. "
    ) + (
        "Complete supported parts, bind this receipt only to the refused intent, "
        "and explain the scope in finalizeAnswer. It is not answer evidence: a section that "
        "states the refusal uses evidence_refs: [], and no deliverable cites it. "
        + ("This is a deployment limitation, not an execution failure or out-of-domain request."
           if limited else "This is not an availability or access failure.")
    )
    observer(SCOPE_EVENT, {"rule": reason if limited else SCOPE_RULE, "site": site})
    return ToolResult(
        status="error",
        tool_name=tool_name,
        error_code=info.code,
        retryable=info.retryable,
        user_message=message,
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

    def __init__(self, *, actions_available=False, product_documentation_available=False, **kwargs):
        super().__init__(**kwargs)
        # True when UI capability tools are enabled: an action can then be prepared for
        # the user, so declining it as unsupported skips the option the user needs.
        self._actions_available = actions_available
        self._product_documentation_available = product_documentation_available

    def _span_args(self, model_args):
        return {}

    async def _execute(self, arguments, credentials, turn=None):
        request = arguments.get("request")
        tags = arguments.get("serves_intents", [])
        reason = arguments.get("reason", "outside_domain")
        # The loop validates/persists intent tags on TrailEntry before dispatch.
        # Direct callers may still supply the optional schema-advertised field.
        normalized = " ".join(request.casefold().split()) if isinstance(request, str) else ""
        # Only the original ask can be declined, never text invented by a tool result.
        if (
            not isinstance(request, str)
            or not request.strip()
            or turn is None
            or normalized not in " ".join(turn.question.casefold().split())
            or set(arguments) - {"request", "serves_intents", "reason"}
            or not isinstance(tags, list)
            or not all(isinstance(tag, str) for tag in tags)
            or reason not in DECLINE_REASONS
        ):
            return ToolResult(
                status="error",
                tool_name=self.tool_name,
                error_code="INVALID_TOOL_ARGUMENTS",
                retryable=True,
                user_message="Supply the affected part of the original request verbatim.",
                denial_detail="Supply the affected part of the original request verbatim.",
                provenance=frozenset(),
                result_preview=None,
                result_full=None,
            )
        if reason == DOCUMENTATION_RULE and self._product_documentation_available:
            detail = "Product documentation is enabled: use searchHelpCenter and getHelpCenterDocument; an actual service failure needs its own receipt."
            return ToolResult(status="error", tool_name=self.tool_name, error_code="INVALID_TOOL_ARGUMENTS",
                              retryable=True, user_message=detail, denial_detail=detail,
                              provenance=frozenset(), result_preview=None, result_full=None)
        if reason == "action_not_supported" and self._actions_available:
            detail = (
                "UI options are available, so an action or navigation is not out of scope: search "
                "them (searchCapabilityTools) and prepare the matching option for the user to click. "
                "If none fits, say in finalizeAnswer that no suitable action is available."
            )
            return ToolResult(
                status="error", tool_name=self.tool_name, error_code="INVALID_TOOL_ARGUMENTS",
                retryable=True, user_message=detail, denial_detail=detail, provenance=frozenset(),
                result_preview=None, result_full=None,
            )
        return scope_request_refused(
            self.tool_name, self._observer, site="agent_scope_declaration", reason=reason
        )
