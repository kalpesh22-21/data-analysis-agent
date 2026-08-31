"""ui/server.py — thin FastAPI backend-for-frontend (BFF) for the minimal Phase-0 UI.

Serves the static page, mints a `session_id` + a JWT for it by calling the token
service server-side, and proxies turns / history / inbox / uploads to the runtime. The
browser gets back ONLY `{"session_id": ...}` — it never sees the JWT (D82/D5: the BFF
is the sole holder of the token). SSE bodies are forwarded byte-for-byte, never
re-parsed or re-framed here.

Config (env vars, all optional):
    RUNTIME_URL                   default http://localhost:8000
    TOKEN_SERVICE_URL             default http://localhost:19000/token
    TOKEN_ISSUER_API_KEY          default issuer-key-abc123
    TOKEN_TTL_SECONDS             default 3600 — MUST match the token service's own
                                  `token_ttl_seconds`. See `_TOKEN_TTL_SECONDS`.
    TOKEN_REFRESH_AFTER_SECONDS   default TTL-300 — token age at which the next
                                  proxied call lazily re-mints. See `_jwt_for_session`.

Run:
    uv run uvicorn ui.server:app --host 0.0.0.0 --port 3000
"""

from __future__ import annotations

import json
import logging
import os
import time
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

logger = logging.getLogger(__name__)

RUNTIME_URL = os.environ.get("RUNTIME_URL", "http://localhost:8000")
TOKEN_SERVICE_URL = os.environ.get("TOKEN_SERVICE_URL", "http://localhost:19000/token")
TOKEN_ISSUER_API_KEY = os.environ.get("TOKEN_ISSUER_API_KEY", "issuer-key-abc123")

