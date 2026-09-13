"""Local pre-dispatch refusals: no warehouse work and no progress start."""

from collections.abc import Mapping

from data_agent.runtime.capabilities.prefetch import route_uses_data_prefetch
from data_agent.runtime.capabilities.router import PrefetchRouter
from data_agent.runtime.dispatch.denial_mapping import (
    BLUEPRINT_NOT_SEARCHED_CODE,
    UNKNOWN_TOOL_CODE,
    classify_denial,
)
from data_agent.runtime.dispatch.tool_dispatcher import ToolResult
from data_agent.runtime.sanitize import sanitize_text

BLUEPRINT_FAMILY = frozenset({"searchBlueprints", "getBlueprint", "runBlueprint"})


def advertised_names(tools, runtime_tools, builtins) -> frozenset[str]:
    return (
        frozenset(
            schema["name"]
            for schema in tools
            if isinstance(schema, Mapping) and isinstance(schema.get("name"), str)
        )
        | frozenset(runtime_tools)
        | frozenset(builtins)
    )


def refusal(name: str, code: str) -> ToolResult:
    detail = classify_denial(code).user_message
    if code == UNKNOWN_TOOL_CODE:
        detail = f"No tool named {sanitize_text(name, 64)!r} is available. " + detail
    return ToolResult(
        status="error",
        tool_name=name,
        error_code=code,
        retryable=True,
        user_message=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        denial_detail=detail,
    )


class BlueprintSearchGate:
    def __init__(self, question, trail, turn_index):
        self._data = bool(question) and route_uses_data_prefetch(PrefetchRouter().route(question))
        self._consulted = any(
            tc.turn_index == turn_index and tc.tool_name in BLUEPRINT_FAMILY for tc in trail
        )
        self._used = False

    def observe_batch(self, calls) -> None:
        self._consulted |= any(call.name in BLUEPRINT_FAMILY for call in calls)

    def check(self, name: str) -> ToolResult | None:
        if name != "runQuery" or not self._data or self._consulted or self._used:
            return None
        self._used = True
        return refusal(name, BLUEPRINT_NOT_SEARCHED_CODE)
