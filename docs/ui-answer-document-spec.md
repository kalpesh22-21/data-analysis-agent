# Unified answer document UI implementation specification

Status: **Backend implemented locally; Ember/BFF implementation pending.**
Date: 2026-10-29
Audience: Ember UI, PHP/BFF, and Lane5 runtime developers

This specification replaces independently rendered prose, tables, blocks, and capability cards with one approved answer document. The UI renders its ordered sections faithfully. The default is zero or one visible table per answer; additional tables must answer distinct requested needs. Supporting data remains accessible without duplicating the main presentation.

This is a coordinated, breaking change for new answers. Implement one live answer contract and remove superseded live rendering paths. Ember owns conversation display and saved-result loading. Backend session history and execution context remain intact for follow-up turns; no backend history migration is required by this change.

## 1. Responsibility and processing order

| Owner | Responsibility |
|---|---|
| Agent | Propose takeaway, sections, evidence references, presentation choices, titles, and relevant prepared actions. Justify each additional visible table internally. |
| Backend | Validate references and semantics; build values and blocks from actual results; prepare an explicit fallback. |
| Judge | Review the complete proposed answer, including presentation, titles, coverage, caveats, duplication, and fallbacks. |
| UI | Render approved order and associations; provide paging, expansion, accessibility, and responsive sizing. Never infer new measurements or combine results. |

Flow: agent proposal → backend construction → whole-answer review → final validation → persistence and delivery.

A repair that changes sources, calculations, or meaning returns through review. Mechanical removal of a duplicate or collapse of an already-reviewed table requires deterministic validation. Failed repair delivers only supported sections with an explicit limitation. No rejected component may reappear through a fallback.

A separate presentation LLM call is not required initially. Extend the existing agent and judge interfaces. The UI receives only approved documents, never proposals or judge repair messages.

## 2. Product rules

- One answer document per completed turn, with a takeaway and zero or more ordered sections.
- Simple answers need no visible section heading. Sections group outcomes, not internal query steps.
- Query execution does not authorize display. Intermediate, diagnostic, and verification results remain hidden unless explicitly selected to answer the user.
- Default to zero or one visible table across the entire document. This is a strong preference, not a hard cap. Explicit requests for separate tables take precedence.
- Each additional visible table must contribute a distinct requested outcome that existing components cannot adequately cover. The judge penalizes redundancy without penalizing necessary coverage.
- A scalar uses prose or a figure; a one-row source table is supporting data, not an automatically visible grid.
- A chart's supporting table appears through “View data,” never again as an unrelated table below the answer.
- Do not reduce visible tables by combining incompatible grains, periods, populations, or units.
- Minimize presentation, not verification or requested information.

## 3. Wire types

The following TypeScript defines the implemented JSON contract. Fields are required unless marked optional. IDs are opaque strings, timestamps are UTC RFC3339 strings, and numeric values must be finite JSON numbers. Plain-text fields do not accept HTML. Only `takeaway`, `explanation`, and text presentation content accept sanitized Markdown.

```ts
type AnswerDocument = {
  schema_version: 1;
  answer_id: string;
  revision: number;                 // positive, monotonic within answer_id
  generated_at: string;
  takeaway: string;                 // nonempty Markdown summary
  sections: AnswerSection[];       // authoritative display order
  sources: Record<string, ResultSource>; // keyed by result_id
};

type ResultSource = {
  retrieved_at: string;
  data_as_of?: string;              // actual source freshness, only when known
  row_count: number;                // nonnegative integer; count for this result
  completeness: 'complete' | 'partial' | 'unknown';
  table_ref?: string;               // opaque server-authorized paging handle
};

type AnswerSection = {
  id: string;
  title?: string;                   // explicit plain-text title
  explanation?: string;             // Markdown, no internal process narration
  evidence_refs: string[];          // opaque approved evidence references
  presentation: Presentation;
  supporting_table?: TableView;
  fallback_table?: TableView;
  caveats: string[];                // plain text; always visible beside the section
  actions: PreparedAction[];
};

type TableView = {
  result_id: string;
};

type PreparedAction = {
  capability_ref: string;           // exact approved, prepared capability
  label: string;
};

type Presentation =
  | { kind: 'text'; markdown: string }
  | { kind: 'figure'; result_id: string; label: string;
      value: string }
  | { kind: 'facts'; result_id: string;
      items: { label: string; value: string }[] }
  | { kind: 'table'; result_id: string }
  | { kind: 'bars'; result_id: string; measure_label: string;
      mode: 'comparison' | 'ranking';
      ranking?: { direction: 'ascending' | 'descending';
                  ties: 'dense' | 'competition' };
      items: { label: string; value: string; raw: number; rank?: number }[] }
  | { kind: 'line'; result_id: string; measure_label: string;
      points: { time: string; label: string;
                value: number | null; formatted_value: string | null }[] };
```

