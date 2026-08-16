# Cleanup Work Log

Newest entry last. One entry per slice: what changed, review outcome, V0/V1/V2
verification results vs baseline.

---

## #1 — Slice 0: baseline (2026-08-16)

State of the world before any cleanup change, on `cleanup/2026-08` (=
`phase0/provenance-extractor` @ 9558e9e).

- Stack: l2-mcp, l2-token, l2-ch, l2-neo4j, l2-embedding, l2-reranker,
  l2-redis, l2-phoenix all Up; **l2-cb unhealthy** (6 days) — does not affect
  V0/V1 (in-memory session store); restart before any V2 that needs Couchbase.
- V0 (offline `uv run pytest -q`): **5580 passed, 225 skipped, 1 xfailed** (18.9s). Green.
- V1 (7-question live gate, `RUN_LIVE_EVAL=1`, RUNS=3): **7 passed, 2 FAILED**
  (189s). **PRE-EXISTING reds, before any cleanup change:**
  - L3: 0/3 — "the residual half never ran a query" (×3, consistent)
  - L5: 0/3 — "not every intent reached a terminal disposition" (×3, consistent)
  - L1, L2, L4, L6, L7: ≥ the 67% gate.
  The 0/3 consistency = real routing defect predating this branch, not model
  noise. Cleanup gate for later slices: no case below its baseline rate; L3/L5
  are red at baseline and stay out-of-scope for refactor slices.
  **Diagnosed same day (detected-issues stack K1/K2): never passed, not a
  regression.** L3 = eval-harness defect (LiveEvalMCPClient advertises MCP
  tools with EMPTY input_schema → the model cannot issue ad-hoc SQL; faithful
  schemas flip L3 green, proven live). L5 = model never declares intents
  (`serves_intent` tags silently dropped with no feedback; late-init lock) +
  the same harness defect. Fix plan: harness-fidelity slice (tests/eval only,
  F1/F2/F3 + G3) then re-baseline all 7; runtime feedback fix (G1) + prompt
  (G2) as separate behaviour-change slices, not inside refactors.

---

## #2 — Tier 1: drift hazards (2026-08-16)

Deduplicated every multi-copy drift hazard from PLAN Tier 1. 40 files
(35 modified, 5 new). Built in an isolated worktree, reviewed
(verdict: APPROVE; should-fix + 2 nits applied), then landed here.

New shared homes:
- `src/data_agent/canonical.py` — `canonical_json`, THE serialization
  convention for dedup canonical keys + blueprint structural keys
  (hash-critical; `tests/test_canonical_json.py` pins literal sha256 digests).
- `src/data_agent/timeutil.py` — `now_iso()` (was 7 byte-identical copies).
- `src/data_agent/runtime/mcp/_transport.py` — `SideChannelError` base,
  `side_channel_headers`, `auth_headers`, `error_from_response` shared by
  catalog/corpus/scratch HTTP clients (exception names preserved as
  subclasses; `MCPToolError` deliberately NOT in the hierarchy).
- `src/data_agent/learning/entrypoint.py` — `configure_daemon_process`
  (was 3 copied preambles; now also used by `run_inbox_service`).

Constant mirrors now import downward (SLOT_TYPES, NODE_KINDS, AcceptedSignal,
PriorArtKind, corpus index map — now a shared read-only `MappingProxyType`,
DATA_TOOLS, SLOT_TOKEN, READ_CORPUS_META_QUERY, TRUTHY_ENV_VALUES); guard
tests tightened `==` → `is`. `sqlparse/oracle.py` imports `mask_sql` from
redaction (its copy's stated cycle was stale; verified cold-import clean).

Flagged behaviour changes (reviewed + approved):
1. `error_from_response` unified on broad `except Exception` (was
   ValueError-only in catalog/corpus) — strict superset, keeps HTTP status.
2. `run_inbox_service.py` gained the daemon preamble (tracing posture line +
   LEARNING_* typo warning). Inert unless OTLP_ENDPOINT is set. Known gap
   (pre-existing): bypassed under `uvicorn scripts.run_inbox_service:app`.

Left for follow-up: `validation.py::_ACCEPTED_SIGNAL_DOMAIN` is a 4th copy of
the AcceptedSignal set (derive via `typing.get_args`); `run_hydrator.py` has
no runtime-plane env-typo warning equivalent (needs a naming convention
first); `tool_dispatcher._estimate_tokens` deliberate cycle-breaking copy kept.

Verification:
- V0: **5587 passed, 225 skipped, 1 xfailed** (baseline 5580 + 7 net new).
- V1: **identical to baseline** — L1 3/3, L2 3/3, L4 3/3, L6 3/3, L7 3/3;
  L3 0/3, L5 0/3 (the two known pre-existing reds, same failure text).
- ruff clean; `ruff format --check` set byte-identical to baseline.
- V2: skipped per protocol (pure-dedup slice, no loop/dispatch/session/wiring
  semantics touched; V0+V1 green).
