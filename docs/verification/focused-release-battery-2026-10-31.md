# Focused release verification — 2026-10-31

Status: PASS for the local scope below. Not a production or Ember deployment certification.

## Scope and verdict

- 13 distinct focused conversational cases passed their final checks; 16 terminal SSE responses, including clarification resumes and the judge-correction follow-up.
- Real DeepSeek via Fireworks, Lane5 HTTP/SSE, local ClickHouse MCP, catalog/retrieval, Couchbase saved results and Chromium reference UI. Phoenix project `answer-document-battery`; final response/trace and persisted-answer checks passed. Existing hydrated formatted corpus was used; clickhouse-api has no new working-tree changes.
- 12 saved source checks and 15 CSV parts: initial page, subsequent saved paging, SQL deduplication, explicit refresh/replay, raw CSV preservation and reference UI controls passed. Empty results correctly produce no CSV parts.
- Full runtime/UI regression suite: **6,940 passed, 5 skipped**. Ruff on all changed Python files and git whitespace checks passed.
- Four additional real-model formatter replays passed: derived USD retained; unknown and conflicting currencies stay numeric; money/money stays a percentage ratio. These replay recorded evidence, rather than executing new queries.

## Cases (latest run per case)

| Case | Seconds | Verdict | Phoenix trace(s) |
|---|---:|---|---|
| v2_blueprint_figure | 71.28 | PASS | [140cc5b9](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/140cc5b985eb49b39fe9ab37bae94c27) |
| v2_zip_phone | 60.98 | PASS | [cba58425](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/cba584254a206b952684b06ec46e3513) |
| dates | 35.54 | PASS | [0f1369b3](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/0f1369b33a4bc71ad6289552468da2cf) |
| review_seam_followup | 46.12 | PASS | [273e55c0](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/273e55c0da0ce51510aadc3737edb74b), [4a4b69ec](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/4a4b69ecb2198594cda4525ad264f2a1) |
| salvage | 16.39 | PASS | [87cd20e1](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/87cd20e1e4af2c73d750d8ecdd775e12) |
| v2_monthly_line | 55.23 | PASS | [71b03d56](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/71b03d5683ee23fcf5c0fbda9ca5c260) |
| salary_rank | 55.19 | PASS | [9d491943](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/9d49194324e35456bb2683c668d386ec) |
| review_seam_caption | 33.86 | PASS | [723f5398](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/723f5398defe8e4225eecee58fe06dcf) |
| continuation | 52.26 | PASS | [61baacd5](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/61baacd5f0ff6c65046ef856283a0e65), [e314f39f](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/e314f39f8402174ab8d858b1739bb4d1) |
| partial | 43.85 | PASS | [79532f14](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/79532f145e7ac0a4f7f14996fb970cf3) |
| v2_zip_phone_transform | 85.16 | PASS | [b183026e](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/b183026e8dad26009d16130be8ca0df1) |
| empty | 55.09 | PASS | [674f19dd](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/674f19dddd8733fa58ccbee484e15971) |
| original_continuation | 66.59 | PASS | [05530cc5](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/05530cc538b57a36b8d0ea6deea78ad8), [0dbcef05](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/0dbcef05a6f3050419d590592661822d) |

The salary-ranking and repeated-name cases completed without salvage. The intentionally injected judge-timeout case used salvage once and delivered the same v2 contract. Injected payroll denial retained the supported employee result with a limitation. Caption correction and the follow-up confirmed that delivered corrections reach subsequent model context. Clarification continued to Active=7 rather than stopping at an acknowledgment.

## Findings resolved during verification

The first ZIP/phone attempt used the restored empty fixture. Its saved-page probe incorrectly demanded all source format keys appear in an empty export's `columns: []`. Direct API inspection confirmed zero rows/no columns/no CSV parts. The probe now permits that zero-row shape while retaining the column check for populated results; the Ember migration guide documents it. Nonempty contacts and transformed phones were then retested using temporary synthetic ZIP/phone values, including leading zeros and punctuation. Both passed and the fixture fields were restored to NULL afterward. The original failed artifact is retained; it is not silently reclassified as a passing run.

The prompt-length regression caught the added continuation guidance exceeding the existing 16,000-character bound. Redundant wording was shortened while preserving the names-alongside-codes safeguard; final prompt tests pass without raising the limit. An unrelated inbox browser test failed once during concurrent testing and passed independently and in the final full-suite run; no inbox implementation was changed.

## Handoff and limits

Send the [Ember v2 migration guide](ember-ui-v2-migration-2026-10-31.md), based on the maintained answer-document/value-formatting/assumptions contracts. It covers removed fields, exact sources and selectors, View data/Download data controls (figure exception), Lane5 paging, existing CSV-part URLs, refresh and recovery. No supporting-data flags, embedded presentation values or table handles should be restored.

Production S3 direct downloads, Trino, actual Ember adapters, and the historical deployment/auth incidents still require remote verification. These local runs use synthetic scoped fixtures and authenticated Couchbase CSV streaming. Phoenix localhost links and `/tmp` artifacts are available on the originating machine, not remotely portable.

Artifacts: `/tmp/lane5-final-focused-20261031/` includes question files, `results0.json`, `results1.json`, `contacts-retest.json`, `continuation-final.json`, `formatter-replay.json`, regression logs and empty-export inspection. Workgate remains stashed and excluded from this release. No clickhouse-api source change is needed for the v2 transport: Lane5 wraps its existing snapshot endpoints.
