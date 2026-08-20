"""Every Couchbase store binds the KEYSPACE its config names — Layer-1.

The five stores can now be pointed at named scopes inside ONE shared bucket
(`pcm_iwant`.`learning`.`audit` next to `pcm_iwant`.`user`.`knowledge`) instead of a
bucket each. That reachability is pure configuration, which makes it exactly the kind of
claim that rots silently: every OTHER store test injects a bucket double that answers
`scope()`/`collection()`/`default_collection()` with the SAME collection and ignores the
names, because those tests are about mutation semantics, not about which handle was
picked. Under those doubles a store that deleted its `_bind_collections` override — and so
fell back to the base's `bucket.default_collection()` — would still pass every one of
them, while writing every document into the wrong collection in production.

So this module asserts the thing those doubles cannot: the NAMES. Its bucket double
records `scope(name).collection(name)` and treats `default_collection()` as a hard
failure, so the fallback is not merely unasserted-against, it is unreachable.

Two directions, per store:
  - a configured non-default scope/collection is the one that gets bound (the shared-
    bucket layout is reachable by config alone);
  - the SHIPPED DEFAULTS bind `_default`/`_default`, which is not an opinion but the
    installed SDK's own definition of `default_collection()` — see
    `test_the_shipped_defaults_are_the_sdks_own_default_collection`.

Hermetic: no cluster, no network. Skipped only when the `couchbase` package itself is
unimportable, since the stores refuse to construct without its options types.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

pytestmark = pytest.mark.skipif(
    not COUCHBASE_AVAILABLE,
    reason="Requires the 'couchbase' package (for its options types only — the whole "
    "handle graph is a double here, no live cluster is used).",
)


# --- the recording handle graph ----------------------------------------------


class _Collection:
    """A leaf handle that remembers the three-part path it was reached by."""

    def __init__(self, bucket: str, scope: str, collection: str) -> None:
        self.path = (bucket, scope, collection)


class _Scope:
    def __init__(self, bucket: _Bucket, name: str) -> None:
        self._bucket = bucket
        self._name = name

    def collection(self, name: str) -> _Collection:
        self._bucket.opened.append((self._name, name))
        return _Collection(self._bucket.name, self._name, name)


class _Bucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.opened: list[tuple[str, str]] = []

    def scope(self, name: str) -> _Scope:
        return _Scope(self, name)

    def default_collection(self) -> Any:
        raise AssertionError(
            "store reached for bucket.default_collection() — the collection it works in "
            "must come from its configured scope/collection, or a shared-bucket "
            "deployment silently reads and writes the bucket's default collection "
            "instead of its own"
        )


class _Cluster:
    def __init__(self) -> None:
        self.buckets: dict[str, _Bucket] = {}

    def bucket(self, name: str) -> _Bucket:
        return self.buckets.setdefault(name, _Bucket(name))


# --- the five stores, each with the settings knobs that name its keyspace ------
#
# (id, build(cluster, **keyspace), the attribute(s) holding its bound collections)
# `build` takes bucket/scope/collection kwargs so ONE parametrized body can drive
# every store; the session store takes two collections, which is why the expected
# path is computed per store rather than shared.


def _session(cluster, **kw):
    return CouchbaseSessionStore(
        RuntimeSettings(
            _env_file=None,
            couchbase_bucket=kw["bucket"],
            couchbase_scope=kw["scope"],
            couchbase_sessions_collection=kw.get("collection", "sessions"),
            couchbase_results_collection=kw.get("results", "session_results"),
        ),
        cluster=cluster,
    )


def _audit(cluster, **kw):
    return CouchbaseAuditStore(
        LearningSettings(
            _env_file=None,
            learning_audit_bucket=kw["bucket"],
            learning_audit_scope=kw["scope"],
            learning_audit_collection=kw["collection"],
        ),
        cluster=cluster,
    )


def _candidates(cluster, **kw):
    return CouchbaseCandidateStore(
        LearningSettings(
            _env_file=None,
            learning_candidates_bucket=kw["bucket"],
            learning_candidates_scope=kw["scope"],
            learning_candidates_collection=kw["collection"],
        ),
        cluster=cluster,
    )


def _corpus(cluster, **kw):
    return CouchbaseBlueprintCorpus(
        LearningSettings(
            _env_file=None,
            learning_corpus_bucket=kw["bucket"],
            learning_corpus_scope=kw["scope"],
            learning_corpus_collection=kw["collection"],
        ),
        cluster=cluster,
    )


def _user(cluster, **kw):
    return CouchbaseUserKnowledgeStore(
        UserKnowledgeStoreConfig(
            _env_file=None,
            user_knowledge_bucket=kw["bucket"],
            user_knowledge_scope=kw["scope"],
            user_knowledge_collection=kw["collection"],
        ),
        cluster=cluster,
    )


def _defaults(store_id: str) -> Any:
    """The store's settings object as SHIPPED — no overrides at all."""
    if store_id == "session":
        return RuntimeSettings(_env_file=None)
    if store_id == "user":
        return UserKnowledgeStoreConfig(_env_file=None)
    return LearningSettings(_env_file=None)


