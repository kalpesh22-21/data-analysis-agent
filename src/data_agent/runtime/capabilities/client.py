from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from .resolution import CapabilityResolutionRegistry
from .router import PrefetchRoute

CapabilityKind = Literal["navigation", "data_widget"]
ResolutionStrategy = Literal["torch", "resolve_values", "direct", "service"]


class CapabilityError(Exception):
    pass


class CapabilityServiceError(CapabilityError):
    def __init__(self, *, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class CapabilityCard:
    id: str
    tool_name: str
    kind: CapabilityKind
    summary: str
    matched_questions: tuple[str, ...]
    matched_actions: tuple[str, ...]
    matched_data_points: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "kind": self.kind,
            "summary": self.summary,
            "matched_questions": list(self.matched_questions),
            "matched_actions": list(self.matched_actions),
            "matched_data_points": list(self.matched_data_points),
        }


@dataclass(frozen=True)
class ToolParam:
    name: str
    description: str
    type: str
    collection: bool
    enum: tuple[str, ...] | None
    enum_descriptions: dict[str, str] | None
    enum_display_names: dict[str, str] | None
    default: str | None
    resolution_strategy: ResolutionStrategy | None = None
    semantic_type: str | None = None
    entity_type: str | None = None

    def model_schema(
        self, resolution_registry: CapabilityResolutionRegistry | None = None
    ) -> dict[str, Any] | None:
        if self.type == "code_induced":
            return None
        if self.collection:
            schema: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
        elif self.enum is not None:
            schema = {"type": "string", "enum": list(self.enum)}
        elif self.type == "boolean":
            schema = {"type": "boolean"}
        elif self.type == "date":
            schema = {"type": "string", "format": "date"}
        elif self.type == "dateRange":
            schema = {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "format": "date"},
                    "end": {"type": "string", "format": "date"},
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            }
        else:
            schema = {"type": "string"}
        description = self.description
        if self.resolution_strategy == "torch" and self.entity_type == "employee":
            target = resolution_registry.get("employee_identifier") if resolution_registry else None
            if target is not None and target.output_column:
                description = (
                    f"{description} Values passed here must be employee names, not employee "
                    "codes or eecodes. When the user supplies an employee code or eecode, "
                    f"first call resolveValues with table {target.table} and column "
                    f"{target.column}. Then retrieve {target.output_column} from "
                    f"{target.table} for that exact resolved {target.column} and pass the "
                    "returned employee name here. Never pass the employee code as a name."
                )
        elif self.resolution_strategy == "resolve_values" and self.semantic_type:
            target = resolution_registry.get(self.semantic_type) if resolution_registry else None
            if target is not None:
                period = (
                    f" and period column {target.period_column}"
                    if target.period_column
                    else ""
                )
                description = (
                    f"{description} First call resolveValues with table {target.table}, "
                    f"column {target.column}{period}, then pass the resolved stored value."
                )
        schema["description"] = description
        if self.default is not None and self.default != "null":
            if self.collection:
                schema["default"] = [self.default]
            elif self.type == "boolean" and self.default.casefold() in {"true", "false"}:
                schema["default"] = self.default.casefold() == "true"
            else:
                schema["default"] = self.default
        return schema


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    version: str
    kind: CapabilityKind
    description: str
    parameters: tuple[ToolParam, ...]
    metadata: dict[str, Any]

    def argument_schema(
        self, resolution_registry: CapabilityResolutionRegistry | None = None
    ) -> dict[str, Any]:
        properties = {
            parameter.name: schema
            for parameter in self.parameters
            if (schema := parameter.model_schema(resolution_registry)) is not None
        }
        return {"type": "object", "properties": properties, "additionalProperties": False}

    def tool_schema(
        self, resolution_registry: CapabilityResolutionRegistry | None = None
    ) -> dict[str, Any]:
        parameters = self.argument_schema(resolution_registry)
        properties = dict(parameters["properties"])
        properties["answer"] = {
            "type": "string",
            "description": (
                "For a mixed request only: the concise answer to work completed before this "
                "UI option. Omit when the UI option alone answers the request. Never mention "
                "cards, widgets, capabilities, tools, or hydration to the user."
            ),
        }
        properties["serves_intent"] = {
            "type": "string",
            "description": "Optional analysis-state intent id served by this UI card.",
        }
        parameters["properties"] = properties
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
        }


