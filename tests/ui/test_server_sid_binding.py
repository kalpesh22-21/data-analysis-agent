"""Hermetic unit tests for the BFF's X-Session-Id ↔ sid_hash binding
(auth-hardening Slice 1, data-agent side).

The BFF mints one JWT per session and MUST thread that session's `session_id`
into the token-service mint request, so the minted JWT carries a `sid_hash`
claim bound to the session. These tests capture the mint calls (the token
service is mocked — no network) and assert:

  * `create_session` mints with `session_id` equal to the session it returns.
  * the monotonic-narrow re-mint (`POST /api/session/scope`) preserves the SAME
    `session_id` (so the binding stays valid) while narrowing `column_scope`.

The wire hash itself lives in clickhouse-api's token service (the BFF only
passes the raw `session_id`), so these tests assert the *plumbing*: the right
`session_id` reaches the mint request. The clickhouse-api suite proves the hash
encoding and the MCP-side enforcement.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient


@pytest.fixture
def mint_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Capture every `_mint_jwt(column_scope, session_id)` call, hermetically."""
    monkeypatch.setenv("UI_TEST_AFFORDANCES", "1")
    calls: list[dict] = []

    async def _fake_mint(column_scope: list[str], session_id: str) -> str:
        calls.append({"column_scope": list(column_scope), "session_id": session_id})
        return f"fake.jwt.{session_id}.{','.join(column_scope)}"

    monkeypatch.setattr(server, "_mint_jwt", _fake_mint)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    yield calls
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()


@pytest.fixture
def client(mint_calls: list[dict]) -> TestClient:
    return TestClient(server.app)


def test_create_session_mints_with_its_session_id(client: TestClient, mint_calls: list[dict]) -> None:
    """create_session → the mint request carries session_id == the returned session."""
    resp = client.post("/api/session")
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    assert len(mint_calls) == 1
    assert mint_calls[0]["session_id"] == session_id
    # Base session is allow-all (D80b) — scope empty, but still session-bound.
    assert mint_calls[0]["column_scope"] == []


def test_remint_preserves_session_id_on_scope_narrow(
    client: TestClient, mint_calls: list[dict]
) -> None:
    """The scope-narrow re-mint keeps the SAME session_id (sid_hash stays valid)."""
    session_id = client.post("/api/session").json()["session_id"]

    resp = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["demo.payroll.department"]},
    )
    assert resp.status_code == 200

    # Two mint calls: the initial create + the narrow re-mint.
    assert len(mint_calls) == 2
    create_call, remint_call = mint_calls
    # Same session id across both → the binding is preserved through the narrow.
    assert create_call["session_id"] == session_id
    assert remint_call["session_id"] == session_id
    # Only the scope changed (narrowed); the session binding did not.
    assert remint_call["column_scope"] == ["demo.payroll.department"]


def test_two_sessions_are_bound_to_distinct_ids(client: TestClient, mint_calls: list[dict]) -> None:
    """Each session mints a token bound to its own id (no cross-session reuse)."""
    s1 = client.post("/api/session").json()["session_id"]
    s2 = client.post("/api/session").json()["session_id"]

    assert s1 != s2
    minted_ids = {c["session_id"] for c in mint_calls}
    assert minted_ids == {s1, s2}
