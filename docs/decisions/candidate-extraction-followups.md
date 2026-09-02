# Candidate extraction and review follow-ups

Status: deferred after the September 2026 extraction-correctness and judge-retry work.

This document records the choices made during the end-to-end audit so later development does
not have to reopen the product decisions. None of these items authorizes automatic production
landing: every mined blueprint remains human-reviewed.

## 1. Server-side review ranking

Materialize a review rank whenever its inputs change. Store at least `measured`, `score`, and
`created_at`, add the Couchbase index needed to order by them, and apply the page limit only
after server-side ordering. Measured candidates sort first by descending score; unmeasured
candidates follow in arrival order. Pagination must use a stable compound cursor.

Do not ship the tempting interim fix of fetching a larger client-side window. It merely moves
the point where later high-quality candidates disappear.

## 2. Evidence summary and detail drawer

Replace raw evidence identifiers on the card with `Evidence (n)` and concise labels such as
`Turn 2 · runQuery`. Expanding shows the evidence list; selecting an entry opens a drawer with
the source turn, tool, captured quote or query context, and full identifier. Existing leakage
withholding applies to every drawer field and endpoint response.

## 3. Static-check label

Rename the UI label `explain` to `catalog`, with help text: "Tables and columns resolve against
the catalog." Keep the stored `explain_ok` field for backward compatibility. This is a display
change, not a claim that the service executed warehouse `EXPLAIN`.

## 4. Ranked list and detail layout

Replace one-tall-card pagination with a compact ranked list and selected-candidate detail panel.
The list shows reason, type, intent, score/measurement state, check summary, age, and status.
The detail panel retains SQL/DAG presentation, evidence, trial run, assistant revision, and
review actions. Preserve keyboard focus and selection across refreshes and transitions.

## 5. Explicit capability profiles

Define `read_only`, `blueprint_review`, and `full_review` profiles. Each profile declares required
stores, model clients, judges, probes, and credentials. Startup fails if the selected profile
cannot provide its contract. Expose one backend capability document and have the UI render only
supported controls, plus a visible environment/capability indicator. Intentional lower profiles
are valid; accidental partial wiring is not.

## 6. Canonical local configuration and full-stack launcher

Provide one checked-in non-secret development configuration consumed by Docker Compose and every
launcher, with ignored overrides for real secrets. Each daemon must preflight its Couchbase
bucket/scope permissions and other mandatory dependencies before entering its loop. Provide one
supported command that starts dependencies, waits for health, then starts runtime, sweeper,
consumer, scheduler, inbox, and UI and reports the selected capability profile.

The launcher must make a missing mining daemon visible. A healthy chat UI alone is not evidence
that candidate extraction is operating.

## Acceptance expectations

- Ranking remains correct with more than 100 review rows and across page boundaries.
- Evidence details never bypass entity/leakage withholding.
- The `catalog` label is present without renaming persisted data.
- Reviewers can scan and select candidates without traversing full cards one at a time.
- Every capability profile has startup-failure and UI-contract tests.
- A clean-machine local-stack test proves all daemons are running with one consistent credential
  set and that one novel session reaches the review queue.
