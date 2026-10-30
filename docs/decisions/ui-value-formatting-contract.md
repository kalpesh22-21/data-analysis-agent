# Answer value formatting contract

Status: backend implementation; Ember adapters must implement this contract.

## Ownership

clickhouse-api owns catalog column semantics. Each authored blueprint declares a
`column_formats` map keyed by its exact output aliases, including derived measures.
Lane5 preserves the declaration through hydration and applies it when executing the
blueprint. **The model-facing getBlueprint response omits formatting. runBlueprint
and finalizeAnswer accept no formatting arguments.** The agent selects results and
presentations; it does not copy, infer, or rewrite format declarations.

Ad-hoc SQL receives conservative catalog/expression-based formats from clickhouse-api.
A count is an integer even when counting ZIP codes. A ZIP code remains an identifier
through a simple alias. Unknown units do not establish currency or percentage scale;
unknown semantics default to text, never a guess based on an alias or numeric-looking
string. Unsupported lineage is safe to display as raw text.

## Transport

Ordered `columns` remain strings. Query results and snapshot create/page responses
add `column_formats`, keyed by those exact strings:

```json
{
  "columns": ["ZIP Code", "Gross Earnings", "Denial Rate"],
  "column_formats": {
    "ZIP Code": {"kind": "identifier"},
    "Gross Earnings": {"kind": "currency", "currency": "USD", "precision": 2},
    "Denial Rate": {"kind": "percent", "scale": "ratio", "precision": 2}
  },
  "rows": [["02108", "1766987.30", "0.1532"]]
}
```

In `answer_document`, the same map is on `sources[result_id].column_formats`.
Figures and each facts item have an optional `format` and an already formatted
`value` string. Bars/lines have a measure `format`; bars retain numeric `raw` and a
formatted `value`, and lines retain numeric/null `value` and `formatted_value`.
Never parse the formatted display string for chart geometry, sorting or export.

Ember continues to create snapshots with `{table_ref}` and refresh them with
`{table_ref, refresh_of}`. Lane5 resolves approved SQL and formats server-side;
Ember must not resubmit format choices. The API persists these formats with the
snapshot. Paging, idempotent creation, refresh and automatic rebuild preserve them.
Saved answer documents retain the source formats too.

## Rendering rules

| kind | Meaning and rendering |
|---|---|
| text | Preserve as text; never numeric-detect. |
| identifier | Preserve exactly, including leading zeros; no grouping, numeric conversion or rounding. |
| integer | Group digits for display; no decimal places for integral values. Do not silently round fractional values. |
| number | Group digits; use `precision` as maximum fractional display places if supplied. Without it, preserve precision. |
| currency | Required ISO currency code. Display currency and fixed `precision` (default 2) fractional places. Never infer USD. |
| percent | Required `scale`: `ratio` multiplies by 100 for display; `percent` is already scaled. Append `%`; default precision 2. |
| date | Calendar date; do not shift it through a timezone. |
| datetime | Timestamp; render with a stated timezone. Preserve its instant. |
| boolean | Yes/No; do not apply numeric formatting. |

Optional `unit` supplies a suffix, such as `hours`, `miles`, or `per hour`.
`precision` is an integer from 0 to 8 and affects display only. Null means missing,
not zero: render “Not available” in cells and preserve null chart gaps.
Unknown or absent metadata uses raw text. Do not guess from names such as “Rate”,
from numeric-looking strings, or from database storage types alone.

Use decimal-safe formatting for large integers and decimal strings; do not first
coerce them through JavaScript Number. Figures/facts and chart exact tooltips can
use the supplied backend-formatted strings directly. Chart axes may use compact
labels with the same unit and scale; exact tooltips must remain available.

Examples: ZIP `"75039"` → `75039`; ZIP `"02108"` → `02108`; employee count `75039`
→ `75,039`; USD `1766987.3` → `$1,766,987.30`; hours `125.250000` → `125.25 hours`;
ratio `0.1532` → `15.32%`; pre-scaled percent `15.32` → `15.32%`.

## Pagination and incomplete results — Ember follow-up

Ember owns the disclosure of how many table rows are currently displayed. Use
paging metadata and the rendered row count; do not depend on the agent's prose or
ask it to repeat pagination status in the takeaway.

- When the total is known, show “Showing 20 of 211 departments” (or “rows” when
  no suitable label is available). Update the count as rows load. For a page that
  replaces the previous page, use a range such as “Showing 21–40 of 211”.