# --- session-token lifetime (E1) ---------------------------------------------
#
# THE BFF CANNOT ASK THE TOKEN SERVICE HOW LONG ITS TOKENS LIVE. The mint request
# deliberately omits `ttl_seconds` (see `_mint_jwt`: the IdP's own configured
# lifetime governs a UI session), and the mint RESPONSE carries only
# `access_token` — no `expires_in`. The BFF also never decodes the JWT: it holds
# the token as an opaque credential and has no verifying key, and parsing `exp`
# out of an unverified payload to make a security-adjacent decision is the wrong
# habit to start. So the lifetime is CONFIGURED here and must be kept equal to the
# token service's `token_ttl_seconds` (`clickhouse-api/app/token_service.py`,
# default 3600). A value LARGER than the service's is the dangerous direction — it
# makes the BFF believe a dead token is still alive — which is why the refresh
# threshold below leaves a wide margin rather than a tight one.
_TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "3600"))
# Re-mint once a held token is this old. Default TTL-300: five minutes of slack
# for a slow mint, a clock skew (`verify_jwt` allows 60s leeway), and a long
# streaming turn that started just under the threshold and is still running when
# the old token would have died.
_TOKEN_REFRESH_AFTER_SECONDS = int(
    os.environ.get("TOKEN_REFRESH_AFTER_SECONDS", str(max(_TOKEN_TTL_SECONDS - 300, 1)))
)
# Both knobs are env-settable and INDEPENDENT, so a deployment can raise the TTL
# and forget the threshold (or vice versa) and get a band of zero or negative
# width. WARN, DO NOT RAISE: a misconfigured band still serves every request in
# the ordinary case — the loss is only the fallback — and refusing to start would
# turn a degraded UI into no UI. See `_jwt_for_session` for the bands themselves.
if not (0 < _TOKEN_REFRESH_AFTER_SECONDS < _TOKEN_TTL_SECONDS):
    logger.warning(
        "session-token refresh threshold is not strictly inside the TTL: "
        "TOKEN_REFRESH_AFTER_SECONDS=%d TOKEN_TTL_SECONDS=%d — the refresh "
        "shock-absorber band is gone, so a token-service blip in the wrong window "
        "becomes a user-facing 502 instead of a survivable retry on the still-valid "
        "held token",
        _TOKEN_REFRESH_AFTER_SECONDS,
        _TOKEN_TTL_SECONDS,
    )

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
# `verify` + `promote` are the Phase-3 promotion hop over an auto-landed VALIDATED
# learning node: a human vouches for it (`verified=true` on the staging node), then asks
# for the canon YAML to open a manual PR with (the service never touches git).
_INBOX_ACTIONS = frozenset(
    {
        "approve", "reject", "retract", "complete", "revise",
        "apply_revision", "attest_scan", "trial_run", "verify", "promote",
    }
)
# The actions that carry a request body: `complete` (the reviewer's missing
# parameterization entries) and `promote` (OPTIONAL `{doc_id, title}` knowledge
# refinements — a bodyless promote is the normal case). Every other action is a bare POST
# and must stay one; see `inbox_action`.
_INBOX_BODY_ACTIONS = frozenset(
    # `approve` carries a body now: the reviewer's warehouse token for the golden replay. The
    # inbox mints nothing for that query, so without the body the approve is refused upstream.
    {
        "approve",
        "complete",
        "promote",
        "revise",
        "apply_revision",
        "attest_scan",
        "trial_run",
    }
)
# The only `?status=` values the list surface accepts (ui-inbox-type-archive contract
# §List API): the live review queue, the durable rejected archive, the promotable set of
# auto-landed learning nodes awaiting verify/promote, the fail-to-review work list, and
# the terminal `promoted` set — listable because `promote` re-emits from it idempotently,
# so the YAML behind a lost PR stays recoverable by someone who can find the row.
# Anything else is rejected at the BFF (400, not proxied); the inbox service validates it
# again (`_LISTABLE_STATUSES`).
_INBOX_LIST_STATUSES = frozenset(
    {"in_review", "rejected", "validated", "needs_parameterization", "promoted"}
)
# Cap on the inbox bodies the BFF forwards (the `complete` action's parameterization
# entries, the `promote` action's optional refinements). Small on purpose and separate
# from `UPLOAD_MAX_BYTES`: these are forms a human types, and the largest legitimate one
# is a few dozen JSON objects. The reviewer is
# authenticated and the surface is internal, so this is not a defence against an
# adversary — it is the same rule the upload route already follows, that no request may
# make the BFF buffer an unbounded amount of memory on a caller's say-so.
INBOX_BODY_MAX_BYTES = int(os.environ.get("INBOX_BODY_MAX_BYTES", str(256 * 1024)))

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_INBOX_HTML = _STATIC_DIR / "inbox.html"
_MINT_HTML = _STATIC_DIR / "mint.html"

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

# Server-side-only session_id -> the wall-clock time its CURRENT token was minted
# (E1). Written by `_store_token` and nowhere else, so `_SESSIONS` can never hold a
# token whose age is unknown.
#
# WALL CLOCK, not `time.monotonic()`. The quantity being approximated is "how much
# of the IdP's TTL is left", and that TTL is a wall-clock `exp` claim the MCP checks
# against ITS wall clock. `monotonic()` would be the right choice for measuring an
# interval in isolation, but on Linux it does not advance across a system suspend —
# so a suspended-and-resumed process would under-estimate the age of every held
# token and serve dead ones. An NTP step is bounded and small next to the 300s of
# slack the refresh threshold leaves.
_SESSION_TOKEN_MINTED_AT: dict[str, float] = {}

app = FastAPI(title="data-agent-ui-bff")


class TurnBody(BaseModel):
    session_id: str
    message: str


class ResumeBody(BaseModel):
    session_id: str
    answer: str


class QueryPageBody(BaseModel):
    """`POST /api/query/page` — one page of a turn's designated answer table.

    `sql` is the `answer_sql` the runtime returned on that turn's `result` event, echoed
    back by the browser. It is NOT trusted on the strength of that echo: the runtime
    re-parses it and runs it through the SAME scope-enforced dispatch path the agent
    uses, under the JWT this BFF attaches server-side — so a tampered `sql` can only
    reach what the same session's own scope already permits.
    """

    session_id: str
    sql: str
    limit: int | None = None
    offset: int | None = None


class ScopeBody(BaseModel):
    session_id: str
    column_scope: list[str]


