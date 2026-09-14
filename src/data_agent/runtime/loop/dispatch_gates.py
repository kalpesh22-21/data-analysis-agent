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
        self._consulted = False
        self._discovery_unavailable = False
        self._turn_index = turn_index

    def observe_batch(self, calls) -> None:
        """Proposed calls are not information received by the model."""

    def observe_context(self, messages) -> None:
        import json

        for message in messages:
            if message.get("role") == "tool":
                try:
                    payload = json.loads(message.get("content", ""))
                except (ValueError, TypeError):
                    payload = {}
                if (
                    isinstance(payload, dict)
                    and payload.get("tool_name") in BLUEPRINT_FAMILY
                    and payload.get("status") == "ok"
                    and payload.get("turn_index") == self._turn_index
                ):
                    self._consulted = True
                if (
                    isinstance(payload, dict)
                    and payload.get("tool_name") in BLUEPRINT_FAMILY
                    and payload.get("turn_index") == self._turn_index
                    and payload.get("error_code")
                    in {"RETRIEVAL_TOOL_UNAVAILABLE", "RETRIEVAL_TOOL_INTERNAL_ERROR"}
                ):
                    self._discovery_unavailable = True

    def observe_prefetch(self):
        self._consulted = True

    def check(self, name: str) -> ToolResult | None:
        if name != "runQuery" or not self._data or self._consulted or self._discovery_unavailable:
            return None
        return refusal(name, BLUEPRINT_NOT_SEARCHED_CODE)