Contract invariants:

- Every data presentation has exactly one `result_id`, present in `sources` and approved for that section. A section can cite other evidence without displaying it.
- `supporting_table` and `fallback_table` must reference the presentation's same result. A table presentation must not also declare either field. A text presentation must not declare either field; use another section when a table is required.
- Every table view requires a valid `sources[result_id].table_ref`. Do not use raw SQL as UI identity or send SQL in the document.
- Source entries contain only data needed by delivered sections. Do not serialize all intermediate query results.
- Chart series joining, arithmetic, and aggregation never occur in Ember. A comparative query may return multiple periods in one result; initial line charts display one measure series.
- Ranking bars require ranking metadata and backend-generated ranks. Comparison bars have no ranks. The frontend neither sorts items nor assigns index-based ranks.
- Figures and facts carry backend-formatted values. Chart geometry uses raw numeric values; displayed values use the supplied formatting. Null is missing, never zero.
- Plain prose may remain a text-only document. A nonempty takeaway is required for agent finalization.
- Section IDs are unique, stable through repairs/refresh where the section retains its identity. `answer_id` stays stable while `revision` increases.
- `schema_version` identifies this single contract, not a license to render legacy payloads. Unsupported versions fail explicitly.
- Actions reference existing prepared capability state; the UI must not reconstruct executable arguments from labels or prose. Existing authorized capability hydration/opening behavior must resolve the reference. Arbitrary routes, HTML, SQL, and model-generated executable payloads are not accepted.

These field names match the backend implementation. Snapshot expiry comes from the snapshot service response and is stored separately by Ember/BFF. Units and periods are included in the approved labels; the UI must not infer them. Use the example below and backend integration tests in `tests/runtime/test_answer_document.py` when implementing the renderer.

## 4. Terminal envelope and saved conversations

Retain non-presentation turn identity, status, authentication, error, review, and tracing fields. For completed turns, `answer_document` is the only presentation payload. For paused turns, it is absent; the approved `pending_question` or existing budget-resume controls remain the interaction payload.

Replace presentation fields such as `assistant_text`, `answer_tables`, `answer_sql`, `blueprint_use`, `verification`, `assumptions`, `capability_cards`, `layout`, and `blocks` with the appropriate document content or section/source metadata. Internal evidence, verification, provenance, and authorization records still exist server-side; removing wire fields must not remove those protections. Caveats formerly surfaced as assumptions belong to the relevant section or takeaway and remain reviewed.

Ember/BFF saves the approved document with its existing conversation records and the exact associated S3 result/snapshot references. On reload, render that saved document and read the saved results; do not call Lane5 for the complete conversation or unconditionally reexecute SQL. Existing snapshot recovery may rebuild during a read when the access scope changes. Backend session history remains available independently for agent context and existing consumers. Existing scope and ownership checks still apply to saved-result access; an old snapshot does not grant access after permissions change.

## 5. Example completed answer

The example uses fictional data and omits unrelated transport metadata.

```json
{
  "status": "done",
  "answer_document": {
    "schema_version": 1,
    "answer_id": "answer-42",
    "revision": 1,
    "generated_at": "2026-10-29T18:00:00Z",
    "takeaway": "There are **250 employees** in the selected population. Sales has the highest department payroll.",
    "sections": [
      {
        "id": "headcount",
        "title": "Employee headcount",
        "evidence_refs": ["result-headcount"],
        "presentation": {
          "kind": "figure", "result_id": "result-headcount",
          "label": "Employees", "value": "250"
        },
        "supporting_table": {"result_id": "result-headcount"},
        "fallback_table": {"result_id": "result-headcount"},
        "caveats": ["Includes records you are authorized to access."],
        "actions": []
      },
      {
        "id": "payroll",
        "title": "Payroll by department",
        "evidence_refs": ["result-payroll"],
        "presentation": {
          "kind": "bars", "result_id": "result-payroll",
          "measure_label": "Payroll (USD)", "mode": "ranking",
          "ranking": {"direction": "descending", "ties": "dense"},
          "items": [
            {"label": "Sales", "value": "150,000", "raw": 150000, "rank": 1},
            {"label": "Operations", "value": "100,000", "raw": 100000, "rank": 2}
          ]
        },
        "supporting_table": {"result_id": "result-payroll"},
        "fallback_table": {"result_id": "result-payroll"},
        "caveats": [],
        "actions": []
      }
    ],
    "sources": {
      "result-headcount": {
        "retrieved_at": "2026-10-29T17:59:00Z", "row_count": 1,
        "completeness": "complete", "table_ref": "table-headcount"
      },
      "result-payroll": {
        "retrieved_at": "2026-10-29T17:59:10Z", "row_count": 2,
        "completeness": "complete", "table_ref": "table-payroll"
      }
    }
  }
}
```

