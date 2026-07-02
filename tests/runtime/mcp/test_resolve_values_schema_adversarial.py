"""Adversarial schema-shape checks for RESOLVE_VALUES_TOOL_SCHEMA (D77 §10).

Asserts the exact parameter names / required-ness / nested `period` structure
the design doc fixes, and that no credential- or descriptionCol-shaped param
sneaks in — beyond the existing equality/count checks in `test_tool_schema.py`.
"""

from __future__ import annotations

from data_agent.runtime.mcp.tool_schema import RESOLVE_VALUES_TOOL_SCHEMA


def test_top_level_shape() -> None:
    assert RESOLVE_VALUES_TOOL_SCHEMA["type"] == "function"
    assert RESOLVE_VALUES_TOOL_SCHEMA["name"] == "resolveValues"


def test_exact_property_set_and_required() -> None:
    params = RESOLVE_VALUES_TOOL_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"table", "column", "concept", "period"}
    assert params["required"] == ["table", "column", "concept"]
    # period is the only optional param — never required.
    assert "period" not in params["required"]


def test_scalar_params_are_strings() -> None:
    props = RESOLVE_VALUES_TOOL_SCHEMA["parameters"]["properties"]
    assert props["table"]["type"] == "string"
    assert props["column"]["type"] == "string"
    assert props["concept"]["type"] == "string"


def test_period_is_nullable_object_with_column_start_end() -> None:
    period = RESOLVE_VALUES_TOOL_SCHEMA["parameters"]["properties"]["period"]
    # L4: JSON-Schema-valid nullable form (["object","null"]) replaces the
    # non-standard "nullable": true.
    assert period["type"] == ["object", "null"]
    assert "nullable" not in period
    assert set(period["properties"]) == {"column", "start", "end"}
    for key in ("column", "start", "end"):
        assert period["properties"][key]["type"] == "string"


def _all_property_keys(schema: dict) -> set[str]:
    keys: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                keys.update(props)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return keys


def test_no_credential_or_client_params() -> None:
    # Scan PARAMETER KEYS (not the human-readable description text, which is
    # allowed to mention "client/tenant is applied automatically").
    keys_lower = {k.lower() for k in _all_property_keys(RESOLVE_VALUES_TOOL_SCHEMA)}
    for forbidden in (
        "session_id",
        "jwt",
        "column_scope",
        "clientcode",
        "client_id",
        "client",
        "tenant",
        "api_key",
        "scope",
    ):
        assert forbidden not in keys_lower, f"forbidden param key in schema: {forbidden}"


def test_no_description_column_param_leaked() -> None:
    # descriptionCol is discovered by convention (design §2.3), NEVER a param.
    props = RESOLVE_VALUES_TOOL_SCHEMA["parameters"]["properties"]
    assert "descriptionCol" not in props
    assert "description_col" not in props


def test_concept_description_forbids_codes_and_points_to_ask_user() -> None:
    # The tool description carries the accept-vs-clarify guidance (design §4.2).
    desc = RESOLVE_VALUES_TOOL_SCHEMA["description"]
    assert "askUser" in desc
    concept_desc = RESOLVE_VALUES_TOOL_SCHEMA["parameters"]["properties"]["concept"]["description"]
    assert "never a code" in concept_desc.lower()
