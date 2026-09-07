# Capability Search and Registry API v1

This is the Data Analysis Agent's consumer summary of the CL contract locked on 2026-09-04. Request classification stays local to this repository. CL provides retrieval, authoritative definitions, and Torch-backed UI-card hydration; it never executes a capability.

## Feature flags

The integration is disabled by default:

```text
CAPABILITY_TOOLS_ENABLED=false
CAPABILITY_PREFETCH_ENABLED=false
```

The master flag removes all capability clients, prompts, schemas, prefetch context, and response data. The prefetch flag disables local routing and automatic search while retaining explicit model-driven search and definition loading.

## Search

`POST /v1/capabilities/search`

```json
{
  "query": "Show Jane's employee profile",
  "kinds": ["data_widget", "navigation"],
  "limit": 5,
  "category_limits": {"questions": 5, "actions": 5, "data_points": 5}
}
```

The only tool kinds are `data_widget` and `navigation`. `Action` is searchable evidence returned in `matched_actions`, not a tool kind. Limits are 1–10, queries are at most 2,000 characters, requests are at most 8 KB, and responses are at most 256 KB.

Search results are compact discovery cards without executable schemas. A search-hit-then-GET-404 race is handled by dropping the stale candidate without retrying.

## Definition

`GET /v1/capabilities/tools/{tool_name}`

Every response contains `name`, `version`, `kind`, `description`, `parameters`, and `metadata`. `version` is currently the literal `"1"`. Each parameter includes:

```json
{
  "name": "employees",
  "description": "The employee or employees.",
  "type": "employee",
  "collection": true,
  "enum": null,
  "enumDescriptions": null,
  "enumDisplayNames": null,
  "default": "all"
}
```

`metadata.preamble_url` is required. Navigation definitions carry their render links in `metadata.arguments.links`. Other metadata fields are opaque.

The runtime derives a separate model-facing JSON Schema using these rules:

- Entity category: array of strings.
- Enum: one string constrained to the enum values.
- Boolean: one boolean.
- Date: one date string.
- Date range: one `{start, end}` object.
- `code_induced`: omitted from the model schema.

The authoritative definition is retained separately and is not reduced to the model schema.

## Hydration

`POST /v1/capabilities/tools/{tool_name}/hydrate`

```http
Authorization: Bearer <service-api-key>
X-End-User-Authorization: Bearer <end-user-jwt>
```

```json
{
  "query": "Who is Jane Doe?",
  "raw_arguments": {"employees": ["Jane Doe"]}
}
```

The end-user JWT header is sent only when supplied arguments require entity resolution. It is attached only at this outbound HTTP boundary and must never enter model context, request bodies, tool results, session history, logs, or telemetry.

Hydration returns the bare `ToolData.dump_as_response()` object, not a Kafka or `ResponseMessage` envelope. The runtime validates its outer shape and returns it unchanged in the final `capability_cards` array.

Partial entity resolution is a successful response with `has_unresolved_entities: true`. The UI owns the subsequent picker and CL interaction; it does not resume the agent loop.

## Errors

- `404 TOOL_NOT_FOUND`: unknown or disabled tool; do not retry-loop.
- `401 END_USER_AUTH_REQUIRED`, `END_USER_AUTH_INVALID`, or `ENTITY_AUTH_FAILED`: authentication failure.
- `422 INVALID_ARGUMENTS` or `ENTITY_LIMIT_EXCEEDED`: correct arguments before retrying.
- `413 PAYLOAD_TOO_LARGE`: reduce request size.
- `503 DEPENDENCY_UNAVAILABLE`: transient and retry-safe.

All errors use `{"error":{"code":"...","message":"...","request_id":"..."}}` and must not expose or echo credentials.

## UI boundary

The final agent SSE result adds an optional sibling field:

```json
{
  "assistant_text": "Here is the requested information.",
  "answer_tables": [],
  "capability_cards": [
    {"name": "get_employee_info", "arguments": {}, "metadata": {}}
  ]
}
```

Each entry is one hydrated `ToolData` object. Multiple entries retain model tool-call order. Capability cards may coexist with answer text and tables.
