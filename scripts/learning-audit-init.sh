#!/usr/bin/env bash
# One-time provisioning for the learning_audit store (Track B, Slice 2; D95/§4.1).
# Creates the audit KEYSPACE (bucket + scope + collection) + a `learning_audit_writer`
# RBAC user scoped to that keyspace ONLY (separate retention clock + RBAC boundary from
# `agent_sessions`), plus a declared read-only `learning_audit_reader` (consumed by the
# S7 review UI, not yet).
#
# TWO LAYOUTS, ONE SCRIPT. Every name below comes from an env var whose DEFAULT is the
# value this script has always used, so running it unchanged reproduces the historical
# bucket-per-store layout exactly:
#
#   (a) bucket-per-store (the default):  learning_audit._default._default
#   (b) shared bucket, named scopes:     BUCKET=pcm_iwant SCOPE=learning COLLECTION=audit
#
# In (b) the scope + collection are CREATED here and the RBAC grant is narrowed to
# `bucket:scope:collection` — Couchbase 7.x supports that granularity, and it is the
# whole point: four stores in one bucket with four bucket-wide grants is one boundary,
# not four. See the SHARED-BUCKET NOTES at the bottom for the two operational
# constraints (maxTTL, separate credentials) that a shared bucket must satisfy.
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
BUCKET="${LEARNING_AUDIT_BUCKET:-learning_audit}"
SCOPE="${LEARNING_AUDIT_SCOPE:-_default}"
COLLECTION="${LEARNING_AUDIT_COLLECTION:-_default}"
WRITER_USER="${LEARNING_AUDIT_WRITER_USER:-learning_audit_writer}"
WRITER_PASS="${LEARNING_AUDIT_WRITER_PASS:-audit-writer-pass}"
READER_USER="${LEARNING_AUDIT_READER_USER:-learning_audit_reader}"
READER_PASS="${LEARNING_AUDIT_READER_PASS:-audit-reader-pass}"

# The three-part N1QL keyspace + the RBAC grant target, derived ONCE from the three
# names above so the index, the query and the grant can never disagree about which
# collection this store owns. `_default`/`_default` yields `bucket`.`_default`.`_default`,
# which is the same collection a bare `bucket` keyspace has always resolved to and the
# same one `CREATE PRIMARY INDEX ON \`bucket\`` has always indexed — so layout (a) is
# byte-identical in effect to what shipped before.
KEYSPACE="\`$BUCKET\`.\`$SCOPE\`.\`$COLLECTION\`"
# Couchbase spells a scoped grant `role[bucket:scope:collection]`. On `_default`/`_default`
# we keep the plain bucket-wide `role[bucket]` form, so an existing deployment's RBAC
# users are not silently narrowed by a re-run.
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
  echo "[learning-audit-init] FATAL: LEARNING_AUDIT_SCOPE=$SCOPE is a named scope, but" \
       "LEARNING_AUDIT_COLLECTION is _default. A named scope has no _default collection," \
       "so that keyspace cannot exist. Set LEARNING_AUDIT_COLLECTION to the collection's" \
       "real name, or leave LEARNING_AUDIT_SCOPE at _default." >&2
  exit 1
fi

echo "[learning-audit-init] keyspace $KEYSPACE, grant [$GRANT]"
echo "[learning-audit-init] waiting for node REST..."
for i in $(seq 1 30); do
  docker exec l2-cb curl -sf "$CLUSTER/pools" >/dev/null 2>&1 && break
  sleep 2
done

# maxTTL=0: NO bucket-level expiry cap. Load-bearing for a SHARED bucket — the corpus
# and user-knowledge stores are configured TTL 0 (durable, no expiry), and a bucket
# maxTTL silently CAPS every document's expiry regardless of what the SDK asks for, so a
# non-zero value here would quietly delete landed artifacts and per-user facts. Audit and
# candidates set their own 90-day expiry per write and are unaffected either way.
# RAMSIZE IS SIZED FOR ONE STORE. In the shared layout create the bucket beforehand
# with a quota covering every store in it — only the first script run creates it, and
# the rest silently accept whatever size that one chose (see SHARED-BUCKET NOTES at the
# bottom of this file).
echo "[learning-audit-init] bucket-create $BUCKET (own retention/RBAC clock, maxTTL=0)..."
$C bucket-create --cluster "$CLUSTER" -u "$U" -p "$P" \
  --bucket "$BUCKET" --bucket-type couchbase --bucket-ramsize 128 \
  --max-ttl 0 \
  --wait 2>&1 | tail -2 || echo "[learning-audit-init] bucket exists (ok)"

sleep 2
# Scope + collection. SKIPPED entirely on `_default`/`_default`: those exist from
# bucket-create and `collection-manage --create-scope _default` would fail.
if [ "$SCOPE" != "_default" ]; then
  echo "[learning-audit-init] create-scope $SCOPE in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-scope "$SCOPE" 2>&1 | tail -1 \
    || echo "[learning-audit-init] scope exists (ok)"
