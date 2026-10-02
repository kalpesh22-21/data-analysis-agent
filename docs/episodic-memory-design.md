# Compact episodic memory and context retrieval

**Date:** 2026-10-01  
**Status:** Proposed design for later implementation. No runtime changes are included.  
**Scope:** Working context, private task episodes, retrieval, and progressive detail loading.

## Purpose

Preserve continuity across long conversations without sending the full transcript and every old tool result to the agent or judge. The agent should remember established entities and interpretations, retrieve a previous procedure when asked to repeat it, and return to original evidence when detail matters.

Blueprints provide procedural memory; knowledge provides semantic memory. Episodes record a particular experience: the task, its interpretation, the method used, and the outcome. An episode is neither a reusable global rule nor fresh warehouse evidence.

This proposal complements [Memory & Learning](05-memory-and-learning.md) and [Context & Retrieval](03-context-and-retrieval.md). It does not replace the existing learning/promotion design or change its publication gates. Storing an episode and promoting a lesson into shared knowledge are separate operations.

## Proposed context model

Use three complementary layers, plus referenced source records:

| Layer | Contents | Inclusion rule |
|---|---|---|
| Recent conversation | Current turn and a small window of completed user/assistant turns, with bounded tool summaries | Starting experiment: last 2–3 completed turns, also subject to a token cap |
| Active task state | Intent, resolved entities, scope, accepted interpretations, pending clarifications, unfinished work, and evidence references | Retain while relevant to an active task, irrespective of turn age |
| Episode briefs | Compact records of older completed work or meaningful checkpoints | Retrieve only relevant episodes; bound count and tokens |
| Source records | Original messages, execution records, SQL, resolution receipts, and tool results | Load selected details on demand, subject to current access and availability |

The recent-turn window is a starting policy, not an approved production constant. Large tool payloads must not bypass its token budget merely because their turns are recent. Preserve complete conversational exchanges or explicit links to omitted material rather than leaving orphaned tool messages.

A fact does not expire just because its source turn leaves the window. Conversely, an old episode must not be injected merely because it exists.

Example: turn 1 establishes an employee; turns 2–5 explore payroll; turn 6 asks about accruals. The established employee remains in active task state. Detailed payroll work can leave the recent window and become retrievable episode material.

## Episode boundaries

Create or update an episode at a meaningful task checkpoint:

- An answer or deliverable is completed.
- A task pauses with a specific unresolved question or dependency.
- The user explicitly abandons or replaces a task.
- A long-running task reaches a stable checkpoint before older context is evicted.

Episodes follow tasks, not arbitrary blocks of N turns. One task may span many turns; a turn with independent requests may contribute to multiple episodes. A topic change must not silently mark unfinished work completed.

Keep a stable task identifier, episode identifier, and source-turn links. Continuing the same task produces a new episode revision or checkpoint, not unrelated duplicate memories. Retrieval should normally return the latest valid revision, with earlier revisions retained according to the history policy.

## Episode contents

Most fields come from structured runtime records. Use a model only to produce a concise description and narrative where necessary.

| Field group | Contents and source |
|---|---|
| Identity and ownership | Episode/task/session identifiers, user and tenant scope, revision, timestamps |
| Intent | Requested outcome and source turn references |
| Resolved scope | Entities, population, periods, filters, and their source/confirmation status |
| Interpretation | Accepted definitions, user clarifications, and explicitly marked agent assumptions |
| Procedure | Blueprint identifier/version, query/execution references, resolution receipts, catalog version when available |
| Outcome | Completed, partial, paused, failed, or abandoned; answered and unfinished parts |
| Corrections | Original issue, correction, validating evidence, and current disposition |
| Evidence index | Immutable references, evidence type, observation time, and availability metadata |
| Retrieval text | Short description derived from the above, with no claim of new authority |

Illustrative shape; names and schema are proposals:

```json
{
  "episode_id": "ep_123",
  "task_id": "task_17",
  "revision": 2,
  "source_turns": [3, 4, 5],
  "intent": "Rank employees by PTO earned",
  "resolved_scope": {
    "period": {"value": "2026 Q3", "source_ref": "turn:3"},
    "population": {"value": "active employees", "source_ref": "turn:4"}
  },
  "interpretations": [
    {
      "text": "Earned PTO excludes adjustments",
      "disposition": "user_confirmed",
      "source_ref": "turn:4"
    }
  ],
  "procedure_refs": ["blueprint:example@version"],
  "evidence_refs": ["execution:example", "resolution:example"],
  "outcome": "completed",
  "unfinished": [],
  "corrections": [],
  "summary": "Ranked active employees by earned PTO for Q3 using the confirmed interpretation."
}
```

