"""QA wire-level suite for the BFF's X-Session-Id ↔ sid_hash binding
(auth-hardening Slice 1, data-agent side).

ADDS to (never modifies) ``test_server_sid_binding.py``.  That file mocks
``_mint_jwt`` and asserts the right ``session_id`` reaches the mint *function*.
This file goes one layer deeper and one layer wider, hermetically (no network):

  * mints through the REAL ``_mint_jwt`` with the outbound httpx call mocked, to
    prove the token-service REQUEST BODY carries ``session_id`` (the wire, not
    just the function arg) — this is what makes the token_service stamp
    ``sid_hash``;
  * proves the monotonic-narrow re-mint POSTs the SAME ``session_id`` (binding
    survives the narrow) while ``column_scope`` changes;
  * proves two sessions produce two DISTINCT ``session_id`` mint bodies (→ two
    distinct sid_hash bindings, no cross-session token reuse);
  * proves the runtime PROXY sends ``X-Session-Id`` == the session AND the
    ``Authorization`` bearer that was minted for THAT session — i.e. the header
    and the bound token always travel as a matched pair (the BFF never sends a
    token bound to one session with another session's header).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import ui.server as server
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Hermetic token service: intercept the outbound httpx POST /token so the real
# _mint_jwt runs (building the request body) without any network.
# ---------------------------------------------------------------------------


@pytest.fixture
def mint_bodies(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Capture the JSON body of every POST the BFF makes to the token service."""
    monkeypatch.setenv("UI_TEST_AFFORDANCES", "1")
    bodies: list[dict] = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            bodies.append(json)
            # A token that echoes the session it was minted for, so the proxy
            # test can prove the header and the token match.
            sid = json.get("session_id")
            request = httpx.Request("POST", url)
            return httpx.Response(
                200, json={"access_token": f"jwt.for.{sid}"}, request=request
            )

    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    yield bodies
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()


@pytest.fixture
def client(mint_bodies: list[dict]) -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# 1. The mint REQUEST BODY carries session_id (the wire, not just the arg)
# ---------------------------------------------------------------------------


def test_create_session_mint_body_carries_session_id(client: TestClient, mint_bodies: list[dict]):
    session_id = client.post("/api/session").json()["session_id"]
    assert len(mint_bodies) == 1
    body = mint_bodies[0]
    # The token service reads body["session_id"] to stamp sid_hash — prove it's on the wire.
    assert body["session_id"] == session_id
    assert body["column_scope"] == []  # base session is allow-all (D80b), still bound
    assert body["user_name"] == "ui-user"


def test_two_sessions_send_two_distinct_session_ids(client: TestClient, mint_bodies: list[dict]):
    s1 = client.post("/api/session").json()["session_id"]
    s2 = client.post("/api/session").json()["session_id"]
    assert s1 != s2
    sent = [b["session_id"] for b in mint_bodies]
    assert sent == [s1, s2]
    # Distinct session ids → distinct sid_hash bindings (no cross-session reuse).
    assert len(set(sent)) == 2


# ---------------------------------------------------------------------------
# 2. Re-mint preserves the SAME session_id across a scope narrow
# ---------------------------------------------------------------------------


def test_remint_body_preserves_session_id_and_narrows_scope(
    client: TestClient, mint_bodies: list[dict]
):
    session_id = client.post("/api/session").json()["session_id"]
    resp = client.post(
        "/api/session/scope",
        json={"session_id": session_id, "column_scope": ["demo.payroll.department"]},
    )
    assert resp.status_code == 200
    assert len(mint_bodies) == 2
    create_body, remint_body = mint_bodies
    # Binding is preserved: the re-mint POSTs the SAME session_id...
    assert create_body["session_id"] == session_id
    assert remint_body["session_id"] == session_id
    # ...only the scope narrowed.
    assert remint_body["column_scope"] == ["demo.payroll.department"]


# ---------------------------------------------------------------------------
# 3. The proxy sends X-Session-Id == session AND the token minted for it
# ---------------------------------------------------------------------------


def test_proxy_pairs_matching_session_header_with_its_bound_token(
    client: TestClient, mint_bodies: list[dict], monkeypatch: pytest.MonkeyPatch
):
    """The runtime proxy must send the token bound to session S together with
    ``X-Session-Id: S`` — never a token bound to one session with a different
    session's header (which the MCP would then 403).  With two live sessions we
    assert each turn carries the correctly-paired (Authorization, X-Session-Id)."""
    s1 = client.post("/api/session").json()["session_id"]
    s2 = client.post("/api/session").json()["session_id"]

    seen: list[dict] = []

    class _FakeStreamClient:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, method, url, headers=None, json=None):
            seen.append({"headers": dict(headers or {}), "url": url})

            class _Ctx:
                async def __aenter__(self):
                    request = httpx.Request(method, url)
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        content=b"event: result\ndata: {}\n\n",
                        request=request,
                    )

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

        async def aclose(self):
            return None

    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeStreamClient)

    # Drive a turn on each session.
    client.post("/api/turn", json={"session_id": s1, "message": "hi"})
    client.post("/api/turn", json={"session_id": s2, "message": "hi"})

    assert len(seen) == 2
    by_session = {}
    for call in seen:
        h = call["headers"]
        sid = h["X-Session-Id"]
        by_session[sid] = h["Authorization"]

    # Each session's header is paired with the token MINTED FOR THAT SESSION
    # (the fake token service echoed the session id into the token string).
    assert by_session[s1] == f"Bearer jwt.for.{s1}"
    assert by_session[s2] == f"Bearer jwt.for.{s2}"
    # And the two are genuinely different — no cross-session token/header mixing.
    assert by_session[s1] != by_session[s2]
