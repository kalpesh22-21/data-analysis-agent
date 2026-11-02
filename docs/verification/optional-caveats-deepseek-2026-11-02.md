# Optional caveats — DeepSeek verification, 2026-11-02

Real DeepSeek (`accounts/fireworks/models/deepseek-v4p1-flash`) via Fireworks, local lane5 HTTP/SSE, MCP, retrieval and SQL execution against synthetic ClickHouse test data. No model/judge/query-response mocks. Not a production tenant or Ember browser verification.

| Case | Result | Seconds | Trace |
| --- | --- | ---: | --- |
| optional_headcount | Approved and persisted; 7 employees; exact requested population title; caveats empty. | 26.95 | f6ec826d1d62eb780a8c19749db63951 |
| material_median | Approved and persisted; median $115,000 across 7 fixture employees; compensation qualifications retained. | 49.07 | f0c24863676ae1fa10fecbc00d20eb1b |

All harness checks passed. Final regression suite: **1,619 passed**.

Limit: the salary answer still repeats some population wording across prose and caveats. The backend removes whole-statement duplicates, not arbitrary semantic overlaps; the model/judge chooses which other qualifications are useful. Embedding-based merging was not added.

A pre-fix run was interrupted after regression tests caught an accidental edit in proposal validation. It is not counted as verification. The corrected tree was rerun; local artifacts are `/tmp/lane5-optional-caveats/results-fixed.json` and `/tmp/lane5-optional-caveats-final-tests.log`.
