# Search batching live verification — 2026-11-05

Live DeepSeek V4.1 Flash with warehouse MCP and Neo4j retrieval. Two two-turn sessions; capabilities OFF for headcount and ON for payroll (local capability fixture). Help Center OFF. Required-search mode. Current batching guidance enabled.

The prior battery forced parallel_tool_calls=false and max_tool_calls_per_iteration=1. This run explicitly enabled parallel_tool_calls and allowed four calls per response. This verifies behavior with batching permitted; it is not a controlled guidance-only A/B or a latency benchmark. Runtime dispatch remains sequential; batching saves model round trips, not concurrent backend execution.

## Results

All four turns issued all required searches in their first response. Each waited for those results before blueprint inspection/execution. Each used runBlueprint, emitted schema-valid finalizeAnswer arguments, and received one normal record_judgement approval. No salvage calls.

| Question | Capabilities | Seconds | Trace |
|---|---|---:|---|
| Show headcount by department for Sales and Engineering, including Active and On Leave employees. | OFF | 19.56 | `302a6934ec474ee8e81218533cb5bf06` |
| Refresh that breakdown and add Operations, keeping both Active and On Leave employees. | OFF | 34.92 | `b0f17ee4fe9b1fe6c8514397dd56a3f5` |
| What are total finalized gross earnings and distinct employee count for January 1 through March 31, 2026? Use the payroll register, EARN lines, pay date, and no employee status filter. | ON | 25.79 | `108c0443db56cf9ffc8164fef662bced` |
| Break that finalized gross earnings total down by department for the same pay-date window and population. | ON | 38.09 | `a3f5efedd2c46b5ff5a60e50bf533f99` |

First-response batches: OFF = searchBlueprints + searchKnowledge; ON adds searchCapabilityTools. The prerequisite phase therefore took one model response instead of two or three sequential search responses.

Artifacts: `/tmp/search-batching-live/required_search_workforce.json`, `/tmp/search-batching-live/required_search_payroll.json`. Phoenix project: `search-batching-guidance`.

No production configuration changes, commits, or pushes were made for this check.
