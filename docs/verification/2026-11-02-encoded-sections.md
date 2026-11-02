# JSON-encoded sections compatibility

Finalization and salvage now decode a string-valued sections field once with the JSON parser before applying their unchanged schema and presentation validators. The tool schema still requests an array. Invalid JSON, non-array values, double-encoded strings, non-JSON constants and invalid section contents remain errors. No recursive decoding, prose extraction, invented fields or skipped evidence validation is introduced. The input object is not mutated.

Validation: 1,184 runtime loop/context tests passed, including malformed input, duplicate IDs, multiple-table reasons and salvage acceptance without retry.

Real DeepSeek V4.1 Flash + local MCP: injected a prose-only Delivered answer document response, followed by stringified sections. Scalar (31.76s) and department-table (18.52s) cases both completed and received judge approval. Neither needed an invalid-argument correction round, query rerun or salvage. Saved sections were arrays and data sections remained present.

Artifacts: /tmp/finalizer-coercion/verification.json and results.json. Phoenix answer-document-battery traces: 8a95eb8410db2a53d782504075b807c2, e7eeb9429a9f05d37e6581d29c3c3ae1. Controlled synthetic fixtures and injected response formatting errors; not a production deployment or proof of universal model reliability.

## Follow-up: every raw answer validation boundary

The shared helper is now normalize_answer_arguments. It allowlists only top-level sections and evidence, decodes strings once, and accepts only JSON lists. Invalid JSON and non-list decoded values leave the original string untouched so the schema retains its path/type feedback. Nested fields, prose, deliverables and derived tables are not normalized.

The helper runs before proposal validation (including partial-answer materialization), before salvage schema validation, and before corrected_document validation. The parsed judge verdict also retains the normalized correction, rather than merely validating a temporary copy. The provider parser remains unchanged. The pre-existing malformed-judge fallback policy is not changed by this patch.

Updated tests: 1,194 loop/context tests passed, including encoded partial delivery/history and corrected-document delivery without another query or agent round. Real DeepSeek V4.1 Flash with local MCP passed both encoded corrected_document (30.10s) and encoded salvageAnswer (33.42s), with sections and evidence stringified at the raw-tool boundary. Both preserved data sections and saved arrays, with no schema correction retry; the judge correction reused existing queries.

Artifacts: /tmp/answer-boundaries-live/verification.json and results.json. Phoenix traces: 2aebb575a4ded2e170f403e22af4d9ba, cd4694d3476855d3025f4e0e8a8b40ff.
