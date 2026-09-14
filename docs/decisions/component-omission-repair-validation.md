# Targeted component omission during answer repair

A rejected card or table can now be removed while retaining unrelated evidence for a partial answer. The existing initial review plus one final validation remains capped at two calls. No per-claim approval ledger or automatic SQL rerun was added.

## Implementation

The judge receives an explicit catalog of selected components and execution result IDs. Capability IDs also appear directly beside each card. `omit_component` is a structured repair action. The runtime validates every target against the current scope-filtered selection before making any removal. Unknown, unselected or ambiguous references do not remove anything. A capability coverage/intent violation cannot target a warehouse table. Shared selection aliases are treated as ambiguous.

Validated exclusions persist in the turn's review state. The next proposal is filtered before dispatch, persistence and judge review, so a stale card/table reference cannot restore an excluded component. Legacy SQL/blueprint aliases are filtered too. Resume reconstruction removes excluded payloads. Unrelated query results remain available.

The omission instruction replaces conflicting rewording feedback. The agent must answer from remaining evidence, disclose missing parts and avoid turning an empty result into a claim of global nonexistence. The second judge reviews remaining evidence and a separate record of excluded components. Its repair-specific instructions distinguish a disclosure that information could not be provided from a factual claim that such information does not exist.

A second rejection still uses the safe fallback. Successful execution or removal of a component alone does not approve the remaining answer.

## Validation

- 211 targeted tests passed, including 13 component-omission tests and all 87 remote-runtime contracts.
- 4 real Couchbase tests passed, including persistence of excluded components through a fresh store instance.
- Tests cover invalid and ambiguous targets, wrong component type, stale reintroduction, legacy aliases, preservation of another table, matching history, resume reconstruction, omission feedback replacing conflicting feedback, removal from second-review evidence and no third judge call.
- Ruff and `git diff --check` passed.
- Live P04 remained verified and judge-approved during development.
- Final live GPT-4.1 P07, `bat17_p07_c6509e411e`, succeeded through the intended repair: first judge rejected the identifier card with `omit_component`; salary SQL executed once and returned zero rows; the next proposal omitted the card and explicitly disclosed the SSN limitation; second judge approved. No capability card or grid was returned. Live answer and history match.

Final P07 trace: `3637742c5b06db7814ec87fdd77977b3` in Phoenix project `data-agent-runtime`.

The final response was:

> No employees with the last name or full name 'Smith' were found in your records, so no salary data can be displayed. The Social Security Number (SSN) or country-specific personal identifier could not be provided or verified in this answer.

Its assumption explicitly described matching last name Smith or a full name beginning with Smith. This is a finding about accessible queried records, not proof of absence outside that scope. The external identifier card renderer was not executed.

## Earlier live findings

Development runs bat11–bat16 continued to fall back. They exposed conflicting rewording/omission instructions, overgeneralization from an empty salary query, rejection of honest missing-data disclosures and a capability rejection targeting a salary-table ID. These failures were retained and used to refine the implementation; the final successful run is not a claim of a perfect model pass rate. Ambiguous or failed final reviews can still produce a safe decline.

Artifacts: `/tmp/component-repair-final-tests.log`, `/tmp/component-repair-couchbase-tests.log`, `/tmp/probe-bat17-results.json`, `/tmp/probe-bat17-spans.json`. Earlier attempts are retained under `/tmp/probe-bat11*` through `/tmp/probe-bat16*`.

Backend port 18104 remains running with nested, unredacted judge traces and a 60-second answer-judge timeout.
