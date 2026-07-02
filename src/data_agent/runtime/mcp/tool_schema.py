"""Model-visible tool schemas — 6 MCP tools (live-fetched) + `askUser` (design §3.2).

Rather than hand-author 6 JSON schemas that can drift from the MCP's own (a
real risk — the MCP is adopted/external, D75), this module fetches
`MCPClient.list_tools()` and translates each `MCPToolSpec.input_schema`
(JSON Schema, produced by FastMCP) into an OpenAI `type: "function"` tool
declaration, passed through **verbatim** — the MCP is the single source of
truth for parameter shape.

`askUser` is the one locally-authored tool (no MCP equivalent — a runtime
control primitive, D6/D45). Neither schema declares `session_id`, `jwt`, or
`scope` as a parameter (D5) — those never appear in any model-visible JSON;
`test_no_credential_params_leak` in
`tests/runtime/mcp/test_tool_schema.py` guards this for every schema this
module can produce, including future MCP tool additions.

Pass-B seam: this module has no dependency on `model/openai_client.py` — it
only produces plain `dict`s in the OpenAI function-tool JSON shape. Pass B's
`ModelClient` implementations consume the output of `ToolSchemaCache.get_schemas()`
directly; no interface here needs to change when that lands.
"""

from __future__ import annotations

from typing import Any

from .client import MCPClient, MCPToolSpec

ASK_USER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "askUser",
    "description": (
        "Pause and ask the user a clarifying question, then resume with their answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "nullable": True},
        },
        "required": ["question"],
    },
}

# `resolveValues` (D77) is the second locally-authored tool — a runtime
# composite (not an MCP tool), intercepted in the agent loop like `askUser` but
# returning an inline tool result. It declares NO session_id/jwt/scope (D5) —
# the client/tenant is applied automatically by the backing runQuery's D5 RLS +
# D57 column-scope. See docs/decisions/resolvevalues-design.md §10.
RESOLVE_VALUES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "resolveValues",
    "description": (
        "Resolve a fuzzy business CONCEPT to the concrete, client-specific values of a "
        "code/category column, ranked by how well each value matches the concept and how "
        "frequently it occurs for THIS client. Use this for client-defined or time-varying "
        "code spaces (e.g. EarnCode, TypeCode, department codes) where the exact codes differ "
        "per client and drift over time — never hardcode such codes. Prefer this over sampleRows "
        "when you need the values that mean a concept (e.g. 'PTO earn codes'), not a raw sample. "
        "Each result has a `score` (0-1); if the top scores are low or clustered (no clear "
        "winner), ask the user to confirm with askUser before filtering on a guessed value. "
        "The result also carries a `degraded` flag and a `ranking` mode: when `ranking` is "
        "'freq_only' (semantic matching was unavailable), the scores reflect how COMMON each "
        "value is for this client, NOT how well it matches your concept — do not treat a high "
        "score as a concept match; prefer askUser to confirm. When `ranking` is 'semantic+freq' "
        "the scores blend concept similarity with frequency as normal. "
        "The client/tenant is applied automatically — do not pass any client identifier."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "description": "Table holding the column, e.g. 'accrual_events' or "
                "'dbpcm_warehouse.accrual_events'.",
            },
            "column": {
                "type": "string",
                "description": "The code/category column to resolve values for, e.g. 'EarnCode'.",
            },
            "concept": {
                "type": "string",
                "description": "The business concept to match, in the user's own words, e.g. "
                "'paid time off' or 'overtime'. Free text — never a code.",
            },
            "period": {
                "type": ["object", "null"],
                "description": "Optional. Restrict to a concrete date window (helps when codes "
                "drift over time). Omit if not needed.",
                "properties": {
                    "column": {
                        "type": "string",
                        "description": "A date/time column on the table to filter on.",
                    },
                    "start": {"type": "string", "description": "Inclusive start (ISO date)."},
                    "end": {"type": "string", "description": "Inclusive end (ISO date)."},
                },
            },
        },
        "required": ["table", "column", "concept"],
    },
}


