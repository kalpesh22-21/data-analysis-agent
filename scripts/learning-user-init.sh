#!/usr/bin/env bash
# One-time provisioning for the per-user knowledge store (Track B, Wave 3b-(ii); S8/D17).
# Creates a DEDICATED Couchbase bucket `user_knowledge` (own RBAC boundary, sibling to
# learning_corpus/learning_candidates/learning_audit) holding the ONE learning target
# allowed to carry ENTITIES: durable, per-user facts surfaced only in that user's
# context (05 §Write targets). A `user_knowledge_writer` RBAC user scoped to that bucket
# ONLY (the load-bearing D17 boundary — no cross-bucket, no cross-user surface), plus a
# declared read-only reader. `list_for_user` is a `user_id`-parameterized N1QL scan, so a
# PRIMARY INDEX is provisioned. Idempotent-ish.
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET=user_knowledge
WRITER_USER=user_knowledge_writer
WRITER_PASS=user-writer-pass
READER_USER=user_knowledge_reader
READER_PASS=user-reader-pass

echo "[learning-user-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

echo "[learning-user-init] bucket-create $BUCKET..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --wait 2>&1 | tail -2 || echo "[learning-user-init] bucket exists (ok)"

sleep 2
# Writer: data_writer + data_reader + query_select on user_knowledge ONLY — NO grant on
# agent_sessions / learning_candidates / learning_audit / learning_corpus / neo4j /
# warehouse (the D17 per-user boundary). This is the ONE store holding entity-bearing
# facts, so the scoped role is load-bearing.
echo "[learning-user-init] rbac user $WRITER_USER (writer, scoped to $BUCKET only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$BUCKET],data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-user-init] writer user exists (ok)"

echo "[learning-user-init] rbac user $READER_USER (reader, declared)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-user-init] reader user exists (ok)"

# Primary index — list_for_user is a user_id-parameterized N1QL scan. `IF NOT EXISTS`
# → re-runs are no-ops.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-user-init] primary index on $BUCKET..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON \`$BUCKET\`" 2>&1 | tail -1 \
  || echo "[learning-user-init] primary index exists (ok)"

echo "[learning-user-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -12
echo "[learning-user-init] done."
