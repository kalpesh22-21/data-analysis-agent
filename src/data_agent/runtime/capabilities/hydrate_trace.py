"""Bounded provider-response telemetry. No values or provider-authored key names."""

from typing import Any

from .client import CapabilityServiceError, safe_provider_code

HYDRATE_RESPONSE_EVENT = "capability.hydrate.response"
_PREFIX = "capability.hydrate."


def hydrate_response_attributes(card: dict[str, Any] | None) -> dict[str, Any]:
    facts: dict[str, Any] = {"status": "ok" if card is not None else "not_found"}
    if card is not None:
        metadata = card.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        for key in ("parameters", "next_best_tools", "resolved_entities", "additional_arguments"):
            value = card.get(key)
            facts[key + "_count"] = len(value) if isinstance(value, (list, dict)) else -1
        for key in ("gql", "preamble_url", "ui_parameters", "widgetName"):
            facts["has_" + key] = key in metadata
        arguments = card.get("arguments")
        if isinstance(arguments, dict) and isinstance(
            arguments.get("has_unresolved_entities"), bool
        ):
            facts["has_unresolved_entities"] = arguments["has_unresolved_entities"]
    return {_PREFIX + key: value for key, value in facts.items()}


def record_hydrate_event(
    span: Any, *, card: dict | None = None, error: Exception | None = None
) -> None:
    """Telemetry must never break the call; construct attributes only for recording spans."""
    try:
        if span is None or not span.is_recording():
            return
        if error is None:
            attributes = hydrate_response_attributes(card)
        else:
            attributes = {_PREFIX + "status": "error"}
            if isinstance(error, CapabilityServiceError):
                attributes[_PREFIX + "http_status"] = error.status_code
                attributes[_PREFIX + "provider_error_code"] = safe_provider_code(error.code)
        span.add_event(HYDRATE_RESPONSE_EVENT, attributes=attributes)
    except Exception:
        pass