# The three model-facing knowledge-plane READ tools (read-tools-design §1) —
# runtime-implemented (not MCP tools), intercepted in the agent loop like
# `resolveValues`/`askUser` and returning an inline tool result. None declares
# session_id/jwt/scope (D5) — this user's scope is applied automatically
# (searchBlueprints pre-filters, getBlueprint denies-as-not-found).
SEARCH_BLUEPRINTS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "searchBlueprints",
    "description": (
        "Search the blueprint library for reusable, validated analyses that match an "
        "intent. You are usually GIVEN the 3 most relevant blueprints as thin cards "
        "already — call this only when those 3 miss, or once you have reformulated the "
        "intent in your own words. It re-searches for THIS user's scope and returns more "
        "candidate cards (id, intent, slots summary, score). Then call getBlueprint(id) to "
        "expand the one you pick. A `degraded` flag of true means semantic ranking was "
        "unavailable and the order is weaker — treat scores with less confidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The intent to search for, in your own words. Free text.",
            },
            "k": {
                "type": "integer",
                "description": "Optional. How many cards to return (clamped to a sane maximum). "
                "Omit for a small default.",
            },
        },
        "required": ["query"],
    },
}

GET_BLUEPRINT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "getBlueprint",
    "description": (
        "Expand one blueprint by id (from a thin card or a searchBlueprints result). "
        "Returns the blueprint's intent, the tables/columns it reads (its `uses` "
        "footprint), and its status (validated/drift). Full parameter and SQL detail "
        "arrives with the ability to run it. If the blueprint does not exist or is not "
        "available to you, this returns `found: false` — re-search with searchBlueprints."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The blueprint id, e.g. 'bp-overtime-by-department'.",
            },
        },
        "required": ["id"],
    },
}

SEARCH_KNOWLEDGE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "searchKnowledge",
    "description": (
        "RAG over global, entity-agnostic institutional knowledge and lessons — "
        "definitions, conventions, gotchas (e.g. how overtime is defined, what a pay "
        "period is). This is NOT client data: do not use it to look up a specific "
        "tenant's values (use resolveValues for that). Returns the most relevant "
        "knowledge chunks; a `degraded` flag of true means ranking was weaker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up, in your own words. Free text.",
            },
        },
        "required": ["query"],
    },
}


# `runBlueprint` (runblueprint-design §5.1) — the deterministic fast-path
# execution tool: run one stored, validated blueprint by id with the model's
# raw slot values. The runtime resolves/binds each slot (never the model),
# executes through the runQuery choke point (D57/D64/D5 free), and runs the
# D56 verify gate before returning — a missing/ambiguous slot pauses to ask the
# user; a verify failure falls back to the raw loop. Declares NO
# session_id/jwt/scope (D5) — scope is applied automatically by the inner
# runQuery's RLS + column-scope enforcement.
RUN_BLUEPRINT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "runBlueprint",
    "description": (
        "Execute a stored, validated blueprint (from a getBlueprint result) by id to "
        "answer the user's question the fast, deterministic way. Fill `slot_bindings` "
        "with the raw values from the conversation and the user's own words (use the "
        "blueprint's `slots` and `resolves` to know what each slot means) — the runtime "
        "validates and binds them safely; you never write SQL or codes. If a required "
        "slot is missing or a value is ambiguous, the run pauses to ask the user. Every "
        "result is verified before you see it; if verification fails, answer from the raw "
        "tools instead. The client/tenant is applied automatically — do not pass any "
        "client identifier."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The blueprint id to run, e.g. 'bp-average-salary-by-department'.",
            },
            "slot_bindings": {
                "type": "object",
                "description": "A flat map of {slot_name: raw_value} — the model's best "
                "natural-language read of each slot (a string, number, boolean, or a list "
                "for IN/enum-list slots). The runtime resolves each to a concrete, "
                "scope-safe value. Omit a slot to leave it unfilled (a required one will "
                "pause to ask).",
            },
        },
        "required": ["id", "slot_bindings"],
    },
}


# The locally-authored (runtime-implemented) tool schemas, appended after the
# live-fetched MCP tools. This tuple is the SINGLE source of truth for "these
# names are ours" — the name-collision guard (§6.1) asserts the MCP never
# advertises one of them.
_LOCAL_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    ASK_USER_TOOL_SCHEMA,
    RESOLVE_VALUES_TOOL_SCHEMA,
    SEARCH_BLUEPRINTS_TOOL_SCHEMA,
    GET_BLUEPRINT_TOOL_SCHEMA,
    SEARCH_KNOWLEDGE_TOOL_SCHEMA,
    RUN_BLUEPRINT_TOOL_SCHEMA,
)
_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(s["name"] for s in _LOCAL_TOOL_SCHEMAS)


