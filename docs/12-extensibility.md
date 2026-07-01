# 12 — Extensibility: Skills & Lifecycle Hooks

This chapter specifies the extensibility mechanism that lets developers add new capabilities to the
agent without modifying core runtime code — analogous in spirit to extensible CLI/agent frameworks
that expose lifecycle hooks plus registered skills.
The design is **NOT** a plugin marketplace or a third-party extension SDK at launch; it is a
well-defined internal extension surface that locks invariants in place while giving first-party
developers clear hook points and a skill-registration path.

Design constraints are **locked invariants** — each is cross-referenced to the decision that owns it.

---

## Relationship to existing extension axes

The agent already has three extension axes. Skills and hooks are a **fourth** — distinct from the others:

| Axis | What it is | Who adds it | Interaction with model |
|---|---|---|---|
| **Tool (D4)** | Fixed 11-tool inventory; data plane + knowledge plane tools; model calls them by name | Core runtime, never extended by skills | Model emits a tool call; runtime dispatches |
| **Blueprint (D9–D13)** | Learned, parameterized query DAG stored in neo4j; arrives via the learning loop | Learning loop (offline) | Model selects and invokes via `runBlueprint` |
| **Skill (D73)** | Reusable capability registered at startup; selected by the runtime based on intent matching | First-party developers | Model is unaware; runtime selects + invokes |
| **Lifecycle hook (D72)** | Code that runs at a named point in the request or learning-loop lifecycle | First-party developers | Observes / may redirect; model never sees it |

**Key distinction — skill vs. blueprint:** a blueprint is a *data retrieval* pattern (a validated
query DAG over the warehouse). A skill is a *capability* that may involve non-warehouse logic:
formatting, cross-system lookups, specialized analysis, document generation. A skill may internally
call tools, but it does so through the same injected-scope, runtime-dispatch path (D5); it is never
a raw SQL author. A skill can be thought of as a first-class pre-approved "recipe" the runtime
invokes; a blueprint is a pre-approved "query". They can compose: a skill may trigger a blueprint as
part of its execution.

---

## 1. Lifecycle hook points

Mapped to the six-step request lifecycle in [01-architecture.md](01-architecture.md) plus the
offline learning loop:

### Request path hook points

```
1. UI sends request
   ├─► [H1] ON_REQUEST_RECEIVED      reads: {message, scope_id, session_id_hash}; may reject/redirect
2. Scope-filter + trail load
   ├─► [H2] POST_TRAIL_FILTER        reads: filtered trail (no JWT/scope token); read-only
3. Context assembly (embed → recall → rerank → inject)
   ├─► [H3] PRE_CONTEXT_ASSEMBLY     reads: raw question; may inject additional context fragments
   ├─► [H4] POST_RETRIEVAL           reads: reranked thin cards + knowledge hits; may reorder/annotate
4. Agent loop (LLM ↔ tools)
   ├─► [H5] PRE_MODEL_CALL           reads: assembled context (redacted); read-only
   ├─► [H6] POST_MODEL_CALL          reads: model output tokens/tool-call list (redacted); read-only
   ├─► [H7] PRE_TOOL_CALL            reads: tool name + args (post-injection redacted form); may veto
   ├─► [H8] POST_TOOL_CALL           reads: tool name, args, result shape + preview; read-only
   ├─► [H9] PRE_BLUEPRINT_VERIFY     reads: runBlueprint result + grain assertions; may veto (→ fallback)
   ├─► [H10] POST_BLUEPRINT_VERIFY   reads: verify gate outcome; read-only
   ├─► [H11] ON_ASKUSER_PAUSE        reads: pause prompt, reason; read-only
   ├─► [H12] ON_ASKUSER_RESUME       reads: user answer; read-only
5. Turn finalization
   ├─► [H13] POST_TURN               reads: final answer text + result shape; may annotate metadata
6. Session end → learning loop entry
   └─► [H14] ON_SESSION_END          reads: session summary (shape/count, no raw values); read-only
```

