"""Unit tests for RuntimeCredentials (Layer 1 — no infra)."""

from __future__ import annotations

import dataclasses

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials


def test_is_frozen() -> None:
    creds = RuntimeCredentials(
        session_id="sess-1", jwt="header.payload.sig", column_scope=frozenset()
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        creds.jwt = "other"  # type: ignore[misc]


def test_empty_scope_is_allow_all() -> None:
    creds = RuntimeCredentials(session_id="sess-1", jwt="tok", column_scope=frozenset())
    assert creds.column_scope == frozenset()


def test_fields_roundtrip() -> None:
    scope = frozenset({"dbpcm_warehouse.employee.Department"})
    creds = RuntimeCredentials(session_id="sess-2", jwt="tok-2", column_scope=scope)
    assert creds.session_id == "sess-2"
    assert creds.jwt == "tok-2"
    assert creds.column_scope == scope
