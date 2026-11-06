"""Narrow correction compatibility and value-free validation diagnostics."""

from __future__ import annotations

import re


def _empty(value):
    # Empty containers and blank strings carry no compatibility payload. Invalid
    # scalars (including 0/False) are not silently treated as empty collections.
    return value is None or (isinstance(value, (list, dict, str)) and not value) or (
        isinstance(value, str) and not value.strip()
    )


def normalize_correction(arguments, declared_fields):
    """Keep only inputs the runtime applies, without changing the model response."""
    # Non-empty compatibility fields carry evidence/coverage semantics, even
    # though they belong inside a document or partial answer, not at top level.
    semantic = {key for key in ("evidence", "unfinished")
                if key in arguments and not _empty(arguments[key])}
    retained = set(declared_fields) | semantic
    dropped = set(arguments) - retained
    result = {key: value for key, value in arguments.items() if key in retained}
    for key in ("feedback", "violation"):
        if result.get(key) is None:
            result[key] = ""
    document = result.get("corrected_document")
    if isinstance(document, dict) and "deliverables" in document:
        # Document corrections replace presentation; the original intent ledger
        # and deliverables remain authoritative in the application path.
        result["corrected_document"] = {key: value for key, value in document.items() if key != "deliverables"}
        dropped.add("deliverables")
    return result, _names(dropped)


def _names(names):
    # Only bounded identifier-shaped names, never arbitrary JSON key strings.
    return ",".join(sorted(
        name for name in names
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name)
    )[:10])


def correction_diagnostic(arguments, schema, reason, schema_path, error=None):
    diagnostic = {"schema_path": schema_path, "schema_keyword": reason, "instance_path": ""}
    guidance = {
        "correction_semantic_fields": "Preserve non-empty evidence in corrected_document.evidence or partial_answer.evidence. Preserve unfinished outcomes in partial_answer.unfinished with approved=false; do not silently remove them.",
        "correction_with_violation": "Approved corrections require empty violation; reject if the issue remains.",
        "correction_count": "Provide exactly one correction and no partial_answer on approval.",
        "correction_repair_type": "Use presentation for corrected_document, prose for corrected_answer or corrected_question.",
        "correction_site": "Use corrected_question only for ask_user; use an answer correction for an answer review.",
        "correction_text": "Provide non-empty plain text without control characters.",
    }
    diagnostic["expected"] = guidance.get(reason, "Return a correction matching the declared schema.")
    if reason != "correction_schema":
        return diagnostic
    kind = next((k for k in ("corrected_document", "corrected_question") if arguments.get(k) is not None), None)
    if kind is None:
        return diagnostic
    if error is None:
        return diagnostic
    # Instance paths contain schema-owned keys and array indexes only. Never echo
    # jsonschema's message: it can embed response values or the whole document.
    trusted = set()

    def collect(node):
        if isinstance(node, dict):
            trusted.update(node.get("properties", {}))
            for item in node.values():
                collect(item)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(schema)
    diagnostic.update(
        schema_keyword=error.validator,
        instance_path="/" + kind + "/" + "/".join(
            str(part) if isinstance(part, int) or part in trusted else "<field>"
            for part in error.absolute_path
        ),
    )
    if error.validator in {"type", "minLength", "maxLength", "minItems", "maxItems"}:
        diagnostic["expected"] = f"{error.validator}={error.validator_value}"
    elif error.validator == "required" and isinstance(error.instance, dict):
        diagnostic["missing_fields"] = _names(set(error.validator_value) - error.instance.keys())
    elif error.validator == "additionalProperties" and isinstance(error.instance, dict):
        diagnostic["unknown_fields"] = _names(error.instance.keys() - error.schema.get("properties", {}).keys())
    return diagnostic
