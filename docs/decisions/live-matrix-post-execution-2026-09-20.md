# Live routing matrix after the judge redesign

2026-09-20. Tested the current uncommitted working tree above `2fea7a1`.

## Configuration

- Live Kimi 2.7 Code, using the existing local credentials file and Chat API with reasoning metadata.
- Live local MCP/ClickHouse and live blueprint retrieval through Neo4j, embedding and reranking services.
- Repository mock HTTP services for Help Center and UI capabilities; these results do not validate production service integrations.
- Final answer judge enabled, **30-second timeout**; separate pre-execution measurement model absent.
- Redaction disabled as authorized; no dropped span names. Raw diagnostics remain outside Git.
- Test-only request pacing: 35 seconds; per-turn wall budget: 900 seconds. Retry harness waits 120 seconds between cases after a judge timeout. No production pacing or runtime changes were made during this matrix.

## Results

All seven routes completed after bounded retries. Five warehouse executions across the attempts matched the independently checked fixture population. Every warehouse attempt used the updated blueprint once; none fell back to agent-authored raw SQL. No identical tool request was repeated.

Six latest attempts received a real judge approval. The warehouse + capability retry completed correctly but its judge timed out and the answer shipped under the existing fail-open policy. This is **not an entirely passing quality/availability result**: manual review also found missing access-scope disclosures in approved answers.

| Route | Latest outcome | Judge | Trace |
|---|---|---|---|
| Capability only | Completed | Approved | `9669a25b9947ed5d431f312dba655777` |
| Help Center only | Completed | Approved | `7fa03ac108c6341e9b54b36676ed2631` |
| Warehouse only | Completed | Approved | `47aa22f75db4b2455a740bab640917e8` |
| Warehouse + capability | Completed | Timed out; unreviewed | `3c66132dc23fccd5fe1f1d92a7dac236` |
| Help Center + warehouse | Completed | Approved | `b163c35da82ea6b55e2933301cdefdb8` |
| Out of scope + warehouse | Completed | Approved | `cf18fdc0152903b32b5808daaa79ec0a` |
| Help Center + capability | Completed | Approved | `230f77a4469c4f9502939a37bd7b4829` |

## Findings

1. **The original blueprint/filter failure did not recur.** Omitted department slots were retained as omitted in execution metadata, and the compiled predicate was `TRUE`. The final judge received compiled SQL, bound/omitted slots, catalog rules and result previews. No premature semantic rejection or redundant raw query occurred.
2. **Judge availability is variable at 30 seconds.** The first warehouse-only attempt and the warehouse + capability retry timed out. Other warehouse reviews completed in approximately 4.8, 8.6 and 20.5 seconds. The current timeout was not raised to obtain passes.
3. **A timeout was followed by provider concurrency failures.** Three first-pass mixed cases received provider errors reporting an organization concurrency limit of one. The test issued requests sequentially. Recovery after cooldown is consistent with a timed-out request continuing server-side, but does not prove ownership of the occupied slot. All three affected routes completed on retry.
4. **Access-coverage disclosure remains a judge miss.** At least the approved latest warehouse-only and Help Center + warehouse answers state broad headcounts without explicitly limiting them to records the caller can access. Neither the table caption nor recorded assumptions supplies that qualifier. The judge was given `company_wide_completeness=unknown`. Correct counts do not establish complete organization coverage.
5. **Catalog column selection is wider than necessary.** The headcount brief includes 43 employee-field definitions, because broad catalog prose/join documentation contributes names to selection. An offline candidate retaining executed-query/rule/grain/key fields reduces this to seven, preserves all table rules and the SQL/results, and reduces one brief from 23,884 to 15,209 serialized characters. This candidate was not applied or live-evaluated. Brief size alone does not explain latency: a similarly sized warehouse review also completed quickly.
6. **Mixed scope handling worked.** The weather part used `OUT_OF_SCOPE_REQUEST`, one scope-refusal event was emitted, that intent was blocked, and the warehouse intent completed with its own evidence. Help Center and UI parts also used the expected source routes.

## Trace audit

Across seven initial attempts and four bounded retries, Phoenix contains **327 spans**, **11 `agent.turn` roots**, and **41 model-call spans**, all with connected parentage. These include 38 successful model responses and three provider-error spans. There were 43 model attempts: the two canceled judge requests have no completion span, but both have the closed judge timeout span and turn outcome. This is an explicit instrumentation limitation, not a claim that a canceled call completed.

Initial trace inspection briefly preceded parent-span export. A later independent audit confirmed all 11 roots and zero missing parents. Successful routes include actual retrieval/tool/finalization spans and SSE progress events.

## Initial failures retained for audit

| Attempt | Failure | Trace |
|---|---|---|
| Warehouse only | Judge timeout; correct warehouse result | `138912de43e65333a3e600f6b2cad992` |
| Warehouse + capability | Provider concurrency error before source work | `86e5c2f4dac03f9c0683a492b68d5f79` |
| Help Center + warehouse | Provider concurrency error before source work | `17452dc0e112c43349e300f8aa44204f` |
| Out of scope + warehouse | Provider concurrency error before source work | `06ea31d7061d1c87624c89f924dca5b9` |

## Follow-up candidates

- Narrow field-document selection while preserving the agreed all-rules strategy for referenced tables, then rerun latency/accuracy pairs.
- Add explicit positive/negative coverage-disclosure regressions and strengthen the judge criterion for broad counts with unknown organization completeness.
- Investigate provider cancellation/concurrency recovery and add explicit canceled-model-call telemetry so every attempted call has a trace record.

No production code, timeout setting, credentials, warehouse rows, or access mappings were changed for this rerun. Reports contain shape-level findings and trace identifiers only; prompts, SQL, result payloads and credentials are not copied into this document.