@dataclass(frozen=True)
class CapabilityPrefetch:
    route: PrefetchRoute
    cards: tuple[CapabilityCard, ...]


class CapabilityClient(Protocol):
    async def search(
        self, query: str, kinds: tuple[CapabilityKind, ...], limit: int = 5
    ) -> list[CapabilityCard]: ...

    async def get_definition(self, tool_name: str) -> CapabilityDefinition | None: ...

    async def hydrate(
        self,
        tool_name: str,
        *,
        query: str,
        raw_arguments: dict[str, Any],
        end_user_jwt: str | None,
    ) -> dict[str, Any] | None: ...


class HttpCapabilityClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _headers(self, end_user_jwt: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if end_user_jwt:
            headers["X-End-User-Authorization"] = f"Bearer {end_user_jwt}"
        return headers

    async def _request(
        self, method: str, path: str, *, end_user_jwt: str | None = None, **kwargs: Any
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers(end_user_jwt),
                    **kwargs,
                )
                if response.status_code == 404:
                    return None
                if response.is_error:
                    try:
                        error = response.json().get("error", {})
                    except ValueError:
                        error = {}
                    code = error.get("code") if isinstance(error, dict) else None
                    raise CapabilityServiceError(
                        status_code=response.status_code,
                        code=code if isinstance(code, str) and code else "SERVICE_ERROR",
                    )
                response.raise_for_status()
                return response.json()
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError(f"Capability request failed: {type(exc).__name__}") from exc

    async def search(
        self, query: str, kinds: tuple[CapabilityKind, ...], limit: int = 5
    ) -> list[CapabilityCard]:
        body = await self._request(
            "POST",
            "/search",
            json={
                "query": query,
                "kinds": list(kinds),
                "limit": limit,
                "category_limits": {"questions": 5, "actions": 5, "data_points": 5},
            },
        )
        raw_cards = body.get("cards") if isinstance(body, dict) else None
        if not isinstance(raw_cards, list):
            raise CapabilityError("Malformed capability search response.")
        return [_decode_card(raw) for raw in raw_cards[:limit]]

    async def get_definition(self, tool_name: str) -> CapabilityDefinition | None:
        body = await self._request("GET", f"/tools/{quote(tool_name, safe='')}")
        if body is None:
            return None
        return _decode_definition(body)

    async def hydrate(
        self,
        tool_name: str,
        *,
        query: str,
        raw_arguments: dict[str, Any],
        end_user_jwt: str | None,
    ) -> dict[str, Any] | None:
        body = await self._request(
            "POST",
            f"/tools/{quote(tool_name, safe='')}/hydrate",
            end_user_jwt=end_user_jwt,
            json={"query": query, "raw_arguments": raw_arguments},
        )
        if body is None:
            return None
        return _decode_hydrated_card(body)


def _strings(raw: Any, field: str) -> tuple[str, ...]:
    value = raw.get(field) if isinstance(raw, dict) else None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CapabilityError(f"Malformed capability card field: {field}.")
    return tuple(value[:10])


def _decode_card(raw: Any) -> CapabilityCard:
    if not isinstance(raw, dict):
        raise CapabilityError("Malformed capability card.")
    kind = raw.get("kind")
    required = (raw.get("id"), raw.get("tool_name"), raw.get("summary"))
    if kind not in {"navigation", "data_widget"} or any(
        not isinstance(value, str) or not value for value in required
    ):
        raise CapabilityError("Malformed capability card.")
    return CapabilityCard(
        id=raw["id"],
        tool_name=raw["tool_name"],
        kind=kind,
        summary=raw["summary"],
        matched_questions=_strings(raw, "matched_questions"),
        matched_actions=_strings(raw, "matched_actions"),
        matched_data_points=_strings(raw, "matched_data_points"),
    )


def _optional_string_map(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise CapabilityError("Malformed capability parameter metadata.")
    return dict(value)


def _decode_param(raw: Any) -> ToolParam:
    if not isinstance(raw, dict):
        raise CapabilityError("Malformed capability parameter.")
    if any(not isinstance(raw.get(field), str) for field in ("name", "description", "type")):
        raise CapabilityError("Malformed capability parameter.")
    enum = raw.get("enum")
    if enum is not None and (
        not isinstance(enum, list) or any(not isinstance(item, str) for item in enum)
    ):
        raise CapabilityError("Malformed capability parameter enum.")
    if not isinstance(raw.get("collection"), bool):
        raise CapabilityError("Malformed capability parameter cardinality.")
    default = raw.get("default")
    if default is not None and not isinstance(default, str):
        raise CapabilityError("Malformed capability parameter default.")
    resolution = raw.get("resolution")
    strategy = semantic_type = entity_type = None
    if not isinstance(resolution, dict) or resolution.get("strategy") not in {
        "torch", "resolve_values", "direct", "service"
    }:
        raise CapabilityError("Capability parameter resolution is required.")
    strategy = resolution["strategy"]
    semantic_type = resolution.get("semantic_type")
    entity_type = resolution.get("entity_type")
    if strategy == "resolve_values" and (
        not isinstance(semantic_type, str) or not semantic_type
    ):
        raise CapabilityError("Capability value resolution requires semantic_type.")
    if strategy == "torch" and (not isinstance(entity_type, str) or not entity_type):
        raise CapabilityError("Capability Torch resolution requires entity_type.")
    return ToolParam(
        name=raw["name"],
        description=raw["description"],
        type=raw["type"],
        collection=raw["collection"],
        enum=tuple(enum) if enum is not None else None,
        enum_descriptions=_optional_string_map(raw.get("enumDescriptions")),
        enum_display_names=_optional_string_map(raw.get("enumDisplayNames")),
        default=default,
        resolution_strategy=strategy,
        semantic_type=semantic_type,
        entity_type=entity_type,
    )


def _decode_definition(raw: Any) -> CapabilityDefinition:
    if not isinstance(raw, dict):
        raise CapabilityError("Malformed capability definition.")
    kind, parameters, metadata = raw.get("kind"), raw.get("parameters"), raw.get("metadata")
    if (
        kind not in {"navigation", "data_widget"}
        or not isinstance(parameters, list)
        or not isinstance(metadata, dict)
        or not isinstance(metadata.get("preamble_url"), str)
        or any(
            not isinstance(raw.get(field), str) or not raw[field]
            for field in ("name", "version", "description")
        )
    ):
        raise CapabilityError("Malformed capability definition.")
    return CapabilityDefinition(
        name=raw["name"],
        version=raw["version"],
        kind=kind,
        description=raw["description"],
        parameters=tuple(_decode_param(parameter) for parameter in parameters),
        metadata=dict(metadata),
    )


def _decode_hydrated_card(raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("name"), str)
        or not isinstance(raw.get("arguments"), dict)
        or not isinstance(raw.get("metadata"), dict)
        or not isinstance(raw["metadata"].get("preamble_url"), str)
        or not isinstance(raw.get("parameters"), list)
        or not isinstance(raw.get("next_best_tools"), list)
        or not isinstance(raw.get("are_best_tools_suggestion"), bool)
        or not isinstance(raw.get("resolved_entities"), dict)
        or not isinstance(raw.get("additional_arguments"), dict)
    ):
        raise CapabilityError("Malformed hydrated capability card.")
    return dict(raw)


__all__ = [
    "CapabilityCard",
    "CapabilityClient",
    "CapabilityDefinition",
    "CapabilityError",
    "CapabilityKind",
    "CapabilityPrefetch",
    "CapabilityServiceError",
    "HttpCapabilityClient",
    "ToolParam",
]
