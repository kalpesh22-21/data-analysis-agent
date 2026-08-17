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

import json
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from ui.entitlements import resolve_caller_identity, resolve_column_scope

from data_agent.learning.promotion.token_minter import (
    HttpTokenMinter,
    TenantClaims,
    TokenMintError,
)

RUNTIME_URL = os.environ.get("RUNTIME_URL", "http://localhost:8000")
TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

# Warehouse TENANT claims stamped into every minted token.
#
# The MCP's auth middleware REQUIRES these — `app/config.py`'s
# CLICKHOUSE_TENANT_SETTINGS maps clientcode/proc_center/jti onto the
# `paycom_client_code` / `paycom_proc_center` / `paycom_authenticated_user`
# ClickHouse settings that the row policies read (`getSetting(...)`). Without them
# every request is rejected `403 MISSING_TENANT_CLAIM` before any tool runs, so the
# whole UI is dead. `app/token_service.py::_mint` stamps only
# sub/iss/aud/exp/user_name/column_scope/sid_hash itself, which is why the BFF has
# to supply them.
#
# ENTRA SEAM: under a real IdP these come from the CALLER'S OWN identity token —
# they are the tenant the user actually belongs to, and minting them from
# process-level config would let one deployment issue tokens for another tenant.
# They are env-configurable here (rather than hardcoded) so a deployment can point
# at its own tenant, but resolving them PER IDENTITY — beside
# `ui/entitlements.py::resolve_column_scope`, which already does exactly that for
# column scope — is the real fix. Defaults match the seeded local warehouse
# (docker/clickhouse-init/hr-4tables-snake-migration.sql).
TENANT_CLIENT_CODE = os.environ.get("TENANT_CLIENT_CODE", "CLIENT_A")
TENANT_PROC_CENTER = os.environ.get("TENANT_PROC_CENTER", "PC01")
TENANT_JTI = os.environ.get("TENANT_JTI", "TESTJTI001")

# Upload BFF wiring (UI Slice 4, §4). The scratch upload routes live on the MCP /
# clickhouse-api host, NOT the runtime — so they get their own base env, MCP_URL
# (matching the runtime's own default, runtime/config.py:35). The `/scratch/v1`
# base is DERIVED from it by swapping the path (see `_scratch_base`), the same
# derivation the runtime does at runtime/config.py:362-374.
MCP_URL = os.environ.get("MCP_URL", "http://localhost:18090/mcp")

# Cheap front-door reject: short-circuit an obviously-oversized upload on its
# declared `Content-Length` BEFORE buffering the body. The authoritative byte cap
# lives downstream in clickhouse-api (§3); this is only a cost optimization, so it
# is deliberately generous and header-only (a client can lie about Content-Length,
# but then the downstream cap still rejects it after the buffered read).
UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(8 * 1024 * 1024)))

# Review-inbox BFF wiring (UI Slice 2, §3). The BFF serves the inbox PAGE and proxies
# the browser's `/api/inbox/*` DATA calls to the dedicated inbox service, holding the
# shared REVIEWER_TOKEN server-side (exactly like TOKEN_ISSUER_API_KEY above) and
# attaching it on the proxy hop so the browser never sees it. The whole surface is
# gated OFF unless REVIEW_INBOX_ENABLED=1 (mirrors UI_TEST_AFFORDANCES).
INBOX_SERVICE_URL = os.environ.get("INBOX_SERVICE_URL", "http://localhost:8100")
REVIEWER_TOKEN = os.environ.get("REVIEWER_TOKEN", "")
# `complete` is the fail-to-review action (the reviewer fills in the missing
# parameterization entries); it is the ONLY one that carries a request body.
_INBOX_ACTIONS = frozenset({"approve", "reject", "retract", "complete"})
# The only `?status=` values the list surface accepts (ui-inbox-type-archive contract
# §List API): the live review queue, the durable rejected archive, and the fail-to-review
# work list. Anything else is rejected at the BFF (400, not proxied); the inbox service
# validates it again.
_INBOX_LIST_STATUSES = frozenset({"in_review", "rejected", "needs_parameterization"})
# Cap on the ONE inbox body the BFF forwards (the `complete` action's parameterization
# entries). Small on purpose and separate from `UPLOAD_MAX_BYTES`: this is a form a human
# types, and the largest legitimate one is a few dozen JSON objects. The reviewer is
# authenticated and the surface is internal, so this is not a defence against an
# adversary — it is the same rule the upload route already follows, that no request may
# make the BFF buffer an unbounded amount of memory on a caller's say-so.
INBOX_BODY_MAX_BYTES = int(os.environ.get("INBOX_BODY_MAX_BYTES", str(256 * 1024)))

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_INBOX_HTML = _STATIC_DIR / "inbox.html"