- When the total is unknown, show only the known count, e.g. “Showing 20 rows”.
  Do not infer a total or completeness from page size.
- Keep paging and export controls available where supported. On refresh or
  snapshot replacement, reset the counts to the new snapshot's metadata.
- A complete result with a limited initial preview is not an incomplete answer.
  Do not label it “Partial answer” solely because more pages remain.
- An incomplete execution is different: preserve the backend's explicit
  completeness/limitation information as an answer-level disclosure. Loading all
  available pages does not establish that the execution was complete.

The backend remains responsible for accurate paging and execution-completeness
metadata. Agent prose should avoid claims about the UI's current visible row
count; that count changes independently as the user pages through results.

Acceptance checks: initial 20-of-211 preview, appended rows, page replacement,
unknown total, empty result, refresh/replacement, and an execution-limited result.
Verify that pagination alone produces no partial-answer warning, while an actual
execution limitation remains visible after paging.

## I11: pause → resume 403 — verification follow-up

Status: historical root cause remains unconfirmed; the rejection logs were lost.
The clickhouse-api scope-token default has been changed locally to 28,800 seconds
(8 hours). This is not proof of a fix or deployment. Explicit deployment settings
override the default. Lane5 refreshes near-expiry tokens with 30 seconds remaining.
A catalog SHA change can invalidate an unexpired token immediately.

The UI developer should verify with the backend owner:

- Pause a turn, answer the clarification, and confirm `/turn/resume` preserves
  the original `X-Session-Id` and supplies valid authorization for that session.
  Repeat after access-token renewal and after a longer pause.
- Capture the complete resume SSE stream through its terminal outcome, including
  cases where the HTTP response starts successfully but a later MCP call fails.
  Confirm the UI leaves its loading state and displays the returned outcome.
- Record UTC timestamps, trace/request correlation IDs, deployed versions, and
  safe error codes. Do not put JWTs, scope tokens, or Authorization headers in
  reports, screenshots, or shared network captures.
- Ask the backend owner to correlate any 403 with the MCP rejection code. Lane5
  may currently expose only `INTERNAL_TRANSPORT_ERROR`; the UI cannot identify
  the cause from that generic error alone.

Possible causes to distinguish: scope-token expiry, catalog staleness, session
binding mismatch, changed identity/authorization claims, missing/invalid scope
tokens, inconsistent signing keys across replicas, and ingress/auth rejection.
Catalog scope tokens and signing keys are backend-owned; Ember should not mint,
inspect, or refresh them itself. Do not blindly retry every 403 or change the
session ID to work around a rejection.

Acceptance evidence: a successful pause/resume with the same session, a successful
resume after valid access-token renewal, and a controlled rejection that produces
a visible terminal outcome without an infinite retry/loading loop. Backend owners
must verify the effective eight-hour configuration and diagnose stale/binding
failures separately before marking I11 resolved.

## A15(a): prose-correction migration — backend follow-up

The current local Lane5 implementation no longer uses the old numeric-identity
gate or `runtime/loop/prose_correction.py`. If the remote branch still contains
that gate, migrate its callers to the current approval/delivery flow. This is
backend work; Ember needs no new fields or numeric comparison logic.

Numeric-token sets or occurrence counts cannot establish that a rewrite preserves
meaning. They can also reject a valid correction that repeats a verified number:
“2,488 employees match” may become “2,488 employees match; showing the first 1,000
of those 2,488.” Do not replace the old gate with a looser numeric regex.

Migration steps:

1. Remove numeric-token comparisons from the correction path. Delete the old
   module only after migrating callers and checking for other responsibilities.
2. Have the judge review the complete corrected takeaway with the unchanged
   answer components and evidence. Preserve numeric values and requested meaning.
   Changes to calculations, evidence or component selection require repair, not
   a prose-only correction.
3. Use `runtime/loop/approved_answer.py::validate_correction()` to enforce the
   approval contract: reviewed approval, `repair_type="prose"`, non-empty
   corrected text, and no conflicting violation or feedback.
4. Apply the correction to the takeaway, update its fingerprint, and bind delivery
   to the reviewed evidence and current access scope. Keep the response, answer
   document and saved history consistent.

Reference the correction path in `runtime/loop/agent_loop.py`, the instructions in
`runtime/loop/answer_judge.py`, and the approval/delivery functions in
`runtime/loop/approved_answer.py` (all under `src/data_agent/`).

