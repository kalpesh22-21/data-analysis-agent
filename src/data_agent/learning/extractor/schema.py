"""Forced structured-output schemas for the extractor (D31, + plan §3a).

The TERMINAL tool is `emit_candidates` and the turn must end in a call to it — no free text.
`parse_candidates` raises `SchemaMismatchError` when the response is not a well-formed call,
which the extractor retries; field-level SEMANTIC validation lives in `validation.py`, and
this module only enforces the transport shape. The optional `searchCorpus` tool is offered
only when a `PriorArtIndex` is wired, and its untrusted arguments are validated in
`prior_art.py`. Tool dicts use the runtime's canonical FLAT function shape, so the same
schema drives the real model and the Layer-1 double.
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

_COLUMN_LOCATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "description": "The fully-qualified 'database.table' the column belongs to.",
        },
        "column": {"type": "string", "description": "The BARE column name (no table prefix)."},
        "value": {
            "type": "string",
            "description": (
                "The BARE semantic literal without surrounding SQL quotes. For "
                "employee_status = 'A' emit A; for column != '' emit the empty string."
            ),
        },
    },
    "required": ["table", "column", "value"],
}

_FUNCTION_ARGUMENT_LOCATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"const": "function_argument"},
        "function": {
            "type": "string",
            "enum": ["numbers"],
            "description": "The allowlisted table-source function containing the horizon.",
        },
        "argument_index": {"type": "integer", "const": 0},
        "occurrence": {"type": "integer", "minimum": 0},
        "context": {"type": "string", "const": "table_source"},
        "value": {
            "type": "string",
            "description": "The positive integer argument as authored, without SQL quoting.",
        },
    },
    "required": ["kind", "function", "argument_index", "occurrence", "context", "value"],
}

_LOCATOR_SCHEMA = {
    "description": (
        "A legacy column predicate locator, or the numeric horizon argument of the "
        "allowlisted table-source function numbers(...)."
    ),
    "oneOf": [_COLUMN_LOCATOR_SCHEMA, _FUNCTION_ARGUMENT_LOCATOR_SCHEMA],
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
                '<unit>" window carried as a BARE WHOLE NUMBER ("last 6 months" -> 6; '
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

_COMPOSE_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "order": {"type": "integer"},
        "node_kind": {"type": "string", "enum": ["query"]},
        "step_intent": {"type": "string"},
        "feeds_from": {"type": "array", "items": {"type": "integer"}},
        "consumes": {"type": "object", "additionalProperties": {"type": "string"}},
        "output": {"type": "object", "additionalProperties": {"type": "string"}},
        "source_tool_call_ref": {"type": ["string", "null"]},
        "when": {"type": ["string", "null"]},
        "requires_approval": {"type": ["object", "null"]},
    },
    "required": ["order", "source_tool_call_ref"],
}

_RESULT_SIGNATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "shape": {
            "type": "array",
            "description": (
                "Every selected output column, using the canonical object shape "
                '{"column": <output name>, "type": <output type>}. For example: '
                '{"column": "projected_month", "type": "date"}.'
            ),
            "items": {
                "type": "object",
                "properties": {
                    "column": {
                        "type": "string",
                        "description": "The selected output column or alias.",
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "A non-empty output type label, such as date, number, string, "
                            "dimension, measure, or the database result type when known."
                        ),
                    },
                },
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
                'fully-qualified column} — e.g. {"total salary": '
                '"dbpcm_warehouse.employee.AnnualSalary"}. The aggregated metric '
                "column (e.g. the argument of sum(...)) is NOT a predicate — record it "
                "here, NOT in parameterization."
            ),
        },
        "source_tool_call_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The smallest set of successful calls that directly produced the accepted "
                "artifact; normally only the final answerWithTable SQL designation. Exclude "
                "denied explain attempts, diagnostics, and superseded/intermediate queries."
            ),
        },
        "accepted_signal": {"type": "string"},
        "parameterization": {
            "type": "array",
            "items": _PARAM_PLAN_SCHEMA,
            "description": (
                "Exactly one entry per literal predicate in the WHERE / JOIN-ON clause, "
                "plus a supported function_argument horizon when present; classify each "
                "as slot|rule|inline (no drop). Do NOT add an entry for the "
                "aggregated metric column."
            ),
        },
        "composes": {
            "type": "array",
            "items": _COMPOSE_NODE_SCHEMA,
            "description": (
                "DAG nodes for a true multi-query composite. A SQL statement with WITH/CTEs "
                "is still kind='single'. Use kind='composite' only when multiple independently "
                "executed SQL tool calls feed one another; then emit one node per call."
            ),
        },
        "result_signature": {
            "anyOf": [_RESULT_SIGNATURE_SCHEMA, {"type": "null"}],
            "description": (
                "Set ONLY when the accepted SQL has a GROUP BY whose grouped columns "
                "appear in the SELECT output, with grain.columns equal to exactly those "
                "grouped columns. Every shape item must use the canonical JSON object "
                '{"column": <selected output name>, "type": <output type>}; for example, '
                '{"column": "projected_month", "type": "date"}. For a single scalar '
                "aggregate (e.g. sum(...) with only "
                "a WHERE filter and NO GROUP BY) there is no per-group output to verify — "
                "emit null."
            ),
        },
        "notes": {"type": "string"},
    },
    "required": ["intent", "kind", "source_tool_call_refs", "accepted_signal", "parameterization"],
}

# --- the non-blueprint payloads -----------------------------------------------------
#
# These were an unspecified `{"type": "object"}` until a `global_knowledge` candidate
# arrived carrying {definition, fact_type, intent, scope} — four plausible names, none of
# them the contract's, and nothing on either side of the wire to say so until the approve
# that should have landed it raised on a `statement` that was never sent. The model-side
# schema is the cheapest of the three places to say it (here it costs a re-generation; at
# intake it costs a corrective turn; at landing it costs a human).
#
# The required fields are the ones `validation.py::_PAYLOAD_READERS` enforces, which are
# in turn the ones landing READS — three statements of one contract, kept in step by
# `tests/learning/extractor/test_validation.py`. Every description here is written for a
# model that has already got it wrong once, so it names the alternative it must not use.

_GLOBAL_KNOWLEDGE_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {
            "type": "string",
            "description": (
                "The fact being learned, as one non-empty ENTITY-FREE sentence. This "
                "field is REQUIRED and is the whole text of the landed knowledge chunk — "
                "do NOT name it 'definition', 'fact' or 'intent'."
            ),
        },
        "knowledge_type": {
            "type": "string",
            "description": (
                "The kind of fact, e.g. 'business_rule' or 'definition'. NOT 'fact_type'."
            ),
        },
        "related_terms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Entity-free terms this fact should also be recalled by.",
        },
        "structured": {
            "type": "object",
            "description": "Optional supporting detail, as an object of entity-free strings.",
        },
        "scope": {
            "type": "string",
            "description": "What the fact is about; it titles the landed chunk.",
        },
    },
    "required": ["statement"],
    # CLOSED, and only for this type: a global_knowledge payload lands in the GLOBAL,
    # scope-bypassed knowledge index, and the S5 leakage gate scans exactly statement,
    # structured, related_terms and scope. Any other key is a text surface NOTHING scans,
    # so the model must not be able to invent one (`validation.py::_global_knowledge_payload`
    # rejects it too — this schema only makes the good emission likelier).
    "additionalProperties": False,
}

_USER_KNOWLEDGE_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {
            "type": "string",
            "description": (
                "The per-user fact being remembered, as one non-empty sentence. REQUIRED."
            ),
        },
        "fact_type": {"type": "string", "description": "e.g. 'preference' or 'alias'."},
        "scope": {"type": "string", "description": "The fact's scope; defaults to 'user'."},
        "structured": {"type": "object", "description": "Optional supporting detail."},
    },
    "required": ["statement"],
    # OPEN, unlike global_knowledge: this target is entity-bearing by contract and lands
    # in a per-user store, so an extra key is not a leak. `user_id` in particular is
    # accepted and then IGNORED — the commit is scoped to the session's authenticated
    # user, never to a model-supplied one (R6/D17).
}

_SCHEMA_EDIT_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {
            "type": "string",
            "description": "What the catalog edit changes and why, in one sentence. REQUIRED.",
        },
        "edit_kind": {
            "type": "string",
            "description": "The kind of edit, e.g. 'add_rule'. REQUIRED (alias: edit_type).",
        },
        "target": {
            "type": "object",
            "properties": {"database": {"type": "string"}},
            "description": (
                "What the edit targets: {'database': '<catalog database>'}. REQUIRED "
                "(alias: a top-level 'target_catalog' string)."
            ),
        },
        "patch": {
            "type": "string",
            "description": (
                "The proposed catalog YAML. REQUIRED (alias: proposed_yaml) — an absent "
                "patch opens an EMPTY pull request."
            ),
        },
        "risk": {
            "type": "string",
            "description": "Reviewer-facing risk note; defaults to 'medium'.",
        },
    },
    # `edit_kind`/`target`/`patch` are each satisfiable under a SECOND name the writer
    # also reads (edit_type / target_catalog / proposed_yaml), so JSON Schema `required`
    # can only carry `statement` without wrongly rejecting the aliased shape. The
    # disjunction is enforced at intake instead (`validation.py::_schema_edit_payload`),
    # where a failure is a correctable decline rather than a refused generation.
    "required": ["statement"],
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
                "Type-specific payload; EVERY type has a required shape — see the "
                "conditional schemas below. blueprint: intent, kind, "
                "source_tool_call_refs, accepted_signal, parameterization. "
                "global_knowledge and user_knowledge: 'statement' (never 'definition'). "
                "schema_edit: statement, edit_kind, target, patch."
            ),
        },
    },
    "required": ["type", "confidence", "evidence", "rationale", "payload"],
    # Polymorphic payload: one if/then per candidate type, because the payload's SHAPE is
    # a function of `type` and there is no other way to say so in JSON Schema. The
    # blueprint branch was once the only one and the other three were left as a generic
    # object — which is exactly how a global_knowledge payload with no `statement` was
    # emitted, accepted, reviewed and approved before anything noticed (see
    # `validation.py::_PAYLOAD_READERS` for the full story). A schema is not a guarantee
    # — a model can still emit off-contract, and structured-output support varies by
    # endpoint — so intake re-checks all of this; this is the cheap half of the belt.
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "blueprint"}}, "required": ["type"]},
            "then": {"properties": {"payload": _BLUEPRINT_PAYLOAD_SCHEMA}},
        },
        {
            "if": {
                "properties": {"type": {"const": "global_knowledge"}},
                "required": ["type"],
            },
            "then": {"properties": {"payload": _GLOBAL_KNOWLEDGE_PAYLOAD_SCHEMA}},
        },
        {
            "if": {"properties": {"type": {"const": "user_knowledge"}}, "required": ["type"]},
            "then": {"properties": {"payload": _USER_KNOWLEDGE_PAYLOAD_SCHEMA}},
        },
        {
            "if": {"properties": {"type": {"const": "schema_edit"}}, "required": ["type"]},
            "then": {"properties": {"payload": _SCHEMA_EDIT_PAYLOAD_SCHEMA}},
        },
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
            "predicate of the accepted SQL, plus supported structural horizon entries; "
            "classify each as slot|rule|inline (no drop)."
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
    """The OPTIONAL prior-art lookup tool, offered only with an index wired and budget unspent.

    Deliberately NOT offered when no index is wired: a tool the process cannot service is a trap
    — the model spends a turn on it, gets an apology, and the turn budget that should have
    produced candidates is gone. Returns CARDS ONLY, which the description says out loud,
    because a model told it can "search the corpus" will otherwise ask for SQL.
    """
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
    """Extract the raw `candidates` list from a well-formed `emit_candidates` call.

    Raises `SchemaMismatchError` on any transport-shape violation (no tool call, wrong name,
    missing or non-list `candidates`) so the extractor can retry.
    """
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
