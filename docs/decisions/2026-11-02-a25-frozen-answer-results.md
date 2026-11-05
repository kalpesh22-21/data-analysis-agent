# A25 Frozen answer results and snapshot handoff

Status: deferred implementation. Recorded 2026-11-02 for lane5, clickhouse-api and Ember.

The first table page must display the same result that supported the reviewed answer.
The remote investigation reproduced a violation: the first page implicitly executes
saved SQL again. Subsequent pages are frozen, but the reviewed takeaway and the
initial table can already disagree. The preferred fix is to capture results once,
review that captured result, and persist its snapshot reference with the answer.

## Reported reproduction

Evidence below comes from the remote developer's controlled synthetic-fixture report
supplied in this conversation. Its code locations describe the investigated builds;
verify them against the adopted branches before implementation. This note does not
claim a new local reproduction or deployed fix.

| Check | Reported result |
| --- | --- |
| Answer versus first page | Reviewed answer at 19:58:50.853Z contained `now() = 1793649525`; first page at 19:58:58Z returned `1793649538`, a 13-second value difference. |
| Source dropped before first page | Answer said “sales 140”; first page failed with `400 UNKNOWN_TABLE`. No table result had been materialized at review time. |
| Paging and download after first mint | Frozen, identical rows. |
| Explicit refresh using `refresh_of` | New snapshot ID; old snapshot retained its original rows. |
| Cross-session page/download | `404 SNAPSHOT_NOT_FOUND`; ownership rechecked. |
| Catalog refresh then page by existing ID | No invalidation; snapshot identity lacked catalog SHA binding. |

The timestamp test and source-drop test establish the first-page race. They do not
establish that every earlier zero-department result was caused by this race; the
identity/RLS problem remains a separate cause of empty employee aggregates.

## Current mechanism and code map

- lane5 stores SQL, formats and row count in answer sources, but no rows or snapshot
  pointer: `runtime/composer/__init__.py:343–358`. Answer persistence:
  `runtime/loop/agent_loop.py:2411–2430`; history projection:
  `runtime/session_history.py:287–300`.
- Ember lazily creates a snapshot when the first page lacks a pointer:
  `QueryPageHandler.php:139–235`, creation at `195–196`. Pointer-persist failures
  are swallowed at `205–215`. `State.ts:1559,1639–1660,995–1005` triggers hydration;
  `deriveTables` at `1701–1740` needs to consume the persisted pointer.
- Creation executes SQL through lane5 `runtime/export_proxy.py:113–124` and
  clickhouse-api `export.py:130–151`. `export_service._create_snapshot` streams rows
  and commits metadata last. Stored-row paging: `export_service.py:753–822`.
- Reads check ownership and column grants (`409 SNAPSHOT_SCOPE_CHANGED` on grant
  drift). The report identifies no effective RLS-revocation recheck after mint
  (`export_service.py:497–505`) and no catalog SHA binding.

## Proposed result lifecycle

1. Execute the authorized query once and capture its complete result in the snapshot
   store. A preview is not a complete result; the execution path must stream/capture
   all rows needed for paging, with explicit limits and failure behavior.
2. Return an opaque result handle and a preview tied to that exact result to lane5.
   The agent and judge review evidence from that result, with its formats and provenance.
3. On approved publication, persist the snapshot reference in
   `answer_document.sources[result_id]`. Do not publish a frozen-table promise if
   materialization or pointer persistence failed. Preserve an existing reviewed answer
   when a replacement publication fails.
4. History and Ember initial display resolve the stored reference. Initial display,
   page 2 and download must never silently re-execute source SQL.
5. Explicit refresh creates a new, distinguishable snapshot. A table refresh is not
   a reviewed-answer refresh: updating the takeaway requires a new review and revision.
6. Handle expiry or loss explicitly. Do not silently rerun SQL and attach fresh rows
   to an old takeaway. Define a recoverable “saved result unavailable” state and an
   explicit refresh path, with bounded retries.

A snapshot created from SQL during finalization is only an interim mitigation. It
still executes twice, so it does not meet exact reviewed-result consistency. If used,
it must not be described as the full A25 fix.

## Authorization and snapshot identity

Continue owner, tenant and grant checks on every page/download. Bind capture metadata
to the relevant catalog and authorization context. A catalog SHA in the cache key alone
is insufficient: reads of old IDs must also validate their applicability.

Before implementation, define how permission changes are detected across both lanes:

- RLS revocation must deny stale access or return a safe invalidation response; frozen
  rows must not become a way to retain revoked access.
- Determine the authoritative access version or revalidation mechanism. Calling
  `ensure_employee_access_fresh` alone is not proof that old stored rows remain allowed.
- Treat an unvalidated catalog change as requiring validation or safe invalidation;
  decide whether harmless metadata-only changes can preserve access.
- Revalidation failure must not expose rows. Do not “repair” authorization by rerunning
  the query silently or broadening identity/scope.

Define the exact wire codes, refresh behavior and old-snapshot policy jointly with
Ember. Existing ownership checks and explicit-refresh behavior should remain covered.

## UI and publication requirements

- Persist and consume an opaque snapshot reference; agree its field name and contract
  version across all three repos. Do not infer it from SQL or synthesize handles.
- Persist-pointer failure is a correctness failure, not merely a log entry. Publication
  needs an atomic or recoverable relationship between answer revision and snapshot.
- Keep Retrieved, Refreshed and genuine Data as of distinct. Snapshot `created_at`
  is not a data-as-of timestamp.
- Whole-result sorting must sort the frozen snapshot. Client-side sorting is sufficient
  only when all relevant rows are loaded; sorting one page is not global sorting.
- Keep `409 SNAPSHOT_REPLACED`, `reset_table`, `Retry-After`, refresh revision conflicts,
  and bounded `404` recovery consistent with the saved-result contract.

## Acceptance tests

1. A nondeterministic expression returns identical reviewed and first-page values.
2. Drop or mutate the synthetic source after capture: initial display, subsequent
   pages and download retain the captured rows while authorization remains valid.
3. Reload history before any page request: it uses the original snapshot without SQL
   re-execution. Repeat after restart and across replicas.
4. Explicit refresh creates new rows/ID; old snapshot behavior follows the documented
   retention and authorization policy. Takeaway is not silently relabeled as refreshed.
5. Cross-session/tenant reads fail. Grant removal, RLS revocation, catalog change and
   authorization-service failure exercise safe read-time validation/invalidation.
6. Snapshot write failure, pointer-persist failure, interrupted publication, expiration
   and retry never silently substitute new results or overwrite a newer answer revision.
7. Paging, CSV and whole-result sorting use the same frozen dataset and formats.
8. Large results stream correctly; preview truncation never becomes silent data loss.
9. Repeat with ClickHouse and Trino, then verify Ember live response and history paths.

Use synthetic data for destructive source-drop and permission-revocation probes.
Record deployed image digests and corpus fingerprints with the run evidence.

## Separate follow-ups

- Deployment identity: the report found v2 routes on a pod tagged `48bb7d33` despite
  that checkout lacking them. Verify running image digest/code before attributing
  behavior; the cause (stale tag or another deployment) is not established here.
- Upload: lane5 `POST /upload` reportedly proxies to a dead scratch upstream while
  the API's `/scratch/v1/upload` works directly. Track separately from A25.

## Resume point

First verify current branches and contracts in all three repos. Design the API's
capture/result-handle lifetime and read-time authorization mechanism before changing
lane5 publication or Ember hydration. Then implement, run the acceptance matrix,
review evidence and push. No runtime changes or deployment are part of this note.
