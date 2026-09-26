# Kimi conversation quality follow-up — 2026-09-26

## Changes

The agent now resolves follow-ups using existing conversation history before asking
another question, carries forward agreed metrics and filters, and uses readable
user-visible SQL aliases with spaces and no underscores. Labels must reflect the
source grain, units, and filters. The judge distinguishes prose-only label repairs
from result-column/caption repairs and incorrect calculations.

Empty results and zeros are limited to the queried accessible population. Relevant
observed date limits accompany the answer; an old latest event does not establish
an ingestion failure or completeness for a requested period. Existing judge evidence
already includes SQL, result previews, catalog grain/rules, and unknown organization-wide
completeness. Additional explicit fields identify ingestion freshness and requested-period
completeness as not established. No date range is inferred from a preview.

The judge now receives up to eight prior user/assistant messages, each capped at
2,000 characters with a truncation flag, using the same scope filtering as the agent.
These are conversational context, not fresh evidence. A bare assistant completion is
preserved as bounded, quoted reference text in the next ephemeral finalization reminder;
the reminder instructs the agent to reuse successful results rather than query again.

No provider pacing, retry, or tool-choice settings were changed. Kimi continues with
its existing `auto` tool choice. Judge-owned prose corrections and the configurable
review budget from the preceding changes are active.

## Readiness diagnosis

The local graph contained 12 blueprints and four knowledge records but no
`:CorpusMeta {id: 'singleton'}` record. `/ready` correctly returned 503. An additive
load of the existing fixture corpus through the normal `load_corpus` path completed
and recorded its content SHA; no corpus garbage collection or warehouse data changes
were performed. `/ready` then returned 200. No application readiness bypass was added.

## Validation

Runtime and UI suites: **4,382 passed, 5 skipped**. Ruff and whitespace checks passed.
New behavioral tests cover previous-turn judge context, scope-safe history filtering,
context bounds, and bare-answer recovery without another warehouse call.

## Live probe results

The initial rerun used session `kimi-quality-6639cbf489b5`.
It used the same five questions; Kimi did not ask the leave clarification, so the
conditional resume was not exercised. These timings include unchanged evaluation
pacing and are not production latency measurements.

| Question | Seconds | Outcome |
|---|---:|---|
| Headcount | 306.72 | Approved; accessible-data qualifier, one judge-owned wording correction |
| Longest tenure | 126.42 | Approved; no accessible active employees, readable output aliases |
| Most leave this month | 580.02 | Rejected after two substantive reviews: wrong leave definition, then an inner join that hid facts |
| What about May? | 485.33 | Stopped for no progress after parse denials; fallback review exhausted |
| Categories | 427.39 | Approved after repair, but also included an incorrect unsolicited claim that May had no approved requests |

The last answer is an observed judge miss, not a successful validation of its May
claim. Independent read-only queries under the same scope returned four approved May
requests, 32 total hours, and the same two-employee 24/8-hour ranking as before.
No model API mitigations were added.

The probe exposed two follow-up changes: preserve event facts when joining optional
employee labels, and improve parse-denial recovery. A live diagnostic query with the
aliases `Rows` and `Hours` was denied by the MCP column-scope parser. Replacing both
with `Request Count` and `Total Hours` returned the expected May results; changing
only one still failed. Local sqlglot accepted the original, so this is a downstream
validation limitation, not proof that spaced aliases are invalid SQL. The agent now
receives descriptive-alias guidance and a parse-denial message explaining that
`explainQuery` uses the same guard, rather than directing it into repeated failures.

A focused follow-up session used the revised guidance and enabled full telemetry
with `OTLP_DISABLE_REDACTION=1` (`effective_llm_hide=False`). It preserved normal access
and answer guards. Its first request, "rank all types of time off", was interpreted as
a category ranking; an additional explicit employee-ranking request disambiguates the
comparison with the original withheld answer.

| Focused request | Seconds | Outcome |
|---|---:|---|
| May time-off categories by approved hours | 415.90 | Approved; Vacation 24 hours, Bereavement 8 hours, total 32 |
| Categories follow-up | 134.53 | Approved; five categories, no clarification or incorrect May absence claim |
| Explicit May employee ranking | 349.92 | Approved; two employees with 24 and 8 hours, unavailable names disclosed, readable labels |

The category result retained raw `earn_code`/`earn_description` labels, so the final
prompt revision explicitly requires aliases for every displayed SELECT field, including
direct source columns. The employee-ranking follow-up runs with that final revision.
Earlier answers remain in the focused session's history; the active system prompt is current.

Private artifacts: `/tmp/kimi-quality-20260926/{conversation,session,oracle}.json`
and `/tmp/kimi-quality-focused-20260926/{conversation,session}.json`. The reusable
runner is `scripts/probe_kimi_conversation.py`; it supports explicit questions and
continuation of completed/stopped probes while refusing to overwrite artifacts.

The final delivered employee SQL was independently re-executed under the same scope:
one table, the same two employee IDs as the original independent ranking, hours 24
and 8, and no underscores in its displayed column labels. The live turn still needed
three SQL attempts (outer-join aggregation rejection, an empty inner join, then
pre-aggregation plus left join) and repeated finalization attempts. This demonstrates
successful recovery and delivery, not elimination of all unnecessary rounds.

Final status: the scoped instructions, judge context, recovery changes, and local
readiness repair are implemented. The focused category, contextual follow-up, and
employee-ranking requests all received judge approval. The full five-question probe
was not rerun from scratch after the last small prompt refinements. Its failures
remain evidence of residual model/reviewer variability, especially unsupported absence
claims after joins and inconsistent first-attempt finalization. No claim of production
latency or universal correctness is made from this fixture run.

Full runtime/UI suite: **4,382 passed, 5 skipped**. Prompt-contract tests were rerun
and passed after the final alias wording change; lint and whitespace checks passed.
The local backend remains on port 8000 with telemetry redaction disabled and a healthy
readiness response. Provider pacing and retry behavior were left unchanged.
