"""learning/inbox/service.py — the review-inbox HTTP service (UI Slice 2, §2/§3).

A SMALL, dedicated FastAPI process (Option A, contract §0) that mounts ONE
`ReviewInbox` over the promotion write plane and exposes the four reviewer routes
(`GET /inbox`, `POST /inbox/{id}/{approve|reject|retract}`, `GET /inbox/health`).
The UI BFF (`ui/server.py`) serves the page and PROXIES `/api/inbox/*` here,
attaching the shared reviewer token server-side — the browser never reaches this
service directly and never sees the token.

Why a separate process (not folded into the BFF): the BFF's charter is chat-session
JWTs ONLY (`ui/server.py:2-16`). This service holds the neo4j + couchbase + MCP +
embedding WRITE plane; injecting that into the BFF would couple reviewer infra to the
chat lifecycle. Keeping it structurally separate mirrors the existing runtime↔BFF
split.

Auth (contract §3): a lightweight gate — an env FLAG (`REVIEW_INBOX_ENABLED`) plus a
shared-secret `X-Reviewer-Token` header compared CONSTANT-TIME to `REVIEWER_TOKEN`.
This is NOT a `column_scope` JWT (a reviewer has no warehouse scope) and this service
never mints a token. Both are enforced BEFORE the inbox is touched.

Degrade (contract §5/§6): with the full write plane wired, `approve` lands then
validates a `global_knowledge`/`blueprint`. In offline dev mode (an
`InMemoryCandidateStore` + the default unwired scheduler) list/reject/retract still
work honestly, but an approve that would need to LAND is refused with `503` — the
service never fakes a `validated`.

Config (env vars, all optional):
    REVIEW_INBOX_ENABLED   "1" to expose the routes; anything else ⇒ every route 404
    REVIEWER_TOKEN         the shared secret the `X-Reviewer-Token` header must match
    (full-plane infra vars, read only when building the default app: COUCHBASE_*,
     MCP_URL, TOKEN_SERVICE_URL, TOKEN_ISSUER_API_KEY, NEO4J_*, EMBEDDING_API_URL)

Run:
    uv run python scripts/run_inbox_service.py
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException

from ..candidate.memory_candidate_store import InMemoryCandidateStore
from ..candidate.models import CandidateStatus
from ..promotion.scheduler import PromotionScheduler
from .inbox import InboxTransitionError, ReviewInbox, _NoOpProbe, _ZeroHitCounts
from .models import InboxItem

_logger = logging.getLogger(__name__)

WritePlaneMode = Literal["full", "offline"]

# The only statuses the list surface exposes (ui-inbox-type-archive contract §List
# API): the live review queue and the durable rejected archive. Any other value is a
# 400 — the inbox never lets a caller enumerate arbitrary lifecycle states.
_LISTABLE_STATUSES = frozenset({CandidateStatus.IN_REVIEW, CandidateStatus.REJECTED})


# --- projections (contract §2a) ----------------------------------------------


def _inbox_item_to_wire(item: InboxItem) -> dict[str, Any]:
    """Project an `InboxItem` to the EXACT wire shape. Carries the real fields plus the
    envelope `status` (a plain string; `in_review` for the review queue, `rejected` for
    the archive view — ui-inbox-type-archive contract §List API). No invented
    `confidence`/`drift`. `payload_view` is the already-redacted dict (D17) rendered
    verbatim — the raw entity values never cross this boundary."""
    return {
        "candidate_id": item.candidate_id,
        "type": item.type,
        "status": item.status,
        "reason": item.reason,
        "summary": item.summary,
        "payload_view": item.payload_view,
        "evidence_refs": list(item.evidence_refs),
        "entity_scan": item.entity_scan.to_doc(),
        "dedup": item.dedup.to_doc() if item.dedup is not None else None,
        "created_at": item.created_at,
    }


def _action_result(env: Any) -> dict[str, Any]:
    """The minimal `ActionResult` (§2a): the new store status after the transition,
    `reason=null` on success. Deliberately NOT the full envelope — the UI just
    refreshes the list."""
    return {
        "candidate_id": env.candidate_id,
        "type": env.type,
        "status": env.status,
        "reason": None,
    }


def _map_transition_error(exc: InboxTransitionError) -> HTTPException:
    """Map an `InboxTransitionError` to the contract §2/§6 status codes.

      * message contains "not found"          → 404 (unknown id)
      * a HELD approve for landing-unavailable → 503 ("landing plane unavailable")
      * anything else                          → 409 (illegal transition / held
                                                 approve — reason surfaced verbatim)
    """
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    # A held approve whose reason is the honest landing-plane gap is a DEGRADE, not an
    # illegal transition — surface it as 503 so the page shows "approve-that-lands is
    # unavailable in this deployment" rather than a transition error (contract §5/§6).
    # `landing_unavailable` = no writer wired (dormant); `landing_failed` = a transient
    # landing-plane outage (e.g. neo4j down) — BOTH are infra, not reviewer, errors.
    if "landing_unavailable" in message or "landing_failed" in message:
        return HTTPException(status_code=503, detail="landing plane unavailable")
    return HTTPException(status_code=409, detail=message)


# --- default (env-driven) construction ---------------------------------------


def _build_inbox_from_env() -> tuple[ReviewInbox, WritePlaneMode, Any]:
    """Build the `ReviewInbox` for a standalone run. Returns `(inbox, mode, driver)`.

    Mirrors `scripts/run_learning_scheduler.py:129-193` — which builds the write plane
    and DISCARDS its inbox. Here we KEEP the inbox. When every full-plane port is
    configured, wire the fully-activated write plane via `build_promotion_write_plane`
    (couchbase store + MCP + token minter + neo4j + embedding). Otherwise fall back to
    the OFFLINE dev mode: an `InMemoryCandidateStore` + the default unwired scheduler
    (list/reject/retract work; a landing-approve honestly 503s, §5). The neo4j driver
    is returned so the caller can close it on shutdown (None in offline mode)."""
    from ..config import LearningSettings

    learning_settings = LearningSettings()

    from data_agent.runtime.config import RuntimeSettings

    runtime_settings = RuntimeSettings()
    write_plane_ready = bool(
        learning_settings.learning_candidates_username
        and learning_settings.learning_candidates_password
        and learning_settings.learning_corpus_username
        and learning_settings.learning_corpus_password
        and runtime_settings.mcp_url
        and runtime_settings.token_service_url
        and runtime_settings.token_issuer_api_key
        and runtime_settings.neo4j_url
        and runtime_settings.neo4j_username
        and runtime_settings.neo4j_password
        and runtime_settings.embedding_api_url
    )
    if not write_plane_ready:
        _logger.info(
            "inbox service running in OFFLINE dev mode (InMemoryCandidateStore) — "
            "list/reject/retract work; landing-approve 503s (require_landing ON, no "
            "writer wired)"
        )
        # `require_landing=True` with NO landing writer is what makes an offline
        # landing-target approve HOLD `approve_blocked_landing_unavailable` (→ 503, §5)
        # instead of the default scheduler's fake `validated`. The no-op probe/hit-count
        # doubles are the same ones an unwired `ReviewInbox` uses.
        store = InMemoryCandidateStore()
        scheduler = PromotionScheduler(
            store,
            probe=_NoOpProbe(),
            hit_counts=_ZeroHitCounts(),
            require_landing=True,
        )
        return ReviewInbox(store, scheduler=scheduler), "offline", None

    from neo4j import AsyncGraphDatabase

    from data_agent.runtime.mcp.real_client import RealMCPClient
    from data_agent.runtime.model.embedding_client import HttpEmbeddingClient

    from ..candidate.couchbase_candidate_store import CouchbaseCandidateStore
    from ..dedup.couchbase_corpus import CouchbaseBlueprintCorpus
    from ..factory import build_promotion_write_plane
    from ..promotion.token_minter import HttpTokenMinter

    candidate_store = CouchbaseCandidateStore(learning_settings)
    corpus = CouchbaseBlueprintCorpus(learning_settings)
    neo4j_driver = AsyncGraphDatabase.driver(
        runtime_settings.neo4j_url,
        auth=(runtime_settings.neo4j_username, runtime_settings.neo4j_password),
        connection_timeout=runtime_settings.neo4j_timeout_seconds,
        connection_acquisition_timeout=runtime_settings.neo4j_timeout_seconds,
        max_transaction_retry_time=runtime_settings.neo4j_timeout_seconds,
    )
    # Same recipe as the scheduler entrypoint; we hold the returned INBOX (the
    # scheduler is wired into it and shares the one candidate store).
    _scheduler, inbox = build_promotion_write_plane(
        learning_settings,
        candidate_store=candidate_store,
        hit_counts=corpus,
        mcp_client=RealMCPClient(runtime_settings.mcp_url),
        token_minter=HttpTokenMinter(
            runtime_settings.token_service_url,
            runtime_settings.token_issuer_api_key,
        ),
        neo4j_driver=neo4j_driver,
        embedding_client=HttpEmbeddingClient(
            url=runtime_settings.embedding_api_url,
            api_key=runtime_settings.embedding_api_key,
            model=runtime_settings.embedding_model,
            timeout_seconds=runtime_settings.embedding_timeout_seconds,
        ),
        model_id=runtime_settings.embedding_model,
    )
    _logger.info("inbox service running with the FULL write plane (neo4j landing ACTIVE)")
    return inbox, "full", neo4j_driver


# --- the app factory ----------------------------------------------------------


def create_inbox_app(
    *,
    inbox: ReviewInbox | None = None,
    write_plane: WritePlaneMode | None = None,
) -> FastAPI:
    """Build the inbox FastAPI app mounting *inbox*.

    When *inbox* is None, build it from env (`_build_inbox_from_env`): the full write
    plane when configured, else the offline dev fallback. Tests inject their own
    `ReviewInbox` (over an `InMemoryCandidateStore`) + an explicit *write_plane*.
    """
    driver: Any = None
    if inbox is None:
        inbox, mode, driver = _build_inbox_from_env()
        write_plane = write_plane or mode
    else:
        write_plane = write_plane or "offline"

    app = FastAPI(title="data-agent-inbox")
    app.state.write_plane = write_plane

    def _require_reviewer(
        x_reviewer_token: str | None = Header(default=None),
    ) -> None:
        """Enforce the flag + reviewer token BEFORE the inbox is touched (§3).

        Flag off (`REVIEW_INBOX_ENABLED` != "1") ⇒ 404: the surface does not exist.
        Token not configured ⇒ 503 (FAIL CLOSED): an unset `REVIEWER_TOKEN` must NOT
        collapse to a passing `compare_digest("", "")` — that would admit an empty
        `X-Reviewer-Token:` header and expose the whole write plane. Missing token ⇒
        401; mismatch ⇒ 403 (constant-time compare, no length oracle)."""
        if os.environ.get("REVIEW_INBOX_ENABLED") != "1":
            raise HTTPException(status_code=404, detail="Not found.")
        expected = os.environ.get("REVIEWER_TOKEN", "")
        if not expected:
            raise HTTPException(
                status_code=503, detail="reviewer token not configured"
            )
        if x_reviewer_token is None:
            raise HTTPException(status_code=401, detail="Missing X-Reviewer-Token.")
        if not hmac.compare_digest(x_reviewer_token, expected):
            raise HTTPException(status_code=403, detail="Invalid reviewer token.")

    guard = [Depends(_require_reviewer)]

    @app.get("/inbox", dependencies=guard)
    async def list_inbox(status: str | None = None) -> dict[str, Any]:
        """List the review queue (default) or, with `?status=rejected`, the durable
        archive (ui-inbox-type-archive contract §List API). The BFF already validates
        `status`, but validate defensively here too — an out-of-set value is a 400, not a
        pass-through to `list_by_status` (which would happily enumerate any status)."""
        selected = status if status is not None else CandidateStatus.IN_REVIEW
        if selected not in _LISTABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="status must be one of {'in_review', 'rejected'}.",
            )
        # The durable, unbounded rejected archive lists NEWEST-first so the LIMIT
        # caps OLD history, not present rejects; the review queue keeps ASC (oldest
        # first — FIFO drain). Chosen explicitly by the caller, per the contract.
        order = "desc" if selected == CandidateStatus.REJECTED else "asc"
        items = await inbox.list(status=selected, limit=100, order=order)
        wire = [_inbox_item_to_wire(it) for it in items]
        return {"items": wire, "count": len(wire)}

    @app.get("/inbox/health", dependencies=guard)
    async def inbox_health() -> dict[str, str]:
        return {"write_plane": app.state.write_plane}

    @app.post("/inbox/{candidate_id}/approve", dependencies=guard)
    async def approve(candidate_id: str) -> dict[str, Any]:
        try:
            env = await inbox.approve(candidate_id)
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        return _action_result(env)

    @app.post("/inbox/{candidate_id}/reject", dependencies=guard)
    async def reject(candidate_id: str) -> dict[str, Any]:
        try:
            env = await inbox.reject(candidate_id)
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        return _action_result(env)

    @app.post("/inbox/{candidate_id}/retract", dependencies=guard)
    async def retract(candidate_id: str) -> dict[str, Any]:
        try:
            env = await inbox.retract(candidate_id)
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        return _action_result(env)

    if driver is not None:

        @app.on_event("shutdown")
        async def _close_driver() -> None:
            await driver.close()

    return app
