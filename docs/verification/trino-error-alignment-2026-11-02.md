# Trino error contract alignment — 2026-11-02

The API's public Trino hint registry now has a matching explicit category/recovery policy for all 42 names. Lane5 accepts those safe symbolic causes through MCP parsing, diagnostic storage/replay and compact model context. Query authoring remains ClickHouse SQL; the backend owns translation. No Trino numeric code is invented from an overlapping ClickHouse name.

| Coordinator causes | Public category | Recovery condition |
|---|---|---|
| FUNCTION_NOT_FOUND | SQL_UNSUPPORTED_FUNCTION | after_query_change |
| COLUMN_NOT_FOUND and ambiguous-column/name aliases | SQL_UNKNOWN_COLUMN | after_query_change |
| SYNTAX_ERROR | SQL_SYNTAX_ERROR | after_query_change |
| EXPRESSION_NOT_AGGREGATE and grouping aliases | SQL_AGGREGATION_ERROR | after_query_change |
| Type/operator/cast/literal/overflow errors | SQL_TYPE_MISMATCH | after_query_change |
| NOT_SUPPORTED | SQL_UNSUPPORTED_CONSTRUCT | after_query_change |
| Invalid arguments, division by zero, multi-row scalar subqueries | SQL_EXECUTION_ERROR | after_query_change |
| Memory/CPU/scan/spill/time limits | QUERY_RESOURCE_LIMIT | after_scope_or_capacity_change |
| Missing routing objects, stale/invalid views, invalid row filters/masks | EXECUTION_CONFIGURATION_ERROR | after_operator_action |
| PERMISSION_DENIED / ACCESS_DENIED | EXECUTION_ACCESS_DENIED | after_access_change |
| Abandoned query, unavailable catalog, full queue | EXECUTION_SERVICE_UNAVAILABLE | after_service_recovery |
| USER_CANCELED | EXECUTION_CANCELLED | after_user_request |
| ADMINISTRATIVELY_KILLED | EXECUTION_CANCELLED | after_operator_action |

A confirmed unavailable logical source still takes precedence as SOURCE_UNAVAILABLE. An unlocalized coordinator TABLE_NOT_FOUND does not prove that a particular logical dataset is absent; it requests operator investigation rather than inventing a source receipt.

Permission denials do not recommend another source, identity or SQL rewrite to bypass access. Lane5 recognizes EXECUTION_ACCESS_DENIED as authoritative NO_ACCESS evidence, including through aggregation probes and blueprint execution. Configuration, service and cancellation failures are not query-repair instructions. Resource recovery permits query optimization only when it preserves the requested answer. Retry conditions are guidance, not new automatic retry loops.

The API classifies known operational TrinoQueryError names even when the coordinator reports a non-USER error type. Unknown external errors retain the existing service-unavailable fallback. Unknown raw text does not become a trusted cause merely by mentioning an allowlisted name.

## Scope limits

This is classification and safe cause preservation, not raw coordinator error disclosure. Private SQL, literals, identities, unmapped positions and query IDs are not included in the public diagnostic. Precise unsupported-operation extraction/source mapping remains a separate task. Query IDs remain available in execution logs.

## Verification

- API: 763 tests passed across Trino conversion/execution/errors/export and model/query-feedback/ClickHouse errors.
- Lane5: 382 dispatcher/recovery tests plus 81 intent/probe/blueprint tests passed.
- Cross-repo contract check: all 42 API-generated public packets passed actual MCP text parsing, Lane5 denial classification, retryability checks and compact cause preservation; enum sets matched exactly.
- Real-model controlled results are recorded below. They use API-generated Trino packets injected at a local MCP test seam, with live DeepSeek and real local MCP queries where allowed. They are not production Trino or production-tenant verification.

## Rollout

Deploy Lane5 support for EXECUTION_ACCESS_DENIED and EXECUTION_CANCELLED before deploying the API producer. Older Lane5 versions do not classify these codes or retain all Trino cause names. No configuration flag enables the mapping. No production deploy is performed by this change.

Local evidence: `/tmp/trino-alignment/packets.json`, `/tmp/trino-alignment/live.json`, and the corresponding test logs under `/tmp/trino-alignment-*.log`. Temporary artifacts are not committed.

### Controlled live DeepSeek observations

- NOT_SUPPORTED: 30.38 seconds; compact packet retained `cause=NOT_SUPPORTED`, `error_code=SQL_UNSUPPORTED_CONSTRUCT`, `retryable=true`. A later query succeeded and the intent completed. Trace `ea6bd4a73a71e49be5d0663d2d94e757`.
- PERMISSION_DENIED: 53.19 seconds; compact packet retained `cause=PERMISSION_DENIED`, `error_code=EXECUTION_ACCESS_DENIED`, `retryable=false`. The model attempted a blueprint after the first direct-query denial; that failed with the same access category. The intent then became blocked and the answer delivered a limitation. Trace `edef4cf53dcdccde963f518043d0d551`.
- Residual behavior: correct category and retry guidance do not constitute a runtime prohibition against every redundant model attempt. This run does not establish zero attempts after an access denial. No denied result produced successful data evidence.
