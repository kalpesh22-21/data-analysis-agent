"""BFF `POST /api/query/page` proxy — paging the model-designated answer table.

Mirrors `test_history_proxy.py`: the browser sends only `session_id` + the
`answer_sql` it was given on the turn's `result` event; the BFF looks the JWT up
server-side and attaches it on the hop, so the browser never holds a token
(D82/D5). The runtime's status + JSON body propagate as-is, so a 400 (unparseable
SQL) or 403 (column-scope denial) reaches the browser's error branch unchanged
rather than being flattened into a generic failure.

The runtime is never started — `httpx.AsyncClient` is faked so no socket opens;
the point is the BFF plumbing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient

_SESSION_ID = "s" + "0" * 32
_JWT = "bff-held-jwt"
_ANSWER_SQL = "SELECT department_code, department_name FROM dbpcm_warehouse.department"
_PAGE = {
    "columns": ["department_code", "department_name"],
    "rows": [["D01", "Engineering"], ["D02", "Sales"]],
    "limit": 50,
    "offset": 0,
    "has_more": False,
}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None, *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    """Drop-in for `httpx.AsyncClient` recording the outbound POST instead of
    opening a socket."""

    captured: list[dict] = []
    response = _FakeResponse(200, _PAGE)

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeResponse:
        type(self).captured.append({"url": url, "headers": headers, "json": json})
        return type(self).response


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    _FakeAsyncClient.captured = []
    _FakeAsyncClient.response = _FakeResponse(200, _PAGE)
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    server._SESSIONS[_SESSION_ID] = _JWT
    yield _FakeAsyncClient.captured
    server._SESSIONS.pop(_SESSION_ID, None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


def test_query_page_proxy_attaches_jwt_server_side(
    fake_httpx: list[dict], client: TestClient
) -> None:
    resp = client.post(
        "/api/query/page",
        json={"session_id": _SESSION_ID, "sql": _ANSWER_SQL, "limit": 50, "offset": 0},
    )

    assert resp.status_code == 200
    assert resp.json() == _PAGE
    assert len(fake_httpx) == 1
    hop = fake_httpx[0]
    assert hop["url"].endswith("/query/page")
    # THE POINT: the browser sent no token — the BFF supplied it.
    assert hop["headers"]["Authorization"] == f"Bearer {_JWT}"
    assert hop["headers"]["X-Session-Id"] == _SESSION_ID
    assert hop["json"] == {"sql": _ANSWER_SQL, "limit": 50, "offset": 0}


def test_query_page_proxy_forwards_paging_params(
    fake_httpx: list[dict], client: TestClient
) -> None:
    client.post(
        "/api/query/page",
        json={"session_id": _SESSION_ID, "sql": _ANSWER_SQL, "limit": 25, "offset": 75},
    )
    assert fake_httpx[0]["json"]["limit"] == 25
    assert fake_httpx[0]["json"]["offset"] == 75


def test_query_page_proxy_omitted_paging_params_ride_through_as_null(
    fake_httpx: list[dict], client: TestClient
) -> None:
    """The runtime clamps `None` to its own defaults — the BFF does not invent
    values, so there is one place paging bounds are decided."""
    client.post("/api/query/page", json={"session_id": _SESSION_ID, "sql": _ANSWER_SQL})
    assert fake_httpx[0]["json"] == {"sql": _ANSWER_SQL, "limit": None, "offset": None}


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"error": "Only read-only SELECT queries can be paged."}),
        (403, {"error": "Access denied.", "error_code": "COLUMN_SCOPE_VIOLATION"}),
    ],
)
def test_query_page_proxy_propagates_a_rejection_unchanged(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, status: int, payload: dict
) -> None:
    """A denial must reach the browser as itself. Flattening it to a 502/500 would
    leave the user staring at an empty panel with no reason for it."""
    _FakeAsyncClient.captured = []
    _FakeAsyncClient.response = _FakeResponse(status, payload)
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    server._SESSIONS[_SESSION_ID] = _JWT
    try:
        resp = client.post(
            "/api/query/page", json={"session_id": _SESSION_ID, "sql": _ANSWER_SQL}
        )
        assert resp.status_code == status
        assert resp.json() == payload
    finally:
        server._SESSIONS.pop(_SESSION_ID, None)


def test_query_page_proxy_unknown_session_is_rejected_before_any_hop(
    fake_httpx: list[dict], client: TestClient
) -> None:
    """No server-side JWT for that session id → no outbound call at all. The
    browser cannot page a session the BFF is not holding a token for."""
    resp = client.post(
        "/api/query/page", json={"session_id": "s" + "9" * 32, "sql": _ANSWER_SQL}
    )
    assert resp.status_code >= 400
    assert fake_httpx == []
