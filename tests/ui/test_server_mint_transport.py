"""`ui/server.py::_mint_jwt` now mints through the shared `HttpTokenMinter`.

The BFF used to hand-roll its own `POST /token` — the fourth copy of a body whose
tenant claims had already been silently omitted once, in the offline plane, where the
only symptom was a 403 on every tool call. Sharing the transport closes that, but the
request path is NOT the promotion probe, and the two ways it differs are exactly the
ways a shared client could quietly break a UI session:

  * **TTL.** The probe fires once and discards its token (300s). A UI session's JWT has
    to outlive a conversation, so the BFF sends no `ttl_seconds` at all and the IdP's
    own configured lifetime applies. A shared default leaking in here would expire live
    sessions mid-conversation, and nothing about that failure would point at a mint.
  * **Allow-all.** `column_scope=[]` is a RESOLVED D80b entitlement here (the demo
    `ui-user` is entitled to everything), not the absent scope the offline backstop
    refuses. Without the explicit opt-in, every default session would 502.

Plus one behaviour this migration ADDS on purpose: `TenantClaims` validates, so a
deployment with a blank `TENANT_*` (the shipped Helm default) is told which knob is
missing at `POST /api/session` instead of getting a session whose every question 403s
with `MISSING_TENANT_CLAIM`.

And the FAILURE half of the rewritten call, which is the part a body-shape test cannot
see: `_mint_jwt` used to catch `httpx.HTTPError` and now catches `TokenMintError`. Those
are different exception types over the same failures, so "the token service is down" and
"the token service answered with something unusable" have to be re-proven to still leave
this function as a 502 — including the second case, which used to escape as an unhandled
`KeyError` (a 500).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import ui.server as server
from fastapi.testclient import TestClient


@pytest.fixture
def mint_bodies(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Capture the JSON body of every POST the BFF makes to the token service.

    Patches `httpx.AsyncClient` itself (not `_mint_jwt`), so the REAL minter builds the
    real body — the wire is the subject here.
    """
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
            request = httpx.Request("POST", url)
            return httpx.Response(200, json={"access_token": "jwt-x"}, request=request)

    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    yield bodies
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()


@pytest.fixture
def client(mint_bodies: list[dict]) -> TestClient:
    return TestClient(server.app)


def test_the_session_mint_sends_no_ttl_so_the_idp_lifetime_applies(
    client: TestClient, mint_bodies: list[dict]
) -> None:
    """Absent, not null: `token_service._mint` reads `ttl or settings
    .token_ttl_seconds`, and this is the body the hand-rolled mint posted."""
    client.post("/api/session")
    assert "ttl_seconds" not in mint_bodies[0]


def test_allow_all_entitlement_still_mints(client: TestClient, mint_bodies: list[dict]) -> None:
    """The default `ui-user` resolves to `[]` (D80b allow-all). The offline backstop
    refuses that scope; the request path opts out of it explicitly."""
    response = client.post("/api/session")
    assert response.status_code == 200
    assert mint_bodies[0]["column_scope"] == []


def test_the_mint_carries_the_three_tenant_claims(
    client: TestClient, mint_bodies: list[dict]
) -> None:
    """The claim NAMES are the MCP's `CLICKHOUSE_TENANT_SETTINGS` keys; a renamed or
    dropped one is a 403 before any tool runs."""
    client.post("/api/session")
    assert set(mint_bodies[0]["claims"]) == {"clientcode", "proc_center", "jti"}


def test_an_unreachable_token_service_is_still_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HttpTokenMinter` converts `httpx.HTTPError` into `TokenMintError` before it can
    reach the old `except httpx.HTTPError` — so the 502 has to be re-proven at the new
    exception type, not assumed from the old one."""

    class _ConnectFailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(server.httpx, "AsyncClient", _ConnectFailingClient)
    server._SESSIONS.clear()

    response = TestClient(server.app).post("/api/session")

    assert response.status_code == 502
    assert "Token service unreachable" in response.json()["detail"]
    assert server._SESSIONS == {}  # no half-built session left behind


def test_a_token_service_answer_with_no_access_token_is_a_502_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with an unusable body used to be an unhandled `KeyError` — a 500 with a
    traceback for what is plainly an upstream failure. The shared minter names it
    (`TokenMintError`), so it joins the other upstream failures at 502."""

    class _EmptyBodyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(server.httpx, "AsyncClient", _EmptyBodyClient)
    server._SESSIONS.clear()

    response = TestClient(server.app).post("/api/session")

    assert response.status_code == 502
    assert "Token service unreachable" in response.json()["detail"]
    assert server._SESSIONS == {}


def test_a_blank_tenant_claim_fails_the_session_loudly_not_the_query_silently(
    client: TestClient, mint_bodies: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank `TENANT_*` is the shipped Helm ConfigMap default. Before, the BFF minted a
    token the MCP then rejected on every question; now the misconfiguration is named at
    the session boundary — and as a 500 (server config), NOT the 502 that means the
    token service is unreachable."""
    monkeypatch.setattr(server, "TENANT_CLIENT_CODE", "")

    response = client.post("/api/session")

    assert response.status_code == 500
    assert "TENANT_CLIENT_CODE" in response.json()["detail"]
    assert mint_bodies == []  # refused before the wire
