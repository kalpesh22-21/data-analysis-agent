# Layer-3 spec-conformance suite (Playwright)

`test_conformance.py` drives the REAL minimal UI (`ui/static/index.html`)
through the REAL BFF (`ui/server.py`) through the REAL agent runtime
(`data_agent.runtime.app.create_app`), made deterministic by the scripted
`DemoModelClient`/`DemoMCPClient` doubles in `scripts/run_ui_runtime.py` — no
OpenAI key, no live ClickHouse. JWT verification against the real
`l2-token` container's JWKS endpoint is NOT bypassed.

## Running

```
RUN_E2E=1 uv run pytest tests/e2e -v
```

Requires:
- `l2-token` (docker-compose.integration.yml) already running on `:19000`
  (JWT minting).
- `:8000` and `:3000` free (the shared fixture launches the runtime + BFF
  there); the restart module additionally uses `:8010` and `:3010`.
- Chromium installed for Playwright (`uv run playwright install chromium`).
- **Restart-durability test only:** the live `l2-cb` Couchbase on `:8091`
  (`docker compose -f docker-compose.integration.yml up -d couchbase &&
  scripts/couchbase-init.sh`). That one test skips cleanly if Couchbase is
  down; the other twelve do not depend on it.

Without `RUN_E2E` set, the whole suite is skipped — `uv run pytest`
(default, no env var) never touches a browser or spawns subprocesses.

### Env flags (all OFF by default → the demo path is byte-identical)

| Flag (where) | Effect |
|---|---|
| `RUN_E2E` | gate the whole suite on |
| `UI_TEST_AFFORDANCES=1` (BFF, `ui/server.py`) | expose `POST /api/session/scope` (D44 re-mint); the shared fixture sets it — inert unless called |
| `DEMO_TEST_SPANS=1` (runtime, `run_ui_runtime.py`) | install an in-memory span exporter + `GET /_test/spans` (D25) |
| `DEMO_SESSION_STORE=couchbase` (runtime) | use `CouchbaseSessionStore` (live `l2-cb`) instead of in-memory — the restart module only |

## Covered scenarios (12)

Slice 0 (progress/clarify/denial/budget):
1. Progress streaming (D61) — `TestProgressStreaming`
2. Clarify -> resume (askUser) — `TestClarifyResume`
3. Scope denial (D57, COLUMN_SCOPE_VIOLATION) — `TestScopeDenial`
4. Parser fail-closed (D63, PARSE_FAILED_CLOSED) — `TestParserFailClosed`
5. Budget-cap pause + continue (D47) — `TestBudgetCapPause`

Slice 1 (runBlueprint, D89):
6. Fast path — `TestRunBlueprintFastPath`
7. No-silent-verification (D56) — `TestRunBlueprintNoSilentVerification`
8. Slot ask->clarify->resume (D49) — `TestRunBlueprintAskClarifyResume`
9. Approval pause/resume (D45/D59b) — `TestRunBlueprintApprovalPauseResume`

Slice 2 (the 3 previously-deferred Phase-0 scenarios):
10. Mid-session scope narrowing (D44) — `TestScopeNarrowingDropsReplay`
    (two methods: a wide-scope baseline that proves the figure is recallable,
    and the narrowed case that proves the re-minted narrower JWT drops it from
    replay). Uses the env-gated BFF `POST /api/session/scope`; D82/D5 intact —
    the BFF stays the sole JWT holder, the browser never sees the token.
11. Observability + PII span inspection (D25) — `TestSpansEmittedPiiClean`.
    An in-process `InMemorySpanExporter` + `GET /_test/spans` (NOT a Phoenix
    container); asserts AGENT + TOOL spans exist and that no span attribute
    carries the JWT, a raw SQL literal, or a result cell value.
12. Pause/resume durability across a runtime restart (D45) —
    `test_restart_conformance.py::TestPauseResumeSurvivesRestart`. The ONLY
    live-infra scenario: a Couchbase-backed runtime pauses at an approval gate,
    the `:8010` runtime subprocess is terminated and relaunched mid-pause, and
    approving resumes the FRESH process from the surviving Couchbase checkpoint.
    Isolated in its own module + fixture + ports so a Couchbase outage fails
    only this test.