References in this example are placeholders. Ownership, access metadata, versioning, and observation timestamps belong in the stored envelope even if omitted from a model-facing brief.

Do not copy full result rows into the default brief. If a historical metric is retained, attach its execution reference, observation time, and scope; never present it as a current value by default. Do not store hidden model reasoning, credentials, or authorization tokens.

An approved answer is not proof the user accepted every assumption. Keep user confirmation, agent assumption, judge disposition, and execution success as distinct attributes. A rejected proposal can remain as historical context but cannot become accepted knowledge through summarization.

## Compaction process

1. **Capture the checkpoint.** After persisting the turn/task state, enqueue references to an immutable source snapshot. Capture its version/content hash so concurrent turns cannot change the job's inputs.
2. **Assemble structured facts.** Read intents, clarifications, final delivery, evidence references, execution outcomes, and known corrections. Prefer these over reconstructing state from prose.
3. **Write a bounded narrative.** If needed, ask a model for a short description and retrieval text. Treat source messages and tool content as data, not instructions to the summarizer.
4. **Validate the candidate.** Resolve references; check task/owner boundaries; reject unsupported identifiers, confirmation claims, or outcome changes. Model-authored narrative is a retrieval aid, not additional evidence.
5. **Persist atomically.** Store the episode revision and update its retrieval index. Make retries idempotent using task/checkpoint identity and the source version.
6. **Mark the checkpoint covered.** Only after successful persistence may its older details be dropped from assembled working context. Original records remain available under their retention policy.

Run compaction asynchronously after answer delivery. It must not consume the answer judge's reserved budget or delay the user-facing response. If summarization fails, preserve the structured checkpoint and source references; do not publish an unvalidated narrative or delete history. Until a checkpoint is covered, use bounded active state and source references rather than silently losing the task's meaning.

Update episodes from source records, not by repeatedly summarizing summaries. Explicitly supersede corrected facts so obsolete and corrected interpretations do not appear as equally current.

## Retrieval and context assembly

Before the agent's next model call:

1. **Load active state and recent conversation.** Resolve explicit continuation links first.
2. **Build a retrieval request.** Use the new question plus active intent, resolved entities, topic, period, and follow-up references. A short message such as “same for last quarter” needs its active task context.
3. **Filter by ownership and current access.** Apply tenant/user boundaries before exposing candidates to a model or reranker. Recheck evidence visibility before constructing the brief.
4. **Retrieve candidates.** Combine semantic similarity with exact identifiers, entity/task matches, and recency. Do not use recency as a substitute for relevance.
5. **Prioritize and deduplicate.** An explicitly linked prior task outranks an unrelated semantic match. Prefer current revisions and avoid repeating facts already in active state or recent conversation.
6. **Build bounded briefs.** Include episode identifiers, scope, accepted interpretations, procedure/evidence references, outcome, and limitations. Omit unrelated history.
7. **Assemble context.** Put current instructions and active state ahead of historical memory. Label episodes as historical context with timestamps and dispositions.

Do not inject every episode, or maintain one ever-growing history summary. If “same report” could refer to several tasks, surface that ambiguity rather than selecting a plausible but unsupported target.

Example brief:

> Relevant prior task — ep_123, revision 2: ranked active employees by earned PTO for Q3, excluding adjustments. The user confirmed the interpretation in turn 4. Procedure and evidence references are available. Historical results have not been refreshed.

Retrieving a brief does not automatically overwrite active task state. Carry a prior fact forward only when it remains applicable to the current request, and preserve its source. Current explicit user instructions supersede historical preferences and interpretations.

## Loading details back into context

Provide a model-callable tool, provisionally:

```text
getEpisodeDetails(
  episode_id="ep_123",
  revision=2,
  sections=["procedure", "clarifications", "evidence"]
)
```

The runtime resolves the identifier; the model does not supply arbitrary storage paths or an authorization scope. Authenticate ownership and current access again on every request.

Return selected sections as a normal tool response, bounded by token/row limits. Include reference identifiers, timestamps, dispositions, and availability states. Large evidence sections should return an index and continuation mechanism rather than unbounded payloads.

| Availability | Behavior |
|---|---|
| Available and permitted | Return the requested detail with its source reference |
| Historical or stale | Return it clearly labeled; refresh if the current claim requires fresh data |
| Missing or expired | State that the source cannot be loaded; do not imply the warehouse data is absent |
| Access no longer permitted | Withhold the material under existing access-error conventions; summaries must not bypass the same restriction |
| Superseded | Identify the current revision and the correction relationship |

