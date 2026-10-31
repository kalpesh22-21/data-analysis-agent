# UI assumptions in answer-document v2

`recordAssumptions` replaces the current-turn list; `[]` clears it. Resume and
history use the last successful list. Normalized duplicates are removed; the
composer removes normalized duplicate caveats across sections before review.
Section authors should not restate recorded assumptions, which the composer
includes automatically. Semantic paraphrases are cosmetic and must not trigger
judge rejection or a repair cycle. Distinct populations, periods and limitations
must remain intact.

The composer places remaining approved assumptions in section caveats, or a text
section when no data section exists. Render the document once. Do not append an
old top-level assumptions array, earlier draft lists, or tool output to it.
Corrections must preserve the relationship between assumptions and the reviewed
answer; a changed assumption may require another review.

Scope-filtered history controls visibility of the saved answer. Replaying a saved
assumption does not authorize queries or restore hidden data. The UI must not infer
freshness from assumption prose; data-as-of is code-provided source metadata only.

See [answer-document v2](../ui-answer-document-spec.md) and
[value formatting](ui-value-formatting-contract.md). Sources hold SQL and formats;
the UI fetches saved pages and offers table/CSV options for every data result except
figures. Text has no data controls. No assumptions-specific transport field is added.