# Server-side-only session_id -> JWT map (D82/D5: the browser never receives
# this). In-memory is fine for this minimal, single-process dev UI — no
# durability/multi-replica requirement here.
_SESSIONS: dict[str, str] = {}

# Server-side-only session_id -> current column_scope (`[]` == allow-all, D80b).
# Tracked so the test-only `/api/session/scope` affordance can enforce that it
# narrows monotonically (never re-widens) relative to the session's ENTITLED base
# — see `set_session_scope`. For a restricted user this base is their entitled
# allowlist (not allow-all), so narrowing works within it and widening beyond it
# is rejected by the subset check.
_SESSION_SCOPES: dict[str, list[str]] = {}

# Server-side-only session_id -> resolved caller identity (auth-hardening Slice 2).
# Tracked so the monotonic-narrow re-mint preserves the SAME identity/user_name
# (and thus the same per-user entitlement basis + sid_hash session binding).
_SESSION_USERS: dict[str, str] = {}

app = FastAPI(title="data-agent-ui-bff")


class TurnBody(BaseModel):
    session_id: str
    message: str


class ResumeBody(BaseModel):
    session_id: str
    answer: str


class QueryPageBody(BaseModel):
    """`POST /api/query/page` — one page of a turn's designated answer table.

    `sql` is the `answer_sql` the runtime returned on that turn's `result` event,
    echoed back by the browser. It is NOT trusted on the strength of that echo: the
    runtime re-parses it and runs it through the SAME scope-enforced dispatch path
    the agent uses, under the JWT this BFF attaches server-side. A browser that
    tampers with it can therefore only ask for something the same session's own
    scope already permits.
    """

    session_id: str
    sql: str
    limit: int | None = None
    offset: int | None = None


class ScopeBody(BaseModel):
    session_id: str
    column_scope: list[str]


async def _mint_jwt(user_name: str, column_scope: list[str], session_id: str) -> str:
    """Call the token service server-side to mint a JWT carrying *column_scope*
    and bound to *session_id* (D82/D5: the BFF is the ONLY holder of the token;
    the browser never sees it). `column_scope=[]` == allow-all, matching the
    runtime's D80(b)/D44 scope semantics.

    *user_name* is the resolved caller identity (auth-hardening Slice 2): it is
    stamped into the token so the warehouse's row-level tenant isolation
    (`SQL_tenant`/`user_name`, D82) attributes the session to the right user, and
    it is the identity whose per-user `column_scope` entitlement produced
    *column_scope* (see `ui/entitlements.py`).

    *session_id* is threaded into the mint request so the token carries a
    `sid_hash` claim (auth-hardening Slice 1): the MCP then rejects any request
    whose `X-Session-Id` header does not hash to that claim, closing the
    session-hijack gap. Every JWT this BFF mints is for exactly one `session_id`
    and is sent with the matching `X-Session-Id` header, so the binding always
    holds for BFF-minted traffic.

    The transport is `HttpTokenMinter` — the SAME mint client the offline promotion
    plane uses — so the `POST /token` body (tenant claims, scope, sid binding) has
    ONE implementation rather than a request-path copy that drifts from it. Two of
    its knobs are set for THIS plane: `ttl_seconds=None` (the IdP's own configured
    lifetime governs a UI session, not the probe's deliberately short 300s) and
    `allow_unscoped=True` (an entitlement of `[]` is a RESOLVED D80b allow-all here,
    not the absent scope the offline backstop refuses). The minter is built per call
    because `user_name` is the per-request caller identity; it does no I/O to build."""
    # Required by the MCP — see TENANT_* above. Omitting them is a 403
    # MISSING_TENANT_CLAIM on every tool call. `TenantClaims` refuses a blank or
    # control-character value HERE rather than letting it reach the wire, where its
    # only symptom is that same 403 on every question. Reported as a 500 naming the
    # env var (NOT the 502 below, which means "the token service is unreachable"):
    # this is deployment misconfiguration, and a blank TENANT_* is the shipped Helm
    # default, so an operator who never set them must be told which knob is missing.
    try:
        tenant = TenantClaims(
            clientcode=TENANT_CLIENT_CODE,
            proc_center=TENANT_PROC_CENTER,
            jti=TENANT_JTI,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail=f"Tenant claims misconfigured: {exc}"
        ) from exc
    minter = HttpTokenMinter(
        TOKEN_SERVICE_URL,
        TOKEN_ISSUER_API_KEY,
        tenant=tenant,
        user_name=user_name,
        ttl_seconds=None,
        timeout=10.0,
        allow_unscoped=True,
    )
    try:
        return await minter.mint(list(column_scope), session_id=session_id)
    except TokenMintError as exc:
        raise HTTPException(
            status_code=502, detail=f"Token service unreachable: {exc}"
        ) from exc


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_INDEX_HTML)


