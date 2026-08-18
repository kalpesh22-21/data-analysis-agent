"""E1/E2 — the BFF's lazy session-token refresh, and the entitlement it must NOT
re-resolve while doing it.

E1 (docs/cleanup/ISSUES.md): the BFF minted ONCE at `POST /api/session` and never
again, while the token service's `token_ttl_seconds` is 3600 and `verify_jwt`
requires `exp`. At ~61 minutes the next turn 401'd with no recovery, against a
product expectation of 8-hour sessions. The fix is a refresh on the token-ATTACH
path (`_jwt_for_session`), so the user's next request renews the session and an
idle session simply expires — no scheduler, no per-session timer.

E2 is the constraint that makes the refresh correct rather than merely working, and
it is the half a reviewer cannot see from inside auth code: the re-mint replays the
session's CACHED `column_scope` and identity. Re-resolving would let an entitlement
change land mid-session, which breaks Decision 7's same-scope guarantee — and that
guarantee is what retired scope-hash stamping on materialized scratch tables (Q15
option (b)). The failure would surface in a scratch table, three components away.
`test_a_refresh_never_re_resolves_the_callers_entitlement` is that pin, and it is
written as a TRAP (the resolvers raise) rather than as a call count, because a
count can be satisfied by a resolver that was called and whose result was thrown
away — which is the same defect with tidier bookkeeping.

TIME IS MANIPULATED, NEVER SLEPT. Every "aged" session is produced by rewriting
`_SESSION_TOKEN_MINTED_AT` — the same value `_store_token` writes — so the bands
are exercised exactly and the suite stays instant.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import ui.entitlements as entitlements
import ui.server as server
from fastapi import HTTPException
from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"turns": []}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stands in for `httpx.AsyncClient` on the `/api/history` hop, recording the
    headers the BFF attached. `/api/history` is the proxy under test throughout
    because it is the SIMPLEST token-attach site — one request, one JSON reply, no
    streaming teardown — and `_jwt_for_session` is shared by all four."""

    captured: list[dict] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, *, headers: dict) -> _FakeResponse:
        type(self).captured.append(dict(headers))
        return _FakeResponse()