# The `pcm_iwant` layout this slice exists to make reachable.
_SHARED_LAYOUT = [
    ("session", _session, {"bucket": "pcm_iwant", "scope": "sessions",
                           "collection": "sessions", "results": "session_results"},
     [("sessions", "sessions"), ("sessions", "session_results")]),
    ("audit", _audit, {"bucket": "pcm_iwant", "scope": "learning", "collection": "audit"},
     [("learning", "audit")]),
    ("candidates", _candidates,
     {"bucket": "pcm_iwant", "scope": "learning", "collection": "candidates"},
     [("learning", "candidates")]),
    ("corpus", _corpus, {"bucket": "pcm_iwant", "scope": "learning", "collection": "corpus"},
     [("learning", "corpus")]),
    ("user", _user, {"bucket": "pcm_iwant", "scope": "user", "collection": "knowledge"},
     [("user", "knowledge")]),
]

# The shipped defaults: field name -> the value that must be `_default`.
_DEFAULT_FIELDS = [
    ("session", "couchbase_scope"),
    ("audit", "learning_audit_scope"),
    ("audit", "learning_audit_collection"),
    ("candidates", "learning_candidates_scope"),
    ("candidates", "learning_candidates_collection"),
    ("corpus", "learning_corpus_scope"),
    ("corpus", "learning_corpus_collection"),
    ("user", "user_knowledge_scope"),
    ("user", "user_knowledge_collection"),
]


@pytest.mark.parametrize(
    ("store_id", "build", "keyspace", "expected"),
    _SHARED_LAYOUT,
    ids=[row[0] for row in _SHARED_LAYOUT],
)
def test_store_binds_the_configured_scope_and_collection(store_id, build, keyspace, expected):
    """The shared-bucket layout is reachable by CONFIG alone, for every store.

    All five open ONE bucket (`pcm_iwant`) and are kept apart only by the scope and
    collection they were configured with — so this asserts the exact `(scope, collection)`
    pairs each one opened, not merely that it opened something.
    """
    cluster = _Cluster()
    build(cluster, **keyspace)

    bucket = cluster.buckets[keyspace["bucket"]]
    assert bucket.opened == expected


@pytest.mark.parametrize(
    ("store_id", "build", "keyspace", "expected"),
    _SHARED_LAYOUT,
    ids=[row[0] for row in _SHARED_LAYOUT],
)
def test_store_never_falls_back_to_the_buckets_default_collection(
    store_id, build, keyspace, expected
):
    """A misconfigured/unknown scope must not degrade into `default_collection()`.

    `_Bucket.default_collection` raises, so this fails LOUDLY if a store ever loses its
    `_bind_collections` override and inherits the base's default. The scope name here is
    one no deployment would provision, which is the case where a silent fallback would be
    most tempting and most damaging: writes would land in the shared bucket's default
    collection, outside the store's RBAC grant and outside every N1QL keyspace that reads
    them back.
    """
    cluster = _Cluster()
    build(cluster, **{**keyspace, "scope": "scope-that-does-not-exist"})

    bucket = cluster.buckets[keyspace["bucket"]]
    assert [scope for scope, _ in bucket.opened] == ["scope-that-does-not-exist"] * len(expected)


@pytest.mark.parametrize(
    ("store_id", "field"), _DEFAULT_FIELDS, ids=[f"{s}.{f}" for s, f in _DEFAULT_FIELDS]
)
def test_the_shipped_defaults_are_the_sdks_own_default_collection(store_id, field):
    """NO REGRESSION for a bucket-per-store deployment, proven against the SDK.

    Every store used to bind `bucket.default_collection()`. It now binds
    `bucket.scope(<scope>).collection(<collection>)`, so "the defaults change nothing"
    rests entirely on those defaults naming the same handle `default_collection()`
    returns — and the installed SDK defines that handle as
    `default_scope().collection(Collection.default_name())`, i.e.
    `scope(Scope.default_name()).collection(Collection.default_name())`.

    Comparing against those SDK constants, rather than against the literal "_default",
    is the point: it pins the equivalence to the library that actually resolves it, so an
    SDK that ever renamed its default scope would fail HERE instead of in production.
    """
    from acouchbase.collection import Collection
    from acouchbase.scope import Scope

    shipped = getattr(_defaults(store_id), field)
    expected = Scope.default_name() if field.endswith("_scope") else Collection.default_name()
    assert shipped == expected


def test_defaults_bind_exactly_the_handle_default_collection_would_have_returned():
    """The same no-regression claim, end to end through a store.

    With no configuration at all, the learning stores open the default scope's default
    collection — the SDK's own definition of the `default_collection()` call they used to
    make. One store stands in for the four KV-shaped ones; the per-field check above
    covers the rest.
    """
    from acouchbase.collection import Collection
    from acouchbase.scope import Scope

    cluster = _Cluster()
    CouchbaseAuditStore(LearningSettings(_env_file=None), cluster=cluster)

    bucket = cluster.buckets["learning_audit"]
    assert bucket.opened == [(Scope.default_name(), Collection.default_name())]


