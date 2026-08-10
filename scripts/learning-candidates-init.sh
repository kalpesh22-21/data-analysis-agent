#!/usr/bin/env bash
# One-time provisioning for the learning_candidates holding store (Track B, Slice 3;
# D101). Creates a DEDICATED Couchbase bucket `learning_candidates` (sibling to
# learning_audit — own retention clock + RBAC boundary) + a `learning_candidates_writer`
# RBAC user scoped to that bucket ONLY, plus a declared read-only reader (for the S7
# review inbox / S9 scheduler). UNLIKE learning_audit (KV-only), candidates are
# QUERYABLE BY status, so a PRIMARY INDEX is provisioned. Idempotent-ish.
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET=learning_candidates
WRITER_USER=learning_candidates_writer
WRITER_PASS=candidates-writer-pass
READER_USER=learning_candidates_reader
READER_PASS=candidates-reader-pass

echo "[learning-candidates-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

echo "[learning-candidates-init] bucket-create $BUCKET..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --wait 2>&1 | tail -2 || echo "[learning-candidates-init] bucket exists (ok)"

sleep 2
# Writer: data_writer + data_reader + query_select on learning_candidates ONLY —
# NO grant on agent_sessions / learning_audit / neo4j / warehouse (D101 boundary).
echo "[learning-candidates-init] rbac user $WRITER_USER (writer, scoped to $BUCKET only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$BUCKET],data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-candidates-init] writer user exists (ok)"

echo "[learning-candidates-init] rbac user $READER_USER (reader, declared for S7/S9)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-candidates-init] reader user exists (ok)"

# Primary index — candidates are queried by `status` (N1QL). `IF NOT EXISTS` so
# re-runs are no-ops.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-candidates-init] primary index on $BUCKET..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON \`$BUCKET\`" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] primary index exists (ok)"
echo "[learning-candidates-init] status GSI..."
$Q "statement=CREATE INDEX idx_candidates_status IF NOT EXISTS ON \`$BUCKET\`(status)" 2>&1 | tail -1 \
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
$Q "statement=CREATE INDEX idx_candidates_scan_rotation IF NOT EXISTS ON \`$BUCKET\`(status, last_scanned_at, candidate_id)" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] scan-rotation GSI exists (ok)"
# Drop the superseded two-key form (shipped one commit earlier, before the tiebreak).
# Created AFTER its replacement is online, so there is never a window with no rotation
# index. A no-op where it never existed.
$Q "statement=DROP INDEX idx_candidates_status_scanned IF EXISTS ON \`$BUCKET\`" 2>&1 | tail -1 \
  || echo "[learning-candidates-init] superseded 2-key GSI already absent (ok)"

echo "[learning-candidates-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -10
echo "[learning-candidates-init] done."
