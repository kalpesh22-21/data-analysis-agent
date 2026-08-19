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
    Later slices added three more to the same param: `needs_parameterization`
    (the fail-to-review work list), `validated` (the **promotable** set —
    auto-landed learning nodes awaiting a human verify/promote), and `promoted`
    (the terminal set whose YAML can still be **re-emitted** idempotently, so a
    lost PR is recoverable by someone who can find the row).
  - BFF validates against `{in_review, rejected, needs_parameterization,
    validated, promoted}` (`ui/server.py::_INBOX_LIST_STATUSES`); any other value
    → HTTP 400 (do not proxy). Service also validates defensively
    (`inbox/service.py::_LISTABLE_STATUSES`).
  - Ordering: the terminal archives (`rejected`, `promoted`) list newest-first
    so the limit caps OLD history; the work lists list oldest-first.
- Response shape is **unchanged**: `{ "items": [ ... ], "count": <int> }`.
- Each item gains a **`status`** field (string; here `"in_review"` or
  `"rejected"`). All existing fields stay: `candidate_id`, `type`, `reason`,
  `summary`, `payload_view`, `evidence_refs`, `entity_scan`, `dedup`,
  `created_at`.
- `type` is one of: `blueprint | global_knowledge | user_knowledge | schema_edit`
  (`extractor/models.py:20`). Already present on every item today.

## Actions (unchanged behavior)

- `POST /api/inbox/{id}/{action}`, action ∈ {approve, reject, retract}. Later
  slices added `complete` (fail-to-review) and the promotion hop `verify` +
  `promote`, both `validated`-only: `verify` flips `verified=true` on the landed
  staging node (status does NOT move; the response's `node_stamped: false` means
  the node write did not land and the reviewer should re-verify), and `promote`
  returns the MCP canon YAML plus PR metadata — **not** the
  `{candidate_id, status, reason}` action shape — and moves the candidate to
  terminal `promoted`. A `promote` on an ALREADY-promoted candidate re-emits the
  same YAML with no status move (idempotent recovery of a lost PR). The BFF
  proxies the response **body-agnostically**; only `complete` and `promote` carry
  a request body.
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
  Three later tabs share the toggle: `Needs parameterization`
  (`?status=needs_parameterization`), `Promotable` (`?status=validated`, whose
  cards carry a verify-state badge and offer Verify + Promote instead of
  approve/reject), and `Promoted` (`?status=promoted`, whose cards offer exactly
  one action — `promote`, labelled **Re-emit YAML**).
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