@app.post("/api/session")
async def create_session(request: Request) -> dict[str, str]:
    """Mint a fresh `session_id` and a JWT scoped to the CALLER'S per-user
    entitlement (auth-hardening Slice 2). The identity and its `column_scope` are
    resolved exclusively through the `ui/entitlements.py` seams — the demo
    `ui-user` resolves to allow-all (`[]`, D80b), a restricted user to their
    entitled allowlist. This replaces D82's hardcoded all-access mint: allow-all
    is now the *default entitlement*, not a blanket, so a real per-user scope is
    honored end-to-end (the MCP enforces it, D57/D80). The token is still bound to
    `session_id` (Slice 1 sid_hash) and never leaves the BFF (D82/D5).

    SID FORMAT (table-intermediate Slice 2): the session_id is minted
    UNDERSCORE-FREE and identifier-safe — ``s`` + a hyphen-stripped uuid4 hex
    (``s<32hex>``, matching ``^[A-Za-z_][A-Za-z0-9_]*$`` with NO ``_``). This is
    load-bearing for the D93 scratch namespace: scratch tables are named
    ``s_<session_id>_bp_<hex>`` and the D64 read gate extracts the owning session
    as the run after ``s_`` up to the next ``_``. A raw uuid4 (hyphens) is not a
    safe SQL identifier and would be rejected at materialize; an underscore in the
    sid would reintroduce the ``_``-boundary ambiguity the read gate now forbids.
    Stripping the hyphens to hex (NOT converting them to ``_``) keeps the sid
    underscore-free."""
    session_id = "s" + uuid.uuid4().hex
    identity = resolve_caller_identity(request)
    column_scope = resolve_column_scope(identity)
    _SESSIONS[session_id] = await _mint_jwt(identity, column_scope, session_id)
    _SESSION_SCOPES[session_id] = list(column_scope)
    _SESSION_USERS[session_id] = identity
    return {"session_id": session_id}


