#!/usr/bin/env bash
# One-time provisioning for the learning_corpus store (Track B, Wave 3b-(i); D48).
# Creates the corpus KEYSPACE (bucket + scope + collection) holding LANDED blueprint
# artifacts keyed by `canonical_key` — own RBAC boundary, sibling to
# learning_candidates/learning_audit — plus a `learning_corpus_writer` RBAC user scoped
# to that keyspace ONLY and a declared read-only reader. Artifacts are QUERYABLE
# (list_artifacts → N1QL scan), so a PRIMARY INDEX is provisioned. Idempotent-ish.
#
# TWO LAYOUTS, ONE SCRIPT. Every name comes from an env var whose DEFAULT is the value
# this script has always used, so running it unchanged reproduces the historical layout:
#
#   (a) bucket-per-store (the default):  learning_corpus._default._default
#   (b) shared bucket, named scopes:     BUCKET=pcm_iwant SCOPE=learning COLLECTION=corpus
#
# In (b) the scope + collection are created here, the RBAC grant is narrowed to
# `bucket:scope:collection`, and the index is built on the THREE-part keyspace — see
# `scripts/learning-audit-init.sh`'s SHARED-BUCKET NOTES for the maxTTL and
# separate-credentials constraints a shared bucket must satisfy. maxTTL matters MOST to
# this store: corpus documents are written with NO expiry at all.
set -uo pipefail

C="docker exec l2-cb couchbase-cli"
CLUSTER="http://127.0.0.1:8091"
U=admin
P=password
BUCKET="${LEARNING_CORPUS_BUCKET:-learning_corpus}"
SCOPE="${LEARNING_CORPUS_SCOPE:-_default}"
COLLECTION="${LEARNING_CORPUS_COLLECTION:-_default}"
WRITER_USER="${LEARNING_CORPUS_WRITER_USER:-learning_corpus_writer}"
WRITER_PASS="${LEARNING_CORPUS_WRITER_PASS:-corpus-writer-pass}"
READER_USER="${LEARNING_CORPUS_READER_USER:-learning_corpus_reader}"
READER_PASS="${LEARNING_CORPUS_READER_PASS:-corpus-reader-pass}"

# The three-part N1QL keyspace + the RBAC grant target, derived ONCE so the index, the
# store's `list_artifacts` statement and the grant cannot disagree. `_default`/`_default`
# resolves to the same collection a bare `bucket` keyspace always did, so layout (a) is
# unchanged in effect and the existing index still serves the query.
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
  echo "[learning-corpus-init] FATAL: LEARNING_CORPUS_SCOPE=$SCOPE is a named scope, but" \
       "LEARNING_CORPUS_COLLECTION is _default. A named scope has no _default collection," \
       "so that keyspace cannot exist. Set LEARNING_CORPUS_COLLECTION to the collection's" \
       "real name, or leave LEARNING_CORPUS_SCOPE at _default." >&2
  exit 1
fi

echo "[learning-corpus-init] keyspace $KEYSPACE, grant [$GRANT]"
echo "[learning-corpus-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

# maxTTL=0 IS LOAD-BEARING HERE, not boilerplate. LEARNING_CORPUS_TTL_SECONDS defaults to
# 0 = no expiry, and the store honours that by omitting `expiry=` from its `insert`
# options entirely — a landed artifact and its accrued cross-session `hit_count` must
# outlive every candidate and session. A bucket-level maxTTL overrides that from
# underneath: the write succeeds, no error is raised anywhere, and the corpus quietly
# empties itself on the bucket's clock. In a SHARED bucket this constrains the WHOLE
# bucket, so whoever creates `pcm_iwant` must create it with maxTTL=0.
# RAMSIZE IS SIZED FOR ONE STORE. In the shared layout create the bucket beforehand
# with a quota covering every store in it — only the first script run creates it, and
# the rest silently accept whatever size that one chose (see SHARED-BUCKET NOTES in
# scripts/learning-audit-init.sh).
echo "[learning-corpus-init] bucket-create $BUCKET (maxTTL=0 — durable, never expires)..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --max-ttl 0 \
  --wait 2>&1 | tail -2 || echo "[learning-corpus-init] bucket exists (ok)"

sleep 2
# Scope + collection. SKIPPED on `_default`/`_default`: those exist from bucket-create.
if [ "$SCOPE" != "_default" ]; then
  echo "[learning-corpus-init] create-scope $SCOPE in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-scope "$SCOPE" 2>&1 | tail -1 \
    || echo "[learning-corpus-init] scope exists (ok)"
fi
if [ "$COLLECTION" != "_default" ]; then
  echo "[learning-corpus-init] create-collection $SCOPE.$COLLECTION in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-collection "$SCOPE.$COLLECTION" 2>&1 | tail -1 \
    || echo "[learning-corpus-init] collection exists (ok)"
  sleep 2
fi

# Writer: data_writer + data_reader + query_select on the corpus KEYSPACE ONLY — NO grant
# on agent_sessions / learning_candidates / learning_audit / neo4j / warehouse, and in a
# shared bucket no grant on the sibling learning/user scopes either (D48 boundary). The
# increment is a KV sub-document counter (data_writer), NOT N1QL.
echo "[learning-corpus-init] rbac user $WRITER_USER (writer, scoped to [$GRANT] only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$GRANT],data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-corpus-init] writer user exists (ok)"

echo "[learning-corpus-init] rbac user $READER_USER (reader, declared for S9)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-corpus-init] reader user exists (ok)"

# Primary index — list_artifacts scans the collection (N1QL), against the SAME three-part
# keyspace `CouchbaseBlueprintCorpus._keyspace` builds. `IF NOT EXISTS` → re-runs are
# no-ops.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-corpus-init] primary index on $KEYSPACE..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON $KEYSPACE" 2>&1 | tail -1 \
  || echo "[learning-corpus-init] primary index exists (ok)"

echo "[learning-corpus-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -10
echo "[learning-corpus-init] done."
