"""Unit tests for the BFF's test-only `POST /api/session/scope` affordance
(D-L3-4) — specifically its S1 MONOTONIC-NARROWING guard.

`/api/session/scope` re-mints a session's JWT with a different `column_scope`.
Because `[]` == allow-all (D80b), an unguarded affordance would let a caller
re-WIDEN a narrowed session back to allow-all — a real escalation surface once
Item-9 per-user scoped tokens make base sessions non-allow-all. These tests lock
in that the endpoint narrows only: `[]` is refused, and a new scope must be a
subset of the session's current scope.

Hermetic — `_mint_jwt` is monkeypatched so no token service / network is needed
(the widen-rejections short-circuit BEFORE any mint anyway).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("UI_TEST_AFFORDANCES", "1")

    async def _fake_mint(column_scope: list[str]) -> str:
        return "fake.jwt." + ",".join(column_scope)

    monkeypatch.setattr(server, "_mint_jwt", _fake_mint)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    yield TestClient(server.app)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()


def _new_session(client: TestClient) -> str:
    response = client.post("/api/session")
    assert response.status_code == 200
    return response.json()["session_id"]


def test_scope_refuses_allow_all_widening(client: TestClient) -> None:
    session_id = _new_session(client)
    response = client.post(
        "/api/session/scope", json={"session_id": session_id, "column_scope": []}
    )
    assert response.status_code == 400
    assert "allow-all" in response.json()["detail"]
    # The refused request must NOT have re-minted (scope unchanged, still allow-all).
    assert server._SESSION_SCOPES[session_id] == []


def test_scope_accepts_narrower_subset_and_remints(client: TestClient) -> None:
    session_id = _new_session(client)
    response = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["demo.payroll.department"]},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert server._SESSION_SCOPES[session_id] == ["demo.payroll.department"]
    # Re-minted with the narrower scope (via the mocked minter).
    assert server._SESSIONS[session_id] == "fake.jwt.demo.payroll.department"


def test_scope_rejects_widening_a_narrowed_session(client: TestClient) -> None:
    session_id = _new_session(client)
    narrow = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["t.tbl.a", "t.tbl.b"]},
    )
    assert narrow.status_code == 200

    # A superset of the current allowlist is a WIDEN — must be refused.
    widen = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["t.tbl.a", "t.tbl.b", "t.tbl.c"]},
    )
    assert widen.status_code == 400
    assert "subset" in widen.json()["detail"]
    # Scope left unchanged by the refused widen.
    assert server._SESSION_SCOPES[session_id] == ["t.tbl.a", "t.tbl.b"]


def test_scope_allows_further_narrowing_within_current(client: TestClient) -> None:
    session_id = _new_session(client)
    client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["t.tbl.a", "t.tbl.b"]},
    )
    # A strict subset of the current allowlist is a valid further narrowing.
    response = client.post(
        "/api/session/scope", json={"session_id": session_id, "column_scope": ["t.tbl.a"]}
    )
    assert response.status_code == 200
    assert server._SESSION_SCOPES[session_id] == ["t.tbl.a"]


def test_scope_404_when_affordance_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _new_session(client)
    monkeypatch.delenv("UI_TEST_AFFORDANCES", raising=False)
    response = client.post(
        "/api/session/scope", json={"session_id": session_id, "column_scope": ["t.tbl.a"]}
    )
    assert response.status_code == 404


def test_scope_unknown_session_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/session/scope", json={"session_id": "no-such-session", "column_scope": ["t.tbl.a"]}
    )
    assert response.status_code == 404
