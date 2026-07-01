#!/usr/bin/env bash
# One-time Couchbase init for the Layer-2 integration stack: cluster-init +
# bucket `agent_sessions` + collections `sessions`/`session_results` (D22/D44/D45).
# Idempotent-ish: re-running is tolerated (already-done steps just warn).
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET=agent_sessions

echo "[couchbase-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

echo "[couchbase-init] cluster-init (data,index,query)..."
$C cluster-init --cluster "$CLUSTER" \
  --cluster-username "$U" --cluster-password "$P" \
  --services data,index,query \
  --cluster-ramsize 512 --cluster-index-ramsize 256 \
  2>&1 | tail -2 || echo "[couchbase-init] cluster-init already done (ok)"

sleep 3
echo "[couchbase-init] bucket-create $BUCKET..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 256 \
  --wait 2>&1 | tail -2 || echo "[couchbase-init] bucket exists (ok)"

sleep 3
for COLL in sessions session_results; do
  echo "[couchbase-init] collection _default.$COLL..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-collection "_default.$COLL" \
    2>&1 | tail -1 || echo "[couchbase-init] collection $COLL exists (ok)"
done

echo "[couchbase-init] verify:"
$C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" --bucket "$BUCKET" --list 2>&1 | tail -12
echo "[couchbase-init] done."
