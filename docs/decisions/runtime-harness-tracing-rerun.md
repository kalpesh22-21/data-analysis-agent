# Unredacted tracing test rerun — 2026-09-13

The live GPT-4.1 question matrix passed, but the complete test stack is **not green**. No product source was changed for this rerun.

## Tracing

The real backend remains available at `http://127.0.0.1:18104` with `OTLP_DISABLE_REDACTION=1` and `OTLP_HIDE_LLM_CONTENT=0`. Phoenix is at `http://localhost:6006`, project `data-agent-runtime`. Startup confirmed `effective_llm_hide=False` and model `gpt-4.1`.

Direct inspection of Phoenix spans from the question matrix verified:

- 94 model spans, all with nonempty, unredacted inputs and outputs.
- 17 SQL tool spans, all with visible SQL and result previews.
- 14 trace IDs covering the 13 scenarios, including clarification resume.

Example session: `harness-clarification-19377258b6`, trace `357e8ae9e502cd73da933af7bb419e6e`.

Trace settings were supplied to the processes; `.env` was not changed. Browser conformance fixtures deliberately override redaction to test their PII-clean contract; that override was preserved. The real backend and real-model question matrix used unredacted tracing.

## Results

| Test layer | Result |
| --- | --- |
| Full repository, with Couchbase tests enabled and local writer credentials supplied | 7,855 passed, 297 failed, 193 skipped |
| Live service integrations plus real MCP client, combining the service run and Redis follow-ups without duplicate Couchbase tests | 162 passed, 35 failed |
| Browser conformance, Chromium | 11 passed, 17 failed |
| Live GPT-4.1 routing evaluation | 7 passed, 2 failed |
| Live GPT-4.1 HTTP scenario matrix | 13/13 passed |

These rows overlap; do not add them into one total. The default repository run skips opt-in service/browser/live-model tests; the separate runs above enable those layers.

The first repository attempt had 7,829 passes, 296 failures, 193 skips and 27 Couchbase authentication setup errors. Supplying the dedicated local writer credentials resolved all 27 setup errors (27/27 passed separately). The final full run gained one environment-sensitive failure: `test_a_totally_broken_vault_degrades_to_env_for_every_field` expects the audit username environment variable to be absent. This is a test-isolation conflict with the integration credentials.

Of the original 296 deterministic failures, 293 match failing IDs from unchanged baseline runs. Three newly failing evaluation IDs are:

- `tests/eval/test_runtime_mechanics.py::test_case_07_narrowed_scope_drops_a_card_and_a_denial_blocks_the_intent`
- `tests/eval/test_runtime_mechanics.py::test_case_07_one_denial_cannot_spread_to_a_second_intent`
- `tests/eval/test_runtime_mechanics.py::test_case_13_a_narrowed_reload_drops_one_table_and_keeps_the_rest`

Those three exhaust their scripted model responses. They need investigation/fixture migration; this run does not establish them as product regressions or dismiss them as harmless. The earlier no-new-failing-ID conclusion applied to the runtime-only suite, not these evaluation tests.

Live routing failures were L3 (blueprint plus SQL residual, 0/3 passing because the evaluator found no intent binding) and L5 (metadata plus analysis, 0/3 because analysis state was absent). The other five routing cases each passed 3/3; two additional evaluation checks passed. The evaluation metric still reads the singular `serves_intent` field and should be reviewed against plural bindings before attributing every binding failure to the model.

Service failures include old/new warehouse column-name mismatches, retrieval/landing expectations, and MCP SQL scope parsing. The Redis-backed end-to-end learning failure also received `PARSE_FAILED_CLOSED`. These service failures were not baseline-compared.

The browser suite initially collided with an existing service on port 8000. That attempt was interrupted and excluded. The completed rerun used a temporary copy at `/tmp/harness-browser-rerun`, with chat runtime/BFF ports 18110/18111 and inbox ports 18112–18114. Repository browser files and assertions were not modified. Failures include missing final answer rendering and clarification/budget flows; the scripted runtime logs show exhausted fake MCP responses. These browser failures were not baseline-compared.

## Local artifacts

- Full repository: `/tmp/harness-complete-traced-tests-final.log`
- Initial repository attempt: `/tmp/harness-complete-traced-tests.log`
- Baseline evaluation: `/tmp/harness-mechanics-baseline-rerun.log`
- Service integrations: `/tmp/harness-full-services-traced-tests.log`
- Redis and end-to-end learning: `/tmp/harness-redis-pipeline-traced-tests.log`
- Consumer/pipeline follow-up: `/tmp/harness-consumer-pipeline-traced-tests.log`
- Browser: `/tmp/harness-e2e-isolated-traced-tests.log`
- Live routing: `/tmp/harness-live-routing-traced-tests.log`
- Question matrix: `/tmp/harness-scenario-matrix-unredacted.json`
- Trace verification, field lengths and redaction flags only: `/tmp/harness-trace-verification.json`
- Backend: `/tmp/harness-backend-unredacted.log`

Redis was started using the existing integration Compose service. The real backend remains running with unredacted tracing for inspection.

## Follow-up: judge reviews nested within the main query

The requested layout is `agent.turn -> answer_judge -> Response` within one trace. The temporary change to separate top-level judge traces was reverted after clarification. The named judge wrapper groups its model call, verdict attributes and token count underneath the main query. Unredacted model content remains enabled.

Validation: 63 targeted tracing/judge tests passed, including same-trace parent/child relationships and restoration of agent context after success or provider failure. Backend port 18104 was restarted with the nested layout. Previously exported top-level judge traces remain historical records and are not rewritten.