# --- and no future statement may reintroduce a one-part keyspace --------------


_STORE_SOURCES = [
    "src/data_agent/runtime/session/couchbase_store.py",
    "src/data_agent/learning/audit/couchbase_audit_store.py",
    "src/data_agent/learning/candidate/couchbase_candidate_store.py",
    "src/data_agent/learning/dedup/couchbase_corpus.py",
    "src/data_agent/learning/user/couchbase_user_store.py",
]


def _is_keyspace_reference(node: Any) -> bool:
    """Is this interpolation something actually CALLED a keyspace?

    Three sanctioned spellings, all in the tree today: the helper call
    (`self._keyspace()`, `self.keyspace()`) the four learning stores use, and the local
    `keyspace` variable `CouchbaseSessionStore.scan_idle_sessions` composes — the reference
    implementation, which this slice was forbidden to touch. All three are covered by ONE
    rule: the identifier ends in `keyspace`.

    Loose in one direction, strict in the other. It admits a future `_results_keyspace()`
    or a renamed local with no edit here, and it admits NOTHING whose name does not claim
    to be a keyspace — `self._bucket_name`, `bucket`, `self._settings.couchbase_bucket` all
    fail it, which is the entire defect class. The name is not proof, but every helper it
    admits has its own three-part composition asserted by the binding tests above, and a
    local is visible in the same function body as the statement.
    """
    import ast

    if isinstance(node, ast.Call):
        node = node.func
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
    return name.lower().endswith("keyspace")


def _rendered_strings(path: str) -> list[str]:
    """Every string literal in *path* — f-string or plain — with interpolations rendered.

    THREE things this must get right, each of which was a hole:

      1. A keyspace-helper call renders as the distinct token `{ks}`; every OTHER
         interpolation renders as `{}`. Collapsing both to one token meant
         `f"FROM {self._bucket_name} c"` was indistinguishable from the sanctioned
         `f"FROM {self._keyspace()} c"`, so the guard's own docstring assumed the thing it
         was not checking.
      2. Plain `ast.Constant` strings are walked too. A statement with a hardcoded bucket
         and no interpolation at all is not a `JoinedStr`, so it was invisible.
      3. Neither token contains a dot. The dots ARE the keyspace separators counted below;
         a `{...}` token would make every offender read as already three-part.

    Docstrings are excluded — prose says "DERIVED FROM `X`" and similar, and that is not a
    statement. Comments never reach the AST at all.

    So are the `Constant` parts NESTED INSIDE an f-string: `ast.walk` yields them as nodes
    in their own right, so `f"SELECT r.* FROM {self.keyspace()} r "` would otherwise be
    counted twice — once whole (correct) and once as the fragment `"SELECT r.* FROM "`,
    which ends at the marker and reads as an empty keyspace. Every real statement in these
    files would have failed as a false positive.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / path).read_text())

    nested_in_fstring = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    part.value
                    if isinstance(part, ast.Constant)
                    else ("{ks}" if _is_keyspace_reference(part.value) else "{}")
                    for part in node.values
                )
            )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in nested_in_fstring
        ):
            out.append(node.value)
    return out


@pytest.mark.parametrize("path", _STORE_SOURCES, ids=[p.split("/")[-1] for p in _STORE_SOURCES])
def test_no_statement_names_a_keyspace_by_bucket_alone(path):
    """A one-part `` FROM `bucket` `` means EVERY scope and collection in that bucket.

    Under the bucket-per-store layout that was harmless. In a shared bucket it is a
    correctness bug that no type checker and no fake can catch: `list_by_status` would
    return other stores' documents, and `supersede` — which KV-REMOVES every id its query
    returns — would delete them. The four statements that exist today were all converted;
    this is the guard for the FIFTH, written by someone who copies an old one.

    Exactly TWO shapes are accepted after `FROM` / `UPDATE` / `INTO`:

      - `{ks}` — a reference to something named a keyspace (a `_keyspace()` helper, or the
        session store's local `keyspace`), whose three-part composition is asserted above; or
      - something naming all three parts itself (two dots), interpolated or literal.

    Everything else is an offender, including the three forms the previous version of this
    guard let through: a bare `` `{}` `` (backticked non-helper interpolation, the original
    defect), an unbackticked `{}` (which used to render identically to the sanctioned
    helper call), and a hardcoded literal with no interpolation at all (not an f-string, so
    never walked). Checking for the ABSENCE of the two good shapes rather than the presence
    of a known-bad one is the point: the next wrong form does not need to be predicted.
    """
    offenders = []
    for statement in _rendered_strings(path):
        for marker in ("FROM ", "UPDATE ", "INTO "):
            _head, sep, tail = statement.partition(marker)
            if not sep:
                continue
            keyspace = tail.split(" ")[0]
            if keyspace == "{ks}" or keyspace.count(".") >= 2:
                continue
            offenders.append(statement)
    assert offenders == []