async def _mint_jwt(user_name: str, column_scope: list[str], session_id: str) -> str:
    """Call the token service server-side to mint a JWT carrying *column_scope* and
    bound to *session_id* (D82/D5: the BFF is the ONLY holder of the token; the browser
    never sees it). `column_scope=[]` == allow-all, matching the runtime's D80(b)/D44
    scope semantics.

    *user_name* is the resolved caller identity (`ui/entitlements.py`), stamped into the
    token so the warehouse's row-level tenant isolation (`SQL_tenant`/`user_name`, D82)
    attributes the session to the right user.

    ALL THREE CALLERS PASS A CACHED SCOPE, and only `create_session` resolves one;
    re-resolving on refresh would be a defect, not a freshening — see
    `_refresh_session_token`.

    *session_id* is threaded into the mint so the token carries a `sid_hash` claim: the
    MCP rejects any request whose `X-Session-Id` header does not hash to that claim,
    closing the session-hijack gap.

    The transport is `HttpTokenMinter`, shared with the offline promotion plane, with two
    knobs set for THIS plane: `ttl_seconds=None` (the IdP's own configured lifetime
    governs a UI session) and `allow_unscoped=True` (an entitlement of `[]` is a RESOLVED
    D80b allow-all here, not the absent scope the offline backstop refuses). Built per
    call because `user_name` is per-request; it does no I/O to build."""
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
    """Mint a fresh `session_id` and a JWT scoped to the CALLER'S per-user entitlement.
    The identity and its `column_scope` are resolved exclusively through the
    `ui/entitlements.py` seams (the demo `ui-user` resolves to allow-all `[]`, D80b).
    The token is bound to `session_id` (sid_hash) and never leaves the BFF (D82/D5).

    SID FORMAT is load-bearing: ``s`` + a hyphen-stripped uuid4 hex, so the sid is
    identifier-safe and contains NO underscore. D93 scratch tables are named
    ``s_<session_id>_bp_<hex>`` and the D64 read gate extracts the owning session as the
    run after ``s_`` up to the next ``_`` — a raw uuid4 is not a safe SQL identifier, and
    an underscore in the sid would reintroduce the boundary ambiguity the gate forbids."""
    session_id = "s" + uuid.uuid4().hex
    identity = resolve_caller_identity(request)
    # THE ONLY `resolve_column_scope` CALL ON THE REQUEST PATH, and it must stay
    # that way — E1's token refresh replays the value cached below rather than
    # calling this again. See `_refresh_session_token`.
    column_scope = resolve_column_scope(identity)
    _store_token(session_id, await _mint_jwt(identity, column_scope, session_id))
    _SESSION_SCOPES[session_id] = list(column_scope)
    _SESSION_USERS[session_id] = identity
    return {"session_id": session_id}


@app.post("/api/session/scope")
async def set_session_scope(body: ScopeBody) -> dict[str, bool]:
    """Test-only (D-L3-4): re-mint the session's JWT server-side with a NARROWER
    `column_scope`, replacing the one held for `session_id`. Active ONLY when
    `UI_TEST_AFFORDANCES=1` — otherwise 404, so the production BFF never exposes it.
    D82/D5 stay intact: the browser still never receives the token. The narrowing itself
    is enforced by the runtime's fail-closed replay; this only supplies the token.

    MONOTONIC NARROWING (S1, security): this may only NARROW, never widen. `[]` (==
    allow-all, D80b) is refused outright and the new scope must be a subset of the
    current one — transitively a subset of the caller's entitled base — so the affordance
    cannot become an escalation surface."""
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
    _store_token(
        body.session_id, await _mint_jwt(user_name, body.column_scope, body.session_id)
    )
    _SESSION_SCOPES[body.session_id] = list(body.column_scope)
    return {"ok": True}


def _store_token(session_id: str, jwt: str) -> None:
    """THE ONLY writer of `_SESSIONS`, so a stored token always has a known age.

    Every mint site goes through here. Writing `_SESSIONS[sid]` directly leaves
    `_SESSION_TOKEN_MINTED_AT` stale, and a stale mint time is the one failure this
    mechanism cannot detect — at worst re-minting on EVERY request forever.
    """
    _SESSIONS[session_id] = jwt
    _SESSION_TOKEN_MINTED_AT[session_id] = time.time()


