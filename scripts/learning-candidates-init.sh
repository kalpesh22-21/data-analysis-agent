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

echo "[learning-candidates-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -10
echo "[learning-candidates-init] done."
