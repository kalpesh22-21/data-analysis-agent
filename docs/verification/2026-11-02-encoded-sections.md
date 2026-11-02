# JSON-encoded sections compatibility

Finalization and salvage now decode a string-valued sections field once with the JSON parser before applying their unchanged schema and presentation validators. The tool schema still requests an array. Invalid JSON, non-array values, double-encoded strings, non-JSON constants and invalid section contents remain errors. No recursive decoding, prose extraction, invented fields or skipped evidence validation is introduced. The input object is not mutated.

Validation: 1,184 runtime loop/context tests passed, including malformed input, duplicate IDs, multiple-table reasons and salvage acceptance without retry.

Real DeepSeek V4.1 Flash + local MCP: injected a prose-only Delivered answer document response, followed by stringified sections. Scalar (31.76s) and department-table (18.52s) cases both completed and received judge approval. Neither needed an invalid-argument correction round, query rerun or salvage. Saved sections were arrays and data sections remained present.

Artifacts: /tmp/finalizer-coercion/verification.json and results.json. Phoenix answer-document-battery traces: 8a95eb8410db2a53d782504075b807c2, e7eeb9429a9f05d37e6581d29c3c3ae1. Controlled synthetic fixtures and injected response formatting errors; not a production deployment or proof of universal model reliability.