Initially this renders two sections and no expanded tables. Each “View data” opens its own result. It must not append two additional grids below the document.

## 6. Rendering and interaction

1. Render takeaway, then sections in the supplied order. Preserve item/point order within each presentation. Remove global `BLOCK_ORDER` sorting from this path.
2. Use `section.title` directly. Keep the measure label as part of the visual. Do not derive a card title from figure/heading precedence or consume the first block to form a title.
3. Reuse Figure, Facts, Bars, Chart, and paginated table components through adapters to the new contract. Do not route through the old independent blocks renderer.
4. “View data” expands a section-local table or an accessible drawer labelled with that section's title. Resolve its exact `table_ref`, retaining existing authorization, paging, snapshot, and download controls. Never open the first global table by default.
5. A visible table is appropriate for requested records or detailed comparisons. Default supporting tables to collapsed; retain expansion state by answer/section/result identity, not array index. Reset on source replacement.
6. Present caveats beside the section even when its table is collapsed. Retain units, periods, labels, and approved disclosures on mobile.
7. Resize or stack on small screens without changing source data or ranking. Give charts accessible labels and access to underlying data; use keyboard-operable expansion/actions with visible focus and correct expanded-state announcements.
8. Initial selectable presentations are text, figure, facts, table, bars, and line. Existing advanced components may remain in code but are not accepted as document presentations yet.
9. Display prepared actions within the relevant section. Actions are not charts, and they are never generated from arbitrary links embedded in a visual.

### Failure behavior

Validate document structure and section source bindings before rendering. Missing identity, duplicate IDs, invalid source mappings, or an unsupported document version invalidate the document: show a clear unavailable state and preserve the user's question. Do not guess or switch to a legacy payload.

For a valid document with a malformed or locally unrenderable visual, use that section's declared, validated `fallback_table`. Preserve title, explanation, and caveats. If it is unavailable, keep those texts and show “This view is unavailable.” An unknown presentation kind may only use a separately valid fallback; it must not become an empty success card.

When the fallback table is displayed, do not also show a duplicate supporting table control for that result. Record fallback use in telemetry. A fallback may cause more visible tables than planned; preserving the approved answer takes precedence over the display preference.

Never coerce malformed numeric values to zero. A missing line-chart point creates a gap, not a zero or interpolated value. Do not silently drop invalid rows and present the remainder as complete. Negative values require a suitable chart scale; otherwise use the declared fallback. Tables do not need all rows held client-side; charts require the backend's approved completeness/selection semantics.

## 7. Backend presentation and repair requirements

These requirements are prerequisites for truthful UI rendering, not frontend calculations:

- Retain exact approved evidence bindings. Remove the fallback that picks the only successful execution when no result was selected.
- A fully received SQL result is not proof of a complete population. Preserve top-N and sampled/filtered scope explicitly.
- Unit and aggregation behavior are distinct. USD, count, hours, or days never alone authorize totals, shares, or “All Others.” Such transformations require explicit validated semantics and reviewed output; defer automatic folding initially.
- Ranking direction and tie policy must match the question and executed result. “Largest” is not universally “best.” Do not encode a favorable color from increase/decrease alone.
- Captions/titles require factual review as well as scrubbing. A safe string can still misstate a metric or population.
- Repair only affected sections and reuse correct results. Presentation repair does not trigger new analysis queries. Wrong measurements or missing requested evidence may require a new query.
- Rejecting a result invalidates all dependent figures, charts, tables, text claims, and actions. Approved fallbacks cannot refer to rejected evidence.
- Presentation-economy checks are whole-document checks. Additional visible tables require distinct requested value; supporting tables must also be relevant.

## 8. Progress and approval states