class ToolNameCollisionError(RuntimeError):
    """A locally-authored tool name collides with an MCP-advertised one (§6.1).

    Raised at schema-fetch (startup), fail-closed and LOUD: were a collision
    allowed, the model would see two schemas with the same name and the loop's
    `runtime_tools` interception would silently SHADOW the MCP tool (or vice
    versa) depending on dispatch order. A config/deploy error surfaced here,
    never a silent runtime ambiguity."""


def translate_tool_spec(tool: MCPToolSpec) -> dict[str, Any]:
    """Translate one `MCPToolSpec` into an OpenAI `type: "function"` declaration."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
    }


async def fetch_function_schemas(
    mcp_client: MCPClient, *, jwt: str, session_id: str
) -> list[dict[str, Any]]:
    """Fetch `list_tools()` from *mcp_client*, translate, and append `askUser`.

    *jwt*/*session_id* are required because the live MCP authenticates every
    request, including `tools/list` — but the tool catalogue itself is
    scope-INDEPENDENT (D5-safe: no credential ever appears in the returned
    schemas, only in the outbound transport headers `list_tools` attaches).

    No caching here — see `ToolSchemaCache` for the cached variant used at
    runtime.
    """
    tools = await mcp_client.list_tools(jwt=jwt, session_id=session_id)
    # §6.1 name-collision guard: the locally-authored tool names MUST be disjoint
    # from the MCP-advertised names. A collision fails LOUD at startup (fail-
    # closed) rather than becoming a silent shadow in the loop's `runtime_tools`
    # interception — the runtime-tool registry is the source of truth for our names.
    mcp_names = {tool.name for tool in tools}
    collisions = mcp_names & _LOCAL_TOOL_NAMES
    if collisions:
        raise ToolNameCollisionError(
            "MCP advertises tool name(s) that collide with locally-authored runtime "
            f"tools: {sorted(collisions)}. Rename the local tool or the MCP tool — a "
            "name collision would silently shadow one of them in the loop."
        )
    schemas = [translate_tool_spec(tool) for tool in tools]
    # The locally-authored runtime tools (askUser + resolveValues + the three read
    # tools + runBlueprint), always advertised, appended after the MCP tools
    # (count 6 → 12).
    schemas.extend(_LOCAL_TOOL_SCHEMAS)
    return schemas


class ToolSchemaCache:
    """Caches the translated tool-schema list, refreshed on explicit reload.

    Startup dependency (design §11 sub-decision C): the first `get_schemas()`
    call requires the MCP to be reachable. Pass B's composition root decides
    the local-dev fallback behavior (e.g. cache-on-disk) — out of scope here.

    Lazy-per-turn-with-cache (2026-07-01 fix): `get_schemas` takes the
    CURRENT turn's `jwt`/`session_id` because the live MCP requires them on
    every request, including `tools/list` — there is no anonymous/startup-time
    introspection call available. But the tool catalogue itself never varies
    by scope (every principal sees the same 6 tools; only per-call *results*
    are scope-filtered by the MCP), so the cache is still keyed on nothing but
    "has a fetch ever succeeded": the FIRST successful `get_schemas()` call —
    made with whichever turn's credentials happens to trigger it — populates
    `self._cache` for every subsequent call, turn, session, and column scope,
    until an explicit `force_reload=True`. Credentials are used ONLY to
    authenticate that one fetch; they are never retained or reflected in the
    cached schemas (D5).
    """

    def __init__(self, mcp_client: MCPClient) -> None:
        self._mcp_client = mcp_client
        self._cache: list[dict[str, Any]] | None = None

    async def get_schemas(
        self, *, jwt: str, session_id: str, force_reload: bool = False
    ) -> list[dict[str, Any]]:
        if self._cache is None or force_reload:
            self._cache = await fetch_function_schemas(
                self._mcp_client, jwt=jwt, session_id=session_id
            )
        return self._cache
