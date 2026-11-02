# Compact SQL failure packets

The model-facing projection of structured SQL/service failures retains tool name, status, public error code, one retryable flag, dialect, safe cause/code when present, source receipt when applicable, and a single hint. The tool-call envelope carries the identifier. Repeated SQL excerpts, null previews, duplicate message/code fields and per-packet failure-evidence boilerplate are omitted. The failure-evidence rule is present in the agent system instructions.

Internal receipts retain the original diagnostic, retry condition and excerpt. Retry conditions are not equivalent to a boolean: query changes, scope/capacity changes, service recovery and access changes remain distinct internally and in actionable guidance. Legacy unstructured diagnostics retain their previous projection.

clickhouse-api preserves allowlisted ClickHouse symbolic names and known observed numeric pairs. No raw exception body, view SQL or location is exposed. A location/operator is not fabricated when unavailable. Lane5 separately validates supported cause/code pairs on ingestion.

Validation: 126 API tests; 1,522 Lane5 dispatch/context/loop tests; 2 additional compact-packet and untrusted-cause tests passed. Tests assert the original internal diagnostic survives projection.

Live local MCP + DeepSeek V4.1 Flash: injected madeUpEmployeeCount SQL produced a real UNKNOWN_FUNCTION/code 46 denial. DeepSeek executed a corrected distinct employee count filtered to Active, and the judge approved. One SQL denial, one successful corrected query, no salvage, 29.11 seconds. The canonical failure projection reconstructed from the receipt is 399 characters. This does not establish an exact percentage saving against the historical Turn 7 packet, which had a different hint.

Evidence: /tmp/lean-sql-recovery/results.json and verification.json; Phoenix trace 3b2d8621ad6400a2abcab8712f0f320f in answer-document-battery. No production deployment. The local MCP container received the two changed API modules for this test.
