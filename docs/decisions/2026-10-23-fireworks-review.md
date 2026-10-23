# Fireworks HR end-to-end review — 2026-10-23

Model: `accounts/fireworks/models/deepseek-v4p1-flash`, Fireworks Chat Completions.
The API key remained outside both repositories. Tests used synthetic tenant data,
24 accessible employees, 480,000 payroll rows across two tenants, MySQL, ClickHouse
25.3 and Trino 483. Actual model calls, MCP transport, scoped session catalogs,
blueprint execution, review and final SSE delivery were exercised.

## Critical review and fixes

- Typed blueprint dates escaped into receipts as Python objects and broke final SSE
  serialization. Receipts now contain ISO strings; SQL bindings retain their types.
  Expanded date-range slots are no longer incorrectly reported as omitted.
- A valid salvage answer was discarded by the final word-count check. Verbosity
  remains a judge signal, not a veto after approval. Scope, evidence, identifier and
  SQL checks remain enforced. A regression covers delivery over the advisory limit.
- Clarification dictionaries were rejected as Phoenix span attributes. Turn output
  now serializes them as JSON, alongside the dedicated askUser span.
- Nested UNION type inference dropped unknown projection positions. It now preserves
  ordinals and still rejects unknown aggregate inputs.
- Removed unreachable native-ClickHouse column-context compilation. Context is
  disabled without Trino; caller identity and bound client parameters remain intact.
- Private context tests now skip when the optional local query is absent. Shipping
  catalog fingerprints were generated without the excluded accrual_events.yaml edit.
- Corrected the payroll glossary: register categories differ from earning codes, and
  employee benefit deductions are not employer-paid costs. Clarification review now
  checks metric definitions in option labels as well as the question.
- Added concise guidance against speculative exploration and arbitrary clarification
  candidates. Catalog payroll-cost ambiguity now requires an agreed cost basis and
  period. The clarification-repair nudge no longer invites invented defaults. No new
  execution budgets or automatic metric choices were introduced.

The review covered all changed production files, corpus contracts, diagnostics,
manifest changes and associated tests. Recovery preserves candidate evidence before
ship disposition, validates selected successful scoped results, and cannot execute
new queries. Saved approvals are invalidated on changed support or scope; new
possibly relevant evidence requires revalidation. Analysis retains the normal repair
opportunity. Salvage has its own timeout and no transport retries.

## Validation

- Agent: 2,201 tests passed, one skipped in the affected loop, blueprint, tracing,
  app and harness suites.
- Backend shipping state: 630 tests passed, four optional private-context tests skipped.
- Fourteen live ClickHouse/Trino parity cases passed: datetime strings, integer/float/
  decimal epochs, fractional precision, filtered aggregates, windowed argMax and
  nullable derived aggregates, addYears, countDistinct and date_diff.
- Fireworks accepted the non-thinking transport settings and reported zero reasoning
  tokens in the provider probe. A deliberately forced editorial rejection recovered
  the correct 24-employee answer through the real salvage call and emitted a table.

Final answer SQL was replayed through MCP. Independent fixture calculations and
raw MySQL checks established the expected values below.

| Case | Expected / observed |
|---|---|
| Hired during 2025 | 4 employees; blueprint succeeded |
| All employee statuses | 24 employees |
| Employees by department | Three departments, 8 each |
| September payroll earnings | 349,632.00 |
| Top five earning employee codes | E024, E023, E022, E021, E020; 28,529 / 27,315 / 26,101 / 24,887 / 23,673 |
| Top earners with MySQL names | Same ranking and amounts; names matched |
| Earnings by employee department | D01: 106,832; D02: 116,544; D03: 126,256 |
| Accrual beginning balance / average accrued | 1,260 / 24, all accessible records |
| Approved expenses incurred in September | 5,400 |
| Paid-date expense request | Clarification: payment dates absent, incurred-date alternative disclosed |
| Employee detail default | Active/On Leave predicate applied; 20 matching fixture employees |
| Undefined payroll cost | Asked for cost basis; corrected options label gross + employer taxes as partial cost |
| Unmatched Alex lookup | Asked for identity, without arbitrary employee options |
| Employer cost including benefits | Correct earnings subtotal; residual unsupported benefit-data claim (below) |

## Limitations and remaining observations

- This is fixture validation, not a replay against the remote production database.
  Retrieval used the real Neo4j/hydrator pipeline but local embedding/reranker mocks;
  semantic retrieval quality is not established by these runs. Scratch and product
  capabilities were disabled.
- The initial expense failures came from a fixture schema mismatch: its location
  columns were VARCHAR while the production view decodes BLOBs. Matching the fixture
  to production removed the errors; no permissive conversion fallback was added.
- Initial ambiguous requests could over-explore or silently choose gross earnings.
  The catalog clarification rule and prompt guidance improved retests; model behavior
  remains nondeterministic. Runtime information-gap tracking remains separate work.
- Updating catalog fingerprints during the battery invalidated active scope tokens
  and produced SESSION_SCOPE_STALE. Those runs were interrupted and repeated against
  a stable catalog. Catalog changes require fresh scoped sessions; transparent
  recovery from a mid-turn scope change is not implemented here.
- **Remaining correctness failure:** the broad employer-cost stress case took 140.6 s
  and 24 tool calls. It returned the correct 349,632 earnings subtotal, but claimed
  employee-benefit lines existed despite the fixture containing only EARN/DDUCT.
  Added explicit guidance separating catalog definitions from observed rows. The
  exact judge replay still rejected on process narration and repeated that mistaken
  premise in its feedback. This model/judge evidence-comprehension case remains
  unresolved; do not treat the full battery as uniformly correct. A cost-label
  clarification replay did improve after the separate option-definition check.
- The existing review envelope still reports attempts=0 on reviewed outcomes; use
  the judge/recovery trace events for call accounting. This observability issue is
  not fixed by the salvage change.
- Native ClickHouse-only execution bypasses Trino; mixed/MySQL queries exercised the
  Trino route. A payroll-summary blueprint can still introduce an unnecessary employee
  join for a company-only total, although the fixture result remained correct.
- Unsupported dynamic timestamp-scale conversions and unresolved aggregate types
  still fail before dispatch rather than guessing semantics.

`accrual_events.yaml` remains local and is excluded from the release. Private MySQL
view definitions remain ignored. No credential or live test artifact is committed.
