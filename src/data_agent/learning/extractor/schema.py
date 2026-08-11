"""Forced structured-output schemas for the extractor (D31, + plan §3a).

The extractor's TERMINAL tool is `emit_candidates`, and the turn must end in a call
to it — no free text (D31). `parse_candidates` pulls the `candidates` array out of
the model's tool call and raises `SchemaMismatchError` when the response is not a
well-formed call to that tool, which the extractor retries (retry-on-mismatch, D31).
Field-level SEMANTIC validation (evidence mandatory, D97 totality/roles) lives in
`validation.py`; this module only enforces the transport shape.

One OPTIONAL tool now sits alongside it: `searchCorpus` (plan §3a), a read-only
prior-art lookup the model may call a bounded number of times BEFORE emitting. It is
offered only when a `PriorArtIndex` is wired, so an extractor with no index sees the
exact single-forced-tool shape it always did. Its untrusted arguments are validated
in `prior_art.py`, next to the port whose operations force the requirements — this
module owns the schema the model is shown, not the guard on what comes back.

The tool dict uses the runtime's canonical FLAT function shape
(`{"type":"function","name",...,"parameters"}` — `OpenAIModelClient` translates
it to the Chat Completions nested form), so the same schema drives the real model
and the Layer-1 `ScriptedModelClient` double.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.model.client import ModelTurnResult

from .models import SLOT_TYPES, UNSUPPORTED_SLOT_TYPES

EXTRACTOR_TOOL_NAME = "emit_candidates"

# The slot types the model may emit: the mirror MINUS what this pipeline cannot carry.
# Sorted for a deterministic enum ordering in the emitted tool schema.
#
# The subtraction is the point, and it is NOT the mirror drift that motivated this
# slice. Drift was the mirror silently disagreeing with the runtime about which types
# EXIST; this is the extractor knowingly declining to offer one it cannot generalize
# (see `models.py::UNSUPPORTED_SLOT_TYPES`). The mirror stays at parity — the parity
# test is what makes a future re-enable a one-line change here rather than an
# archaeology exercise.
_SLOT_TYPE_ENUM = sorted(SLOT_TYPES - UNSUPPORTED_SLOT_TYPES)


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
        "table": {
            "type": "string",
            "description": "The fully-qualified 'database.table' the column belongs to.",
        },
        "column": {"type": "string", "description": "The BARE column name (no table prefix)."},
        "value": {"type": "string", "description": "The literal as it appeared in the SQL."},
    },
    "required": ["table", "column", "value"],
}

_SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {
            "type": "string",
            "enum": _SLOT_TYPE_ENUM,
            "description": (
                "Slot semantic type; MUST be exactly one of the enum values "
                f"({', '.join(_SLOT_TYPE_ENUM)}). A named entity such as a department "
                "is best modeled as 'entity'. 'period' is a warehouse pay-period key, "
                "NOT a free calendar date. 'relative_window' is a trailing \"last N "
                "<unit>\" window carried as a BARE WHOLE NUMBER (\"last 6 months\" -> 6; "
                "the unit lives in the SQL, e.g. INTERVAL {n} MONTH) — use it whenever "
                "the SQL expresses a trailing window, rather than falling back to "
                "'period' or 'string'."
            ),
        },
        "binds_to": {
            "type": ["string", "null"],
            "description": (
                "The FULLY-QUALIFIED 'database.table.column' this slot binds to (e.g. "
                "'dbpcm_warehouse.employee.Department') — i.e. locator.table + '.' + "
                "locator.column. NEVER a bare column name; it MUST lie within the "
                "blueprint's uses (the columns the accepted SQL touches). MUST be null "
                "for a 'relative_window' slot ONLY: it carries a plain number rather "
                "than a value drawn from a column's domain, and declaring one is "
                "rejected. Required for every other type."
            ),
        },
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
        "intent": {
            "type": "string",
            "description": (
                "Natural-language, ENTITY-FREE description of what the query does "
                "(no literal values such as 'Sales' or '2025')."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["single", "composite"],
            "description": "'single' for one query; 'composite' for a multi-step plan.",
        },
        "resolves": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                "A JSON OBJECT (map), NOT a list, of {ambiguous_term: "
                "fully-qualified column} — e.g. {\"total salary\": "
                "\"dbpcm_warehouse.employee.AnnualSalary\"}. The aggregated metric "
                "column (e.g. the argument of sum(...)) is NOT a predicate — record it "
                "here, NOT in parameterization."
            ),
        },
        "source_tool_call_refs": {"type": "array", "items": {"type": "string"}},
        "accepted_signal": {"type": "string"},
        "parameterization": {
            "type": "array",
            "items": _PARAM_PLAN_SCHEMA,
            "description": (
                "Exactly one entry per literal predicate in the WHERE / JOIN-ON clause, "
                "each classified slot|rule|inline (no drop). Do NOT add an entry for the "
                "aggregated metric column."
            ),
        },
        "result_signature": {
            "anyOf": [_RESULT_SIGNATURE_SCHEMA, {"type": "null"}],
            "description": (
                "Set ONLY when the accepted SQL has a GROUP BY whose grouped columns "
                "appear in the SELECT output, with grain.columns equal to exactly those "
                "grouped columns. For a single scalar aggregate (e.g. sum(...) with only "
                "a WHERE filter and NO GROUP BY) there is no per-group output to verify — "
                "emit null."
            ),
        },
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
        "payload": {
            "type": "object",
            "description": (
                "Type-specific payload. For type=='blueprint' it MUST be the blueprint "
                "payload (required: intent, kind, source_tool_call_refs, accepted_signal, "
                "parameterization) — see the conditional schema below."
            ),
        },
    },
    "required": ["type", "confidence", "evidence", "rationale", "payload"],
    # Polymorphic payload: only a blueprint candidate's payload is fully specified
    # (required `kind` + FQ slot `binds_to` + slot-type enum). The if/then leaves the
    # other three candidate types' payloads as a generic object (not wrongly rejected).
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "blueprint"}}, "required": ["type"]},
            "then": {"properties": {"payload": _BLUEPRINT_PAYLOAD_SCHEMA}},
        }
    ],
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


# --- the OPTIONAL prior-art lookup tool (plan §3a) ---------------------------------

SEARCH_CORPUS_TOOL_NAME = "searchCorpus"

# The corpora `PriorArtIndex.search` can be asked about. Spelled here rather than
# imported from `priorart.models.PriorArtKind` because a JSON-Schema enum needs a
# concrete list and a `Literal` is not one; `prior_art.py::KNOWN_KINDS` is the runtime
# side of the same closed set and `test_search_corpus_tool` pins the two together.
_SEARCH_KIND_ENUM = ["blueprint", "knowledge"]

_SEARCH_CORPUS_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What to look for, as a short natural-language description of the "
                "ARTIFACT you are considering emitting (e.g. 'headcount by department "
                "as of a pay period'). Entity-free: describe the shape of the question, "
                "not the specific values in it."
            ),
        },
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": _SEARCH_KIND_ENUM},
            "description": (
                "Which corpora to search. Omit to search both. Use ['knowledge'] when "
                "checking whether a fact/definition is already written down, and "
                "['blueprint'] when checking whether a query already exists."
            ),
        },
    },
    "required": ["query"],
}


def build_search_corpus_tool() -> dict[str, Any]:
    """The OPTIONAL prior-art lookup tool, offered alongside `emit_candidates` only
    when a `PriorArtIndex` is wired AND the per-extraction call budget is unspent.

    Deliberately NOT offered when no index is wired: a tool the process cannot service
    is a trap — the model spends a turn on it, gets an apology, and the turn budget
    that should have produced candidates is gone. With no index the extractor's tool
    list is byte-identical to what it was before this slice.

    Returns CARDS ONLY (see `priorart/models.py`), which is why the description says
    so out loud: a model told it can "search the corpus" will otherwise ask for SQL,
    and the whole safety story of the port is that payloads never come back."""
    return {
        "type": "function",
        "name": SEARCH_CORPUS_TOOL_NAME,
        "description": (
            "Search the EXISTING corpus (the MCP canon the agent already recalls, plus "
            "everything the learning loop has landed) for artifacts similar to one you "
            "are considering emitting. Returns summary CARDS only — id, intent, trust "
            "tier, status and match score — never SQL and never payloads. Use it to "
            "avoid re-proposing something that already exists, especially for a second "
            "candidate on a different topic from the one the PRIOR ART block covers. "
            "Optional: emit_candidates without calling this at all if the PRIOR ART "
            "block already answers the question."
        ),
        "parameters": _SEARCH_CORPUS_PARAMETERS,
    }


# For the blueprint payload schema / slot-type enum to be reused by the prompt
# builder + docs (the system prompt states the enum verbatim).
BLUEPRINT_PAYLOAD_SCHEMA = _BLUEPRINT_PAYLOAD_SCHEMA
SLOT_TYPE_ENUM = _SLOT_TYPE_ENUM
SEARCH_KIND_ENUM = _SEARCH_KIND_ENUM


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
