#!/usr/bin/env bash
# One-time provisioning for the per-user knowledge store (Track B, Wave 3b-(ii); S8/D17).
# Creates the user-knowledge KEYSPACE (bucket + scope + collection) holding the ONE
# learning target allowed to carry ENTITIES: durable, per-user facts surfaced only in that
# user's context (05 §Write targets). Own RBAC boundary, sibling to
# learning_corpus/learning_candidates/learning_audit. A `user_knowledge_writer` RBAC user
# scoped to that keyspace ONLY (the load-bearing D17 boundary — no cross-keyspace, no
# cross-user surface), plus a declared read-only reader. `list_for_user` is a
# `user_id`-parameterized N1QL scan, so a PRIMARY INDEX is provisioned. Idempotent-ish.
#
# TWO LAYOUTS, ONE SCRIPT. Every name comes from an env var whose DEFAULT is the value
# this script has always used, so running it unchanged reproduces the historical layout:
#
#   (a) bucket-per-store (the default):  user_knowledge._default._default
#   (b) shared bucket, named scopes:     BUCKET=pcm_iwant SCOPE=user COLLECTION=knowledge
#
# Layout (b) is where the scoped grant EARNS its keep. Once this store shares a bucket
# with audit/candidates/corpus, a bucket-wide `data_reader[pcm_iwant]` would let any one
# of the four read the entity-bearing per-user facts — the exact boundary D17 draws. So in
# (b) the grant is narrowed to `bucket:scope:collection` and the store's own N1QL names
# all three parts. See `scripts/learning-audit-init.sh`'s SHARED-BUCKET NOTES.
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET="${USER_KNOWLEDGE_BUCKET:-user_knowledge}"
SCOPE="${USER_KNOWLEDGE_SCOPE:-_default}"
COLLECTION="${USER_KNOWLEDGE_COLLECTION:-_default}"
WRITER_USER="${USER_KNOWLEDGE_WRITER_USER:-user_knowledge_writer}"
WRITER_PASS="${USER_KNOWLEDGE_WRITER_PASS:-user-writer-pass}"
READER_USER="${USER_KNOWLEDGE_READER_USER:-user_knowledge_reader}"
READER_PASS="${USER_KNOWLEDGE_READER_PASS:-user-reader-pass}"

# The three-part N1QL keyspace + the RBAC grant target, derived ONCE so the index, the
# store's `list_for_user` statement, its `open_keyspace` guard and the grant cannot
# disagree. `_default`/`_default` resolves to the same collection a bare `bucket` keyspace
# always did, so layout (a) is unchanged in effect.
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
  echo "[learning-user-init] FATAL: USER_KNOWLEDGE_SCOPE=$SCOPE is a named scope, but" \
       "USER_KNOWLEDGE_COLLECTION is _default. A named scope has no _default collection," \
       "so that keyspace cannot exist. Set USER_KNOWLEDGE_COLLECTION to the collection's" \
       "real name, or leave USER_KNOWLEDGE_SCOPE at _default." >&2
  exit 1
fi

echo "[learning-user-init] keyspace $KEYSPACE, grant [$GRANT]"
echo "[learning-user-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

# maxTTL=0 IS LOAD-BEARING HERE. USER_KNOWLEDGE_TTL_SECONDS defaults to 0 = no expiry, and
# the store honours that by omitting `expiry=` from its upsert options entirely — a
# durable per-user fact outlives sessions by design. A bucket-level maxTTL silently
# overrides it: writes still succeed, nothing errors, and a user's learned facts vanish on
# the bucket's clock. In a SHARED bucket this constrains the WHOLE bucket, so whoever
# creates `pcm_iwant` must create it with maxTTL=0.
# RAMSIZE IS SIZED FOR ONE STORE. In the shared layout create the bucket beforehand
# with a quota covering every store in it — only the first script run creates it, and
# the rest silently accept whatever size that one chose (see SHARED-BUCKET NOTES in
# scripts/learning-audit-init.sh).
echo "[learning-user-init] bucket-create $BUCKET (maxTTL=0 — durable, never expires)..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --max-ttl 0 \
  --wait 2>&1 | tail -2 || echo "[learning-user-init] bucket exists (ok)"

sleep 2
# Scope + collection. SKIPPED on `_default`/`_default`: those exist from bucket-create.
if [ "$SCOPE" != "_default" ]; then
  echo "[learning-user-init] create-scope $SCOPE in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-scope "$SCOPE" 2>&1 | tail -1 \
    || echo "[learning-user-init] scope exists (ok)"
fi
if [ "$COLLECTION" != "_default" ]; then
  echo "[learning-user-init] create-collection $SCOPE.$COLLECTION in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-collection "$SCOPE.$COLLECTION" 2>&1 | tail -1 \
    || echo "[learning-user-init] collection exists (ok)"
  sleep 2
fi

# Writer: data_writer + data_reader + query_select on the user-knowledge KEYSPACE ONLY —
# NO grant on agent_sessions / learning_candidates / learning_audit / learning_corpus /
# neo4j / warehouse, and in a shared bucket no grant on the sibling learning/sessions
# scopes either (the D17 per-user boundary). This is the ONE store holding entity-bearing
# facts, so the scoped role is load-bearing.
echo "[learning-user-init] rbac user $WRITER_USER (writer, scoped to [$GRANT] only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$GRANT],data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-user-init] writer user exists (ok)"

echo "[learning-user-init] rbac user $READER_USER (reader, declared)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-user-init] reader user exists (ok)"

# Primary index — list_for_user is a user_id-parameterized N1QL scan, against the SAME
# three-part keyspace `CouchbaseUserKnowledgeStore.keyspace()` builds. `IF NOT EXISTS`
# → re-runs are no-ops.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-user-init] primary index on $KEYSPACE..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON $KEYSPACE" 2>&1 | tail -1 \
  || echo "[learning-user-init] primary index exists (ok)"

echo "[learning-user-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -12
echo "[learning-user-init] done."
