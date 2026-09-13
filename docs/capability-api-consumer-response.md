# Capability Search and Registry API — Consumer Response

| Field | Value |
|---|---|
| Status | Consumer confirmation with two required clarifications |
| Date | 2026-09-04 |
| Consumer | Data Analysis Agent runtime |
| Provider | CL IWant Retrieval Layer |
| API base | `/v1/capabilities` |

## Decisions confirmed

We agree with the following ownership boundary:

- The Data Analysis Agent runtime owns request classification, capability selection, and model tool-call logic.
- The CL API owns semantic capability search, authoritative registry lookup, Torch-backed parameter grounding, and production of the existing UI-compatible `ToolData` payload.
- The CL API does not execute navigation, actions, or widgets.
- The Paycom UI renders the returned cards and the user chooses whether to interact with them.
- Version 1 is implicitly scoped to CL. It does not need a `system` request parameter.

We accept the following read-only API surface:

1. `POST /v1/capabilities/search`
2. `GET /v1/capabilities/tools/{tool_name}`
3. `POST /v1/capabilities/tools/{tool_name}/hydrate`

All three endpoints must be retry-safe. Hydration performs entity resolution and card construction; it is not capability execution.

## Corrected taxonomy

We accept that the external tool-kind enum contains exactly:

```text
data_widget
navigation
```

`Action` is a searchable graph-node category and appears in `matched_actions`. It is not a tool kind.

Our local prefetch classifier will continue to use its orchestration categories independently of the registry taxonomy:

| Local classification | Search `kinds` |
|---|---|
| `action_navigation` | `["navigation"]` |
| `data` | `["data_widget"]` |
| `both` | `["navigation", "data_widget"]` |
| `ambiguous` | `["navigation", "data_widget"]` |

The classifier is implemented locally in our repository. No routing endpoint is required from the CL API.

## Search contract confirmation

We accept the proposed search request and response contract, including:

- The query is embedded once by the service.
- `limit` restricts the total returned tool cards.
- `category_limits` independently restricts the matched questions, actions, and data points included with each card.
- Scores remain internal and are not returned to the model.
- Search results contain discovery metadata only and do not contain executable parameter definitions.

We will tolerate a search-hit-then-GET-404 deletion race. An unknown or disabled definition will be dropped from consideration and will not cause an automatic retry loop.

## Definition contract confirmation

We accept the full-fidelity `ToolParam` contract. Every parameter returned by `GET /tools/{tool_name}` must include all seven fields, including explicit `null` values where appropriate:

```json
{
  "name": "employees",
  "description": "The employee or employees.",
  "type": "employee",
  "enum": null,
  "enumDescriptions": null,
  "enumDisplayNames": null,
  "default": "all"
}
```

We also require the complete rendering metadata:

- `metadata.preamble_url` for every tool.
- `metadata.arguments.links` for navigation tools when applicable.

The runtime will retain this definition without reducing its UI fidelity. It may derive a separate model-facing JSON Schema from the definition, but that derived schema will never replace the authoritative definition or hydrated UI payload.

## Hydration contract confirmation

We choose Option A: server-side hydration using the existing CL/Torch grounding implementation.

The runtime will send the model's raw capability arguments to:

```http
POST /v1/capabilities/tools/{tool_name}/hydrate
```

The response must be the exact output of `ToolData.dump_as_response()`, including:

- Full resolved Torch entity objects.
- `arguments.filters` wrapping.
- Flattened `code_induced` arguments.
- `unresolved_entities` and `has_unresolved_entities`.
- `metadata.ui_parameters` with full `ToolParam` fidelity.
- `next_best_tools`, including opaque `echo` payloads.
- `are_best_tools_suggestion`.
- `resolved_entities`.
- Any required `additional_arguments` representation.

The runtime will treat the hydrated response as opaque UI data after validating its outer shape. It will not reconstruct entity objects, enum wrappers, next-best echoes, or other rendering details.

The hydrated cards will be returned alongside normal agent output:

```json
{
  "status": "done",
  "assistant_text": "Here is the requested information.",
  "answer_tables": [],
  "capability_cards": [
    {
      "name": "get_employee_info",
      "arguments": {},
      "metadata": {"ui_parameters": []},
      "next_best_tools": [],
      "are_best_tools_suggestion": false,
      "resolved_entities": {}
    }
  ]
}
```

