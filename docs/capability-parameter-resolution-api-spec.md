# Capability Parameter Resolution API Contract

| Field | Value |
|---|---|
| Status | Proposed extension to Capability API v1 |
| Date | 2026-09-04 |
| Audience | Capability Search & Registry API developers |
| Scope | CL capability definitions and hydration |

## Objective

The Data Analysis Agent must resolve user-supplied capability arguments before calling
`/hydrate`. The capability definition must state the semantic type and resolution strategy
for every parameter. The capability provider does not know or expose the consumer's
warehouse catalog.

Employee and employee-derived entity parameters are resolved by Torch during `/hydrate`.
Other tenant-specific business values are resolved by the consumer's `resolveValues` tool.
Registry enums and primitive values are validated directly. Service-injected parameters
remain hidden from the model.

## Resolution strategies

Every parameter returned by:

```http
GET /v1/capabilities/tools/{tool_name}
```

must include a `resolution` object with one of these strategies:

| Strategy | Use |
|---|---|
| `torch` | Employee and employee-derived entity resolution during `/hydrate` |
| `resolve_values` | Tenant-specific business values that the consumer maps through its catalog |
| `direct` | Registry enums, booleans, dates, date ranges, and other primitives |
| `service` | Service-injected values that are never model-facing |

The consumer must reject an unknown strategy rather than infer behavior.

## Revised parameter contract

```ts
type ParameterResolution =
  | {
      strategy: "torch";
      entity_type: string;
    }
  | {
      strategy: "resolve_values";
      semantic_type: string;
    }
  | {
      strategy: "direct";
    }
  | {
      strategy: "service";
    };

interface ToolParam {
  name: string;
  description: string;
  type: string;
  collection: boolean;
  enum: string[] | null;
  enumDescriptions: Record<string, string> | null;
  enumDisplayNames: Record<string, string> | null;
  default: string | null;
  resolution: ParameterResolution;
}
```

### Resolution fields

| Field | Required | Meaning |
|---|---:|---|
| `strategy` | Always | One of the four closed strategy values |
| `entity_type` | `torch` | Torch entity category expected by `/hydrate` |
| `semantic_type` | `resolve_values` | Stable business type that the consumer maps to its own catalog |

## Employee parameters

Employee and employee-derived entity parameters use `torch`:

```json
{
  "name": "employees",
  "description": "The employee or employees.",
  "type": "employee",
  "collection": true,
  "enum": null,
  "enumDescriptions": null,
  "enumDisplayNames": null,
  "default": "all",
  "resolution": {
    "strategy": "torch",
    "entity_type": "employee"
  }
}
```

The runtime sends the user's employee wording to `/hydrate`. The capability service uses
the forwarded end-user JWT to resolve it through Torch and returns either full Torch
objects or the existing unresolved-entity picker payload. The runtime does not call
`resolveValues` for these parameters.

The provider must explicitly identify every employee-derived type that uses Torch. The
consumer will not infer this behavior from a parameter name.

## Tenant-specific business values

Nonemployee values that refer to tenant-specific stored data use `resolve_values`.
Examples include department, PAF type, PAF reason, position, position seat, pay class,
deduction code, benefit code, earn code, work location, and job code.

```json
{
  "name": "department",
  "description": "The employee's destination department.",
  "type": "department",
  "collection": false,
  "enum": null,
  "enumDescriptions": null,
  "enumDisplayNames": null,
  "default": null,
  "resolution": {
    "strategy": "resolve_values",
    "semantic_type": "department"
  }
}
```

The provider supplies only the semantic type. The consumer owns a resolution registry that
maps that type to its local catalog:

```json
{
  "department": {
    "table": "dbpcm_warehouse.department",
    "column": "department_name"
  }
}
```

The mapping above is consumer-owned and is never returned by the capability API. The
consumer uses it to call `resolveValues` with the user's concept, then sends the selected
stored value, such as `Engineering`, to `/hydrate`. When the semantic type has no local
catalog mapping, or resolution is ambiguous or empty, the consumer asks the user to clarify
or treats the capability as unavailable. It never asks the model to guess a table or column.

## Direct parameters

Registry enums, booleans, dates, and date ranges use `direct`:

```json
{
  "name": "report_period",
  "description": "Pay period to report on.",
  "type": "string",
  "collection": false,
  "enum": ["current", "previous", "ytd"],
  "enumDescriptions": {},
  "enumDisplayNames": {},
  "default": "current",
  "resolution": {
    "strategy": "direct"
  }
}
```