### Learning loop hook points

```
Write-router stages (D26/D27):
   ├─► [H15] POST_EXTRACTION         reads: candidate envelope shape + type; read-only
   ├─► [H16] POST_LEAKAGE_GATE       reads: gate outcome (pass/quarantine/reject); read-only
   └─► [H17] POST_CANDIDATE_WRITE    reads: candidate type, target store, dedup outcome; read-only
```

### Hook contracts per point

| Hook | Reads | May mutate | May veto/abort | Notes |
|---|---|---|---|---|
| H1 ON_REQUEST_RECEIVED | message, scope_id, session_id_hash | — | Yes — return `Reject(reason)` for rate-limiting, content policy, etc. | session_id is hashed before delivery to the hook (same as all hook points); scope_id is a hash, not the JWT |
| H2 POST_TRAIL_FILTER | filtered trail (shape + SQL, no raw cell values) | — | No | PII-redacted view |
| H3 PRE_CONTEXT_ASSEMBLY | raw question text | May inject additional context fragments (string, ≤ N tokens, counted against D46 budget) | No | Must declare max-token budget contribution |
| H4 POST_RETRIEVAL | reranked blueprint thin cards + knowledge excerpts | May reorder, annotate, or suppress thin cards | No | Cannot add blueprints not in neo4j |
| H5 PRE_MODEL_CALL | assembled context (counts/shape only per D25 redaction) | — | No | No raw cell values visible |
| H6 POST_MODEL_CALL | model output (redacted: no slot values, no raw tool outputs) | — | No | Token/cost counts available |
| H7 PRE_TOOL_CALL | tool name + redacted args (no JWT/scope/session_id — D5) | — | Yes — return `Veto(reason)`; counts against D47 budget | Veto surfaces as a graceful denial ([06](06-security-and-governance.md) §Graceful denial) |
| H8 POST_TOOL_CALL | tool name, result shape (row count, column names, preview flag), latency | — | No | No raw cell values |
| H9 PRE_BLUEPRINT_VERIFY | runBlueprint result shape + grain-integrity assertion outcomes | — | Yes — force fallback to raw loop | D56 invariant: the verify gate itself always runs first; hook fires after code-computed assertions, before LLM review |
| H10 POST_BLUEPRINT_VERIFY | verify outcome (pass/fail/fallback) | — | No | |
| H11 ON_ASKUSER_PAUSE | pause reason, question text | — | No | |
| H12 ON_ASKUSER_RESUME | user answer text | — | No | |
| H13 POST_TURN | final answer text, result shape metadata | May attach metadata annotations (structured, non-PII) | No | |
| H14 ON_SESSION_END | session summary (turn count, tool-call counts, shape — no entities, D17) | — | No | |
| H15–H17 (learning loop) | candidate type + envelope header (type, confidence, rationale — no entities) | — | No | All read-only to prevent write-path corruption (D1) |

### What hooks may never do

These restrictions are **not advisory** — they are load-bearing invariants:

- **Never receive, hold, log, or forward the JWT, raw scope token, or session_id** (D5). The runtime
  strips these before invoking any hook. A hook that attempts to capture them via closure over the
  request context is a security defect.
- **Never write to the data plane** (ClickHouse). The data path is read-only (D1/D21); a hook that
  issues warehouse writes circumvents the two-plane split.
- **Never write directly to the global knowledge plane** (neo4j blueprints, global knowledge vector
  index). Global writes are the learning loop's job, guarded by the leakage gate and the entity-agnostic
  rule (D17). A hook can annotate metadata on a turn, but cannot inject knowledge.
- **Never bypass column scope or the entity-agnostic constraint.** If a hook triggers a tool call,
  the runtime injects scope into that call as normal (D5). The hook sees only the redacted/shape view
  of results, never raw cell values — so it cannot leak entities into global state even accidentally.
- **Never skip the D56 verification gate** (H9 can force a *fallback*, but not bypass verification).

---

## 2. Skill registration and discovery

