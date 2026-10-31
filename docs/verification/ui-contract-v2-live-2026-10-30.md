# Answer-document v2: live contract and formatting verification

Date: 2026-10-30. Scope: local real-model/real-service testing; not production or remote Ember certification.

## Verdict

**Contract PASS: 16/16 live answers.** Each passed SQL-only schema-v2 checks, source SQL matching executed queries, persisted-document parity, saved paging, CSV-part download, refresh/replay, and exact Phoenix delivery/SSE equality. Requested presentation was preserved in 15/16; the remaining case produced an explained, judge-approved facts-to-table fallback.

Eight additional checks within these cases ran the actual reference UI assets in Chromium. Its BFF calls were bridged to the running Lane5 HTTP server with session authorization; result rows and download bytes were real, not mocked. Table, facts, bars and lines exposed table/CSV controls; figures did not. This does not verify the remote Ember adapter or the reference BFF JWT lookup itself.

## Environment and fixture repair

- Model: Fireworks `accounts/fireworks/models/deepseek-v4p1-flash`, live agent and judge.
- Real local ClickHouse, MCP, token service, retrieval/hydrator, Phoenix and Couchbase snapshot storage.
- Rehydrated the current formatted corpus before running cases.
- Found the earlier empty-result cause: the documented test-principal access fixture has a 24-hour TTL and had expired. Reinserted the exact documented access rows; row policies remained enabled.
- Used an isolated API container mounted to the clean clickhouse-api source, with exports enabled and CSV part size set to three rows. No clickhouse-api repository changes.
- Temporarily populated three synthetic employees with ZIP/phone fixtures; restored those fields to NULL after the run. No production data was used.

## Cases

| Case | Delivered kind | Formatter calls | Blueprint calls | Seconds | Contract |
|---|---|---:|---:|---:|---|
| v2_share_table | table | 1 | 0 | 66.08 | PASS |
| v2_blueprint_figure | figure | 0 | 1 | 57.58 | PASS |
| v2_monthly_line | line | 0 | 1 | 54.41 | PASS |
| v2_facts | table | 1 | 0 | 36.74 | PASS |
| v2_department_bars | bars | 0 | 1 | 32.45 | PASS |
| v2_report_identifier | table | 1 | 0 | 39.81 | PASS |
| v2_repair | bars | 0 | 1 | 28.8 | PASS |
| v2_salvage | bars | 0 | 1 | 11.84 | PASS |
| v2_zip_phone | table | 1 | 0 | 58.03 | PASS |
| v2_zip_phone_transform | table | 1 | 0 | 60.83 | PASS |
| v2_integer_reference | table | 1 | 0 | 55.08 | PASS |
| v2_browser_figure | figure | 1 | 0 | 37.69 | PASS |
| v2_browser_line | line | 0 | 1 | 59.61 | PASS |
| v2_browser_facts | facts | 0 | 1 | 58.49 | PASS |
| v2_browser_bars | bars | 0 | 1 | 34.89 | PASS |
| v2_browser_percent | table | 2 | 0 | 100.24 | PASS |

## Formatting and identifier results

- All eight blueprint-backed cases made zero formatting calls.
- Nine formatter calls across eight ad-hoc cases. One case required a second call because review produced a new SQL result; it was not repeated formatting of the same receipt.
- Phoenix recorded `reasoning_effort: none` and zero reasoning tokens for every formatter call. Structured metadata was applied before the delivered document was reviewed.
- Percentage output used an explicit scale: SQL already multiplied by 100, so the formatter selected `scale: percent`; the browser displayed 100%, not 10,000%.
- Numeric `CAST(75039 AS UInt32)` arrived as generic integer metadata and was corrected to identifier; the browser displayed 75039 without commas.
- ZIP values `02108`, `75039-1234`, and `75039` survived UI rendering and CSV exactly.
- Phone values `+1 202-555-0100`, `(202) 555-0123 ext 12`, and `0044 20 7946 0958` survived UI rendering and CSV exactly.
- The explicitly requested phone-space removal produced `+1202-555-0100`, `(202)555-0123ext12`, and `00442079460958`. That transformed column remained text, which preserves the value and prohibits quantitative chart treatment; it was not converted to a number.
- Employee codes remained identifiers. Counts remained integer measures; USD blueprint metadata stayed intact.

