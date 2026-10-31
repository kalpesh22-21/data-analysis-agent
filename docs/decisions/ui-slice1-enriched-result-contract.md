# UI Slice 1 — migrated to answer-document v2

The earlier nine-field/enriched-table result contract is retired. Implement the
[current answer-document contract](../ui-answer-document-spec.md), not the former
assistant_text, answer_tables, sql_executed, layout or blocks projections.

Sources carry exact executed SQL and column formats; presentations carry kind and
column selectors. The UI fetches rows through Lane5 `/query/page` and retains its
snapshot ID for later pages. Formatting stays in the saved document. Downloads
use the existing CSV part URLs. Table/CSV options are required for every data
result except figures; text has no data controls. No supporting/fallback flag
controls access to rows. Use the same adapter for live and saved v2 documents.

Progress uses `{turn_id, sequence, phase, message}` and SSE heartbeats. Preserve
pause/resume and error states; a paused turn has no sealed answer document.