Progress describes current work, never an outcome that still awaits review. Agent tool intent alone must not emit “Finalizing,” “Asking for clarification,” or equivalent promises.

Implemented progress payload:

```ts
type AnswerProgress = {
  turn_id: string;
  sequence: number;  // monotonically increasing within the active turn
  phase: 'analyzing' | 'checking_answer' | 'refining_analysis'
       | 'checking_information_need';
  message: string;   // backend-owned, user-facing wording
};
```

| Actual state | Display |
|---|---|
| Analysis is running | Concrete activity, e.g. “Checking department totals” |
| Proposed answer is under review | “Checking the answer” |
| Review requires correction | “Refining the analysis” |
| Proposed clarification is under review | “Checking whether more information is needed” |
| Clarification approved | Display the actual question; no speculative clarification status |
| Answer approved and persisted | Deliver the answer document directly |

Allowed transitions include checking-answer → refining-analysis → analyzing → checking-answer, and checking-information-need → analyzing when a question is unnecessary. Do not force a monotonic progress bar or fake completion percentage.

Keep the last useful message during short internal transitions. Coalesce rapid updates; proposed initial minimum display time is 1 second, bypassed by a terminal result, approved question, error, or cancellation. Ignore stale/out-of-order sequences and events for another turn. Terminal delivery ends progress; late progress must not overwrite it. Do not display judge names, rejection counts, or internal retry details.

Transport heartbeats keep the stream alive without changing visible status. Backend interval: 10 seconds using SSE comments, with BFF/proxy streaming configured to flush rather than buffer them (`X-Accel-Buffering: no` where honored). Verify this through the deployed proxy path. Heartbeats must not create transcript entries or live-region announcements. Persisted history contains approved answers/questions, not transient progress messages.

## 9. Persistence, freshness, and refresh

- Lane5 persists the exact approved document before terminal delivery. Ember/BFF persists that document with its own conversation record and associated S3 results. Ordinary reload uses these saved artifacts without query reexecution or a backend complete-history fetch. Preserve existing automatic snapshot recovery on access-scope changes. Do not recompute presentation from current heuristics.
- `retrieved_at` means query retrieval time. `data_as_of` means known data freshness. The snapshot service’s expiry timestamp means the deadline for snapshot access. Never substitute one for another.
- Expired snapshots disable unavailable paging/download actions and show the affected section's expiry notice. Preserve the authorized historical answer. Blocks-only sections still have source retrieval timestamps even without snapshots.
- Old retrieval time is not automatic proof of stale source data. If a business freshness threshold is needed, the backend supplies that policy separately; the UI must not invent it.
- User-requested refresh uses authorized backend analysis/refresh facilities. Automatic rebuilding during a read remains allowed for the existing access-scope recovery path. Keep the previous approved section visible with an “Updating” state while work proceeds. Replace it only when a higher document revision containing the refreshed section, its source metadata, supporting table, and fallback is approved and persisted.
- If a change affects a takeaway or other sections, include those dependent changes in the same atomic document revision. Never mix refreshed values with an old dependent summary/table. Ignore responses older than the currently rendered revision.
- A failed refresh retains the prior approved revision with a clear refresh failure notice. Permission denial suppresses unauthorized content instead of retaining it merely for continuity.

## 10. Coordinated implementation and migration

1. Freeze the shared schema and fixtures across Lane5, BFF, and Ember. Implement finalization proposal and judge changes, result handles, and explicit fallback validation.
2. Update new-document persistence and live serialization. Preserve backend session history, provenance, capability preparation, and execution context for follow-up turns.
3. Implement one Ember parser, state model, and renderer for new answers. Remove automatic block sorting/title extraction and parallel live presentation fields.
4. Bind every displayed table to its exact saved S3 result/snapshot in Ember/BFF storage. Reload and paging read that saved result; missing or expired results offer refresh. Preserve existing automatic rebuilding on access-scope changes.
5. Preserve existing saved conversations through Ember's existing storage mechanism. A Lane5 history migration is outside this change.
6. Coordinate runtime, BFF, and UI cutover. Handle in-flight old-version live requests explicitly through reload/retry rather than mixing presentation payloads.
7. Verify follow-up turns retain backend context and UI reload performs no query execution. Fix forward using declared table/text fallbacks when richer presentations are unavailable.

