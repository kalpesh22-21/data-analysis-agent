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
- `:8000` and `:3000` free (the fixture launches the runtime + BFF there).
- Chromium installed for Playwright (`uv run playwright install chromium`).

Without `RUN_E2E` set, the whole module is skipped — `uv run pytest`
(default, no env var) never touches a browser or spawns subprocesses.

## Covered scenarios (5)

1. Progress streaming (D61) — `TestProgressStreaming`
2. Clarify -> resume (askUser) — `TestClarifyResume`
3. Scope denial (D57, COLUMN_SCOPE_VIOLATION) — `TestScopeDenial`
4. Parser fail-closed (D63, PARSE_FAILED_CLOSED) — `TestParserFailClosed`
5. Budget-cap pause + continue (D47) — `TestBudgetCapPause`

## Deliberately DEFERRED (not covered by this suite)

These three Phase-0 conformance scenarios are out of scope for this
Playwright suite, honestly, because the current dev stack cannot exercise
them without infrastructure this suite does not stand up:

1. **Mid-session scope narrowing.** Exercising D44's fail-closed replay
   filter end-to-end through the UI would require sending two turns with
   *different* JWT column scopes in the same browser session — but
   `ui/server.py`'s BFF mints exactly ONE JWT per `session_id` at
   `/api/session` time and reuses it for every turn; there is no per-turn
   scope-switching endpoint in the BFF today. Covered instead at the
   integration level by `tests/runtime/provenance/test_fail_closed_replay_adversarial.py`
   and `tests/runtime/context/` (server-side, real `ContextAssembler` +
   `scope_filter`).
2. **Observability + PII redaction.** Asserting that a real OTel/Phoenix
   span never carries raw SQL/PII would require a running Phoenix (or other
   OTLP) collector in the stack to inspect emitted spans against — this dev
   launcher never configures `otlp_endpoint`, so no spans are even emitted
   (see `app.py`'s `tracing.configure_tracing`). Covered instead at the unit
   level by `tests/runtime/observability/test_redaction.py` and
   `test_tracing.py`.
3. **Pause/resume durability across a runtime restart.** Proving a paused
   checkpoint survives the RUNTIME PROCESS itself restarting (not just a
   page reload) requires actually killing and relaunching the `:8000`
   subprocess mid-pause against a REAL Couchbase-backed `SessionStore` (D45)
   — this suite's `InMemorySessionStore`-backed launcher loses all session
   state on restart by design, so it cannot demonstrate durability; it would
   only demonstrate the opposite. Covered instead by
   `tests/runtime/session/test_couchbase_store_cas_retry.py` and the
   `test_couchbase_store.py` (currently `skip`ped without a live Couchbase,
   see the integration compose file).
