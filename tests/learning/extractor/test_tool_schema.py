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


def _blueprint_payload_schema() -> dict:
    """The payload schema a blueprint candidate is steered toward — reached via the
    type-conditional `allOf[*].then` in the candidate schema."""
    item = _candidate_item_schema()
    for branch in item.get("allOf", []):
        cond = branch.get("if", {}).get("properties", {}).get("type", {})
        if cond.get("const") == "blueprint":
            return branch["then"]["properties"]["payload"]
    raise AssertionError("no blueprint-conditional payload schema wired into the tool")


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
    # The other three candidate types keep a generic-object payload — the conditional
    # must not impose `kind` on them.
    item = _candidate_item_schema()
    assert item["properties"]["payload"]["type"] == "object"
    assert "required" not in item["properties"]["payload"]
