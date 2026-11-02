# A18 DeepSeek verification — 2026-11-02

Real DeepSeek via Fireworks (`accounts/fireworks/models/deepseek-v4p1-flash`),
lane5 HTTP/SSE, live MCP at localhost:18090, actual ClickHouse query execution,
catalog/retrieval and live model/judge calls. The warehouse contains synthetic
local test data. This is not production-tenant or Ember browser certification.
No model/judge response or query-result mocks were injected.

| Case | Result | Seconds | Trace |
| --- | --- | ---: | --- |
| Department ranking | Approved, saved; Sales 3, Engineering 2, Operations 2; business caveats | 46.70 | 21d78679bda244af8c5a7aea72ff339c |
| Forced duplicate/UI assumptions | Found a remaining paraphrase duplicate; fixed below. The model correctly declined the invented 211-department premise. | 111.13 | 2830d4453e7838693a05c493979a1d84 |
| Clarification then resume | Paused then approved/saved; 7 employees; population stated once | 77.41 | f16bc3e8b4d05796061b0f42eb716982 / 4b87866f883c1225fbb25f56021c6b34 |
| Live post-fix recheck | Approved/saved; 7 employees; business caveats only | 32.94 | 809ed8a213fe8052f50ebb905c00d519 |

Phoenix project: `answer-document-battery`.

The stress case produced both “Current means Active plus On Leave” and
“Headcount counts distinct employees with status Active or On Leave.” The
follow-up keeps the more informative headcount definition and drops the redundant
standalone population definition, irrespective of section order. Qualified or
unmatched definitions remain. Three regression tests cover the captured pair,
reversed order and a distinct departmental restriction. Final regression suite:
**1,613 passed**.

The post-fix live model used separate population and distinct-count definitions,
which are complementary and were preserved. It did not reproduce the exact
captured pair; deterministic regressions establish that pair's cleanup. It put
the explicitly requested paging/CSV instructions in the takeaway, not caveats.
A18 cleans assumptions/caveats; it does not strip arbitrary answer prose.

The first attempt ended at the budget cap with zero accessible employee rows:
the local employee-access fixture had expired under its one-day TTL. Restoring
exactly the seven access rows documented in `hr-demo-expansion.sql` restored the
seeded scope. No production permissions or application configuration changed.
That attempt is retained as a failed environment run, not counted as a pass.

Local artifacts: `/tmp/lane5-a18-deepseek/` (`results.json`,
`results-restored.json`, `recheck.json`, question files and regression logs).
The successful runs verified terminal SSE, saved-document parity and connected
model/tool/delivery traces. General semantic deduplication remains deliberately
bounded to recognized definitions; this is not a universal paraphrase merger.