@app.post("/api/session/scope")
async def set_session_scope(body: ScopeBody) -> dict[str, bool]:
    """Test-only (D-L3-4): re-mint the session's JWT server-side with a NARROWER
    `column_scope`, replacing the one held for `session_id`. Active ONLY when
    `UI_TEST_AFFORDANCES=1` — otherwise 404, so the production BFF never exposes
    it. D82/D5 stay intact: the BFF is still the sole JWT holder and the browser
    still never receives the token; this endpoint only lets the Layer-3 harness
    drive a mid-session scope change (D44) that a real product would drive from
    its identity provider. The scope-narrowing itself is enforced server-side by
    the runtime's `ContextAssembler`/`scope_filter` fail-closed replay — this
    just supplies the narrower token.

    MONOTONIC-NARROWING (S1, security): the affordance may only NARROW scope,
    never widen it — otherwise a caller could POST `[]` (== allow-all, D80b) to
    re-widen a narrowed session, turning this into an escalation surface. With
    Item-9 per-user scoped tokens (Slice 2), the session's base is the caller's
    ENTITLED scope (which may already be a non-allow-all allowlist), so narrowing
    happens WITHIN that base: `[]` is refused outright, and the new scope must be a
    subset of the current scope (transitively a subset of the entitled base) —
    widening beyond the entitled base is therefore rejected."""
    if os.environ.get("UI_TEST_AFFORDANCES") != "1":
        raise HTTPException(status_code=404, detail="Not found.")
    if body.session_id not in _SESSIONS:
        raise HTTPException(
            status_code=404, detail="Unknown session_id — call POST /api/session first."
        )
    if body.column_scope == []:
        raise HTTPException(
            status_code=400, detail="Test affordance narrows only; [] (allow-all) refused."
        )
    # Hard lookup (fail-closed): the session-existence check above guarantees the
    # happy path, so a miss here means an inconsistent server state (e.g. a future
    # _SESSIONS writer like restart-restore that forgot to seed _SESSION_SCOPES).
    # A `.get(..., [])` default would silently treat that as allow-all and REOPEN
    # the widen surface — the wrong failure direction for a security check — so we
    # crash (KeyError, matching _SESSION_USERS[...] below) instead of failing open.
    current = _SESSION_SCOPES[body.session_id]
    # A non-empty current scope is an allowlist; the new scope must be ⊆ it. A
    # current `[]` (allow-all) admits any non-empty narrowing (already checked).
    if current and not set(body.column_scope).issubset(set(current)):
        raise HTTPException(
            status_code=400,
            detail="Test affordance narrows only; new scope must be a subset of the current scope.",
        )
    # Re-mint with the SAME session_id AND the SAME identity/user_name so both the
    # sid_hash session binding (Slice 1, invariant §6.5) and the per-user identity
    # (Slice 2) stay valid across the scope narrow: only column_scope changes.
    user_name = _SESSION_USERS[body.session_id]
    _SESSIONS[body.session_id] = await _mint_jwt(user_name, body.column_scope, body.session_id)
    _SESSION_SCOPES[body.session_id] = list(body.column_scope)
    return {"ok": True}


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


@app.get("/api/history")
async def history(session_id: str) -> JSONResponse:
    """UI Slice 3 (§4): JSON (non-streaming) proxy for the runtime's
    `GET /session/history`. Mirrors `_proxy_inbox`'s server-side-token attach —
    the browser sends only `session_id` (a query param), the JWT is looked up and
    attached server-side (D82/D5: the browser never sees the token). The runtime's
    status + JSON body propagate as-is so a non-2xx (401/400) reaches the browser's
    error branch unchanged."""
    jwt = _jwt_for_session(session_id)
    headers = {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{RUNTIME_URL}/session/history", headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Runtime unreachable: {exc}") from exc
    try:
        content = r.json()
    except ValueError:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