The UI, not the runtime, executes the user's later interaction with a card. The runtime must not claim that a page was opened or that an action succeeded merely because hydration succeeded.

## Authentication and security

Runtime integration revision (2026-09-13): the per-request end-user JWT now authenticates all capability endpoints. Provider audience compatibility still needs confirmation; see `followup.md`.

- Search and GET use `Authorization: Bearer <end-user-jwt>`.
- Hydrate uses the same Authorization header and adds `X-End-User-Authorization` only for entity resolution.
- The CL service must not log, persist, reflect, or trace the end-user JWT.
- The JWT must not be included in a JSON request body or response.
- The Data Analysis Agent attaches the JWT only at outbound capability HTTP boundaries. It will never place it in model context, tool results, session history, or telemetry.

### Required clarification 1: forwarded JWT header

Please confirm the exact header used to carry the end-user JWT. Our proposed contract is:

```http
Authorization: Bearer <end-user-jwt>
X-End-User-Authorization: Bearer <end-user-jwt>
```

If an existing standard header is already used by the CL service, please specify its exact name and value format. This must be locked before integration so the runtime does not guess at a security-sensitive transport contract.

## Model-facing schema translation

The agent must receive a valid function schema before it can call a selected capability. The runtime will translate the authoritative parameter list into a model-facing JSON Schema while preserving the original definition for hydration and rendering.

Entity categories such as `employee` must remain identifiable as entity categories. The model-facing representation may still use JSON primitives, but the original entity type will be retained as metadata and the raw value will always pass through `/hydrate` before reaching the UI.

### Required clarification 2: parameter cardinality

Please identify the authoritative source of parameter cardinality.

The seven documented `ToolParam` fields do not indicate whether an entity parameter accepts:

- One string
- An array of strings
- Either form

For example, `name: "employees"` and `type: "employee"` suggest multiple values, but cardinality must not be inferred from a plural name or natural-language description.

Preferred resolution: add an explicit field to the definition contract, such as:

```json
{
  "name": "employees",
  "type": "employee",
  "collection": true
}
```

An equivalent established registry field is acceptable. Please document how it maps to the OpenAI-facing JSON Schema and how `/hydrate` expects single- and multi-value arguments to be encoded.

## Unresolved-entity behavior

When hydration returns `has_unresolved_entities: true`, the runtime will return that hydrated `ToolData` to the UI so the existing picker can be rendered.

We need the UI integration owner to confirm how the selected entities return to our application. For v1, we propose treating picker selection and any subsequent `chat.entity_resolution` or `chat.call_tool` exchange as a separate UI/CL interaction, not as a resume of the Data Analysis Agent's original loop.

The `echo` value in `next_best_tools` will be treated as opaque and round-tripped without modification.

## Runtime feature flags

The complete integration remains disabled by default:

```text
CAPABILITY_TOOLS_ENABLED=false
CAPABILITY_PREFETCH_ENABLED=false
```

When `CAPABILITY_TOOLS_ENABLED` is false, the agent must have no capability prompt guidance, search/get/hydrate tools, prefetched cards, hydrated schemas, or capability response fields populated by this feature.

When the master flag is enabled but `CAPABILITY_PREFETCH_ENABLED` is false, explicit model-driven search and hydration remain available, but the runtime performs no automatic classification or capability prefetch.

## Requested provider confirmation

Please confirm the following before the consumer implementation is considered contract-complete:

1. The exact header name and format for the forwarded end-user JWT on `/hydrate`.
2. The authoritative representation of parameter cardinality.
3. That `/hydrate` returns the exact `ToolData.dump_as_response()` object without the outer Kafka `ResponseMessage` envelope.
4. Whether `additional_arguments` is flattened into the dumped response or exposed under its own key.
5. That unknown and disabled tools return the same clean `404` envelope for GET and hydrate.
6. That `next_best_tools[*].echo` can be treated as an opaque JSON value and persisted verbatim when history replay is required.
7. The maximum request and response sizes for search, GET, and hydrate.

Once these points are confirmed, the remaining consumer integration is mechanical and can be validated against captured CL fixtures.
