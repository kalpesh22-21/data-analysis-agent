#!/usr/bin/env bash
# One-time provisioning for the learning_audit store (Track B, Slice 2; D95/§4.1).
# Creates a DEDICATED Couchbase bucket `learning_audit` (separate retention clock +
# RBAC boundary from `agent_sessions`) + a `learning_audit_writer` RBAC user scoped
# to that bucket ONLY, plus a declared read-only `learning_audit_reader` (consumed
# by the S7 review UI, not yet).
#
# NO LONGER KV-only. Evidence snapshots are still read by `evidence_ref` and never
# scanned, but the coverage judge's verdict records (plan §3b, `record_type` =
# 'judge_verdict') exist to be AGGREGATED: the judge DROPS analyst sessions before
# extraction, and the agreed mitigation for a drop being invisible is that
# "we dropped N candidates last quarter" is a query rather than a guess. That needs
# `query_select` and an index — both added below. RE-RUN THIS SCRIPT wherever the
# earlier KV-only version was provisioned; without it the drops are still recorded but
# nobody can count them.
#
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
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --wait 2>&1 | tail -2 || echo "[learning-audit-init] bucket exists (ok)"

sleep 2
# Writer: data_writer + data_reader on learning_audit ONLY — NO grant on
# agent_sessions / neo4j / warehouse (D95 RBAC boundary; enforced by the A4 test).
echo "[learning-audit-init] rbac user $WRITER_USER (writer, scoped to $BUCKET only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$BUCKET],data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-audit-init] writer user exists (ok)"

# Reader: declared for the future review-inbox UI (S7); read-only, same bucket.
echo "[learning-audit-init] rbac user $READER_USER (reader, declared for S7)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$BUCKET],query_select[$BUCKET]" \
  2>&1 | tail -1 || echo "[learning-audit-init] reader user exists (ok)"

# Indexes for the coverage judge's verdict records (plan §3b). The loop itself reads
# these docs by KEY only — the deterministic redelivery read-through — so nothing in
# the runtime needs an index. These exist for the HUMAN question the drop mitigation
# promised: how many drops, and how do the verdicts distribute?
#
#   SELECT verdict, count(*) FROM `learning_audit`
#    WHERE record_type = 'judge_verdict' AND judged_at >= '2026-07-01'
#    GROUP BY verdict;
#
# `record_type` leads the composite as an EQUALITY key so the remaining index order is
# `judged_at`, which is what makes the date-ranged form stream in index order. Evidence
# snapshots carry NO `record_type` at all, so they are absent from the index entirely —
# the predicate is exact rather than merely selective, and the index stays small.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-audit-init] primary index on $BUCKET..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON \`$BUCKET\`" 2>&1 | tail -1 \
  || echo "[learning-audit-init] primary index exists (ok)"
echo "[learning-audit-init] judge-verdict GSI (record_type, judged_at)..."
$Q "statement=CREATE INDEX idx_audit_judge_verdicts IF NOT EXISTS ON \`$BUCKET\`(record_type, judged_at)" 2>&1 | tail -1 \
  || echo "[learning-audit-init] judge-verdict GSI exists (ok)"

echo "[learning-audit-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -8
echo "[learning-audit-init] done."