The existing `export_proxy.py` router proxies `/query/snapshots` creation, saved-snapshot paging, and downloads to clickhouse-api, which owns storage. Use `/query/snapshots/{id}/page` for saved results. The separate `/query/page` executes SQL and MUST NOT be used for reload or paging a saved answer. A `table_ref` identifies an authorized result, not an S3 object URL or proof that a snapshot exists; Ember/BFF must persist its exact snapshot association. Explicit refresh must also rebuild and review affected summaries/visuals before replacing the saved document; merely refreshing table rows would leave stale figures and prose.

On `SNAPSHOT_SCOPE_CHANGED`, retain the existing single recovery attempt: refresh the catalog scope and rebuild through clickhouse-api. A successful rebuild returns HTTP 409 with `SNAPSHOT_REPLACED`, the replacement snapshot, and `reset_table: true`. Ember must clear old rows and replace the snapshot reference; it must not append replacement rows to the old result. Propagate recovery denial without retry loops. Dependent summaries and visuals must not be presented as describing the replacement data until they are rebuilt and reviewed.
The existing remote title/history/pause issues are implementation dependencies to reconcile, not behavior to preserve. The latest Ember notes describe a snapshot-based staleness banner; reuse it where appropriate while adding section/source association and blocks-only timestamp handling.

## 11. Telemetry follow-up

The remote feature diffs describe composer decision telemetry that is not present in this checkout. Reconcile that work during integration and extend it with answer revision, section/presentation counts, visible/supporting table counts, fallback reason, repair category, and outcome. Record explicit decision reasons inside the composer rather than inferring them in a wrapper. Distinguish hint agreement from actual hint application.

Do not record SQL, cell values, titles, captions, or answer prose. Treat row counts as potentially sensitive result metadata rather than assuming they are content-free. Keep events associated with the originating turn trace, including terminal response emission; avoid orphan traces after the turn span closes. Telemetry must not mutate the approved response.

## 12. Acceptance tests

| Scenario | Required outcome |
|---|---|
| One scalar answer | Figure/text, no automatically visible one-row table |
| Five executions but two selected results | Only selected results display; verification queries remain hidden |
| Two-part answer with a figure and chart | Correct order and explicit titles; each View data opens its own result |
| Chart and supporting table | No duplicate table appended below the answer |
| Multiple visible tables | Judge requires distinct requested value; explicit user requests are respected |
| Unordered/tied ranking | Backend validates direction/ties; UI preserves approved ranks and order |
| Average salary in USD | No automatic sum/share/folding based on unit |
| Blank figure label plus section title | Explicit title renders once; no lede-selection ambiguity |
| Known result rejected during review | All dependent content and fallback references removed or repaired |
| One successful but unselected execution | No automatic card |
| Malformed/unknown visual | Valid declared table fallback, or explicit unavailable message; no empty card |
| Null, zero, negative, nonfinite numeric | Null stays missing, zero stays zero, unsupported geometry falls back, nonfinite rejected |
| Pause after successful data execution | Approved question/controls only, no unfinished answer document |
| Proposed clarification rejected | No “asking” promise or question flash; analysis continues |
| Proposed answer requires repair | Checking → refining → analysis as needed; no premature “finalizing” |
| Long judge call through BFF/proxy | Heartbeats arrive; current visible message remains; terminal stops progress |
| Reload after live delivery | Same approved content, ordering, title, source associations, and actions |
| Snapshot expires / blocks-only answer | Correct source-specific availability/timestamps; expiry is not called source freshness |
| Refresh races or fails | Atomic higher revision only; old dependent content never mixed with new |
| Scope narrows | Unauthorized content and dependent summaries withheld on history and paging |
| Mobile and keyboard/screen reader | Correct reading order, visible caveats/units, labelled accessible actions |
| Migrated/cached historical turn | New contract only; no legacy renderer or guessed visual binding |

Test actual rendered DOM from backend-produced JSON through the BFF and shared schema, not just isolated builder dictionaries. Include a deployed SSE/proxy check and live/history parity. Existing component snapshots remain useful but do not replace these cross-system tests.

## 13. Implementation handoff

Agreed product decisions: one coherent answer, fewer visible tables, explicit evidence binding, agent/backend/judge ownership, section-driven UI, fixed-forward migration, and approval-aware progress.

Lane5 implements the JSON contract, reference-resolution endpoints, and 10-second heartbeat described here. Ember/BFF still owns progress coalescing (suggested minimum: 1 second), snapshot associations, renderer cutover, and deployment verification. Those choices must not reintroduce dual presentation contracts or move semantic decisions into Ember.

