# Contract-lock: Inbox type-segregation + archive-on-reject

Status: locked (Wave-0). Builders: backend-developer (service + BFF + store),
frontend-developer (`ui/static/inbox.html`). Disjoint files → parallel.

## Motivation

- Reviewers want the inbox **segregated by candidate type** (blueprint,
  global_knowledge, user_knowledge, schema_edit) rather than a flat list.
- **Reject must archive, not delete.** The write side *already* archives in
  place — `apply_human_decision(env, "reject")` CAS-writes `status="rejected"`
  and keeps the row (D29, negative training signal). The gap is that rejected
  rows are (a) invisible (list filters to `in_review`) and (b) TTL-evicted in
  the Couchbase store. This change makes them visible and durable.

## List API contract (the seam)

`GET /api/inbox` (BFF) → proxied to service `GET /inbox`.

- New **optional** query param `status`:
  - Allowed values: `in_review` (the default when omitted) and `rejected`.
  - BFF validates against `{in_review, rejected}`; any other value → HTTP 400
    (do not proxy). Service also validates defensively.
- Response shape is **unchanged**: `{ "items": [ ... ], "count": <int> }`.
- Each item gains a **`status`** field (string; here `"in_review"` or
  `"rejected"`). All existing fields stay: `candidate_id`, `type`, `reason`,
  `summary`, `payload_view`, `evidence_refs`, `entity_scan`, `dedup`,
  `created_at`.
- `type` is one of: `blueprint | global_knowledge | user_knowledge | schema_edit`
  (`extractor/models.py:20`). Already present on every item today.

## Actions (unchanged behavior)

- `POST /api/inbox/{id}/{action}`, action ∈ {approve, reject, retract}.
- Approve/reject remain valid only on `in_review` items. Reject transitions
  `in_review → rejected` and retains the row (no write change needed).
- In the **Archived** view the frontend hides all action buttons (terminal
  items). The service must still reject an approve/reject on a non-`in_review`
  item (existing guard in `ReviewInbox`), so this is defense-in-depth, not the
  only guard.

## Frontend (client-side only, no new data needed beyond `status`)

- **Type tabs:** fixed set of all four types with live count badges (show 0s).
  Grouping is computed client-side from `item.type` over the fetched set.
- **Status toggle:** `Review queue` (fetch `?status=in_review`) ↔ `Archived`
  (fetch `?status=rejected`). Default = Review queue. Switching refetches.
- Tab counts are computed from the currently-fetched status set.
- Archived cards show a `REJECTED` badge (`data-testid="inbox-status"`) and
  omit approve/reject/retract.
- Preserve the existing two-step-confirm on approve/reject in Review queue.
- Keep existing `data-testid`s (`inbox-reason`, `inbox-type`, `answer`, etc.);
  add `data-testid="inbox-tab"`, `inbox-status-toggle`, `inbox-status`.

## Retention

- `CouchbaseCandidateStore.put`: for **terminal** statuses (`rejected`,
  `validated`, `retired`) write with **no expiry** (`expiry=0`) so archived
  rows persist; transient statuses keep the existing
  `learning_candidates_ttl_seconds` TTL. In-memory store already persists.

## Deploy note (retention backfill)

The no-TTL rule is **prospective** — it only takes effect on the next `put` of
a candidate. Any row already `rejected`/`validated`/`retired` in the Couchbase
bucket before this change keeps its original ticking TTL and will still be
evicted when it lapses (the D29 signal loss this change set out to stop). If a
deployed bucket already holds terminal-status rows worth keeping, run a one-time
backfill at deploy: select terminal-status ids and re-`put` (or `touch` with
expiry 0) each. No-op on an empty/fresh bucket.

## Out of scope

- No change to the reject/approve *write* path or the promotion scheduler.
- No server-side type filtering (types grouped client-side).
- `retract` stays disabled in these views (only applies to `validated`).
