"""ui/server.py — thin FastAPI backend-for-frontend (BFF) for the minimal Phase-0 UI.

Responsibilities (and ONLY these — see docs/08-ui.md / D82):
    1. Serve the static page (`ui/static/index.html`) at `GET /`.
    2. `POST /api/session` — mint a fresh `session_id` (uuid4) and a JWT for it
       by calling the token service's guarded `POST /token` server-side, then
       hold the JWT in an in-memory dict keyed by `session_id`. The browser
       gets back ONLY `{"session_id": ...}` — it never sees the JWT (D82/D5:
       credentials are model-invisible AND browser-invisible in this design;
       the BFF is the only thing that ever holds the token).
    3. `POST /api/turn` / `POST /api/turn/resume` — look the JWT up by the
       `session_id` the browser sends, call the runtime's `/turn` /
       `/turn/resume` with `Authorization: Bearer <jwt>` + `X-Session-Id`, and
       stream the SSE response straight back to the browser byte-for-byte
       (no re-parsing/re-framing here — the browser's own SSE parser in
       `index.html` does that).

Config (env vars, all optional):
    RUNTIME_URL            default http://localhost:8000
    TOKEN_SERVICE_URL      default http://localhost:19000/token
    TOKEN_ISSUER_API_KEY   default issuer-key-abc123

Run:
    uv run uvicorn ui.server:app --host 0.0.0.0 --port 3000
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

RUNTIME_URL = os.environ.get("RUNTIME_URL", "http://localhost:8000")
TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

# Server-side-only session_id -> JWT map (D82/D5: the browser never receives
# this). In-memory is fine for this minimal, single-process dev UI — no
# durability/multi-replica requirement here.
_SESSIONS: dict[str, str] = {}

app = FastAPI(title="data-agent-ui-bff")


class TurnBody(BaseModel):
    session_id: str
    message: str


class ResumeBody(BaseModel):
    session_id: str
    answer: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_INDEX_HTML)


@app.post("/api/session")
async def create_session() -> dict[str, str]:
    session_id = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                TOKEN_SERVICE_URL,
                headers={"Authorization": f"Bearer {TOKEN_ISSUER_API_KEY}"},
                json={"user_name": "ui-user", "column_scope": []},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Token service unreachable: {exc}"
            ) from exc

    jwt = response.json()["access_token"]
    _SESSIONS[session_id] = jwt
    return {"session_id": session_id}


def _jwt_for_session(session_id: str) -> str:
    jwt = _SESSIONS.get(session_id)
    if jwt is None:
        raise HTTPException(status_code=404, detail="Unknown session_id — call POST /api/session first.")
    return jwt


async def _proxy_stream(path: str, session_id: str, json_body: dict[str, str]) -> StreamingResponse:
    """POST *json_body* to the runtime's *path* and forward the response —
    status code, content-type, and body bytes — straight back to the browser
    as it arrives.

    The runtime's happy path is always `text/event-stream` (`event: progress`
    / `event: result` / `event: error` frames, forwarded byte-for-byte, no
    re-parsing here); but a request the runtime rejects BEFORE ever opening
    the stream (e.g. `409` "no pending checkpoint" from a stale/duplicate
    `/turn/resume`, or a `401`/`400` from the auth/header checks) comes back
    as a plain JSON error body with a non-2xx status — that status and
    content-type are propagated as-is (not silently coerced to a `200`
    `text/event-stream`) so the browser's `!response.ok` branch renders it in
    the error banner instead of trying to SSE-parse it.
    """
    jwt = _jwt_for_session(session_id)
    headers = {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None))
    stream_ctx = client.stream("POST", f"{RUNTIME_URL}{path}", headers=headers, json=json_body)
    try:
        response = await stream_ctx.__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Runtime unreachable: {exc}") from exc

    async def _body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await stream_ctx.__aexit__(None, None, None)
            await client.aclose()

    content_type = response.headers.get("content-type", "text/event-stream")
    return StreamingResponse(_body(), status_code=response.status_code, media_type=content_type)


@app.post("/api/turn")
async def turn(body: TurnBody) -> StreamingResponse:
    return await _proxy_stream("/turn", body.session_id, {"message": body.message})


@app.post("/api/turn/resume")
async def turn_resume(body: ResumeBody) -> StreamingResponse:
    return await _proxy_stream("/turn/resume", body.session_id, {"answer": body.answer})
