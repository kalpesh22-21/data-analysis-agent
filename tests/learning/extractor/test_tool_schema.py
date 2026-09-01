"""Layer-1 schema-completeness guard (no LLM) for `build_extractor_tool()`.

A real OpenAI model only sees the tool's JSON schema + the system prompt — it does
NOT see the Python dataclasses or `validation.py`. A prior regression typed each
candidate `payload` as a bare `{"type": "object"}`, so the model was told
"payload = any object", omitted `kind`, and every real emission was rejected as
`malformed_candidate`. These tests assert the emitted tool schema actually carries
the blueprint payload's required `kind`, the slot-type enum, and the FQ `binds_to`
description — so the underspecification can never silently regress.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.models import SLOT_TYPES, UNSUPPORTED_SLOT_TYPES
from data_agent.learning.extractor.schema import (
    EXTRACTOR_TOOL_NAME,
    SLOT_TYPE_ENUM,
    build_extractor_tool,
)

pytestmark = pytest.mark.extractor_tool_schema_complete


def _candidate_item_schema() -> dict:
    tool = build_extractor_tool()
    assert tool["type"] == "function"
    assert tool["name"] == EXTRACTOR_TOOL_NAME
    return tool["parameters"]["properties"]["candidates"]["items"]


def _payload_schema(candidate_type: str) -> dict:
    """The payload schema a *candidate_type* candidate is steered toward — reached via
    the type-conditional `allOf[*].then` in the candidate schema."""
    item = _candidate_item_schema()
    for branch in item.get("allOf", []):
        cond = branch.get("if", {}).get("properties", {}).get("type", {})
        if cond.get("const") == candidate_type:
            return branch["then"]["properties"]["payload"]
    raise AssertionError(f"no {candidate_type}-conditional payload schema wired into the tool")


def _blueprint_payload_schema() -> dict:
    return _payload_schema("blueprint")


def test_blueprint_payload_requires_kind_and_parameterization():
    payload = _blueprint_payload_schema()
    required = set(payload.get("required", []))
    # `kind` is the field real models omitted under the old bare-object schema.
    assert "kind" in required
    assert "parameterization" in required
    assert "accepted_signal" in required
    assert payload["properties"]["kind"]["enum"] == ["single", "composite"]


def test_slot_type_is_constrained_to_the_real_enum():
    payload = _blueprint_payload_schema()
    slot = payload["properties"]["parameterization"]["items"]["properties"]["slot"]
    slot_obj = next(s for s in slot["anyOf"] if s.get("type") == "object")
    assert slot_obj["properties"]["type"]["enum"] == SLOT_TYPE_ENUM
    # In lockstep with the models, MINUS what the pipeline cannot generalize. This was
    # a bare `== set(SLOT_TYPES)` until `period_range` was withdrawn — the enum is now
    # a DERIVED subtraction rather than a copy, so there is still exactly one place to
    # change. See `test_period_range_withdrawn_qa.py` for why the type is withheld.
    assert set(SLOT_TYPE_ENUM) == set(SLOT_TYPES) - set(UNSUPPORTED_SLOT_TYPES)
    assert set(UNSUPPORTED_SLOT_TYPES) <= set(SLOT_TYPES)


def test_slot_binds_to_documents_the_fully_qualified_rule():
    payload = _blueprint_payload_schema()
    slot = payload["properties"]["parameterization"]["items"]["properties"]["slot"]
    slot_obj = next(s for s in slot["anyOf"] if s.get("type") == "object")
    desc = slot_obj["properties"]["binds_to"]["description"].lower()
    assert "database.table.column" in desc
    assert "never a bare" in desc


def test_result_signature_documents_the_scalar_aggregate_null_rule():
    payload = _blueprint_payload_schema()
    desc = payload["properties"]["result_signature"]["description"].lower()
    assert "group by" in desc
    assert "null" in desc


def test_non_blueprint_payload_is_not_forced_to_blueprint_shape():
    # The BASE payload property stays a generic object: the shape is imposed per type by
    # the `allOf` conditionals, so `kind` is never required of a knowledge candidate.
    item = _candidate_item_schema()
    assert item["properties"]["payload"]["type"] == "object"
    assert "required" not in item["properties"]["payload"]
    for ctype in ("global_knowledge", "user_knowledge", "schema_edit"):
        assert "kind" not in set(_payload_schema(ctype).get("required", []))


@pytest.mark.parametrize(
    "candidate_type", ["global_knowledge", "user_knowledge", "schema_edit"]
)
def test_every_non_blueprint_payload_requires_statement(candidate_type):
    """The regression this closes: these three were a bare `{"type": "object"}`, so the
    model was told "payload = any object" and emitted a `global_knowledge` payload keyed
    {definition, fact_type, intent, scope} — which nothing rejected until the approve
    that should have landed it raised on the missing `statement`."""
    payload = _payload_schema(candidate_type)
    assert "statement" in set(payload.get("required", []))
    assert payload["properties"]["statement"]["type"] == "string"


def test_global_knowledge_payload_is_a_closed_key_set():
    """CLOSED for this type alone: it lands in the GLOBAL, scope-bypassed knowledge
    index and the S5 leakage gate scans four named fields, so any other key would be an
    unscanned text surface. The other two targets stay open (entity-bearing by
    contract)."""
    from data_agent.learning.extractor.validation import _GLOBAL_KNOWLEDGE_KEYS

    payload = _payload_schema("global_knowledge")
    assert payload["additionalProperties"] is False
    # The SAME closed set intake enforces — one contract, stated on both sides.
    assert set(payload["properties"]) == set(_GLOBAL_KNOWLEDGE_KEYS)
    for ctype in ("user_knowledge", "schema_edit"):
        assert "additionalProperties" not in _payload_schema(ctype)


def test_schema_edit_payload_names_the_fields_the_pr_bot_reads():
    """`SchemaEditPatch.from_payload` reads each of these under two names, so `required`
    can only carry `statement` — the disjunction is enforced at intake. The DESCRIPTIONS
    still have to name them, or the model has no way to know they are mandatory."""
    payload = _payload_schema("schema_edit")
    properties = payload["properties"]
    assert {"edit_kind", "target", "patch"} <= set(properties)
    for field in ("edit_kind", "target", "patch"):
        assert "REQUIRED" in properties[field]["description"]