async def _refresh_session_token(session_id: str, age: float) -> str:
    """Re-mint the session's JWT from its CACHED claims and return the new token.

    THIS FUNCTION MUST NEVER CALL `resolve_column_scope` (E2 — Decision 7 / Q22,
    docs/decisions/prompt-routing-review-qa.md). It replays `_SESSION_SCOPES[session_id]`
    and `_SESSION_USERS[session_id]`, resolved ONCE at `create_session`, because "a
    session has ONE `column_scope` for its whole lifetime" is a product guarantee THIS
    component supplies. Q15(b) was retired on the strength of it: scratch tables are not
    stamped with the scope they were materialized under, so a scope change landing
    mid-session would leave wide-scope rows readable under a narrower one — and the
    defect would surface three components away. Any change that reintroduces a second
    resolution owes a fail-closed comparison AND a revisit of Q15(b). Accepted cost,
    recorded upstream: a revocation only takes effect at the user's next session.

    CONCURRENCY: two in-flight requests can both re-mint. Accepted rather than locked
    out — both mints carry identical claims and `sid_hash`, so both tokens are valid and
    the loser's simply goes unused; the alternative is a per-session `asyncio.Lock` on
    the hot path of every proxied call.
    """
    jwt = await _mint_jwt(
        _SESSION_USERS[session_id], _SESSION_SCOPES[session_id], session_id
    )
    _store_token(session_id, jwt)
    logger.info(
        "re-minted session token: session_id=%s previous_age_seconds=%d", session_id, age
    )
    return jwt


async def _jwt_for_session(session_id: str) -> str:
    """The session's CURRENT JWT, lazily re-minted when the held one is getting old (E1).
    Every proxy hop attaches the token through here, so the next request the user makes
    is what renews the session — no background task, scheduler or per-session timer.

    THREE AGE BANDS:
      age <= REFRESH_AFTER        serve the held token; no mint.
      REFRESH_AFTER < age <= TTL  try to re-mint; on failure serve the HELD token and
                                  warn — it is valid for another `TTL - REFRESH_AFTER`
                                  seconds, so a token-service blip must not break a
                                  working session. This band is the shock absorber.
      age > TTL                   the held token is DEAD; on mint failure fail the
                                  request LOUDLY (502) rather than proxy a known-expired
                                  token, which would surface as a bare 401 pointing at
                                  the wrong component.

    A session with no token at all is still a 404: `_SESSIONS` is the membership test.
    """
    jwt = _SESSIONS.get(session_id)
    if jwt is None:
        raise HTTPException(status_code=404, detail="Unknown session_id — call POST /api/session first.")
    minted_at = _SESSION_TOKEN_MINTED_AT.get(session_id)
    if minted_at is None:
        # AN INCOMPLETE SESSION RECORD — a token this process did not mint through
        # `_store_token`. Its age is not merely unknown, it is UNKNOWABLE here (the
        # BFF does not decode the JWT), and the cached claims a re-mint would need
        # are equally likely to be missing. So this serves the held token and
        # behaves exactly as the BFF did before E1: the session works until the
        # token dies, then 401s.
        #
        # NOT a fail-open weakening: refreshing is an AVAILABILITY feature, and the
        # only two moves available here are "serve it" and "kill a session that
        # would otherwise have worked". Nothing about the same-scope invariant (E2)
        # depends on this branch — it mints nothing. The warning is so that a future
        # `_SESSIONS` writer (a restart-restore, say) that skipped `_store_token`
        # shows up as a named gap rather than as sessions that mysteriously still
        # die at an hour.
        logger.warning(
            "session token has no recorded mint time and cannot be refreshed: "
            "session_id=%s",
            session_id,
        )
        return jwt
    age = time.time() - minted_at
    if age <= _TOKEN_REFRESH_AFTER_SECONDS:
        return jwt
    expired = age > _TOKEN_TTL_SECONDS
    try:
        return await _refresh_session_token(session_id, age)
    except HTTPException:
        if expired:
            # LOUD, and a distinct message from `_mint_jwt`'s own 502: "the token
            # service is unreachable" and "your session has expired and could not be
            # renewed" are different things to a person reading the error banner.
            logger.error(
                "session token expired and could not be renewed: session_id=%s age_seconds=%d",
                session_id,
                age,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Session token expired and could not be renewed — the token "
                    "service is unavailable. Start a new session once it recovers."
                ),
            ) from None
        logger.warning(
            "session token refresh failed; serving the still-valid held token: "
            "session_id=%s age_seconds=%d",
            session_id,
            age,
        )
        return jwt


