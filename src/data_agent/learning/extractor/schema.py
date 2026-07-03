"""Forced structured-output schema for the extractor (D31).

The extractor gives the model exactly ONE tool (`emit_candidates`) and requires a
call to it — no free text (D31). `parse_candidates` pulls the `candidates` array
out of the model's tool call and raises `SchemaMismatchError` when the response is not
a well-formed call to that tool, which the extractor retries (retry-on-mismatch,
D31). Field-level SEMANTIC validation (evidence mandatory, D97 totality/roles)
lives in `validation.py`; this module only enforces the transport shape.

The tool dict uses the runtime's canonical FLAT function shape
(`{"type":"function","name",...,"parameters"}` — `OpenAIModelClient` translates
it to the Chat Completions nested form), so the same schema drives the real model
and the Layer-1 `ScriptedModelClient` double.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

EXTRACTOR_TOOL_NAME = "emit_candidates"


class SchemaMismatchError(Exception):
    """The model response was not a well-formed `emit_candidates` tool call."""


_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "turn_ref": {"type": "integer"},
        "tool_call_ref": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["turn_ref", "tool_call_ref", "quote"],
}

_LOCATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string"},
        "column": {"type": "string"},
        "value": {"type": "string"},
    },
    "required": ["table", "column", "value"],
}

_SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "binds_to": {"type": "string"},
        "required": {"type": "boolean"},
        "optional_pattern": {"type": ["string", "null"]},
        "enum_values": {"type": ["array", "null"], "items": {"type": "string"}},
    },
    "required": ["name", "type", "binds_to", "required"],
}

_PARAM_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "locator": _LOCATOR_SCHEMA,
        "role": {"type": "string", "enum": ["slot", "rule", "inline"]},
        "slot": {"anyOf": [_SLOT_SCHEMA, {"type": "null"}]},
        "rule_id": {"type": ["string", "null"]},
        "why": {"type": ["string", "null"]},
    },
    "required": ["locator", "role"],
}

_RESULT_SIGNATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "shape": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"column": {"type": "string"}, "type": {"type": "string"}},
                "required": ["column", "type"],
            },
        },
        "grain": {
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"}},
                "verifiable": {"type": "boolean"},
            },
            "required": ["columns", "verifiable"],
        },
        "invariants": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["shape", "grain", "invariants"],
}

_BLUEPRINT_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "kind": {"type": "string", "enum": ["single", "composite"]},
        "resolves": {"type": "object", "additionalProperties": {"type": "string"}},
        "source_tool_call_refs": {"type": "array", "items": {"type": "string"}},
        "accepted_signal": {"type": "string"},
        "parameterization": {"type": "array", "items": _PARAM_PLAN_SCHEMA},
        "result_signature": {"anyOf": [_RESULT_SIGNATURE_SCHEMA, {"type": "null"}]},
        "notes": {"type": "string"},
    },
    "required": ["intent", "kind", "source_tool_call_refs", "accepted_signal", "parameterization"],
}

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["blueprint", "global_knowledge", "user_knowledge", "schema_edit"],
        },
        "confidence": {"type": "number"},
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
        "rationale": {"type": "string"},
        "proposed_action": {"type": "string"},
        "entity_self_check": {
            "type": "object",
            "properties": {
                "contains_entities": {"type": "boolean"},
                "found": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["contains_entities"],
        },
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "payload": {"type": "object"},
    },
    "required": ["type", "confidence", "evidence", "rationale", "payload"],
}

_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {"candidates": {"type": "array", "items": _CANDIDATE_SCHEMA}},
    "required": ["candidates"],
}


def build_extractor_tool() -> dict[str, Any]:
    """The single forced tool the model must call (canonical flat shape)."""
    return {
        "type": "function",
        "name": EXTRACTOR_TOOL_NAME,
        "description": (
            "Emit zero or more typed learning candidates extracted from the accepted "
            "session. Emit a PLAN only — never SQL. Every candidate MUST cite at least "
            "one evidence quote from the session (turn_ref + tool_call_ref). For a "
            "blueprint, parameterization must have exactly one entry per literal "
            "predicate of the accepted SQL, each classified slot|rule|inline (no drop)."
        ),
        "parameters": _PARAMETERS_SCHEMA,
    }


# For the blueprint payload schema to be reused by the prompt builder / docs.
BLUEPRINT_PAYLOAD_SCHEMA = _BLUEPRINT_PAYLOAD_SCHEMA


def parse_candidates(result: ModelTurnResult) -> list[dict[str, Any]]:
    """Extract the raw `candidates` list from a well-formed `emit_candidates`
    call. Raises `SchemaMismatchError` on any transport-shape violation (no tool call,
    wrong name, missing/!list `candidates`) so the extractor can retry."""
    call = next((c for c in result.tool_calls if c.name == EXTRACTOR_TOOL_NAME), None)
    if call is None:
        raise SchemaMismatchError(
            f"model did not call {EXTRACTOR_TOOL_NAME!r} "
            f"(got {[c.name for c in result.tool_calls]!r}, free text: "
            f"{result.assistant_text is not None})"
        )
    arguments = call.arguments
    if not isinstance(arguments, dict) or "candidates" not in arguments:
        raise SchemaMismatchError("tool call missing a 'candidates' object")
    candidates = arguments["candidates"]
    if not isinstance(candidates, list):
        raise SchemaMismatchError("'candidates' is not a list")
    return candidates
