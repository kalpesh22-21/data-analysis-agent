# Single post-execution semantic review

2026-09-20. Implements the agreed judge redesign on top of `2fea7a1`, alongside
existing uncommitted B8, warehouse safety and blueprint work.

## Execution and evidence

- Deterministic access, read-only SQL, structural blueprint and join/cardinality
  checks run before analytical execution. The separate measurement model call and
  `MEASUREMENT_REVIEW_ENABLED` setting are removed.
- `ANSWER_JUDGE_ENABLED` controls semantic review at finalization. Its opt-in default
  is unchanged: deployments wanting semantic review must enable it. The judge sees
  actual executed SQL, bound and omitted blueprint slots, resolved rule bindings,
  bounded result previews and the proposed answer together. Structural verification
  is explicitly not semantic approval.
- Catalog evidence includes all authorized table rules for SQL-referenced tables,
  including rules whose predicates are absent from the SQL. Relevant column types,
  descriptions/units, grain, joins and defaults accompany them. Blueprint rule IDs
  are expanded against those rules; unavailable definitions are marked explicitly.
  Rule applicability remains the judge's decision. Catalog exports are not assumed
  scope-filtered: denied-column documentation and referencing sections are omitted.
- Existing intent IDs, explicit result bindings and `serves_intents` associate the
  requested outcome, proposed answer and evidence. Unassigned or missing evidence
  stays explicit. Existing loaded/prepared capability evidence remains in the brief.
  Complete fetched Help Center documents are included when they fit.
- User-requested scope, applied blueprint bindings and caller-access coverage are
  distinct. Full organization coverage remains unknown unless established; an empty
  column allowlist or nonempty query does not establish it.
- The global number-substring corroboration flag and its full-result scans are
  removed. No per-number claim schema or deterministic numeric verifier was added.
  The judge checks numbers with their entity, metric, period and units.

## Bounds and failure handling

The default final-judge timeout is 30 seconds in runtime settings and Helm examples.
Existing two-review repair limits and fail-open policy remain. An unavailable review
cannot clear a prior rejection. Rejections retain intent/result targeting so repairs
can preserve unaffected work.

The judge span records model, duration, token usage, reviewed status and outcome.
Approved, rejected, timeout, provider error and malformed are distinct; cancellation
and an oversized mandatory brief are separately labelled. Approval-rate reporting
must filter to `reviewed=true`; fail-open is not a positive semantic verdict.

Result rows remain bounded previews. Only authorized blueprint metadata and fetched
Help Center documents are read from stored results; warehouse rows are not loaded for
numeric corroboration. Evidence referenced by the proposed answer has trimming
priority. Catalog/definition overflow and omitted documents/results carry markers.
If mandatory content still exceeds the configured estimated-token budget, review is
unavailable rather than sending an oversized request. This is a character-based
estimate, not an exact provider-token bound.

## Validation

Deterministic tests cover post-execution SQL/bindings, rules missing from SQL, catalog
scope filtering, missing documentation, explicit intent bindings, unknown company
coverage, full article inclusion, marked evidence trimming, prior-rejection retention
and oversized briefs. Existing safety, finalization and tracing tests remain in place.
Tests of the retired pre-execution model were replaced, not retained as dead behavior.

The opt-in `scripts/evaluate_post_execution_judge.py` evaluates seven synthetic
positive/negative pairs: optional filters, populations, swapped department values,
empty results, join multiplication, unsupported UI values and missing deliverables.
It records false approvals, false rejections and unavailable reviews separately. It
can export judge and completion spans to Phoenix. It does not execute warehouse SQL
or test retrieval/routing; that requires the full route matrix. Its optional pacing
is confined to this evaluation script, never deployment behavior.

Live results and final test counts will be appended after validation. An initial
fixture omitted its table designation; its result is retained outside Git and is not
counted as evidence of semantic accuracy. Repository fixtures contain synthetic data
only. Credentials and live diagnostic artifacts stay outside Git.

### Completed validation

Runtime suite: **3,890 passed, 5 skipped**. Ruff and whitespace checks passed.

Final live run: **14/14 expected verdicts**, seven positive and seven negative
cases. Zero false approvals, false rejections or unavailable reviews. All negative
verdicts used an expected violation category. Model: `kimi-k2.7-code`; Chat API,
reasoning metadata enabled, 30-second timeout, 35-second evaluation-only pacing.

Phoenix independently confirmed **42 spans, 14 completion spans**, with connected
`judge.evaluation → answer_judge → ChatCompletion` lineage in every case. These are
judge-only evaluation traces; the full route/retrieval matrix was not rerun.
Redaction was disabled as authorized. This small synthetic battery does not establish
general accuracy. A preliminary Responses-mode attempt and the incomplete-table
fixture attempt remain separate diagnostic artifacts outside Git.

| Case | Verdict correct | Trace |
|---|---|---|
| `optional_filter_correct` | Yes | `dc578884c9973ff3c84c5354674abede` |
| `optional_filter_incorrect` | Yes | `744bcfe4d23759b22607a0a644feb507` |
| `population_correct` | Yes | `158bbcf715c47121afeee229d0a4d9f0` |
| `population_incorrect` | Yes | `4484e7fcb3b4eb81d0f8ea4c06357bfd` |
| `swapped_values_correct` | Yes | `5e1aa1143dd9220b8ad7016131839d4b` |
| `swapped_values_incorrect` | Yes | `741a12c2a6175c92b71232a9629dd276` |
| `empty_population_correct` | Yes | `8f91cb5980b07c96a0b76bb20cc6c73e` |
| `empty_population_incorrect` | Yes | `6f4ea680cfe3a4476d0340f599599c17` |
| `join_multiplication_correct` | Yes | `43aa75a83b94ab21751b2aca71299fff` |
| `join_multiplication_incorrect` | Yes | `c3f1cc7cd3d0bb195fa2fbf1dc94f30f` |
| `ui_claim_correct` | Yes | `f5b3ff4e527e93dfdc9457fafd05a481` |
| `ui_claim_incorrect` | Yes | `cd2d21e72f38f9c2c758793c790d3baf` |
| `missing_deliverable_correct` | Yes | `88d2492a98554de5373173bf1a6a090d` |
| `missing_deliverable_incorrect` | Yes | `aecaca13a19d1f3b2363ca57b0748b4e` |
