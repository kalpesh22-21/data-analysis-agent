#!/usr/bin/env bash
# One-time provisioning for the learning_candidates holding store (Track B, Slice 3;
# D101). Creates the candidates KEYSPACE (bucket + scope + collection) — sibling to
# learning_audit, own retention clock + RBAC boundary — plus a `learning_candidates_writer`
# RBAC user scoped to that keyspace ONLY and a declared read-only reader (for the S7
# review inbox / S9 scheduler). UNLIKE learning_audit's KV reads, candidates are QUERYABLE
# BY status, so a PRIMARY INDEX and two GSIs are provisioned. Idempotent-ish.
#
# TWO LAYOUTS, ONE SCRIPT. Every name comes from an env var whose DEFAULT is the value
# this script has always used, so running it unchanged reproduces the historical layout:
#
#   (a) bucket-per-store (the default):  learning_candidates._default._default
#   (b) shared bucket, named scopes:     BUCKET=pcm_iwant SCOPE=learning COLLECTION=candidates
#
# In (b) the scope + collection are created here, the RBAC grant is narrowed to
# `bucket:scope:collection`, and every index is built on the THREE-part keyspace — see
# `scripts/learning-audit-init.sh`'s SHARED-BUCKET NOTES for the maxTTL and
# separate-credentials constraints a shared bucket must satisfy.
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET="${LEARNING_CANDIDATES_BUCKET:-learning_candidates}"
SCOPE="${LEARNING_CANDIDATES_SCOPE:-_default}"
COLLECTION="${LEARNING_CANDIDATES_COLLECTION:-_default}"
WRITER_USER="${LEARNING_CANDIDATES_WRITER_USER:-learning_candidates_writer}"
WRITER_PASS="${LEARNING_CANDIDATES_WRITER_PASS:-candidates-writer-pass}"
READER_USER="${LEARNING_CANDIDATES_READER_USER:-learning_candidates_reader}"
READER_PASS="${LEARNING_CANDIDATES_READER_PASS:-candidates-reader-pass}"

# The three-part N1QL keyspace + the RBAC grant target, derived ONCE so the indexes, the
# store's statements and the grant cannot disagree. `_default`/`_default` resolves to the
# same collection a bare `bucket` keyspace always did, so layout (a) is unchanged in
# effect — and, critically, the GSIs below land on the SAME collection the existing ones
# already occupy, so `IF NOT EXISTS` still makes a re-run a no-op there.
KEYSPACE="\`$BUCKET\`.\`$SCOPE\`.\`$COLLECTION\`"
if [ "$SCOPE" = "_default" ] && [ "$COLLECTION" = "_default" ]; then
  GRANT="$BUCKET"
else
  GRANT="$BUCKET:$SCOPE:$COLLECTION"
fi

# REFUSE the one pair Couchbase cannot hold, BEFORE creating anything. Only the
# `_default` scope has a `_default` collection; a named scope is created EMPTY. Without
# this check the run below would create the scope, skip collection creation (the
# `COLLECTION != "_default"` gate), print "done", and leave a keyspace that does not
# exist — the store would then fail at its first write, far from the typo that caused it.
# The settings classes reject the same pair at construction (`check_keyspace_pair` in
# src/data_agent/learning/config.py); this is the provisioning half of that rule, so a
# misconfiguration cannot be half-applied from either side.
if [ "$SCOPE" != "_default" ] && [ "$COLLECTION" = "_default" ]; then
  echo "[learning-candidates-init] FATAL: LEARNING_CANDIDATES_SCOPE=$SCOPE is a named scope, but" \
       "LEARNING_CANDIDATES_COLLECTION is _default. A named scope has no _default collection," \
       "so that keyspace cannot exist. Set LEARNING_CANDIDATES_COLLECTION to the collection's" \
       "real name, or leave LEARNING_CANDIDATES_SCOPE at _default." >&2
  exit 1
fi

echo "[learning-candidates-init] keyspace $KEYSPACE, grant [$GRANT]"
echo "[learning-candidates-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