Reference: [previous UI/backend contract](ui-backend-contract.md). This specification defines the implemented Lane5 presentation contract for Ember/BFF integration.


## 14. Backend integration details

- `POST /query/snapshots` accepts `{ "table_ref": "<sources[result_id].table_ref>" }` for a new answer. Lane5 resolves it against the session's approved document and current scope, then forwards the selected SQL to the existing clickhouse-api snapshot service. Do not send both `table_ref` and `sql`. Existing generic SQL snapshot callers are unchanged.
- Snapshot creation executes the selected SQL; it does not upload the earlier preview. Persist the returned snapshot ID with its exact result reference. Ordinary reload and pagination use that saved snapshot ID. Do not treat snapshot creation time as the original query retrieval time or as proof that independently executed results are identical.
- `GET /answer/action?answer_id=…&capability_ref=…` resolves a selected prepared action under the current session and scope. Use the returned prepared state; do not rebuild its arguments from display text.
- Backend session history is unchanged. New assistant records additionally retain `answer_document`; model context continues to use stored conversation and execution records.
- Each new turn currently emits revision 1. An atomic, reviewed refresh of a whole document is a coordinated follow-up; the existing snapshot rebuild alone must not relabel old summaries as newly verified.

## 15. Recovery delivery

Judge partial answers and terminal salvage use the same section selections as normal answers, including figures, bars, lines, and tables. The backend validates and materializes their source references before delivery. Ember renders the approved `answer_document` through the same renderer; no salvage-specific payload or renderer is needed.

A partial answer includes explicit unfinished work. A complete answer must satisfy the requested substantive scope, using the requested presentation or an approved, explained fallback. If a chart cannot render, the backend builds a table with an explicit reason and submits that exact candidate for judge approval. An approved, explained presentation fallback alone does not make the answer partial; missing substantive findings do. Missing data does not authorize a different metric or date basis without user confirmation.

## 16. Chart datatype handling and automatic fallback

The backend adapts warehouse values into the existing chart payload; Ember does not guess types or units. Normal answers, judge partial answers, and salvage use the same materializer.

| Field | Accepted source values | UI payload |
|---|---|---|
| Bar category | Non-empty strings, finite numeric scalars, booleans, dates/timestamps, null | String label; booleans use Yes/No and null uses Not available. Colliding labels require a table. |
| Bar/line measure | Finite integers/floats/Decimals and unformatted decimal/scientific numeric strings | JSON number for geometry; original precision retained in display text. Values outside safe numeric precision require a table. |
| Line period | ISO dates/timestamps, YYYY years (including four-digit integers), YYYY-Q1…Q4, YYYY-MM months | ISO time for positioning; source period label retained. Year/quarter/month use their first day as an axis anchor. |
| Missing measure | Null | Gap in lines. Bars fall back to a table; missing is never rendered as zero. |

Line periods must be distinct and have consistent granularity. The backend orders whole points chronologically, preserving each value, label and null gap; at least two periods are required. Mixed timezone-aware/naive timestamps, ambiguous locale dates and numeric epoch timestamps require a table rather than guessed dates. Currency symbols, percentage strings and locale separators are not inferred numeric units. Arrays/objects remain table data. Rows are never dropped, grouped or aggregated to make a chart fit. Bars and tables retain result order; only line points are ordered chronologically.

For a valid accessible result with a pageable source, unsupported chart data, incomplete previews, excessive preview size or invalid chart selectors automatically produce a table. The backend adds a caveat explaining why the requested presentation could not render. The judge reviews that exact table and reason before delivery. Recovery requires an additional bounded, non-reasoning approval only if materialization changed its selected presentation. The UI receives the approved table through the normal document renderer; no new widget object or separate salvage renderer is needed.

Source/scope failures still invalidate the answer component. A table fallback never authorizes inaccessible, failed or rejected evidence.

Blueprint corpus audit additions: the model may select `category_columns` (2–6 exact source columns) instead of `category_column` for bars. The backend joins their scalar labels with ` · `, checks the resulting labels for collisions, and preserves every row and measure. This supports department/pay-type, code/description, and other compound groups without falsely merging categories. The UI still receives ordinary `items[].label`, `raw`, and `value` fields.

Facts accept up to 16 selected columns from one row, covering the 12-measure workforce KPI blueprint. Source alias underscores become spaces in displayed fact labels. Existing source references remain exact. No new UI object type is introduced. Results containing multiple independent time series continue to require a suitable table or compound-category bars; they must not be flattened into one misleading line.
