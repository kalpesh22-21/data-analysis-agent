"""QA5 Layer-1: the `runBlueprint` advertised-schema contract (runblueprint-design
§5.1 / D5). The count (12) and the name-collision guard are pinned in
`test_tool_schema.py` / `test_tool_schema_qa4.py`; this file pins the SHAPE
invariants of the schema the model actually sees:

  - both `id` AND `slot_bindings` are REQUIRED (a partial call is a schema error,
    not a silent default);
  - the schema declares NO credential parameter (session_id / jwt / scope / client /
    tenant) — D5: the runtime injects scope; the model must never pass it;
  - `slot_bindings` is typed as an object (the flat {name: raw} map, §3.1).
"""

from __future__ import annotations

from data_agent.runtime.mcp.tool_schema import (
    _LOCAL_TOOL_NAMES,
    RUN_BLUEPRINT_TOOL_SCHEMA,
)

_FORBIDDEN_PARAM_SUBSTRINGS = ("session", "jwt", "scope", "client", "tenant", "credential")


def test_run_blueprint_schema_requires_id_and_slot_bindings() -> None:
    params = RUN_BLUEPRINT_TOOL_SCHEMA["parameters"]
    assert set(params["required"]) == {"id", "slot_bindings"}
    assert params["properties"]["id"]["type"] == "string"
    assert params["properties"]["slot_bindings"]["type"] == "object"


def test_run_blueprint_schema_declares_no_credential_param_d5() -> None:
    # D5: no session_id / jwt / scope / client / tenant surface — the runtime applies
    # the client + scope automatically; the model has no way to spoof it.
    props = RUN_BLUEPRINT_TOOL_SCHEMA["parameters"]["properties"]
    for prop_name in props:
        lowered = prop_name.lower()
        for forbidden in _FORBIDDEN_PARAM_SUBSTRINGS:
            assert forbidden not in lowered, f"schema exposes a credential-ish param {prop_name!r}"


def test_run_blueprint_schema_function_name_matches_registry_name() -> None:
    # The advertised name must be exactly the registry-interception name — otherwise
    # the loop would dispatch it to the MCP under a mismatched name.
    assert RUN_BLUEPRINT_TOOL_SCHEMA["name"] == "runBlueprint"
    assert RUN_BLUEPRINT_TOOL_SCHEMA["type"] == "function"
    assert "runBlueprint" in _LOCAL_TOOL_NAMES  # the collision-guard source of truth
