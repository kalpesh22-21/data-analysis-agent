"""`load_learning_settings_from_vault()` — the VAULT_ENABLED branch of the learning
settings surface.

The default posture (VAULT_ENABLED unset) is covered everywhere by construction: every
other test in this package builds `LearningSettings` and gets env/defaults. What has no
coverage without this module is the branch that only runs in a deployed environment —
where a mistake is a WRONG SECRET rather than a crash, and therefore invisible:

  - a per-key read failure is FAIL-SOFT (env/default is kept, the load keeps booting),
    so a group whose Vault path is wrong looks exactly like one that is correct and
    empty. That is the behaviour a test has to pin, because nothing else will notice it;
  - an empty `*_VAULT_PATH` skips its whole group, which is the documented escape hatch
    for "this secret is not in Vault yet";
  - VAULT_ENABLED with no `paycompy` installed must fail LOUD (RuntimeError), not
    silently fall back to env — an operator who asked for Vault must not get env values
    while believing otherwise.

The stub client below stands in for `paycompy.vault`'s client: `read_kv_secret(key, path)`
returns a secret-like object and records every call, so a test can assert both the VALUE
that landed and the (key, path) pair it was asked for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import data_agent.learning.config as learning_config
from data_agent.learning.config import LearningSettings, load_learning_settings_from_vault
from data_agent.runtime.config import RuntimeSettings

# --- stub Vault client -------------------------------------------------------


class _Secret:
    """`read_kv_secret` returns a secret WRAPPER, not a bare string (paycompy shape)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _StubVaultClient:
    """Records every `(key, path)` asked for; returns `f"vault:{key}"` by default.

    `raise_on` names keys whose read blows up, so a test can drive the per-key fail-soft
    path without needing a real Vault to be broken in a particular way.
    """

    def __init__(self, *, raise_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raise_on = raise_on

    def read_kv_secret(self, key: str, path: str) -> _Secret:
        self.calls.append((key, path))
        if key in self._raise_on:
            raise RuntimeError("vault exploded")
        return _Secret(f"vault:{key}")

    def keys_read(self) -> set[str]:
        return {key for key, _ in self.calls}


@pytest.fixture
def vault_on(monkeypatch: pytest.MonkeyPatch):
    """Turn the shared VAULT_ENABLED switch on and install a stub client factory.

    Returns a callable taking the stub and wiring it in, so a test can choose which keys
    fail before the load runs.
    """
    monkeypatch.setenv("VAULT_ENABLED", "true")

    def _install(client: _StubVaultClient) -> _StubVaultClient:
        monkeypatch.setattr(
            learning_config,
            "vault",
            SimpleNamespace(get_client_using_os_environ=lambda: client),
        )
        return client

    return _install


# --- (a) the full override ---------------------------------------------------

# Every (field, KV key) pair the loader is contracted to override. Spelled out rather
# than derived from the loader, so a key RENAMED in the loader fails here instead of
# being silently re-asserted against itself.
_ALL_FIELDS = (
    ("learning_redis_url", "LEARNING_REDIS_URL"),
    ("learning_audit_connection_string", "LEARNING_AUDIT_CONNECTION_STRING"),
    ("learning_audit_bucket", "LEARNING_AUDIT_BUCKET"),
    ("learning_audit_scope", "LEARNING_AUDIT_SCOPE"),
    ("learning_audit_collection", "LEARNING_AUDIT_COLLECTION"),
    ("learning_audit_username", "LEARNING_AUDIT_USERNAME"),
    ("learning_audit_password", "LEARNING_AUDIT_PASSWORD"),
    ("learning_candidates_connection_string", "LEARNING_CANDIDATES_CONNECTION_STRING"),
    ("learning_candidates_bucket", "LEARNING_CANDIDATES_BUCKET"),
    ("learning_candidates_scope", "LEARNING_CANDIDATES_SCOPE"),
    ("learning_candidates_collection", "LEARNING_CANDIDATES_COLLECTION"),
    ("learning_candidates_username", "LEARNING_CANDIDATES_USERNAME"),
    ("learning_candidates_password", "LEARNING_CANDIDATES_PASSWORD"),
    ("learning_corpus_connection_string", "LEARNING_CORPUS_CONNECTION_STRING"),
    ("learning_corpus_bucket", "LEARNING_CORPUS_BUCKET"),
    ("learning_corpus_scope", "LEARNING_CORPUS_SCOPE"),
    ("learning_corpus_collection", "LEARNING_CORPUS_COLLECTION"),
    ("learning_corpus_username", "LEARNING_CORPUS_USERNAME"),
    ("learning_corpus_password", "LEARNING_CORPUS_PASSWORD"),
    ("learning_extractor_api_key", "LEARNING_EXTRACTOR_API_KEY"),
)


def test_every_sensitive_field_is_overridden_from_vault(vault_on) -> None:
    """All 20 values come from Vault, each read under its documented key.

    Six of them (`*_SCOPE`/`*_COLLECTION`) are not secret material. They are read from the
    same KV path anyway because a credential and the keyspace it is granted on are ONE
    deployment fact — see `load_learning_settings_from_vault`.
    """
    client = vault_on(_StubVaultClient())

    settings = load_learning_settings_from_vault()

    for field, key in _ALL_FIELDS:
        assert getattr(settings, field) == f"vault:{key}", field
    assert client.keys_read() == {key for _, key in _ALL_FIELDS}


def test_each_group_is_read_from_its_own_path(vault_on) -> None:
    """The five groups read from five DIFFERENT paths — a copy-paste that pointed one
    group at another's secret would still pass the value assertions above."""
    client = vault_on(_StubVaultClient())

    settings = load_learning_settings_from_vault()

    by_key = dict(client.calls)
    assert by_key["LEARNING_REDIS_URL"] == settings.learning_redis_vault_path
    assert by_key["LEARNING_AUDIT_USERNAME"] == settings.learning_audit_vault_path
    assert by_key["LEARNING_CANDIDATES_USERNAME"] == settings.learning_candidates_vault_path
    assert by_key["LEARNING_CORPUS_USERNAME"] == settings.learning_corpus_vault_path
    assert (
        by_key["LEARNING_EXTRACTOR_API_KEY"] == settings.learning_extractor_api_key_vault_path
    )


def test_vault_disabled_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: no client is built and no key is read (the env-based deploy
    path is byte-identical to what it was before Vault existed)."""
    monkeypatch.setenv("VAULT_ENABLED", "false")
    monkeypatch.setenv("LEARNING_AUDIT_USERNAME", "env-user")
    client = _StubVaultClient()
    monkeypatch.setattr(
        learning_config,
        "vault",
        SimpleNamespace(get_client_using_os_environ=lambda: client),
    )

    settings = load_learning_settings_from_vault()

    assert client.calls == []
    assert settings.learning_audit_username == "env-user"


# --- (b) an empty path skips its group --------------------------------------


def test_empty_corpus_path_skips_that_group_only(
    vault_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LEARNING_CORPUS_VAULT_PATH=""` ⇒ the corpus group keeps env/default and NOTHING
    is read for it, while the other four groups are read as normal."""
    monkeypatch.setenv("LEARNING_CORPUS_VAULT_PATH", "")
    # Env values for the skipped group, so "kept" is distinguishable from "defaulted".
    monkeypatch.setenv("LEARNING_CORPUS_USERNAME", "env-corpus-user")
    monkeypatch.setenv("LEARNING_CORPUS_PASSWORD", "env-corpus-pw")
    client = vault_on(_StubVaultClient())

    settings = load_learning_settings_from_vault()

    assert settings.learning_corpus_username == "env-corpus-user"
    assert settings.learning_corpus_password == "env-corpus-pw"
    assert not any(key.startswith("LEARNING_CORPUS_") for key in client.keys_read())
    # The other four groups are untouched by one group's absence.
    assert settings.learning_redis_url == "vault:LEARNING_REDIS_URL"
    assert settings.learning_audit_username == "vault:LEARNING_AUDIT_USERNAME"
    assert settings.learning_candidates_username == "vault:LEARNING_CANDIDATES_USERNAME"
    assert settings.learning_extractor_api_key == "vault:LEARNING_EXTRACTOR_API_KEY"


@pytest.mark.parametrize(
    ("path_var", "prefix"),
    [
        ("LEARNING_REDIS_VAULT_PATH", "LEARNING_REDIS_URL"),
        ("LEARNING_AUDIT_VAULT_PATH", "LEARNING_AUDIT_"),
        ("LEARNING_CANDIDATES_VAULT_PATH", "LEARNING_CANDIDATES_"),
        ("LEARNING_CORPUS_VAULT_PATH", "LEARNING_CORPUS_"),
        ("LEARNING_EXTRACTOR_API_KEY_VAULT_PATH", "LEARNING_EXTRACTOR_API_KEY"),
    ],
)
def test_every_group_is_individually_skippable(
    vault_on, monkeypatch: pytest.MonkeyPatch, path_var: str, prefix: str
) -> None:
    """Each of the five paths is its own escape hatch — no group is unconditionally read."""
    monkeypatch.setenv(path_var, "")
    client = vault_on(_StubVaultClient())

    load_learning_settings_from_vault()

    assert not any(key.startswith(prefix) for key in client.keys_read())


# --- (c) enabled without paycompy is LOUD ------------------------------------


def test_enabled_without_paycompy_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """VAULT_ENABLED with the internal package absent must RAISE, never quietly serve
    env values to an operator who asked for Vault."""
    monkeypatch.setenv("VAULT_ENABLED", "true")
    monkeypatch.setattr(learning_config, "vault", None)

    with pytest.raises(RuntimeError, match="paycompy"):
        load_learning_settings_from_vault()


def test_missing_paycompy_is_fine_while_vault_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror of the above: no paycompy + Vault off is the ordinary dev/test posture
    and must construct normally."""
    monkeypatch.setenv("VAULT_ENABLED", "false")
    monkeypatch.setattr(learning_config, "vault", None)

    assert load_learning_settings_from_vault().vault_enabled is False


# --- (d) per-key fail-soft ---------------------------------------------------


def test_a_raising_key_keeps_env_and_the_rest_of_its_group_lands(
    vault_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing key falls back to env/default and does NOT abort its group or the
    load — the documented fail-soft, so a single renamed key cannot take a daemon down."""
    monkeypatch.setenv("LEARNING_AUDIT_BUCKET", "env-bucket")
    vault_on(_StubVaultClient(raise_on=frozenset({"LEARNING_AUDIT_BUCKET"})))

    settings = load_learning_settings_from_vault()

    assert settings.learning_audit_bucket == "env-bucket"
    # Siblings in the same group still came from Vault.
    assert settings.learning_audit_username == "vault:LEARNING_AUDIT_USERNAME"
    assert settings.learning_audit_password == "vault:LEARNING_AUDIT_PASSWORD"
    # And so did the later groups — the failure did not stop the walk.
    assert settings.learning_corpus_username == "vault:LEARNING_CORPUS_USERNAME"
    assert settings.learning_extractor_api_key == "vault:LEARNING_EXTRACTOR_API_KEY"


def test_a_totally_broken_vault_degrades_to_env_for_every_field(
    vault_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape of the fail-soft an operator has to know about: when EVERY read fails,
    the load still succeeds and every value is the env one. It is only distinguishable
    from a healthy load by the warning `_read_vault_secret` logs — which is why that
    warning has to exist."""
    monkeypatch.setenv("LEARNING_EXTRACTOR_API_KEY", "env-key")
    vault_on(_StubVaultClient(raise_on=frozenset(key for _, key in _ALL_FIELDS)))

    settings = load_learning_settings_from_vault()

    assert settings.learning_extractor_api_key == "env-key"
    assert settings.learning_audit_username == ""  # the shipped default


def test_a_failed_read_is_logged_with_no_secret_material(
    vault_on, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The warning names the KEY, the PATH and the EXCEPTION TYPE — and nothing else. A
    Vault client error can carry the response body, so the exception's own message must
    never reach the log."""
    vault_on(_StubVaultClient(raise_on=frozenset({"LEARNING_AUDIT_PASSWORD"})))

    with caplog.at_level("WARNING", logger="data_agent.runtime.config"):
        settings = load_learning_settings_from_vault()

    warnings = [r for r in caplog.records if "Vault read failed" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "LEARNING_AUDIT_PASSWORD" in message
    assert settings.learning_audit_vault_path in message
    assert "RuntimeError" in message
    assert "vault exploded" not in message


# --- the shared LLM path claim ----------------------------------------------


def test_extractor_key_path_matches_the_runtime_llm_path() -> None:
    """The field description says the extractor key lives at the SAME Vault path the
    runtime reads OPENAI_API_KEY from (different key, one LLM secret). Pinned, because a
    drift in either default turns that sentence into a lie an operator would act on."""
    assert (
        LearningSettings.model_fields["learning_extractor_api_key_vault_path"].default
        == RuntimeSettings.model_fields["openai_api_key_path"].default
    )


def test_vault_enabled_is_the_same_env_var_on_both_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONE operator switch flips BOTH planes (the deliberate sharing documented on the
    field). Asserted through the env var rather than the field name, because it is the
    env var an operator actually sets."""
    monkeypatch.setenv("VAULT_ENABLED", "true")

    assert LearningSettings().vault_enabled is True
    assert RuntimeSettings().vault_enabled is True


# --- (e) Vault cannot walk around the keyspace validator ---------------------
#
# The loader ASSIGNS onto an already-constructed settings object, and pydantic does not
# re-run a `mode="after"` validator on assignment. So `LearningSettings.__init__`'s check
# saw the ENV values and nothing Vault wrote over them — and `_read_vault_secret` is
# fail-soft, so an absent or misspelled KV key silently keeps the current value. A KV path
# holding a scope with no collection beside it therefore produced the unprovisionable
# `<scope>`.`_default` pair on the PRIMARY production path (every daemon reaches
# `load_learning_settings_from_vault` through `get_learning_settings()`), while the env-var
# spelling of the identical typo failed at boot.


class _PartialVaultClient(_StubVaultClient):
    """A Vault whose KV path holds SOME of the keys the loader asks for.

    `absent` names keys the path does not carry. `_read_vault_secret` is fail-soft on a
    read that raises, which is how a missing key behaves, so this reproduces "the operator
    set the scope in Vault and never added the collection" — or misspelled it, which is the
    same thing from the loader's side.
    """

    def __init__(self, *, absent: frozenset[str], overrides: dict[str, str]) -> None:
        super().__init__(raise_on=absent)
        self._overrides = overrides

    def read_kv_secret(self, key: str, path: str) -> _Secret:
        secret = super().read_kv_secret(key, path)  # records + raises for absent keys
        return _Secret(self._overrides[key]) if key in self._overrides else secret


@pytest.mark.parametrize("group", ["AUDIT", "CANDIDATES", "CORPUS"])
def test_a_vault_scope_without_its_collection_key_is_refused(vault_on, group: str) -> None:
    """The blocker: a named scope from Vault + a missing collection key must RAISE.

    Before the loader re-validated, this returned a settings object naming a keyspace
    Couchbase cannot hold, and the first KV write against it failed with
    keyspace-not-found — arbitrarily far from the KV path that caused it.
    """
    vault_on(
        _PartialVaultClient(
            absent=frozenset({f"LEARNING_{group}_COLLECTION"}),
            overrides={f"LEARNING_{group}_SCOPE": "learning"},
        )
    )

    with pytest.raises(ValueError) as exc:
        load_learning_settings_from_vault()

    # Names the KV key to add, not the one that was set correctly.
    assert f"LEARNING_{group}_COLLECTION" in str(exc.value)


def test_a_complete_vault_keyspace_still_loads(vault_on) -> None:
    """The shared-bucket deployment this slice exists for must pass the new gate.

    Guards against fixing the blocker with something that refuses any non-default scope:
    scope AND collection both present is the configuration the loader has to allow.
    """
    client = _PartialVaultClient(
        absent=frozenset(),
        overrides={
            "LEARNING_AUDIT_SCOPE": "learning",
            "LEARNING_AUDIT_COLLECTION": "audit",
            "LEARNING_CANDIDATES_SCOPE": "learning",
            "LEARNING_CANDIDATES_COLLECTION": "candidates",
            "LEARNING_CORPUS_SCOPE": "learning",
            "LEARNING_CORPUS_COLLECTION": "corpus",
        },
    )
    vault_on(client)

    settings = load_learning_settings_from_vault()

    assert settings.learning_audit_scope == "learning"
    assert settings.learning_audit_collection == "audit"
    assert settings.learning_corpus_collection == "corpus"


def test_the_default_vault_path_shape_still_loads(vault_on) -> None:
    """The stub's default `vault:<KEY>` values give every scope AND collection a non-default
    name, so the re-validation must not fire — the gate rejects one specific PAIR, not
    "anything that came from Vault"."""
    vault_on(_StubVaultClient())

    settings = load_learning_settings_from_vault()

    assert settings.learning_audit_scope == "vault:LEARNING_AUDIT_SCOPE"
    assert settings.learning_audit_collection == "vault:LEARNING_AUDIT_COLLECTION"