fi
if [ "$COLLECTION" != "_default" ]; then
  echo "[learning-audit-init] create-collection $SCOPE.$COLLECTION in $BUCKET..."
  $C collection-manage --cluster "$CLUSTER" -u "$U" -p "$P" \
    --bucket "$BUCKET" --create-collection "$SCOPE.$COLLECTION" 2>&1 | tail -1 \
    || echo "[learning-audit-init] collection exists (ok)"
  sleep 2
fi

# Writer: data_writer + data_reader on the audit KEYSPACE ONLY — NO grant on
# agent_sessions / neo4j / warehouse, and in a shared bucket no grant on the sibling
# learning/user scopes either (D95 RBAC boundary; enforced by the A4 test).
echo "[learning-audit-init] rbac user $WRITER_USER (writer, scoped to [$GRANT] only)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$WRITER_USER" --rbac-password "$WRITER_PASS" \
  --rbac-name "$WRITER_USER" --auth-domain local \
  --roles "data_writer[$GRANT],data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-audit-init] writer user exists (ok)"

# Reader: declared for the future review-inbox UI (S7); read-only, same keyspace.
echo "[learning-audit-init] rbac user $READER_USER (reader, declared for S7)..."
$C user-manage --cluster "$CLUSTER" -u "$U" -p "$P" --set \
  --rbac-username "$READER_USER" --rbac-password "$READER_PASS" \
  --rbac-name "$READER_USER" --auth-domain local \
  --roles "data_reader[$GRANT],query_select[$GRANT]" \
  2>&1 | tail -1 || echo "[learning-audit-init] reader user exists (ok)"

# Indexes for the coverage judge's verdict records (plan §3b), built on the THREE-part
# keyspace. The loop itself reads these docs by KEY only — the deterministic redelivery
# read-through — so nothing in the runtime needs an index. These exist for the HUMAN
# question the drop mitigation promised: how many drops, and how do the verdicts
# distribute?
#
#   SELECT verdict, count(*) FROM `learning_audit`.`_default`.`_default`
#    WHERE record_type = 'judge_verdict' AND judged_at >= '2026-07-01'
#    GROUP BY verdict;
#
# `record_type` leads the composite as an EQUALITY key so the remaining index order is
# `judged_at`, which is what makes the date-ranged form stream in index order. Evidence
# snapshots carry NO `record_type` at all, so they are absent from the index entirely —
# the predicate is exact rather than merely selective, and the index stays small.
sleep 3
Q="docker exec l2-cb curl -sf -u $U:$P http://127.0.0.1:8093/query/service --data-urlencode"
echo "[learning-audit-init] primary index on $KEYSPACE..."
$Q "statement=CREATE PRIMARY INDEX IF NOT EXISTS ON $KEYSPACE" 2>&1 | tail -1 \
  || echo "[learning-audit-init] primary index exists (ok)"
echo "[learning-audit-init] judge-verdict GSI (record_type, judged_at)..."
$Q "statement=CREATE INDEX idx_audit_judge_verdicts IF NOT EXISTS ON $KEYSPACE(record_type, judged_at)" 2>&1 | tail -1 \
  || echo "[learning-audit-init] judge-verdict GSI exists (ok)"

echo "[learning-audit-init] verify buckets:"
$C bucket-list --cluster "$CLUSTER" -u "$U" -p "$P" 2>&1 | tail -8
echo "[learning-audit-init] done."

# --- SHARED-BUCKET NOTES (layout (b)) ----------------------------------------------
#
# 1. maxTTL MUST BE 0 on the shared bucket. A bucket-level maxTTL caps EVERY document's
#    expiry in every scope, so a non-zero value would silently expire the durable corpus
#    artifacts and per-user knowledge facts (both configured TTL 0 = no expiry) on the
#    bucket's clock. The `--max-ttl 0` above is explicit for that reason. If the bucket
#    was created elsewhere, verify it:
#      couchbase-cli bucket-list -o json | jq '.[] | {name, maxTTL}'
#    Audit and candidates set a 90-day expiry per WRITE and are unaffected by the cap
#    being absent.
#
# 2. RBAC STAYS FOUR SEPARATE SCOPED CREDENTIALS. One shared bucket is not a reason for
#    one shared superuser: collapsing them would re-merge the four boundaries this script
#    and its three siblings exist to draw, and the user store's is load-bearing (D17 — it
#    is the only entity-bearing store). Each of the four scripts creates its own
#    `*_writer`/`*_reader` pair granted at `bucket:scope:collection`, and the per-store
#    `*_username`/`*_password` settings already accept four different credentials.
#
# 3. SIZE THE SHARED BUCKET YOURSELF, BEFOREHAND. Each script asks for
#    `--bucket-ramsize 128` because each was written to own a bucket. Point all of them at
#    one bucket and only the FIRST one run creates it — the other three no-op on "bucket
#    exists (ok)" — leaving five stores' working sets (sessions, results, audit,
#    candidates, corpus, user knowledge) inside a quota provisioned for one. Create the
#    shared bucket ahead of time with a ramsize that covers all of them (and maxTTL=0, per
#    note 1); every script below then takes the "bucket exists" path and touches neither.
