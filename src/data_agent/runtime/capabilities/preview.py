"""Model projection of a prepared card; the renderer retains the full provider object."""

from typing import Any

from data_agent.runtime.sanitize import sanitize_text

from .digest import filter_labels_digest, metadata_data_digest, presentation_label


def capability_preview(card: dict[str, Any]) -> dict[str, Any]:
    evidence = card.get("_agent_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    kind = evidence.get("kind", card.get("kind", "unknown"))
    kind = kind if isinstance(kind, str) and kind in {"navigation", "data_widget"} else "unknown"
    metadata = card.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    arguments = card.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    projected = {
        "prepared": card.get("prepared") is True,
        "kind": kind,
        "data": list(metadata_data_digest(kind, metadata)),
        "presentation": presentation_label(
            kind, preamble_url=metadata.get("preamble_url"), widget_name=metadata.get("widgetName")
        ),
        "filters": list(filter_labels_digest(metadata)),
        "filter_definitions": metadata.get("ui_parameters", []),
        "parameters": card.get("parameters", evidence.get("parameters", [])),
        "arguments": arguments,
        "additional_arguments": card.get("additional_arguments", {}),
        "unresolved_entities": card.get("unresolved_entities", {}),
        "has_unresolved_entities": arguments.get("has_unresolved_entities") is True,
    }
    for key, limit in (
        ("capability_ref", 120),
        ("reused_from_result_id", 200),
        ("next_step", 1600),
    ):
        if isinstance(card.get(key), str):
            projected[key] = sanitize_text(card[key], limit)
    # Provider-resolved identities let the model explain the actual selection.
    # Preserve their structure; callers apply the standard model-preview budget.
    resolved = card.get("resolved_entities")
    if isinstance(resolved, dict):
        projected["resolved_entities"] = resolved
    projected["binding_note"] = (
        "Read arguments, additional_arguments, resolved_entities, unresolved_entities, "
        "parameters and filter_definitions together to determine the prepared selection. "
        "Filter definitions describe supported controls; arguments describe supplied values. "
        "Preparation is not data retrieval. Do not claim filters or employee selection "
        "succeeded unless the available evidence establishes that binding."
    )
    return projected