The consumer validates these values against the registry definition and passes them
directly to `/hydrate`.

## Service-injected parameters

`code_induced` parameters use `service`:

```json
{
  "name": "_team_reference",
  "description": "Internal team reference.",
  "type": "code_induced",
  "collection": false,
  "enum": null,
  "enumDescriptions": null,
  "enumDisplayNames": null,
  "default": null,
  "resolution": {
    "strategy": "service"
  }
}
```

These parameters must not appear in the model-facing schema. `/hydrate` must reject them
if they are supplied in `raw_arguments`; the capability service injects them itself.

## Complete GET example

```json
{
  "name": "submit_paf_transaction",
  "version": "1",
  "kind": "data_widget",
  "description": "Create a saved Personnel Action Form draft.",
  "parameters": [
    {
      "name": "employees",
      "description": "The employee or employees.",
      "type": "employee",
      "collection": true,
      "enum": null,
      "enumDescriptions": null,
      "enumDisplayNames": null,
      "default": "all",
      "resolution": {
        "strategy": "torch",
        "entity_type": "employee"
      }
    },
    {
      "name": "paf_type",
      "description": "PAF action type.",
      "type": "paf_type",
      "collection": true,
      "enum": null,
      "enumDescriptions": null,
      "enumDisplayNames": null,
      "default": "all",
      "resolution": {
        "strategy": "resolve_values",
        "semantic_type": "paf_type"
      }
    },
    {
      "name": "paf_reason",
      "description": "PAF reason.",
      "type": "paf_reason",
      "collection": true,
      "enum": null,
      "enumDescriptions": null,
      "enumDisplayNames": null,
      "default": "all",
      "resolution": {
        "strategy": "resolve_values",
        "semantic_type": "paf_reason"
      }
    }
  ],
  "metadata": {
    "preamble_url": "ember:PafCard"
  }
}
```

## Hydrate request

The endpoint shape remains unchanged:

```http
POST /v1/capabilities/tools/submit_paf_transaction/hydrate
Authorization: Bearer <service-api-key>
X-End-User-Authorization: Bearer <end-user-jwt>
```

```json
{
  "query": "Promote Jane Doe because of outstanding performance.",
  "raw_arguments": {
    "employees": ["Jane Doe"],
    "paf_type": ["Promotion"],
    "paf_reason": ["Outstanding Performance"]
  }
}
```

In this request:

- `employees` remains user wording and is resolved by Torch inside `/hydrate`.
- `paf_type` and `paf_reason` have already been resolved by the consumer.
- The user JWT is required because an employee parameter needs Torch.

`/hydrate` remains responsible for producing the final `ToolData.dump_as_response()` UI
contract.

## Provider validation requirements

- Every parameter must carry exactly one resolution strategy.
- `torch` requires a valid `entity_type`.
- `resolve_values` requires a nonempty, stable `semantic_type`.
- `direct` must not include `semantic_type` or `entity_type`.
- `service` must correspond to a non-model-facing parameter.
- Registry enums should use `direct`.
- Unknown and disabled tools retain identical `404 TOOL_NOT_FOUND` behavior.
- Unknown hydrate argument keys retain `422 INVALID_ARGUMENTS` behavior.
- Service-injected arguments remain rejected when supplied by the consumer.
- Already-resolved nonemployee values must be accepted without Torch resolution.
- Employee arguments continue to require the end-user JWT.

## Missing metadata

`resolution` is mandatory for every parameter. The consumer applies no legacy inference or
fallback. A definition with an unannotated parameter is rejected, and a missing local
mapping for a declared `semantic_type` also fails closed. The consumer never guesses a
resolution strategy, warehouse table, or column.

## Acceptance criteria

1. GET returns `resolution` for every parameter.
2. Employee parameters are explicitly marked `torch`.
3. Every tenant-specific nonemployee value includes a stable `semantic_type`; no warehouse
   table or column is exposed by this API.
4. Primitive and registry-enum parameters are marked `direct`.
5. Service-injected parameters are marked `service` and remain absent from model schemas.
6. `/hydrate` accepts consumer-resolved nonemployee values without resolving them again.
7. Existing Torch, error, UI payload, and authentication contracts remain unchanged.
