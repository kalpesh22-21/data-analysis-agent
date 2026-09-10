from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jsonschema import ValidationError, validate

from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolResult,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase
from data_agent.runtime.observability.redaction import tool_span_args

from .client import (
    CapabilityClient,
    CapabilityDefinition,
    CapabilityError,
    CapabilityServiceError,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.loop.agent_loop import TurnContext

INVALID_ARGS = "CAPABILITY_INVALID_ARGS"
UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
INTERNAL_ERROR = "CAPABILITY_INTERNAL_ERROR"


def _ok(name: str, value: dict[str, Any], *, terminal: bool = False) -> ToolResult:
    return ToolResult(
        status="ok",
        tool_name=name,
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=frozenset(),
        result_preview=_build_preview(value, 20, 4_000),
        result_full=value,
        terminal=terminal,
    )


class _CapabilityTool(RuntimeToolBase):
    _INTERNAL_ERROR_CODE = INTERNAL_ERROR
    _INTERNAL_ERROR_MESSAGE = "UI capabilities are temporarily unavailable."
    _GUARDED_EXCEPTIONS = (CapabilityError,)

    def __init__(
        self,
        *,
        client: CapabilityClient,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        super().__init__(observer=observer, tracer=tracer, disable_redaction=disable_redaction)
        self._client = client

    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        return tool_span_args(self.tool_name, model_args, disable_redaction=self._disable_redaction)

    def _on_guarded_exception(self, exc: Exception, model_args: dict[str, Any]) -> ToolResult:
        if isinstance(exc, CapabilityServiceError):
            invalid = exc.code in {
                "INVALID_ARGUMENTS",
                "ENTITY_LIMIT_EXCEEDED",
                "PAYLOAD_TOO_LARGE",
            }
            return self._error(
                f"CAPABILITY_{exc.code}",
                (
                    "The capability arguments need to be corrected."
                    if invalid
                    else "UI capabilities are temporarily unavailable."
                ),
                retryable=invalid or exc.status_code == 503,
            )
        return self._error(
            UNAVAILABLE, "UI capabilities are temporarily unavailable.", retryable=True
        )


class SearchCapabilityToolsTool(_CapabilityTool):
    tool_name = "searchCapabilityTools"

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: TurnContext | None
    ) -> ToolResult:
        query = model_args.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._error(INVALID_ARGS, "'query' must be non-empty text.", retryable=True)
        cards = await self._client.search(
            query.strip(), ("navigation", "data_widget"), limit=5
        )
        return _ok(self.tool_name, {"cards": [card.to_dict() for card in cards]})


class GetCapabilityTool(_CapabilityTool):
    tool_name = "getCapabilityTool"

    def __init__(
        self,
        *,
        hydrate: Callable[[CapabilityDefinition], bool | None],
        visible_names: set[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._hydrate = hydrate
        self._visible_names = visible_names

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: TurnContext | None
    ) -> ToolResult:
        name = model_args.get("tool_name")
        if not isinstance(name, str) or not name.strip():
            return self._error(INVALID_ARGS, "'tool_name' must be non-empty text.", retryable=True)
        name = name.strip()
        definition = await self._client.get_definition(name)
        if definition is None:
            return _ok(self.tool_name, {"found": False, "tool_name": name})
        ready = self._hydrate(definition) is not False
        if ready:
            self._visible_names.add(name)
        return _ok(
            self.tool_name,
            {"found": True, "tool_name": name, "kind": definition.kind, "ready": ready},
        )


class PresentCapabilityCardTool(_CapabilityTool):
    tool_name = "dynamicCapability"
    intent_taggable = True

    def __init__(self, *, definition: CapabilityDefinition, **kwargs: Any) -> None:
        self.tool_name = definition.name
        self._definition = definition
        super().__init__(**kwargs)

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: TurnContext | None
    ) -> ToolResult:
        arguments = dict(model_args)
        answer = arguments.pop("answer", None)
        arguments.pop("serves_intent", None)
        try:
            validate(arguments, self._definition.argument_schema())
        except ValidationError as exc:
            return self._error(
                INVALID_ARGS,
                f"Capability arguments are invalid: {exc.message}",
                retryable=True,
            )
        query = turn.question if turn is not None else ""
        needs_entity_resolution = any(
            parameter.collection and parameter.name in arguments
            for parameter in self._definition.parameters
        )
        result = await self._client.hydrate(
            self.tool_name,
            query=query,
            raw_arguments=arguments,
            end_user_jwt=credentials.jwt if needs_entity_resolution else None,
        )
        if result is None:
            return self._error(
                "CAPABILITY_NOT_FOUND",
                "That capability is no longer available.",
                retryable=False,
            )
        result = {
            **result,
            "_agent_evidence": {
                "kind": self._definition.kind,
                "description": self._definition.description,
                "parameters": [
                    {
                        "name": parameter.name,
                        "description": parameter.description,
                        "type": parameter.type,
                        "collection": parameter.collection,
                    }
                    for parameter in self._definition.parameters
                ],
                "metadata": self._definition.metadata,
            },
        }
        if isinstance(answer, str) and answer.strip():
            result = {**result, "answer": answer.strip()}
        return _ok(
            self.tool_name,
            result,
            terminal=True,
        )


__all__ = ["GetCapabilityTool", "PresentCapabilityCardTool", "SearchCapabilityToolsTool"]