## Transport and tracing

- Sixteen saved sources checked; two line sources exercised multiple CSV parts. Every part kept its own header and aggregate CSV row counts matched the saved result.
- Verified identifier/text CSV cells against the corresponding saved JSON values, including leading zeros and punctuation.
- Initial SQL request returned first rows and snapshot ID; later requests used only snapshot ID and bounds. No column_formats request field.
- Repeated SQL reused its saved snapshot; explicit refresh returned a replacement and reset_table, and repeating that refresh reused the replacement.
- Couchbase CSV bytes streamed through the authenticated Lane5 proxy. Direct presigned S3 byte downloads were not exercised.
- All sixteen Phoenix traces had connected parentage and terminal delivery matching the captured SSE object. Project: `answer-document-battery`.

## Findings and disposition

1. **Facts shape mismatch:** the agent chose facts over a two-row Active/On Leave result. The backend correctly supplied an explained table fallback, which the judge approved. This is a presentation-fidelity miss, not malformed v2 data. A separate one-row facts case passed.
2. **Duplicate table title in reference UI:** live DOM showed the section heading and grid heading repeating the title. Fixed by letting primary tables use their existing grid heading. Seventeen focused browser regression tests passed afterward, including title uniqueness, control ownership, formatting and snapshot recovery. The recorded live DOM predates this fix; no new live-model run was claimed for that one-line UI change.
3. **Repeated caveat:** the contact answer repeated the current-population/ZIP exclusion caveat in different words. The composer now removes normalized duplicate caveats across sections before review, and section authors are instructed not to restate recorded assumptions. Semantic paraphrases do not block delivery or trigger repair. No output-boundary suppression was introduced.
4. **Actual judge timeout:** the repeated percentage case needed population repair and then hit review timeout. Salvage plus final presentation approval delivered the corrected table. The injected judge rejection and injected timeout cases also delivered valid documents.

## Evidence and reproduction

- `/tmp/v2-contract-live.json`: eight core cases, including injected repair/salvage.
- `/tmp/v2-identifiers-live.json`: contact identifiers, integer identifier, figure controls.
- `/tmp/v2-browser-live.json`: line/facts/bars/percentage browser cases.
- `/tmp/v2-phoenix-audit.json`: trace connectivity, exact final payload, formatter reasoning/token checks.
- `/tmp/v2-csv-audit.json`: sixteen source exports, exact text/identifier preservation.
- `/tmp/v2-title-check.log`: 17 passing post-fix browser tests.

The probe now accepts `--mcp-url` and `--export-base-url`; providing the latter enables live saved-result and reference-browser checks. Use explicit `--cases` to select custom question names; omitting it also runs the predefined cases. API/token/export stores must be available and the local access fixture must not have expired.

Selected Phoenix traces:

- [v2_integer_reference](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/9725366ddc446fd0d7ad7b205468f8f1)
- [v2_zip_phone](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/c291524a2cd59fd0a7fc24b725d19ede)
- [v2_blueprint_figure](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/ec300788f1b85c3f5ed8550620ef6588)
- [v2_browser_percent](http://localhost:6006/projects/UHJvamVjdDoxMw==/spans/a1ba85a3af5520740039156368a0e633)

No commit or push performed. Workgate remains stashed.

## Caveat deduplication follow-up

The experimental `repeated_caveat` rejection and its repair probe were removed:
cosmetic repetition must not block an otherwise correct answer. Keep deterministic
composer cleanup and authoring guidance only. Earlier injected-repair live runs
exercise the discarded behavior, not the current policy.
