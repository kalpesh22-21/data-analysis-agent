"""`load_settings_from_vault()` — the Neo4j group of the runtime settings surface.

Mirrors `tests/learning/test_vault_config.py` (same stub client, same fail-soft
contract) but pins the branch that only runs in a deployed environment for the RUNTIME
plane. The value under test here is `NEO4J_DATABASE`: it joined the group late, after a
period in which the field existed and NOTHING consumed it, so a read that silently
stopped happening would look exactly like the bug that was just fixed — the pods would
keep booting and quietly read the wrong (default) database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import data_agent.runtime.config as runtime_config
from data_agent.runtime.config import RuntimeSettings, load_settings_from_vault


class _Secret:
    """`read_kv_secret` returns a secret WRAPPER, not a bare string (paycompy shape)."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _StubVaultClient:
    """Records every `(key, path)` asked for; returns `f"vault:{key}"` by default."""

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
    monkeypatch.setenv("VAULT_ENABLED", "true")

    def _install(client: _StubVaultClient) -> _StubVaultClient:
        monkeypatch.setattr(
            runtime_config,
            "vault",
            SimpleNamespace(get_client_using_os_environ=lambda: client),
        )
        return client

    return _install


# Every (field, KV key) pair the Neo4j group is contracted to override. Spelled out
# rather than derived from the loader, so a key RENAMED there fails here instead of
# being silently re-asserted against itself.
_NEO4J_FIELDS = (
    ("neo4j_url", "NEO4J_URL"),
    ("neo4j_username", "NEO4J_USERNAME"),
    ("neo4j_password", "NEO4J_PASSWORD"),
    ("neo4j_database", "NEO4J_DATABASE"),
)


def test_the_whole_neo4j_group_including_the_database_comes_from_vault(vault_on) -> None:
    """All four Neo4j values are read from the ONE creds path, under their documented
    keys — the database among them, not just the connection triple."""
    client = vault_on(_StubVaultClient())

    settings = load_settings_from_vault()

    for field, key in _NEO4J_FIELDS:
        assert getattr(settings, field) == f"vault:{key}", field
    by_key = dict(client.calls)
    for _, key in _NEO4J_FIELDS:
        assert by_key[key] == settings.neo4j_creds_path, key


def test_an_empty_creds_path_skips_the_group(
    vault_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NEO4J_CREDS_PATH=""` ⇒ env/default is kept and NOTHING Neo4j is read — the
    documented escape hatch for "this secret is not in Vault yet"."""
    monkeypatch.setenv("NEO4J_CREDS_PATH", "")
    monkeypatch.setenv("NEO4J_DATABASE", "env-db")
    client = vault_on(_StubVaultClient())

    settings = load_settings_from_vault()

    assert settings.neo4j_database == "env-db"
    assert not any(key.startswith("NEO4J_") for key in client.keys_read())


def test_a_failed_database_read_keeps_env_and_its_siblings_land(
    vault_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-key fail-soft: a broken NEO4J_DATABASE read falls back to env and does
    NOT abort the group, so one renamed key cannot take a pod down."""
    monkeypatch.setenv("NEO4J_DATABASE", "env-db")
    vault_on(_StubVaultClient(raise_on=frozenset({"NEO4J_DATABASE"})))

    settings = load_settings_from_vault()

    assert settings.neo4j_database == "env-db"
    assert settings.neo4j_username == "vault:NEO4J_USERNAME"
    assert settings.neo4j_password == "vault:NEO4J_PASSWORD"


def test_vault_disabled_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: no client is built and no key is read."""
    monkeypatch.setenv("VAULT_ENABLED", "false")
    monkeypatch.setenv("NEO4J_DATABASE", "env-db")
    client = _StubVaultClient()
    monkeypatch.setattr(
        runtime_config,
        "vault",
        SimpleNamespace(get_client_using_os_environ=lambda: client),
    )

    settings = load_settings_from_vault()

    assert client.calls == []
    assert settings.neo4j_database == "env-db"


def test_unset_database_is_the_driver_default() -> None:
    """Unset NEO4J_DATABASE must equal the database every call site hardcoded before
    the field was honoured — otherwise wiring it up silently repoints live deployments
    at a database that does not exist."""
    assert RuntimeSettings(_env_file=None).neo4j_database == "neo4j"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_database_normalises_to_the_default(blank: str) -> None:
    """`NEO4J_DATABASE=""` is not a third mode. Left alone it reaches the driver as
    "the server's home database" — which an operator did not ask for and cannot see in
    the config — so blank means exactly what unset means."""
    assert RuntimeSettings(_env_file=None, neo4j_database=blank).neo4j_database == "neo4j"


def test_a_blank_database_from_vault_normalises_too(vault_on) -> None:
    """The same claim on the OTHER write path. Vault mutates the field AFTER
    construction, so the field validator never runs on it; a KV key that exists and
    holds "" must still land on the default."""

    class _BlankDatabase(_StubVaultClient):
        def read_kv_secret(self, key: str, path: str) -> _Secret:
            secret = super().read_kv_secret(key, path)
            return _Secret("") if key == "NEO4J_DATABASE" else secret

    vault_on(_BlankDatabase())

    settings = load_settings_from_vault()

    assert settings.neo4j_database == "neo4j"
    # The rest of the group is untouched by the normalisation.
    assert settings.neo4j_username == "vault:NEO4J_USERNAME"
