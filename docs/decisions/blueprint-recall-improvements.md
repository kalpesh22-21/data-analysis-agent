# Blueprint recall improvements

Implemented 2026-09-14. Provider-independent; no model-specific branches.

## Approved scope

1. Search each analytical deliverable independently. `searchBlueprints` now accepts
   either the existing `query` string or `deliverables`, an array of up to four focused
   queries. Each part is embedded, retrieved and reranked separately. The agent
   identifies the parts; the harness does not add an LLM decomposition call. Prompts
   tell the agent to include each part's metric, grouping, period and filters.
2. Retrieve blueprints through semantic and keyword channels concurrently. Keyword
   matching searches `intent` and `slots_summary` using a bounded, escaped OR query.
   Both channels retain the corpus lifecycle/trust restrictions and undergo column
   scope filtering before reciprocal rank fusion and reranking. Embedding failure
   can still return keyword matches; an unavailable keyword index preserves semantic
   retrieval. Knowledge retrieval retains its existing behavior.
3. Retrieve a broader pool before reranking. `RETRIEVAL_BLUEPRINT_RECALL_K` defaults
   to 60 candidates per channel, with a configured range of 1–200. The effective
   pool also respects larger existing recall/display settings. Up to 120 candidates
   can reach fusion at the default. Prefetch still displays three blueprint cards;
   explicit searches keep their existing default and maximum display limits.

Example batch:

```json
{
  "deliverables": [
    "Active employee headcount by department",
    "Average annual salary by department"
  ],
  "k": 4
}
```

Supply exactly one of `query` or `deliverables`. Null or blank optional `query`
placeholders are normalized as absent when a batch is supplied; competing nonempty
queries are rejected. For more than four parts, issue
another batch. `k` is a shared budget, raised to at least one slot per part. Slots
are divided fairly, results are interleaved and duplicate cards are displayed once.
`searches` maps one-based input positions to blueprint IDs without echoing query
text. A matching card can serve multiple groups. Empty groups do not borrow slots
from other groups. Preview truncation reports omissions per group and removes
references to cards absent from the preview.

The existing blueprint discovery gate is unchanged. Per-part search is supported
and prompted, not deterministically enforced against inferred user intents.

## Index provisioning

The normal corpus schema/seed path now creates this idempotent index, including
when corpus contents have not changed:

```cypher
CREATE FULLTEXT INDEX blueprint_intent_text IF NOT EXISTS
FOR (b:Blueprint) ON EACH [b.intent, b.slots_summary]
```

Existing deployments need to apply the schema/seed step. The index was created and
confirmed ONLINE on local Neo4j without rebuilding or modifying corpus records.
Missing index failures are observable and degrade to semantic retrieval.

## Validation

- Full repository suite, with local Couchbase integration enabled: **8,234 passed,
  193 skipped** (`/tmp/hybrid-retrieval-suite.xml`).
- Final targeted suite after instruction clarification: **79 passed**.
- Ruff and `git diff --check` passed.
- Twenty-one new test cases cover lexical-only discoveries, independent per-part searches,
  fair budgets, deduplication, scope filtering before reranking, conflicting
  duplicate payloads, embedding/keyword failures, safe keyword syntax, argument
  validation and grouped preview truncation.
- Real Neo4j, embedding and reranking services: three focused searches found the
  expected headcount, salary-average and hires blueprints in their respective
  groups. A six-card budget displayed five unique cards. Private artifact:
  `/tmp/hybrid-retrieval-live.json`.
- Initial backend smoke returned two verified tables with judge approval and no
  ship guard. It also revealed a rejected search containing both argument forms;
  the instructions were clarified to explicitly omit `query` for batches.
  Trace: `8c4726415d7019e99c84465733b7159d`.
- Follow-up exposed an empty optional `query` placeholder, now normalized as absent.
  Final trace `145b828762ce83afccf7f1e97106a40a` confirms a successful batch tool call
  and two separate hybrid retrieval passes, each with 12 semantic and 11 keyword
  candidates before scope filtering. Both resulting tables passed structural
  verification. However, the final judge rejected prose that did not adequately
  distinguish active-employee headcount from salary averages covering all statuses.
  The existing `loop_answer_judge_ship_guarded` path returned both tables with a
  verification hedge after two refusals. This is a successful retrieval integration
  check, **not a clean end-to-end answer pass**. Private artifacts:
  `/tmp/hybrid-backend-probe-validated.json` and
  `/tmp/hybrid-backend-traces-validated.json`. Judge/answer repair was not changed
  in this retrieval task.

The local corpus has only 12 blueprints, so the live checks establish integration
behavior, not a measured recall improvement over the former 30-candidate pool.
Controlled tests demonstrate recovery of candidates absent from semantic results.

## Point 5 — discussed, not implemented

One targeted reformulation would apply when retrieved cards do not cover a named
requirement. For example, after finding current headcount cards for a monthly
hiring request, search once for “monthly new hires by department using hire date.”
The revised query must preserve the original intent and target the missing metric,
grain or period. Inspect the returned definitions; if no blueprint fits, proceed
through normal schema discovery and SQL checks. This is a bounded second search,
not an automatic retry loop or permission to use an adjacent blueprint.
