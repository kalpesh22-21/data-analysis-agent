"""BFF `GET /api/history` proxy — the server-side JWT attach (UI Slice 3 §4).

Mirrors `test_inbox_proxy.py`: the BFF proxies the browser's `/api/history`
(query-param `session_id`) DATA call to the runtime's `GET /session/history`,
looking the JWT up server-side and attaching `Authorization: Bearer <jwt>` +
`X-Session-Id` on the hop so the browser never holds the token (D82/D5). The
runtime's status + JSON body propagate as-is (a non-2xx reaches the browser's
error branch unchanged). The runtime is never started — `httpx.AsyncClient` is
faked so no socket opens; the point is the BFF plumbing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import ui.server as server
from fastapi.testclient import TestClient

_SESSION_ID = "s" + "0" * 32
_JWT = "bff-held-jwt"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, *, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    """Drop-in for `httpx.AsyncClient` recording the outbound GET instead of
    opening a socket. `response` is the canned reply; `captured` collects the
    (url, headers) of each hop."""

    captured: list[dict] = []
    response = _FakeResponse(200, {"session_id": _SESSION_ID, "turns": [], "pending_question": None})

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, *, headers: dict) -> _FakeResponse:
        type(self).captured.append({"url": url, "headers": headers})
        return type(self).response


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    _FakeAsyncClient.captured = []
    _FakeAsyncClient.response = _FakeResponse(
        200, {"session_id": _SESSION_ID, "turns": [], "pending_question": None}
    )
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeAsyncClient)
    # Seed the server-side session→JWT map (the browser never sends the JWT).
    server._SESSIONS[_SESSION_ID] = _JWT
    yield _FakeAsyncClient.captured
    server._SESSIONS.pop(_SESSION_ID, None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


def test_history_proxy_attaches_jwt_server_side(fake_httpx: list[dict], client: TestClient) -> None:
    resp = client.get(f"/api/history?session_id={_SESSION_ID}")
    assert resp.status_code == 200
    assert resp.json() == {"session_id": _SESSION_ID, "turns": [], "pending_question": None}
    assert len(fake_httpx) == 1
    hop = fake_httpx[0]
    assert hop["url"].endswith("/session/history")
    # The JWT + session-id are attached server-side — the browser sent only the
    # session_id query param.
    assert hop["headers"]["Authorization"] == f"Bearer {_JWT}"
    assert hop["headers"]["X-Session-Id"] == _SESSION_ID


def test_history_proxy_passes_assumptions_field_through_unchanged(
    fake_httpx: list[dict], client: TestClient
) -> None:
    # recordAssumptions (docs/decisions/ui-assumptions-contract.md): the BFF is a
    # transparent JSON proxy — a turn's `assumptions` (list or null) rides through
    # the `/api/history` hop byte-for-byte, so the browser renders exactly what the
    # runtime projected. The BFF neither re-shapes nor scope-gates it (the runtime
    # already did, in `project_history`).
    body = {
        "session_id": _SESSION_ID,
        "turns": [
            {
                "turn_index": 0,
                "question": "how many active employees?",
                "answer": "42",
                "provenance_union": ["hr.employees.status"],
                "assumptions": ["'Active' was taken to mean currently-employed staff"],
                "tool_calls": [],
            },
            {
                "turn_index": 1,
                "question": "hi",
                "answer": "hello",
                "provenance_union": [],
                "assumptions": None,  # no assumptions -> null, passes through as null
                "tool_calls": [],
            },
        ],
        "pending_question": None,
    }
    _FakeAsyncClient.response = _FakeResponse(200, body)

    resp = client.get(f"/api/history?session_id={_SESSION_ID}")
    assert resp.status_code == 200
    out = resp.json()
    assert out["turns"][0]["assumptions"] == [
        "'Active' was taken to mean currently-employed staff"
    ]
    assert out["turns"][1]["assumptions"] is None


def test_history_proxy_propagates_non_2xx(fake_httpx: list[dict], client: TestClient) -> None:
    _FakeAsyncClient.response = _FakeResponse(401, {"detail": "bad token"})
    resp = client.get(f"/api/history?session_id={_SESSION_ID}")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "bad token"}


def test_history_proxy_unknown_session_404s_before_hop(
    fake_httpx: list[dict], client: TestClient
) -> None:
    resp = client.get("/api/history?session_id=sunknownsession")
    assert resp.status_code == 404
    assert fake_httpx == []  # no network hop for an unminted session


def test_history_proxy_non_json_body_wrapped_as_detail(
    fake_httpx: list[dict], client: TestClient
) -> None:
    _FakeAsyncClient.response = _FakeResponse(502, None, text="upstream boom")
    resp = client.get(f"/api/history?session_id={_SESSION_ID}")
    assert resp.status_code == 502
    assert resp.json() == {"detail": "upstream boom"}