@app.post("/api/query/page")
async def query_page(body: QueryPageBody) -> JSONResponse:
    """JSON proxy for the runtime's `POST /query/page` — the paging behind the
    answer table that replaced the old `result_table` preview.

    Same server-side-token attach as `/api/history` (D82/D5: the browser never
    sees the JWT, it sends only `session_id`). The runtime's status + JSON body
    propagate as-is, so a 400 (unparseable/non-SELECT SQL) or 403 (column-scope
    denial) reaches the browser's error branch unchanged rather than being
    flattened into a generic failure."""
    jwt = _jwt_for_session(body.session_id)
    headers = {"Authorization": f"Bearer {jwt}", "X-Session-Id": body.session_id}
    payload = {"sql": body.sql, "limit": body.limit, "offset": body.offset}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(f"{RUNTIME_URL}/query/page", headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Runtime unreachable: {exc}") from exc
    try:
        content = r.json()
    except ValueError:
        content = {"detail": r.text}
    return JSONResponse(status_code=r.status_code, content=content)


# --- review-inbox BFF (UI Slice 2, §3) ---------------------------------------


def _require_inbox_enabled() -> None:
    """Gate the inbox page + proxy behind `REVIEW_INBOX_ENABLED` (mirrors the
    `UI_TEST_AFFORDANCES` 404 pattern above). Unset/≠"1" ⇒ the surface does not
    exist — every inbox route 404s (defense in depth: the inbox service enforces the
    same flag on its own routes)."""
    if os.environ.get("REVIEW_INBOX_ENABLED") != "1":
        raise HTTPException(status_code=404, detail="Not found.")


async def _read_bounded_body(request: Request, cap: int) -> bytes | None:
    """The request body, or `None` when it exceeds *cap*.

    Streamed rather than `await request.body()`, for the reason the upload route already
    documents: `body()` buffers an unbounded amount for a chunked (no-`Content-Length`)
    request, so the cap has to be enforced WHILE reading, not after. The header check
    first is only a cheap early exit for the honest client."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _proxy_inbox(
    method: str, path: str, json_body: Any | None = None
) -> JSONResponse:
    """Proxy a JSON (non-streaming) inbox request to the inbox service, attaching the
    server-held `X-Reviewer-Token` on the hop (the browser never sees it — same
    pattern as the TOKEN_ISSUER_API_KEY the mint uses). The service's status code +
    JSON body are propagated as-is so a `4xx`/`5xx` (unknown id, illegal transition,
    landing-plane `503`) reaches the browser's error branch unchanged. A JSON,
    non-streaming sibling of `_proxy_stream`.

    *json_body* is forwarded VERBATIM when present (the fail-to-review `complete`
    action's parameterization entries). The BFF does not inspect or reshape it: the
    inbox service validates it, and behind it the extractor's own readers do — a second
    schema here would be a second vocabulary for the same mistake, and the one that
    names the fix is the one furthest down."""
    headers = {"X-Reviewer-Token": REVIEWER_TOKEN}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.request(
                method, f"{INBOX_SERVICE_URL}{path}", headers=headers, json=json_body
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Inbox service unreachable: {exc}"
            ) from exc
    # The service always answers JSON (success bodies AND HTTPException details), but a
    # crash / misconfigured upstream could return HTML/plain text — don't 500 the proxy
    # decoding it; forward the raw text as a `detail` so the browser's error branch
    # still renders something.
    try:
        content = response.json()
    except ValueError:
        content = {"detail": response.text}
    return JSONResponse(status_code=response.status_code, content=content)


@app.get("/inbox")
async def inbox_page() -> FileResponse:
    """Serve the reviewer PAGE (mirrors `index()`). The browser's DATA calls go to
    `/api/inbox/*` below — this route is only the HTML shell."""
    _require_inbox_enabled()
    return FileResponse(_INBOX_HTML)


@app.get("/api/inbox")
async def inbox_list(status: str | None = None) -> JSONResponse:
    """Proxy the inbox list, optionally filtered by `?status=`. When absent, proxy
    exactly as before (review queue, no param). When present, validate against
    `{in_review, rejected}` (400 on anything else — do NOT proxy) and forward it as an
    upstream query param (ui-inbox-type-archive contract §List API)."""
    _require_inbox_enabled()
    if status is None:
        return await _proxy_inbox("GET", "/inbox")
    if status not in _INBOX_LIST_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "status must be one of {'in_review', 'rejected', "
                "'needs_parameterization'}."
            ),
        )
    query = urllib.parse.urlencode({"status": status})
    return await _proxy_inbox("GET", f"/inbox?{query}")


@app.get("/api/inbox/health")
async def inbox_health() -> JSONResponse:
    _require_inbox_enabled()
    return await _proxy_inbox("GET", "/inbox/health")


@app.post("/api/inbox/{candidate_id}/{action}")
async def inbox_action(
    candidate_id: str, action: str, request: Request
) -> JSONResponse:
    _require_inbox_enabled()
    if action not in _INBOX_ACTIONS:
        raise HTTPException(status_code=404, detail="Not found.")
    # Only `complete` carries a body. Read it here rather than typing it: the shape is
    # the inbox service's contract (and, under it, the extractor's readers), and a model
    # here would reject reviewer input with a message that names no fix. What IS enforced
    # here is the size, because that is the BFF's own resource and nobody downstream can
    # give it back.
    body: Any | None = None
    if action == "complete":
        raw = await _read_bounded_body(request, INBOX_BODY_MAX_BYTES)
        if raw is None:
            raise HTTPException(
                status_code=413, detail="Completion body exceeds the maximum allowed size."
            )
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            raise HTTPException(
                status_code=400, detail="complete requires a JSON body."
            ) from None
    # Percent-encode the decoded id before re-interpolating it into the upstream path
    # (candidate ids carry `::` and could carry other reserved chars) so it is passed as
    # a single, unambiguous path segment — never able to inject extra path structure.
    safe_id = urllib.parse.quote(candidate_id, safe="")
    return await _proxy_inbox("POST", f"/inbox/{safe_id}/{action}", body)


# --- upload BFF (UI Slice 4, §4) ---------------------------------------------


def _scratch_base() -> str:
    """Derive the `/scratch/v1` base URL from `MCP_URL` by keeping its scheme +
    netloc and swapping the path — the identical derivation the runtime does at
    runtime/config.py:362-374. The scratch upload routes are custom HTTP routes on
    the MCP host (`clickhouse-api`), NOT on `RUNTIME_URL`, so this is where the
    upload proxy hops to. E.g. `http://localhost:18090/mcp` -> `http://localhost:18090/scratch/v1`."""
    parts = urllib.parse.urlsplit(MCP_URL)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/scratch/v1", "", ""))


async def _proxy_upload(path: str, session_id: str, request: Request) -> JSONResponse:
    """RAW-BODY multipart passthrough to a clickhouse-api scratch route (§4).

    Unlike `_proxy_stream` (JSON in / SSE out) and `_proxy_inbox` (JSON in / JSON
    out), this proxies a `multipart/form-data` upload — but WITHOUT parsing it. The
    BFF must not parse the form (that would pull `python-multipart` into this
    package's deps, which the contract forbids), so it reads the raw body bytes and
    forwards them verbatim with the browser's ORIGINAL `Content-Type` header (which
    carries the multipart boundary the downstream parser needs). The `session_id`
    arrives as a query param, so the BFF needs zero form parsing to know it.

    The session's JWT + `X-Session-Id` are looked up and attached server-side — the
    SAME credential pair `_proxy_stream` attaches (D82/D5: the browser holds neither
    the token nor the session binding, only the opaque `session_id`). The upstream
    status + JSON body propagate as-is (non-JSON -> `{"detail": text}`), so a
    413/400/401 reaches the browser's error branch unchanged.
    """
    jwt = _jwt_for_session(session_id)
    # The BFF caps the WHOLE multipart body, but the downstream cap (UPLOAD_MAX_BYTES)
    # is on the FILE PART only — so give the BFF slack for the multipart envelope
    # (headers + boundaries, a few hundred bytes) to avoid 413-ing a file downstream
    # would accept. 64 KiB is generous headroom for the envelope.
    body_cap = UPLOAD_MAX_BYTES + 65536
    # Cheap header-only reject before reading the body (the downstream byte cap is
    # authoritative — this only avoids ingesting an obviously-oversized upload). A
    # missing/unparseable Content-Length just falls through to the bounded stream read.
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > body_cap:
        return JSONResponse(
            status_code=413,
            content={"error": "Upload exceeds the maximum allowed size.", "code": "UPLOAD_TOO_LARGE"},
        )
    # Bounded stream read: `request.body()` would buffer an unbounded amount for a
    # chunked (no-Content-Length) body, so read incrementally and bail the moment we
    # cross the cap — capping BFF memory even when the client omits Content-Length.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > body_cap:
            return JSONResponse(
                status_code=413,
                content={"error": "Upload exceeds the maximum allowed size.", "code": "UPLOAD_TOO_LARGE"},
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    headers = {
        "Authorization": f"Bearer {jwt}",
        "X-Session-Id": session_id,
        "Content-Type": request.headers.get("content-type", "application/octet-stream"),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(f"{_scratch_base()}{path}", headers=headers, content=body)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"clickhouse-api unreachable: {exc}"
            ) from exc
    try:
        content = resp.json()
    except ValueError:
        content = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=content)


@app.post("/api/upload/analyze")
async def upload_analyze(session_id: str, request: Request) -> JSONResponse:
    """Preview a CSV/XLSX upload: raw-body proxy to `POST /scratch/v1/analyze`
    (parse + column/type/sample preview, no materialize). `session_id` is a query
    param; the file rides in the raw multipart body."""
    return await _proxy_upload("/analyze", session_id, request)


@app.post("/api/upload")
async def upload(session_id: str, request: Request) -> JSONResponse:
    """Materialize a mapped upload: raw-body proxy to `POST /scratch/v1/upload`
    (parse + rename + materialize the session-scoped scratch table). `session_id`
    is a query param; the file + `mapping` field ride in the raw multipart body."""
    return await _proxy_upload("/upload", session_id, request)
