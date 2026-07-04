#!/usr/bin/env bash
# One-time provisioning for the learning_corpus store (Track B, Wave 3b-(i); D48).
# Creates a DEDICATED Couchbase bucket `learning_corpus` (own RBAC boundary, sibling
# to learning_candidates/learning_audit) holding LANDED blueprint artifacts keyed by
# `canonical_key` + a `learning_corpus_writer` RBAC user scoped to that bucket ONLY,
# plus a declared read-only reader. Artifacts are QUERYABLE (list_artifacts → N1QL
# scan), so a PRIMARY INDEX is provisioned. Idempotent-ish.
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET=learning_corpus
WRITER_USER=learning_corpus_writer
WRITER_PASS=corpus-writer-pass
READER_USER=learning_corpus_reader
READER_PASS=corpus-reader-pass

echo "[learning-corpus-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

echo "[learning-corpus-init] bucket-create $BUCKET..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --wait 2>&1 | tail -2 || echo "[learning-corpus-init] bucket exists (ok)"

sleep 2
# Writer: data_writer + data_reader + query_select on learning_corpus ONLY — NO grant
# on agent_sessions / learning_candidates / learning_audit / neo4j / warehouse (D48
# boundary). The increment is a KV sub-document counter (data_writer), NOT N1QL.
echo "[learning-corpus-init] rbac user $WRITER_USER (writer, scoped to $BUCKET only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$BUCKET],data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-corpus-init] writer user exists (ok)"

echo "[learning-corpus-init] rbac user $READER_USER (reader, declared for S9)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-corpus-init] reader user exists (ok)"

# Primary index — list_artifacts scans the bucket (N1QL). `IF NOT EXISTS` → re-runs
# are no-ops.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-corpus-init] primary index on $BUCKET..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON \`$BUCKET\`" 2>&1 | tail -1 \
  || echo "[learning-corpus-init] primary index exists (ok)"

echo "[learning-corpus-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -10
echo "[learning-corpus-init] done."
