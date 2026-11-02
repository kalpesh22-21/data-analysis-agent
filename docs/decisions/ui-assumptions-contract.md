# UI assumptions in answer-document v2

Caveats default to `[]`. Include only material qualifications needed to interpret
the answer: population, calculation/exclusion rules, reporting basis, or missing
data. A qualification already clear in a title, explanation or takeaway does not
need a second caveat. Do not manufacture boilerplate to populate the field. The
field remains present in the v2 contract; an empty list is valid.

The model and judge assess whether disclosure is sufficient. The backend also
removes normalized whole-statement duplicates found in displayed text, before
review and at sealing. It does not delete substring matches inside negated or
more qualified statements, or infer equivalence from a short title. Essential
qualifications remain even if other caveats are removed. No SQL rerun is needed.

`recordAssumptions` replaces the current-turn list; `[]` clears it. Resume and
history use the last successful list. Normalized duplicates are removed. The composer cleans the combined caveats and
recorded assumptions across sections before review, and repeats caveat cleanup
when sealing a corrected or partial document. The first wording is retained.

A deterministic canonical key also consolidates recognized whole-statement
headcount definitions with explicit Active + On Leave populations (including
“Active or On Leave” and “current employees (Active plus On Leave)”). This is
bounded semantic normalization, not general similarity matching. When a full
headcount definition already specifies Active + On Leave, a standalone “Current
means Active plus On Leave” definition is redundant; keep the full definition
regardless of section order. Unknown wording,
Active-only populations, record counts, dates, department restrictions and
exclusions remain distinct. “Current employees” alone does not establish an
explicit population and is not silently merged.

The server removes recognized chart/preview row-limit and paging/CSV instructions
from assumptions and caveats. Business sentences adjacent to a removed UI
sentence remain. Source limitations (for example, a CSV source excluding a
population) are not UI instructions. A generated chart-to-table fallback reason
belongs in the section explanation and remains visible to the judge and user.
Section authors should not restate recorded assumptions, which the composer
includes automatically. Unrecognized semantic paraphrases must not trigger
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
