# I7 Trino aggregation verification audit — 2026-11-02

**Result: the reported system-table dependency is absent on this tree. The tested
join-cardinality checks execute through Trino and fail closed. No runtime patch
was necessary.** This verifies the bounded join-check behavior, not universal
ClickHouse/Trino equivalence or a production deployment.

Audited lane5 `3cf516e` and clickhouse-api `1082262`.

## Code path

- `runtime/loop/measurement.py:294`: builds `COUNT(*)` / `uniqExact(join keys)`
  probes over the business relations, preserving required local filters and scope.
  These probes do not read ClickHouse `system.*` metadata.
- `measurement.py:558`: dispatches each probe through the normal scoped `runQuery`
  path. Duplicate keys, unsupported shapes, failed queries, and malformed counts
  cannot establish successful verification.
- `runtime/loop/agent_loop.py:3914`: checks ad-hoc `runQuery` before dispatching the
  requested aggregate. A verification issue becomes a gate refusal.
- `runtime/blueprint/executor.py:628`: checks the same condition before executing
  each blueprint node; a failure returns `ExecFailed`.

## Live execution

The committed opt-in test uses real MCP HTTP/SDK, clickhouse-api's production
`service.run_query` validation/provenance and Trino conversion, the running local
Trino coordinator, and its MySQL connector. Employee/department private source
SQL comes from the API checkout. Results are not mocked. The principal, scope and
single local shard inventory are test fixtures; this does not test production JWT
or mimic handling. Warehouse data is synthetic and is not modified.

| Case | Verification result | Aggregate | Coordinator query IDs |
| --- | --- | --- | --- |
| Employee salary joined to department | 4 department rows / 4 unique keys | Executes | `20261030_085318_00013_a3mqv`, `20261030_085319_00014_a3mqv` |
| Employee salary joined to employees by department | 24 rows / 3 unique department keys; `AGGREGATION_DUPLICATE_KEYS` | Blocked | `20261030_085320_00015_a3mqv` |
| Unreachable coordinator | `AGGREGATION_CHECK_UNAVAILABLE`, non-retryable | Blocked | None |
| Valid blueprint | Verification succeeds | Executes | `20261030_085322_00016_a3mqv`, `20261030_085322_00017_a3mqv` |
| Duplicate-producing blueprint | `AGGREGATION_DUPLICATE_KEYS` | Blocked | `20261030_085324_00018_a3mqv` |
| Blueprint with unreachable coordinator | `AGGREGATION_CHECK_UNAVAILABLE` | Blocked | None |

Coordinator IDs retain the container's clock; their dates are not the audit date.
Unavailable tests direct requests to a closed local port without stopping the
shared coordinator. Tests assert that the aggregate SQL is never dispatched in
blocked cases. The successful sum is returned as a decimal string, which the test
checks without float coercion.

**6 live tests passed; 63 focused guard/measurement/blueprint regressions passed.**
The live test intentionally uses deterministic SQL, not DeepSeek, to force both
unsafe and unavailable cases independently of model choices. It is not a full
conversational/UI test. Guard unit tests cover other failure and recovery shapes.

## Reproduction

From lane5, using an interpreter with both repositories' dependencies and the
seeded `battery-trino` / `battery-mysql` containers (Trino at localhost:18081):

```sh
CLICKHOUSE_API_REPO=/path/to/clickhouse-api RUN_TRINO_AGGREGATION=1 PYTHONPATH=src \
  .venv/bin/python -m pytest tests/integration/test_trino_aggregation_live.py -q -s
```

Local evidence: `/tmp/lane5-i7-trino/final.log` and `unit.log`. Early test failures
were harness setup/expectation errors (unset API configuration, decimal-string
results, three staffed department keys versus four directory keys, and normalized
SQL spelling), corrected before the final passing run. No runtime safety failure
was found.

## Remaining limits

A uniqueness check is not an independent recomputation of the final business
metric. It does not prove correct date basis, population or every supported SQL
shape. Separate probe and aggregate queries are not a transactional snapshot;
concurrent source changes can occur between them. Deployment-specific access,
connector grants, routing, and broader Trino dialect parity still need their own
verification. Do not close all of I6/I7 merely from this local audit.