@pytest.fixture
def mints(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Every `_mint_jwt` call, with a token that names the call ordinal — so "which
    token was attached" answers "which mint produced it"."""
    calls: list[dict] = []

    async def _fake_mint(user_name: str, column_scope: list[str], session_id: str) -> str:
        calls.append(
            {"user_name": user_name, "column_scope": list(column_scope), "session_id": session_id}
        )
        return f"jwt-{len(calls)}"

    monkeypatch.setattr(server, "_mint_jwt", _fake_mint)
    _FakeAsyncClient.captured = []
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    server._SESSION_USERS.clear()
    server._SESSION_TOKEN_MINTED_AT.clear()
    yield calls
    server._SESSIONS.clear()
    server._SESSION_SCOPES.clear()
    server._SESSION_USERS.clear()
    server._SESSION_TOKEN_MINTED_AT.clear()


@pytest.fixture
def client(mints: list[dict]) -> TestClient:
    return TestClient(server.app)


def _new_session(client: TestClient) -> str:
    response = client.post("/api/session")
    assert response.status_code == 200
    return response.json()["session_id"]


def _age_to(session_id: str, seconds: float) -> None:
    """Backdate the session's recorded mint time so its token is *seconds* old."""
    server._SESSION_TOKEN_MINTED_AT[session_id] -= seconds


class _FrozenClock:
    """`time`, as `ui/server.py` alone sees it — stopped. Only the module's own
    binding is replaced, so nothing else in the process loses its clock."""

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now


def _pin_age(monkeypatch: pytest.MonkeyPatch, session_id: str, age: float) -> None:
    """Make the session's token age EXACTLY *age* — the boundary-case sibling of
    `_age_to`.

    `_age_to` backdates the stamp and leaves the clock running, so by the time
    `_jwt_for_session` subtracts, the real age is microseconds PAST the target —
    always on the far side of a `<=`, which is precisely the comparison an exact
    boundary case exists to pin. Freezing the clock is the only way to land ON it.
    """
    frozen = _FrozenClock(server._SESSION_TOKEN_MINTED_AT[session_id] + age)
    monkeypatch.setattr(server, "time", frozen)


def _attached_tokens() -> list[str]:
    return [h["Authorization"].removeprefix("Bearer ") for h in _FakeAsyncClient.captured]


# --- the three age bands -----------------------------------------------------


def test_a_fresh_session_attaches_its_original_token_and_never_re_mints(
    client: TestClient, mints: list[dict]
) -> None:
    """The default case, and the one a refresh must not disturb: a session used
    inside the refresh window pays for exactly the one mint it made at creation, no
    matter how many calls it proxies."""
    session_id = _new_session(client)

    for _ in range(3):
        assert client.get(f"/api/history?session_id={session_id}").status_code == 200

    assert len(mints) == 1
    assert _attached_tokens() == ["jwt-1", "jwt-1", "jwt-1"]


def test_age_exactly_at_the_refresh_threshold_is_still_the_serve_band(
    client: TestClient, mints: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `age <= REFRESH_AFTER` edge, exactly. The threshold is the age at which
    the NEXT call re-mints, not the last age served — a `<` here would re-mint one
    second early, which is harmless in production and invisible in every other test
    in this file, so this is the only place the boundary is stated."""
    session_id = _new_session(client)
    _pin_age(monkeypatch, session_id, server._TOKEN_REFRESH_AFTER_SECONDS)

    assert client.get(f"/api/history?session_id={session_id}").status_code == 200

    assert len(mints) == 1, "the threshold age is served, not refreshed"
    assert _attached_tokens() == ["jwt-1"]


def test_an_aged_session_re_mints_once_and_every_later_call_uses_the_new_token(
    client: TestClient, mints: list[dict]
) -> None:
    """The fix. The FOLLOW-UP calls are the real assertion: a refresh that minted a
    fresh token per request would also make the first call succeed, and would burn a
    mint on every message of an 8-hour conversation."""
    session_id = _new_session(client)
    _age_to(session_id, server._TOKEN_REFRESH_AFTER_SECONDS + 1)

    assert client.get(f"/api/history?session_id={session_id}").status_code == 200
    assert client.get(f"/api/history?session_id={session_id}").status_code == 200

    assert len(mints) == 2, "one mint at creation, one refresh — not one per request"
    assert _attached_tokens() == ["jwt-2", "jwt-2"]
    assert server._SESSIONS[session_id] == "jwt-2"


def test_the_refresh_re_stamps_the_mint_time_so_the_window_restarts(
    client: TestClient, mints: list[dict]
) -> None:
    """`_store_token` writes the token AND its age together. Without the re-stamp
    the session stays permanently over the threshold and re-mints forever — the
    failure the single-writer rule exists to prevent, and one that looks like a
    working refresh until you count the mints."""
    session_id = _new_session(client)
    _age_to(session_id, server._TOKEN_REFRESH_AFTER_SECONDS + 1)

    for _ in range(4):
        client.get(f"/api/history?session_id={session_id}")

    assert len(mints) == 2


# --- E2: the entitlement that must not be re-resolved ------------------------


def test_a_refresh_never_re_resolves_the_callers_entitlement(
    client: TestClient, mints: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2 / Decision 7 / Q22. THE assertion of this file.

    The resolvers are replaced with traps AFTER the session exists, so any call is a
    hard failure rather than a number to reconcile. A count-based version of this
    test would pass against a refresh that re-resolved and then discarded the
    result — which is exactly as broken, because the next person removes the
    discard.

    The mint is then asserted to have carried the ORIGINAL claims: same identity,
    same scope, same `session_id` (so the Slice-1 `sid_hash` binding survives too).
    """
    monkeypatch.setenv("UI_TEST_AFFORDANCES", "1")
    response = client.post("/api/session", headers={"X-Debug-User": "restricted-hr"})
    session_id = response.json()["session_id"]
    entitled = list(server._SESSION_SCOPES[session_id])
    assert entitled == [
        "dbpcm_warehouse.employee.EmployeeCode",
        "dbpcm_warehouse.employee.Department",
    ], "fixture check: the refresh must have a NON-allow-all scope to lose"

    def _trap(*args: object, **kwargs: object):
        raise AssertionError(
            "the token refresh re-resolved the caller's entitlement — this breaks "
            "Decision 7's same-scope invariant (see _refresh_session_token)"
        )

    monkeypatch.setattr(server, "resolve_column_scope", _trap)
    monkeypatch.setattr(server, "resolve_caller_identity", _trap)
    monkeypatch.setattr(entitlements, "resolve_column_scope", _trap)
    monkeypatch.setattr(entitlements, "resolve_caller_identity", _trap)

    _age_to(session_id, server._TOKEN_REFRESH_AFTER_SECONDS + 1)
    assert client.get(f"/api/history?session_id={session_id}").status_code == 200

    assert mints[-1] == {
        "user_name": "restricted-hr",
        "column_scope": entitled,
        "session_id": session_id,
    }
    assert server._SESSION_SCOPES[session_id] == entitled


def test_a_narrowed_session_refreshes_at_the_narrowed_scope_not_the_entitled_base(
    client: TestClient, mints: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same-scope invariant is about the scope the session IS ON, which the
    test-only narrow affordance can move. A refresh that replayed the entitled base
    instead of the current cached scope would silently RE-WIDEN a narrowed session —
    the escalation `/api/session/scope`'s own monotonic guard exists to prevent,
    arriving through the back door an hour later."""
    monkeypatch.setenv("UI_TEST_AFFORDANCES", "1")
    response = client.post("/api/session", headers={"X-Debug-User": "restricted-hr"})
    session_id = response.json()["session_id"]
    narrowed = ["dbpcm_warehouse.employee.EmployeeCode"]
    assert (
        client.post(
            "/api/session/scope",
            json={"session_id": session_id, "column_scope": narrowed},
        ).status_code
        == 200
    )

    _age_to(session_id, server._TOKEN_REFRESH_AFTER_SECONDS + 1)
    assert client.get(f"/api/history?session_id={session_id}").status_code == 200

    assert mints[-1]["column_scope"] == narrowed


# --- the failure matrix ------------------------------------------------------


def _break_the_minter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_mint_jwt` fail the way it really does — the `HTTPException(502)` it
    raises when `HttpTokenMinter` reports a `TokenMintError`."""

    async def _failing(user_name: str, column_scope: list[str], session_id: str) -> str:
        raise HTTPException(status_code=502, detail="Token service unreachable: boom")

    monkeypatch.setattr(server, "_mint_jwt", _failing)


def test_a_failed_refresh_inside_the_ttl_keeps_serving_the_old_token(
    client: TestClient, mints: list[dict], monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The shock-absorber band. The held token is past the refresh threshold but NOT
    past the TTL, so it still works — a token-service blip must not break a
    conversation that would otherwise have kept going for another five minutes."""
    session_id = _new_session(client)
    _break_the_minter(monkeypatch)
    _age_to(session_id, server._TOKEN_REFRESH_AFTER_SECONDS + 1)

    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        response = client.get(f"/api/history?session_id={session_id}")

    assert response.status_code == 200
    assert _attached_tokens() == ["jwt-1"], "the original, still-valid token"
    assert server._SESSIONS[session_id] == "jwt-1"
    assert any(
        "session token refresh failed" in record.message for record in caplog.records
    ), "the degradation is logged — silently limping is how this becomes invisible"


def test_a_failed_refresh_past_the_ttl_is_a_loud_502_not_a_dead_token_on_the_wire(
    client: TestClient, mints: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The band with no fallback. Proxying a KNOWN-expired token would come back as
    a bare `401` from the runtime — an auth error pointing at the wrong component,
    for a token service that is merely down. So the BFF answers for itself, and the
    message says what happened."""
    session_id = _new_session(client)
    _break_the_minter(monkeypatch)
    _age_to(session_id, server._TOKEN_TTL_SECONDS + 1)

    response = client.get(f"/api/history?session_id={session_id}")

    assert response.status_code == 502
    assert "expired" in response.json()["detail"]
    assert _FakeAsyncClient.captured == [], "nothing was proxied with the dead token"


def test_a_failed_refresh_at_exactly_the_ttl_still_serves_the_held_token(
    client: TestClient, mints: list[dict], monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The `expired = age > TTL` edge, exactly: at PRECISELY the TTL the token is
    still alive, so this is the fallback row of the matrix and not the 502 row. The
    two rows differ by one comparison operator, and `>=` would move a token that
    still verifies (`verify_jwt` even allows 60s of skew on top) into the band with
    no fallback — turning a survivable blip into a dead session."""
    session_id = _new_session(client)
    _break_the_minter(monkeypatch)
    _pin_age(monkeypatch, session_id, server._TOKEN_TTL_SECONDS)

    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        response = client.get(f"/api/history?session_id={session_id}")

    assert response.status_code == 200
    assert _attached_tokens() == ["jwt-1"], "the held token is still valid AT the TTL"
    assert server._SESSIONS[session_id] == "jwt-1"
    assert any("session token refresh failed" in record.message for record in caplog.records)


def test_a_past_ttl_session_recovers_when_the_token_service_does(
    client: TestClient, mints: list[dict]
) -> None:
    """Past-TTL is not a terminal state for the SESSION, only for the token: with a
    working minter the same request re-mints and proceeds. Without this, the 502
    above could be satisfied by simply refusing every over-TTL session outright,
    which would cap sessions at an hour again — the bug."""
    session_id = _new_session(client)
    _age_to(session_id, server._TOKEN_TTL_SECONDS + 1)

    assert client.get(f"/api/history?session_id={session_id}").status_code == 200
    assert _attached_tokens() == ["jwt-2"]


# --- shape / plumbing --------------------------------------------------------


def test_every_token_attaching_proxy_shares_the_refresh(
    client: TestClient, mints: list[dict]
) -> None:
    """The refresh lives in `_jwt_for_session`, which is the single lookup all four
    proxies use — so `/api/query/page` renews a session exactly as `/api/history`
    does. Pinned because a per-route copy of the attach is the obvious next
    "optimization", and only one of the copies would get the refresh."""
    session_id = _new_session(client)
    _age_to(session_id, server._TOKEN_REFRESH_AFTER_SECONDS + 1)

    class _PostingClient(_FakeAsyncClient):
        async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
            type(self).captured.append(dict(headers))
            return _FakeResponse()

    server.httpx.AsyncClient = _PostingClient  # type: ignore[misc]
    try:
        response = client.post(
            "/api/query/page", json={"session_id": session_id, "sql": "SELECT 1"}
        )
    finally:
        server.httpx.AsyncClient = _FakeAsyncClient  # type: ignore[misc]

    assert response.status_code == 200
    assert len(mints) == 2
    assert _attached_tokens() == ["jwt-2"]


def test_an_unknown_session_is_still_a_404_and_mints_nothing(
    client: TestClient, mints: list[dict]
) -> None:
    """`_SESSIONS` remains the membership test. A refresh path that treated "no
    token" as "expired token" would mint one for a `session_id` a caller invented."""
    response = client.get("/api/history?session_id=s" + "9" * 32)

    assert response.status_code == 404
    assert mints == []


def test_a_token_stored_without_a_mint_time_is_served_not_refreshed(
    client: TestClient, mints: list[dict], caplog
) -> None:
    """`_store_token` is the only writer of `_SESSIONS`, so this state is
    unreachable through the API — but the three older proxy test modules seed
    `_SESSIONS` directly, and a future restart-restore could too. Such a record has
    no age to measure and no cached claims to re-mint from, so it is served
    unchanged (pre-E1 behaviour) and the gap is named in the log, rather than being
    silently guessed at in either direction.
    """
    session_id = "s" + "1" * 32
    server._SESSIONS[session_id] = "hand-seeded"

    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        response = client.get(f"/api/history?session_id={session_id}")

    assert response.status_code == 200
    assert _attached_tokens() == ["hand-seeded"]
    assert mints == []
    assert any("no recorded mint time" in record.message for record in caplog.records)


def test_the_refresh_threshold_sits_strictly_inside_the_ttl() -> None:
    """The margin is the whole safety story: the refresh must fire while the held
    token is still USABLE, so a failed mint has something to fall back on. Equal
    values would delete the shock-absorber band and make every token-service blip at
    the wrong moment a 502."""
    assert 0 < server._TOKEN_REFRESH_AFTER_SECONDS < server._TOKEN_TTL_SECONDS
