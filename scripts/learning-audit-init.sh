#!/usr/bin/env bash
# One-time provisioning for the learning_audit store (Track B, Slice 2; D95/§4.1).
# Creates a DEDICATED Couchbase bucket `learning_audit` (separate retention clock +
# RBAC boundary from `agent_sessions`) + a `learning_audit_writer` RBAC user scoped
# to that bucket ONLY, plus a declared read-only `learning_audit_reader` (consumed
# by the S7 review UI, not yet). KV-only (get/put by evidence_ref) → NO GSI needed.
# Idempotent-ish: re-running is tolerated (already-done steps just warn).
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET=learning_audit
WRITER_USER=learning_audit_writer
WRITER_PASS=audit-writer-pass
READER_USER=learning_audit_reader
READER_PASS=audit-reader-pass

echo "[learning-audit-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

echo "[learning-audit-init] bucket-create $BUCKET (own retention/RBAC clock)..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 256 \
  --wait 2>&1 | tail -2 || echo "[learning-audit-init] bucket exists (ok)"

sleep 2
# Writer: data_writer + data_reader on learning_audit ONLY — NO grant on
# agent_sessions / neo4j / warehouse (D95 RBAC boundary; enforced by the A4 test).
echo "[learning-audit-init] rbac user $WRITER_USER (writer, scoped to $BUCKET only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$BUCKET],data_reader[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-audit-init] writer user exists (ok)"

# Reader: declared for the future review-inbox UI (S7); read-only, same bucket.
echo "[learning-audit-init] rbac user $READER_USER (reader, declared for S7)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-audit-init] reader user exists (ok)"

echo "[learning-audit-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -8
echo "[learning-audit-init] done."