A **skill** is a named, versioned capability registered at agent startup. It is not discovered at
runtime, not loaded from a remote registry, and not invoked by the model directly.

### Skill manifest

```yaml
# skills/headcount_report_formatter.yaml
name:        headcount_report_formatter
version:     "1.0.0"
description: >
  Formats a headcount-by-department query result into a structured report
  with a department hierarchy table and a summary paragraph.
when_to_use: >
  Use when the user asks for a formatted headcount report, departmental
  headcount breakdown, or a summary suitable for an HR dashboard.
capability:  format_tabular_result_as_report
triggers:
  intent_patterns:
    - "headcount report"
    - "formatted headcount"
    - "department summary"
  blueprint_ids: []         # if non-empty, skill is auto-triggered after these blueprints
  hook_points: [H13]        # which lifecycle hook(s) this skill wires into
max_latency_ms: 2000        # wall-clock budget; runtime terminates + GUARDRAIL-spans on overrun (D47)
entry_point: skills.headcount_report_formatter:run
schema_version: "1"
```

### Manifest fields

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique slug. Kebab-case. |
| `version` | Yes | SemVer string. Used to pin skills in tests and for compatibility checks. |
| `description` | Yes | One paragraph. Used by the runtime to decide whether to activate this skill. |
| `when_to_use` | Yes | Natural-language guidance used by the runtime's intent-match step. |
| `capability` | Yes | A closed-enum capability tag (`format_tabular_result_as_report`, `cross_system_lookup`, `specialized_analysis`, `document_generation`). Constrains what the runtime will let the skill do. |
| `triggers.intent_patterns` | At least one of `intent_patterns` or `blueprint_ids` | Phrase patterns triggering intent-match consideration. |
| `triggers.blueprint_ids` | Optional | Auto-activate after a specific blueprint completes (H13 hook point). |
| `triggers.hook_points` | Yes | Which H-numbered hook points this skill registers for. Must be a subset of {H1, H3, H4, H7, H9, H13, H14}. Skills cannot register for read-only observational hooks (H2, H5, H6, H8, H10–H12, H15–H17) — those are reserved for monitoring and logging hooks only. |
| `max_latency_ms` | Yes | Maximum wall-clock budget in ms; the runtime terminates a skill that exceeds it and emits a `GUARDRAIL` span (D47). |
| `entry_point` | Yes | Python module path + callable (`module.path:function`). |
| `schema_version` | Yes | Version of the skill manifest schema itself. |

### Discovery and registration

1. At agent startup, the runtime scans the `skills/` directory (configurable path) and loads all
   YAML manifests. Unknown fields → warning, not rejection (forward compatibility).
2. Each manifest is **validated** against the manifest schema and the `entry_point` is resolved (the
   callable is imported but not invoked).
3. Registered skills are indexed by `capability` and `hook_points` for fast dispatch.
4. The runtime logs all registered skills at startup. No skill is registered dynamically at request time.

### Intent matching and selection

The runtime selects a skill when **all** of:
- The turn's intent (a phrase extracted from the user message + context) matches one of the skill's
  `intent_patterns` above a threshold, **or** the most recently executed blueprint is in
  `triggers.blueprint_ids`.
- The skill's registered `hook_points` include the current lifecycle point.
- The skill's `capability` is permitted at this hook point (the runtime enforces a capability ↔
  hook-point whitelist — e.g. `format_tabular_result_as_report` is only permitted at H13/POST_TURN).

Selection is deterministic at each hook point — the runtime evaluates all registered skills and
invokes only those that match. If multiple skills match the same hook point, they run sequentially
in registration order. The model **never participates** in skill selection; skill dispatch is
runtime-only.

---

## 3. Skill invocation interface

### Hook function signature

Every hook entry point — whether registered as a monitoring hook or as a skill — conforms to the
same function signature:

```python
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class HookContext:
    hook_point: str           # e.g. "H13"
    session_id_hash: str      # hash of session_id — NOT the raw session_id (D5)
    scope_id: str             # scope identifier/hash — NOT the JWT (D5)
    turn_index: int
    payload: dict[str, Any]   # hook-point-specific; see table above for contents

@dataclass
class HookResult:
    action: str               # "continue" | "reject" | "veto" | "fallback" | "annotate"
    reason: str | None = None
    annotation: dict[str, Any] | None = None   # only for "annotate" action at H13/POST_TURN

def my_hook(ctx: HookContext) -> HookResult:
    ...
```

The runtime invokes the hook synchronously within the turn. If the hook raises an unhandled
exception, the runtime **logs the exception as a `GUARDRAIL` span** (D23/D74), treats it as
`action="continue"`, and proceeds — a buggy hook must never bring down a live turn. The only
exception: H1 ON_REQUEST_RECEIVED failures are surfaced to the UI as a transient error (the turn
has not yet started, so safe to abort).

### Skill entry point (same signature, richer payload)

Skills registered at H13/POST_TURN receive the following in `payload`:

```python
{
    "final_answer_text": str,          # the agent's prose answer
    "result_shape": {
        "row_count": int,
        "column_names": list[str],
        "truncated": bool
    },
    "blueprint_id": str | None,        # if the fast path ran
    "slot_names": list[str],           # slot names only — NOT values (D5/D25)
    "intent_matched": str | None       # the intent_pattern that triggered this skill
}
```

The skill may return `action="annotate"` with a structured `annotation` dict (non-PII, shape-only)
that the runtime attaches to the session doc's turn record. The skill **may not** modify the answer
text returned to the user in H13 — that would require re-triggering the D56 verify gate, which is
not supported at this hook. A skill that needs to modify the answer should use H9/PRE_BLUEPRINT_VERIFY
to force a fallback and re-run via a different path.

### Skill concurrency and budget

- Skills run **synchronously** within the turn. A skill that blocks (e.g. an expensive external
  lookup) consumes wall-clock time toward the D47 budget cap. Skills must declare a maximum wall-clock
  budget in their manifest (field: `max_latency_ms`); the runtime terminates a skill that exceeds it
  and logs a `GUARDRAIL` span.
- Skills **do not get their own tool-call budget**. If a skill internally triggers a tool call (via
  the runtime's `dispatch_tool` API), that call counts against the turn's D47 iteration budget
  exactly as any model-initiated tool call would.

---

## 4. Observability (D74)

Skills and hooks emit spans consistent with D23/D24. No new span kind is introduced; existing span
kinds are reused:

| Hook/skill activity | Span kind | Key attributes |
|---|---|---|
| Monitoring hook execution | `CHAIN` | `hook.point`, `hook.name`, `action`, latency |
| Skill execution | `CHAIN` | `skill.name`, `skill.version`, `hook.point`, `action`, latency |
| Skill-triggered tool call | `TOOL` | standard tool attributes (D23); parent span = skill `CHAIN` |
| Veto / reject / fallback | `GUARDRAIL` | `hook.name`, `reason`, `action` |
| Unhandled hook exception | `GUARDRAIL` | `hook.name`, `exception.type`, `action=continue` |

**PII posture (D25):** skill/hook spans must follow the same redaction rules as the rest of the
trace — no cell values, no bound slot values, no JWT, no raw scope token. Skills that receive the
`payload` dict must not log raw values from it. The `slot_names` field (not values) is
allowed in span attributes.

---

## 5. Interaction with locked invariants

### D1 — Two-plane split
The hooks at H15–H17 (learning loop) are **read-only** with no mutation permitted. This prevents a
hook from writing entity-bearing data into the global knowledge plane via a side channel. Skills may
not be registered at learning-loop hook points.

### D5 — Injected JWT/scope/session_id
The runtime **never passes** the raw JWT, raw scope token, or raw `session_id` to any hook or skill
invocation. `HookContext` carries only `session_id_hash` and `scope_id` (a hash/identifier — not
the token). Tool calls made by a skill via `dispatch_tool` have their credentials injected by the
runtime at dispatch time, as with all tool calls. A skill that receives a `scope_id` hash has no
path to reconstruct the JWT or forge a wider scope.

### D6 — askUser control-flow primitive
A skill or hook may **not** call `askUser` directly. The `askUser` primitive is a runtime control-flow
construct; invoking it from hook/skill code would create a nested pause that the D45 checkpoint model
does not cover. If a skill needs clarification, it should return `action="veto"` at H7/PRE_TOOL_CALL
with a reason string; the runtime surfaces this via the standard graceful-denial path, and the model
may then emit an `askUser` tool call in the next loop iteration. The D47 budget cap and D55 "continue
grants a fresh window" semantics are unaffected — skill execution consumes wall-clock and iterations
from the existing budget.

### D17/D58 — Entity-agnostic governance
Skills operate on shape/count/SQL data, never on raw cell values (enforced by the `HookContext`
payload design). If a skill annotates a turn record (H13 action="annotate"), the annotation is
structural metadata; it never enters the global knowledge vector index or the blueprint store. If
a skill's output enters the learning loop as a candidate (possible if it touches a session that later
closes), the standard leakage gate (D58) and human pre-gate (D58a) apply — the skill is not exempt.