async def _proxy_stream(path: str, session_id: str, json_body: dict[str, str]) -> StreamingResponse:
    """POST *json_body* to the runtime's *path* and forward the response — status code,
    content-type and body bytes — straight back to the browser as it arrives.

    The happy path is `text/event-stream`, forwarded byte-for-byte with no re-parsing.
    A request the runtime rejects BEFORE opening the stream (409 stale resume, 401/400)
    comes back as JSON with a non-2xx status, and that status + content-type propagate
    as-is rather than being coerced into a 200 `text/event-stream` — so the browser's
    `!response.ok` branch renders it instead of trying to SSE-parse it.
    """
    jwt = await _jwt_for_session(session_id)
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
    `GET /session/history`. The browser sends only `session_id`; the JWT is looked up and
    attached server-side (D82/D5). The runtime's status + JSON body propagate as-is so a
    non-2xx reaches the browser's error branch unchanged."""
    jwt = await _jwt_for_session(session_id)
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
    """JSON proxy for the runtime's `POST /query/page` — the paging behind the answer
    table. Same server-side token attach as `/api/history` (D82/D5). The runtime's status
    + JSON body propagate as-is, so a 400 (unparseable/non-SELECT SQL) or 403 (column-
    scope denial) reaches the browser's error branch unflattened."""
    jwt = await _jwt_for_session(body.session_id)
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

    Streamed rather than `await request.body()`, which buffers an unbounded amount for a
    chunked (no-`Content-Length`) request — the cap must be enforced WHILE reading, not
    after. The header check first is only a cheap early exit for the honest client."""
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


# How long the BFF waits on an inbox hop. Everything on this surface is a fast store
# operation EXCEPT `revise`, which makes a model call upstream.
#
# ⚠ THE BFF MUST OUTLAST THE UPSTREAM'S OWN DEADLINE, or it preempts it. The reviser gives
# itself `learning_revise_timeout_seconds` (30s default) and converts its own expiry into a
# 200 carrying "the assistant timed out; try again" — a result the reviewer can act on. With
# the CRUD timeout applied here, the BFF abandoned the hop at 10s and rendered
# `502 Inbox service unreachable` while the model was still answering (observed live: the
# upstream logged `POST /v1/responses 200` after the browser had already given up). The
# proposal was produced and thrown away, and the error blamed the wrong component.
_INBOX_HOP_TIMEOUT_SECONDS = 10.0
# A dead local service must still fail FAST, whatever the read budget is.
_INBOX_CONNECT_TIMEOUT_SECONDS = 5.0
# Must stay ABOVE `learning_revise_timeout_seconds` (120s) so the upstream's own
# graceful 'timed out; try again' reaches the browser instead of a bare 502.
_INBOX_MODEL_HOP_TIMEOUT_SECONDS = 180.0
_INBOX_MODEL_ACTIONS = frozenset({"revise"})
# A WAREHOUSE hop, not a model one: `trial_run` binds the template and executes it through the
# MCP. Its own budget because it is neither a fast store read nor a model call — a real query
# against live ClickHouse, which can legitimately take tens of seconds on a wide scan.
_INBOX_WAREHOUSE_ACTIONS = frozenset({"trial_run"})
_INBOX_WAREHOUSE_HOP_TIMEOUT_SECONDS = 90.0

# Path segments that reach a model, for the TIMEOUT lookup only. A superset of
# `_INBOX_MODEL_ACTIONS` and deliberately a separate name: that set means "a per-candidate
# ACTION that calls a model", and every member of it must also be in `_INBOX_ACTIONS` and
# `_INBOX_BODY_ACTIONS` — an invariant with its own test. `mint` calls a model but is NOT a
# per-candidate action; it has no candidate id and its own route. Adding it to the action set
# to buy the longer timeout would have broken that invariant to describe something it was
# never about, so the two concerns get two names.
_INBOX_MODEL_PATH_SEGMENTS = _INBOX_MODEL_ACTIONS | frozenset({"mint"})


def _hop_timeout(path: str) -> httpx.Timeout:
    """The timeout for one inbox hop, by whether the upstream will call a model.

    ⚠ READ is what varies; CONNECT never does. A scalar `timeout=60.0` sets all four httpx
    phases, so a service that is DOWN would take a full minute to report "unreachable" — the
    fast-failure case would be sacrificed to fix the slow-success one. Connecting to a local
    service either works immediately or is not going to.
    """
    action = path.rsplit("/", 1)[-1].split("?", 1)[0]
    if action in _INBOX_MODEL_PATH_SEGMENTS:
        read = _INBOX_MODEL_HOP_TIMEOUT_SECONDS
    elif action in _INBOX_WAREHOUSE_ACTIONS:
        read = _INBOX_WAREHOUSE_HOP_TIMEOUT_SECONDS
    else:
        read = _INBOX_HOP_TIMEOUT_SECONDS
    return httpx.Timeout(read, connect=_INBOX_CONNECT_TIMEOUT_SECONDS)


async def _inbox_json_body(request: Request, action: str) -> Any:
    """One inbox request body, size-capped and parsed. Shared by the minting routes.

    SIZE-CAPPED with the same budget every other inbox write uses. A minting body is the
    largest this BFF accepts — a whole query plus prose — which makes it the one most worth
    bounding, and the cap is the BFF's own resource that nothing downstream can give back.
    """
    raw = await _read_bounded_body(request, INBOX_BODY_MAX_BYTES)
    if raw is None:
        raise HTTPException(
            status_code=413, detail="Request body exceeds the maximum allowed size."
        )
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"{action} requires a JSON body."
        ) from None


async def _proxy_inbox(
    method: str, path: str, json_body: Any | None = None
) -> JSONResponse:
    """Proxy a JSON (non-streaming) inbox request to the inbox service, attaching the
    server-held `X-Reviewer-Token` on the hop (the browser never sees it). The service's
    status code + JSON body are propagated as-is so a 4xx/5xx (unknown id, illegal
    transition, landing-plane 503) reaches the browser's error branch unchanged.

    *json_body* is forwarded VERBATIM when present. The BFF does not inspect or reshape
    it: the inbox service validates it, and the extractor's own readers do behind that —
    a second schema here would just be a second vocabulary for the same mistake."""
    headers = {"X-Reviewer-Token": REVIEWER_TOKEN}
    async with httpx.AsyncClient(timeout=_hop_timeout(path)) as client:
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
    `_INBOX_LIST_STATUSES` (400 on anything else — do NOT proxy) and forward it as an
    upstream query param (ui-inbox-type-archive contract §List API)."""
    _require_inbox_enabled()
    if status is None:
        return await _proxy_inbox("GET", "/inbox")
    if status not in _INBOX_LIST_STATUSES:
        # Message DERIVED from the allowlist, so a status added to the set can never be
        # accepted while the error still names the previous ones.
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {sorted(_INBOX_LIST_STATUSES)}.",
        )
    query = urllib.parse.urlencode({"status": status})
    return await _proxy_inbox("GET", f"/inbox?{query}")