# maxTTL=0 — no bucket-level expiry cap. Candidates set a 90-day expiry per WRITE (and
# terminal rows deliberately set expiry=0 to persist forever), so a bucket cap would
# silently evict the rejected archive and the promoted records the Phase-3 re-emit needs.
# RAMSIZE IS SIZED FOR ONE STORE. In the shared layout create the bucket beforehand
# with a quota covering every store in it — only the first script run creates it, and
# the rest silently accept whatever size that one chose (see SHARED-BUCKET NOTES in
# scripts/learning-audit-init.sh).
echo "[learning-candidates-init] bucket-create $BUCKET (maxTTL=0)..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --max-ttl 0 \
  --wait 2>&1 | tail -2 || echo "[learning-candidates-init] bucket exists (ok)"

sleep 2
# Scope + collection. SKIPPED on `_default`/`_default`: those exist from bucket-create.
if [ "$SCOPE" != "_default" ]; then
  echo "[learning-candidates-init] create-scope $SCOPE in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-scope "$SCOPE" 2>&1 | tail -1 \
    || echo "[learning-candidates-init] scope exists (ok)"
fi
if [ "$COLLECTION" != "_default" ]; then
  echo "[learning-candidates-init] create-collection $SCOPE.$COLLECTION in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-collection "$SCOPE.$COLLECTION" 2>&1 | tail -1 \
    || echo "[learning-candidates-init] collection exists (ok)"
  sleep 2
fi

# Writer: data_writer + data_reader + query_select on the candidates KEYSPACE ONLY — NO
# grant on agent_sessions / learning_audit / neo4j / warehouse, and in a shared bucket no
# grant on the sibling learning/user scopes either (D101 boundary).
echo "[learning-candidates-init] rbac user $WRITER_USER (writer, scoped to [$GRANT] only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$GRANT],data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-candidates-init] writer user exists (ok)"

echo "[learning-candidates-init] rbac user $READER_USER (reader, declared for S7/S9)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-candidates-init] reader user exists (ok)"

# Primary index — candidates are queried by `status` (N1QL), against the SAME three-part
# keyspace `CouchbaseCandidateStore._keyspace` builds. An index on the bucket alone would
# not serve a query on a named collection (and vice versa), so these two must move
# together. `IF NOT EXISTS` so re-runs are no-ops.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-candidates-init] primary index on $KEYSPACE..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON $KEYSPACE" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] primary index exists (ok)"
echo "[learning-candidates-init] status GSI..."
$Q "statement=CREATE INDEX idx_candidates_status IF NOT EXISTS ON $KEYSPACE(status)" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] status GSI exists (ok)"
# S9 scan-rotation GSI. The cron reads
#   WHERE status = $status ORDER BY last_scanned_at ASC, candidate_id ASC LIMIT $limit
# and this composite serves ALL of it: `status` is a leading EQUALITY key, so the
# remaining index order IS `(last_scanned_at, candidate_id)` ASC — the query streams in
# index order and stops at the LIMIT, with no sort stage and no scan of the rows it will
# not return. That is what lets the scan rotate fairly without a freshness predicate in
# the WHERE. Every candidate doc has `status`, so no doc is skipped for having a missing
# key; a never-scanned candidate simply has `last_scanned_at` MISSING, which is the
# LOWEST value in the N1QL collation and therefore comes FIRST — new work jumps the queue.
#
# ALL THREE KEYS ARE REQUIRED. Measured on couchbase 7.6.5: with `candidate_id` present
# the plan is `IndexScan3 index_order=[keypos 1, keypos 2] limit=200`; drop it back to
# two keys while the query still asks for the tiebreak and the planner abandons this
# index for the plain status index plus a full `Order` stage — index order and LIMIT
# pushdown both lost, silently. `META().id` as the tiebreak keeps the index but still
# adds an `Order`. Re-run EXPLAIN if you touch either the index or the ORDER BY.
echo "[learning-candidates-init] scan-rotation GSI (status, last_scanned_at, candidate_id)..."
$Q "statement=CREATE INDEX idx_candidates_scan_rotation IF NOT EXISTS ON $KEYSPACE(status, last_scanned_at, candidate_id)" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] scan-rotation GSI exists (ok)"
# Drop the superseded two-key form (shipped one commit earlier, before the tiebreak).
# Created AFTER its replacement is online, so there is never a window with no rotation
# index. A no-op where it never existed.
$Q "statement=DROP INDEX idx_candidates_status_scanned IF EXISTS ON $KEYSPACE" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] superseded 2-key GSI already absent (ok)"

echo "[learning-candidates-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -10
echo "[learning-candidates-init] done."