### D56 — Mandatory verification gate
H9/PRE_BLUEPRINT_VERIFY fires **after** the code-computed grain-integrity assertions and before the
LLM review step. A skill at H9 may force `action="fallback"`, which routes the turn to the raw agent
loop — but it cannot **bypass** verification entirely. There is no hook point between the raw
code-computed assertion and the LLM review; both always run.

### Column-scope and entity-agnostic governance ([06](06-security-and-governance.md))
A skill may not receive or expose raw column values from out-of-scope tables (the payload redaction
prevents this). Any tool call a skill triggers is scope-enforced by the MCP in the normal way (D57).
A skill that triggers `resolveValues` has the `ClientCode`/scope injected by the runtime (D5/D66 — implemented as a runtime composite per D77) —
it cannot supply its own scope.

---

## 6. Skill security and trust model (PROPOSED)

The launch trust model is **first-party only**: skills are registered by the same team that ships
the agent runtime, live in the same repository, and are reviewed as code PRs. There is no sandboxing
boundary between a skill and the runtime process at this stage.

Open questions about extending this — third-party skills, sandboxing, capability grants — are listed
below. These are **deliberately deferred** until the first-party model is proven.

---

**Status:** Partial (design agreed; interfaces PROPOSED — not yet locked; sandboxing + trust model open)

**Open questions:**
- **Sandboxing model for skills.** At launch, skills are trusted first-party code running in-process.
  If/when third-party skills are considered, what sandboxing boundary is used (subprocess, WASM, gVisor)?
  What is the security review process for a new skill author?
- **Skill versioning and compatibility.** When the `HookContext` payload shape changes (e.g. new
  fields added to H13), how are skill versions gated? Is there a `min_schema_version` check at
  startup, or is the contract purely additive?
- **Permission model for capability tags.** The `capability` enum constrains what a skill can do.
  Who approves new capability tags, and how is the capability↔hook-point whitelist extended?
- **Skill testing conventions.** How are skills tested in the Layer 1 / Layer 2 / Layer 3 pyramid
  ([11-testing.md](11-testing.md))? Should the `docker-compose` Layer 3 stack include a fixture skill
  to exercise the dispatch path?
- **Multiple skills at the same hook point.** The sequential-in-registration-order rule is simple but
  may produce non-deterministic ordering if skills are loaded from a directory (file-system order
  varies). Should there be an explicit `priority` field in the manifest?
- **Skill failure modes and the D47 budget.** If a skill exceeds `max_latency_ms` mid-turn, the
  runtime terminates it and continues. Should the remaining turn budget be reduced by the consumed
  wall-clock, or reset? (Current answer: consumed — consistent with D47's "never a silent infinite
  loop" intent.)