Regression coverage: valid repetition of a verified number ships; malformed
corrections cannot clear a rejection; selected components and evidence stay
unchanged; corrected text matches the saved history. Local reference tests are
`tests/runtime/loop/test_judge_prose_correction.py`,
`tests/runtime/loop/test_approved_answer_delivery.py`, and
`tests/runtime/test_answer_document.py`. These tests validate runtime enforcement;
they do not prove that model review will never miss a semantic error.

## A15(e): scope-refusal schema parity — backend verification

The current local `declineOutOfScope` schema and handler agree: `request` is
required and `serves_intents` is optional. Unknown fields and malformed tags are
rejected. The request must match part of the original user question, allowing
case and whitespace normalization.

If the remote branch still rejects the advertised `serves_intents` field, align
`src/data_agent/runtime/composite/scope_refusal.py` with
`src/data_agent/runtime/mcp/tool_schema.py`. Preserve strict argument validation;
the loop validates and persists intent associations before dispatch, while direct
handler calls also accept the optional string-list field.

For mixed requests, bind the refusal only to its matching unsupported intent and
finish the supported work. Example: “Count active employees and give the weather
in Paris” should deliver the verified count plus a weather-scope limitation.
Missing data, access denial and unavailable services are not out-of-domain refusals.
No new UI fields are needed.

Local verification: 28 tests passed across
`tests/runtime/loop/test_scope_refusal.py` and
`tests/runtime/mcp/test_tool_schema.py`, including mixed-request completion,
direct calls with intent tags, invented-request rejection and malformed arguments.
This confirms the local implementation, not the remote branch or deployment.

## Exports and validation

CSV cells remain raw, without currency symbols, grouping separators or display
rounding. Headers include declared units/currency/percentage scale, e.g.
`Gross Earnings (USD)` and `Denial Rate (ratio)`. Identifier strings retain leading
zeros in the file; spreadsheet import settings must treat those columns as text.

Invalid authored formats fail blueprint loading. Runtime formatting never
reinterprets identifiers as quantitative chart measures: a chart selecting such a
measure becomes an explained table for judge review. Formatting is not permission
to add, aggregate, convert currencies, infer a missing ZIP prefix, or infer accrual
plan units. Raw results and query predicates remain unchanged.

Verification should cover first answer, saved answer, page 2, refresh/replacement,
CSV, nulls, negative money, large identifiers, and both percentage scales. The
current corpus audit covers 188 blueprints and all 770 terminal output columns,
including the three composed blueprints. Corpus manifests must be regenerated and
the corpus rehydrated before these declarations become active in a deployment.

## Freshness display ownership

`answer_document.sources[result_id].data_as_of` is supplied exclusively by backend
execution receipts and copied by the composer. The model does not provide, infer,
or rewrite it. The UI may display this structured value; when absent, do not
substitute the current date, a query event date, or snapshot creation time.

Agent/judge answer text must not narrate freshness, even when the metadata is
known: no data-as-of statements or claims that results are live, up to date,
stale, or recently refreshed in takeaways, titles, explanations or caveats.
Requested reporting periods, event dates and population labels such as “current
employees” remain valid and are distinct from source freshness. This policy
requires no new wire fields.

## Corrected assumptions and derived formatting

`recordAssumptions` now replaces the current-turn list; `[]` clears it. Rendering,
resume and history use the last successful list. Normalized duplicates are shown
once, including copies in section caveats; semantic paraphrases are consolidated
by the agent/judge. The UI should render the resulting document without re-adding
assumptions from earlier drafts. No new answer-document fields are required.

Ad-hoc UNION/INTERSECT/EXCEPT output formats are propagated by column position
when branch semantics agree (numeric kinds/precision widen safely); conflicting units or scales stay text. CTE and derived-table
column aliases carry the corresponding source format. Reporting completeness must
not be inferred from the latest event date or absent later rows.


### Automatic presentation selection

The agent selects the appropriate presentation without asking users to choose a
chart, table, display format or CSV. Large record sets and results unsuitable for
charts use a table; the UI owns paging, display guards and CSV download. Display
size alone must not narrow the requested population or introduce a top-N limit.
The clarification judge rejects rendering-only questions and directs the agent to
proceed. Genuine business ambiguity remains eligible for clarification. Explicit
user presentation preferences are honored when supported, with table fallback
when needed. No UI contract fields change.