@app.get("/mint")
async def mint_page() -> FileResponse:
    """Serve the blueprint AUTHORING page. HTML shell only; its data calls go to
    `/api/inbox/mint*` below. Behind the same inbox switch as the reviewer page, because it
    writes to the same access-controlled review queue."""
    _require_inbox_enabled()
    return FileResponse(_MINT_HTML)


@app.get("/api/inbox/mint/schema")
async def inbox_mint_schema() -> JSONResponse:
    """Proxy the tables and columns the minting form may offer.

    Declared BEFORE the generic `POST /api/inbox/{candidate_id}/{action}` route below. They do
    not collide today (that one is POST-only, this is GET), but the shapes overlap —
    `mint/schema` reads as `candidate_id="mint", action="schema"` — and relying on the method to
    keep them apart is a coincidence rather than a design."""
    _require_inbox_enabled()
    return await _proxy_inbox("GET", "/inbox/mint/schema")


@app.post("/api/inbox/mint/prior_art")
async def inbox_mint_prior_art(request: Request) -> JSONResponse:
    """Proxy the duplicate check. Reads only — it drafts nothing and writes nothing."""
    _require_inbox_enabled()
    return await _proxy_inbox(
        "POST", "/inbox/mint/prior_art", await _inbox_json_body(request, "prior_art")
    )


