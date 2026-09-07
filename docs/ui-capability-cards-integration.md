# Agent Result Integration for Existing Capability Rendering

| Field | Value |
|---|---|
| Status | Ready for UI integration |
| Date | 2026-09-04 |
| Producer | Data Analysis Agent runtime |
| Consumer | Paycom chat UI |

## What is changing

The Data Analysis Agent is adding one optional field to the existing final SSE `result` event:

```text
capability_cards
```

The UI already knows how to render capability payloads. No new renderer or card format is required. Pass each entry in this array unchanged to the existing capability-rendering path.

## Where to read it

Capability payloads arrive in the existing final result event:

```text
event: result
data: { ...existing agent result fields... }
```

Read:

```javascript
result.capability_cards
```

It is a sibling of `assistant_text` and `answer_tables`. It is not a separate SSE event and is not nested inside either field.

Example:

```text
event: result
data: {"status":"done","assistant_text":"Here is the requested information.","answer_tables":null,"capability_cards":[{"name":"navigate_to_position_management","arguments":{},"metadata":{"preamble_url":"ember:GenericButton","ui_parameters":[]}}]}
```

## How to use it

Normalize the optional field and send every member to the existing capability renderer:

```javascript
const capabilityCards = Array.isArray(result.capability_cards)
  ? result.capability_cards
  : [];

for (const capability of capabilityCards) {
  renderExistingCapability(capability);
}
```

Replace `renderExistingCapability` with the UI's established integration point.

Do not transform or reconstruct each object. Every production array member is the hydrated CL `ToolData.dump_as_response()` payload expected by the existing renderer.

## Presence rules

| Value | UI behavior |
|---|---|
| Missing | Render the response exactly as today. |
| `null` | Render no capabilities. |
| `[]` | Render no capabilities. |
| One entry | Pass it to the existing renderer. |
| Multiple entries | Pass every entry to the renderer in array order. |

The field remains optional because backend capability support is feature-flagged and deployments may be version-skewed.

## Mixed responses

A result can contain text, tables, and capabilities simultaneously:

```json
{
  "status": "done",
  "assistant_text": "Employee counts are grouped below.",
  "answer_tables": [
    {
      "sql": "SELECT employee_status, count() ...",
      "caption": "Employee counts by status",
      "blueprint_use": null,
      "verification": null
    }
  ],
  "capability_cards": [
    {
      "name": "navigate_to_position_management",
      "arguments": {},
      "metadata": {
        "preamble_url": "ember:GenericButton",
        "ui_parameters": [],
        "arguments": {
          "links": [
            {
              "clRedirect": "web.php/positionmanagement/index",
              "webPage": "Position Management",
              "description": "Open Position Management."
            }
          ]
        }
      },
      "next_best_tools": [],
      "are_best_tools_suggestion": false,
      "resolved_entities": {}
    }
  ]
}
```

Continue rendering `assistant_text` and `answer_tables` through their existing paths. Additionally pass every `capability_cards` member through the existing capability path. One output type must not suppress another.

## Important behavior

- Preserve array order.
- Preserve each capability object unchanged, including unknown fields.
- Preserve opaque `next_best_tools[*].echo` values unchanged.
- Do not navigate when a navigation card is received; the existing card remains user-clicked.
- A failure to render one capability must not hide text, tables, or other capabilities.
- Persist capability objects verbatim if final results are stored for history replay.

## Acceptance checks

- Existing responses without `capability_cards` are unchanged.
- One entry reaches the existing renderer unchanged.
- Multiple entries reach it in array order.
- Text and capabilities render together.
- Tables and capabilities render together.
- Text, tables, and multiple capabilities render together.
- Missing, `null`, and empty-array values produce no capability UI and no error.