If automatic retrieval misses a previous task, the agent also needs a bounded search entry point. Either expose `searchEpisodes(query, ...)` or support a model-requested second retrieval pass. This interface choice is still open; the agent must not be limited to whichever briefs happened to fit the initial context.

For a follow-up such as “do the same for last quarter,” retrieve the prior procedure and interpretation, change the requested period, and obtain evidence for that period. Reuse of an old result is appropriate only when the user asks about that historical result and the referenced evidence remains available and permitted.

## Judge context

The judge receives the current request, relevant clarifications/active state, proposed delivery, and evidence supporting its claims. Include only episode details necessary to understand the follow-up or verify a correction.

An episode summary is not final-answer evidence. A historical factual claim must resolve to its original eligible evidence; a current factual claim needs evidence appropriate to its time and scope. Retain compact value-resolution receipts where they explain literal codes or other query choices.

Keep answer review and memory compaction as separate jobs with separate budgets. A judge timeout must not be converted into an approval when recording the episode.

## Access, retention, and learning

Start with private user-and-tenant scoped episodes, preferably within the current conversation for the initial rollout. Cross-session recall is a separately enabled extension using the same ownership and access checks.

Episode text itself may contain facts derived from restricted evidence. Filter or withhold the affected episode content when access changes; checking only the detail-loading tool is insufficient. If safe partial filtering is uncertain, omit that brief and fall back to permitted context.

Evidence reference lifetime, source retention, episode expiry, and deletion must have a coordinated policy. Deleting a source or user history should invalidate affected episode content and retrieval index entries according to that policy. An episode must not become an undeletable copy of removed data.

A validated episode can later feed the existing learning pipeline as a candidate source. It must not automatically publish a global rule, rewrite a blueprint, or convert a single judge rejection into durable guidance. Explicit reusable user preferences belong in preference memory with their own provenance; an episode may link to them rather than accumulating duplicate copies.

## Implementation sequence

1. **Contracts and active state:** define task identity, episode schema, source references, outcome/disposition semantics, and versioning. Preserve established entities independently of the recent-turn window.
2. **Checkpoint storage:** add idempotent background compaction and a structured-only fallback. Initially collect episodes without injecting them.
3. **Read path:** retrieve conversation-scoped briefs and add bounded detail loading. Compare against full-context behavior on recorded conversations.
4. **Judge integration:** pass only relevant historical context and resolve evidence references through existing eligibility checks.
5. **Rollout:** enable gradually behind flags; add cross-session retrieval only after access, retention, and relevance tests pass.

Proposed controls: episode write/read flags; recent-turn and token limits; episode count/token limits; detail-response limits; compaction timeout/token budget; cross-session recall flag; retention policy. Names and values remain implementation decisions. Start experiments at 2–3 recent completed turns; tune using measured continuity and context size rather than treating N as fixed architecture.

## Acceptance scenarios

- A resolved employee from turn 1 survives beyond the recent window and is not needlessly re-asked about.
- “Same report for last quarter” recovers the correct procedure and interpretation and refreshes the data.
- Two similar prior reports do not cause silent scope substitution.
- A task spanning many turns produces coherent checkpoints; a multi-intent turn preserves separate outcomes.
- A user correction supersedes an earlier assumption without erasing its history or inventing confirmation.
- A rejected answer remains rejected in memory; its text is not promoted to evidence.
- Judge briefs contain original supporting records/receipts, not duplicated transcripts or summaries offered as proof.
- Missing/expired evidence triggers a clear limitation or refresh, not a claim that data is absent.
- Narrowed access blocks both episode-derived facts and detail retrieval; another user's episodes are inaccessible.
- Duplicate jobs, concurrent turns, failed compaction, and index lag do not lose active state or publish duplicate/currently stale revisions.
- Source deletion invalidates related memory/index content; no historical credential material is retained.
- Long conversations reduce agent/judge context size without reducing supported follow-up accuracy.

Measure tokens and latency separately for agent, judge, and compaction; retrieval relevance; unnecessary clarification rate; wrong-scope carryover; repeated failed attempts; evidence-refresh behavior; and supported-answer quality. Do not treat smaller prompts alone as success.

## Open decisions

- Storage/index placement and transaction boundary with existing session records.
- Stable task identity and episode split/merge rules for interleaved requests.
- Exact brief schema, token budgets, and source/detail pagination contracts.
- Semantic/exact-match ranking weights and the model-requested search interface.
- Retention/deletion rules and how unavailable source records affect an episode's useful lifetime.
- Rollout criteria for cross-session memory and conflict handling with explicit preference memory.

The agreed direction is a hybrid: recent turns for conversational continuity, active state for facts that must survive, and compact task episodes with a reliable path back to original detail. These open decisions should be resolved before implementation.