@app.post("/api/inbox/mint")
async def inbox_mint(request: Request) -> JSONResponse:
    """Proxy one minting submission. The body is forwarded VERBATIM — the inbox service owns
    the validation, and a second schema here would be a second vocabulary for the same
    mistake."""
    _require_inbox_enabled()
    return await _proxy_inbox("POST", "/inbox/mint", await _inbox_json_body(request, "mint"))


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
    # `complete` and `promote` carry a body. Read it here rather than typing it: the shape
    # is the inbox service's contract (and, under it, the extractor's readers), and a model
    # here would reject reviewer input with a message that names no fix. What IS enforced
    # here is the size, because that is the BFF's own resource and nobody downstream can
    # give it back. An EMPTY body stays `None` — `promote`'s refinements are optional, and
    # the upstream route must see no body at all rather than a `null` one.
    body: Any | None = None
    if action in _INBOX_BODY_ACTIONS:
        raw = await _read_bounded_body(request, INBOX_BODY_MAX_BYTES)
        if raw is None:
            raise HTTPException(
                status_code=413, detail="Request body exceeds the maximum allowed size."
            )
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"{action} requires a JSON body."
            ) from None
    # The RESPONSE goes back body-agnostically through `_proxy_inbox`: `promote` answers a
    # `PromotionEmit` (yaml + PR metadata) and `revise` answers a PROPOSAL (entries + diff,
    # with nothing written), NEITHER of which is the `{candidate_id, status, reason}` shape
    # every other action answers. The BFF must never assume any of those THREE shapes.
    #
    # Percent-encode the decoded id before re-interpolating it into the upstream path
    # (candidate ids carry `::` and could carry other reserved chars) so it is passed as
    # a single, unambiguous path segment — never able to inject extra path structure.
    safe_id = urllib.parse.quote(candidate_id, safe="")
    return await _proxy_inbox("POST", f"/inbox/{safe_id}/{action}", body)


# --- upload BFF (UI Slice 4, §4) ---------------------------------------------


def _scratch_base() -> str:
    """Derive the `/scratch/v1` base URL from `MCP_URL` by keeping its scheme + netloc
    and swapping the path — the identical derivation `runtime/config.py` does. The
    scratch upload routes are custom HTTP routes on the MCP host (`clickhouse-api`), NOT
    on `RUNTIME_URL`. E.g. `http://localhost:18090/mcp` -> `http://localhost:18090/scratch/v1`."""
    parts = urllib.parse.urlsplit(MCP_URL)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/scratch/v1", "", ""))


async def _proxy_upload(path: str, session_id: str, request: Request) -> JSONResponse:
    """RAW-BODY multipart passthrough to a clickhouse-api scratch route (§4).

    The BFF must NOT parse the form — that would pull `python-multipart` into this
    package's deps, which the contract forbids — so it reads the raw body bytes and
    forwards them verbatim with the browser's ORIGINAL `Content-Type` header, which
    carries the multipart boundary the downstream parser needs. `session_id` arrives as a
    query param, so no form parsing is needed to know it. The session's JWT +
    `X-Session-Id` are attached server-side (D82/D5). Upstream status + JSON body
    propagate as-is (non-JSON -> `{"detail": text}`).
    """
    jwt = await _jwt_for_session(session_id)
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
