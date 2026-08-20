"""A configured scope/collection pair must name a keyspace Couchbase can HOLD — Layer-1.

`test_couchbase_keyspace_binding.py` (next to this file) proves each store binds the
keyspace its config names. This module guards the step before that: which pairs a config is
allowed to name at all.

It lives runtime-side because the rule does. `check_keyspace_pair` is shared plumbing in
`runtime/config.py` alongside `TRUTHY_ENV_VALUES` and `_read_vault_secret`, for the reason
stated there: the offline plane may import from `runtime/`, never the reverse, so a check
both planes need can only live on this side. That is also why the table below spans all
three settings surfaces from one place — a per-plane copy of these assertions would be the
same drift the single checker exists to prevent.

Only the `_default` SCOPE has a `_default` COLLECTION — a named scope is created empty. So
`learning`.`_default` can be spelled but never provisioned, and nothing downstream says so:
`bucket.scope("learning").collection("_default")` resolves lazily, constructs fine, and
dies at the FIRST operation with a keyspace-not-found, arbitrarily far from the typo. The
provisioning scripts hide it further — they create the scope, then skip collection creation
for the `_default` name and report success.

The reason this is a VALIDATOR and not a comment: every one of these settings classes is
`extra="ignore"`, so a misspelled `LEARNING_AUDIT_COLLECTIION` is discarded in silence and
the collection falls back to `_default` while the correctly-spelled scope stands — landing
on exactly this pair by ordinary typo. Failing at construction turns it into a boot-time
error naming the variable to fix.

Hermetic: pure settings construction, no cluster and no `couchbase` package needed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from data_agent.learning.config import LearningSettings
from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.runtime.config import RuntimeSettings

# (id, settings factory, scope field, collection field, scope env, collection env).
# All SIX keyspace-carrying pairs across the three settings surfaces, so a pair added to
# any class without a validator entry fails here rather than in a cluster.
#
# The session store contributes TWO rows against ONE scope field: it binds `sessions` and
# `session_results` from the same scope, so naming that scope without renaming BOTH
# collections leaves one of them unprovisionable. A table keyed on the scope alone would
# have covered that store with a single row and missed exactly that case.
_PAIRS = [
    (
        "session-docs",
        RuntimeSettings,
        "couchbase_scope",
        "couchbase_sessions_collection",
        "COUCHBASE_SCOPE",
        "COUCHBASE_SESSIONS_COLLECTION",
    ),
    (
        "session-results",
        RuntimeSettings,
        "couchbase_scope",
        "couchbase_results_collection",
        "COUCHBASE_SCOPE",
        "COUCHBASE_RESULTS_COLLECTION",
    ),
    (
        "audit",
        LearningSettings,
        "learning_audit_scope",
        "learning_audit_collection",
        "LEARNING_AUDIT_SCOPE",
        "LEARNING_AUDIT_COLLECTION",
    ),
    (
        "candidates",
        LearningSettings,
        "learning_candidates_scope",
        "learning_candidates_collection",
        "LEARNING_CANDIDATES_SCOPE",
        "LEARNING_CANDIDATES_COLLECTION",
    ),
    (
        "corpus",
        LearningSettings,
        "learning_corpus_scope",
        "learning_corpus_collection",
        "LEARNING_CORPUS_SCOPE",
        "LEARNING_CORPUS_COLLECTION",
    ),
    (
        "user",
        UserKnowledgeStoreConfig,
        "user_knowledge_scope",
        "user_knowledge_collection",
        "USER_KNOWLEDGE_SCOPE",
        "USER_KNOWLEDGE_COLLECTION",
    ),
]

_IDS = [row[0] for row in _PAIRS]


@pytest.mark.parametrize(
    ("_id", "cls", "scope_field", "collection_field", "scope_env", "collection_env"),
    _PAIRS,
    ids=_IDS,
)
def test_named_scope_with_default_collection_is_refused(
    _id, cls, scope_field, collection_field, scope_env, collection_env
):
    """The unprovisionable pair fails at construction, not at the first KV op.

    BOTH halves are set explicitly rather than letting the collection fall back to its
    default, because the six surfaces do not share a default: the learning collections ship
    as `_default`, the session ones as `sessions`/`session_results`. Spelling the bad pair
    out drives the same construction on all six. The realistic route INTO this pair — a
    typo'd env var silently dropped, leaving the `_default` fallback standing — is driven
    through the environment in `test_a_misspelled_collection_env_var_is_caught_...`.
    """
    with pytest.raises(ValidationError):
        cls(_env_file=None, **{scope_field: "learning", collection_field: "_default"})


@pytest.mark.parametrize(
    ("_id", "cls", "scope_field", "collection_field", "scope_env", "collection_env"),
    _PAIRS,
    ids=_IDS,
)
def test_the_refusal_names_the_env_var_to_fix(
    _id, cls, scope_field, collection_field, scope_env, collection_env
):
    """The message must be actionable from a pod log alone.

    An operator reading it has a stack trace and no source tree, so the FIELD name is
    useless — the error has to name the ENV VAR they set, and the one they must set next.
    Both are asserted because naming only the scope would send them to un-set the half
    they got right.
    """
    with pytest.raises(ValidationError) as exc:
        cls(_env_file=None, **{scope_field: "learning", collection_field: "_default"})

    message = str(exc.value)
    assert scope_env in message
    assert collection_env in message


@pytest.mark.parametrize(
    ("_id", "cls", "scope_field", "collection_field", "scope_env", "collection_env"),
    _PAIRS,
    ids=_IDS,
)
def test_a_named_collection_in_the_default_scope_stays_legal(
    _id, cls, scope_field, collection_field, scope_env, collection_env
):
    """The REVERSE pair is ordinary Couchbase and must NOT be caught.

    `_default`.`sessions` is a named collection in the default scope — what the session
    store has shipped with since before scopes were configurable. A validator that
    rejected it would break every such deployment on upgrade, so the asymmetry is the
    point of the rule, not an oversight in it.
    """
    settings = cls(_env_file=None, **{collection_field: "knowledge"})

    assert getattr(settings, collection_field) == "knowledge"
    assert getattr(settings, scope_field) == "_default"


@pytest.mark.parametrize(
    ("_id", "cls", "scope_field", "collection_field", "scope_env", "collection_env"),
    _PAIRS,
    ids=_IDS,
)
def test_a_fully_named_keyspace_is_accepted(
    _id, cls, scope_field, collection_field, scope_env, collection_env
):
    """The shared-bucket layout this slice exists for must pass the validator."""
    settings = cls(_env_file=None, **{scope_field: "learning", collection_field: "audit"})

    assert getattr(settings, scope_field) == "learning"
    assert getattr(settings, collection_field) == "audit"


def test_the_shipped_defaults_construct():
    """No override at all — the bucket-per-store layout must not trip its own guard.

    Every existing deployment runs these, so a validator that refused any of them would
    turn this slice into an outage on upgrade. Note the session store ships the LEGAL
    asymmetric shape (`_default` scope, NAMED collections), which is the concrete reason
    the rule has to be one-directional rather than "scope and collection must agree".
    """
    assert LearningSettings(_env_file=None).learning_audit_scope == "_default"
    assert UserKnowledgeStoreConfig(_env_file=None).user_knowledge_scope == "_default"

    runtime = RuntimeSettings(_env_file=None)
    assert runtime.couchbase_scope == "_default"
    assert runtime.couchbase_sessions_collection == "sessions"
    assert runtime.couchbase_results_collection == "session_results"


def test_a_misspelled_collection_env_var_is_caught_not_silently_dropped(monkeypatch):
    """The exact typo the validator exists for, driven through the ENVIRONMENT.

    `extra="ignore"` means the misspelling below is not an error and not a warning — it is
    discarded, and `learning_audit_collection` keeps its `_default` default. Before this
    validator that produced a store pointed at `pcm_iwant`.`learning`.`_default`: a
    keyspace that cannot exist, provisioned "successfully", failing at the first write.
    """
    monkeypatch.setenv("LEARNING_AUDIT_SCOPE", "learning")
    monkeypatch.setenv("LEARNING_AUDIT_COLLECTIION", "audit")  # note the typo

    with pytest.raises(ValidationError) as exc:
        LearningSettings(_env_file=None)

    assert "LEARNING_AUDIT_COLLECTION" in str(exc.value)


def test_all_three_settings_surfaces_share_one_definition_of_a_valid_keyspace():
    """The three classes must not drift on what a keyspace is.

    They are deliberately separate surfaces — `RuntimeSettings` and `LearningSettings` are
    two planes, and `UserKnowledgeStoreConfig` is split off again (S8) — which is precisely
    how three copies of a rule diverge. Each imports the ONE checker instead of restating
    it, and this pins that as object identity: same function, one place to change.

    The identity is asserted against `runtime.config` specifically, not just "all equal",
    because WHICH module owns it is the layering constraint: `learning/` may import from
    `runtime/`, never the reverse, so a future move of this function back onto the offline
    side would break the import direction while still leaving three names in agreement.
    """
    from data_agent.learning import config as learning_config
    from data_agent.learning.user import config as user_config
    from data_agent.runtime import config as runtime_config

    assert learning_config.check_settings_keyspaces is runtime_config.check_settings_keyspaces
    assert user_config.check_settings_keyspaces is runtime_config.check_settings_keyspaces


def test_runtime_does_not_import_the_offline_plane():
    """The direction that makes the shared checker legal, asserted where it is relied on.

    `tests/learning/test_kill_switch.py` already text-scans `runtime/` for the offline
    package path as a D58c guard. This is the same fact stated from the other end and for a
    different reason: the checker was MOVED here so both planes could share it, and the
    move is only correct while nothing under `runtime/` reaches back. If that ever changes,
    the failure should name the keyspace validator, which is the thing that tempted it.
    """
    from pathlib import Path

    runtime_root = Path(__file__).resolve().parents[2] / "src" / "data_agent" / "runtime"
    assert runtime_root.is_dir()

    offenders = [
        str(path)
        for path in runtime_root.rglob("*.py")
        if "data_agent.learning" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "shared plumbing flows inward-to-outward ONLY; runtime/ must not import the "
        f"offline package: {offenders}"
    )


# --- the table may not fall behind the fields ---------------------------------


def _derived_pairs(cls) -> set[tuple[str, str, str]]:
    """Every (class name, scope field, collection field) *cls* declares, derived here.

    Deliberately a SECOND implementation of the pairing rule, not a call to
    `runtime.config.keyspace_pairs`. This is the assertion that the production deriver
    finds everything, so sharing its code would make it assert that a function agrees with
    itself. It is six lines; that duplication is the test.
    """
    fields = list(cls.model_fields)
    return {
        (cls.__name__, scope, collection)
        for scope in fields
        if scope.endswith("_scope")
        for collection in fields
        if collection.endswith("_collection")
        and collection.startswith(f"{scope[: -len('_scope')]}_")
    }


_ALL_DERIVED = (
    _derived_pairs(RuntimeSettings)
    | _derived_pairs(LearningSettings)
    | _derived_pairs(UserKnowledgeStoreConfig)
)


def test_every_declared_keyspace_pair_is_in_the_table():
    """A pair added to any settings class must appear in `_PAIRS` — WITHOUT anyone
    remembering to add it here.

    The table above drives every behavioural assertion in this module, and its comment
    claims an unguarded pair "fails here rather than in a cluster". Hand-maintained, that
    claim was false in the way that matters: the person who adds `learning_metrics_scope`
    and forgets the validator is the same person who would have had to remember the row.
    This derives the truth from `model_fields` — the thing being protected — so forgetting
    is no longer possible. Same lesson this repo has already paid for by spot-patching a
    named list instead of deriving the guard.
    """
    tabled = {(cls.__name__, scope, collection) for _, cls, scope, collection, _, _ in _PAIRS}

    missing = _ALL_DERIVED - tabled
    assert missing == set(), (
        "these scope/collection pairs are declared but untested — add a row to _PAIRS "
        f"(and check the class actually validates them): {sorted(missing)}"
    )


def test_the_table_names_no_pair_that_does_not_exist():
    """The other direction: a row whose fields were renamed away must not sit there
    passing vacuously. `getattr` on a stale field name would raise, but a row for a class
    that no longer declares the pair at all would simply never be exercised as a keyspace."""
    tabled = {(cls.__name__, scope, collection) for _, cls, scope, collection, _, _ in _PAIRS}

    stale = tabled - _ALL_DERIVED
    assert stale == set(), f"_PAIRS names pairs no settings class declares: {sorted(stale)}"


def test_the_production_deriver_finds_exactly_the_declared_pairs():
    """`keyspace_pairs` is what the validators and the Vault loader actually iterate.

    If it silently stopped finding pairs — a tightened suffix check, a prefix rule that
    stopped matching `couchbase_sessions_collection` — every validator would start
    accepting everything. The `refused` cases above would catch a TOTAL failure; this
    catches a PARTIAL one, which is the shape that ships.
    """
    from data_agent.runtime.config import keyspace_pairs

    for cls in (RuntimeSettings, LearningSettings, UserKnowledgeStoreConfig):
        found = {(cls.__name__, scope, collection) for scope, collection in keyspace_pairs(cls)}
        assert found == _derived_pairs(cls), cls.__name__


def test_a_scope_field_with_no_collection_sibling_is_ignored():
    """Not every `*_scope` is a Couchbase scope.

    An OAuth-style `*_scope` field with no `*_collection` beside it must yield NO pairs
    rather than a made-up one — otherwise adding an unrelated setting whose name happens to
    end in `_scope` would start failing boot on a rule that does not apply to it.
    """
    from pydantic_settings import BaseSettings

    from data_agent.runtime.config import keyspace_pairs

    class _Unrelated(BaseSettings):
        oauth_scope: str = "read:all"
        unrelated_collection: str = "_default"

    assert keyspace_pairs(_Unrelated()) == []
