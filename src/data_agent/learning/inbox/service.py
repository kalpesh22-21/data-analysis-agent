"""learning/inbox/service.py — the review-inbox HTTP service (UI Slice 2, §2/§3).

A SMALL, dedicated FastAPI process mounting ONE `ReviewInbox` over the promotion write plane
and exposing the reviewer routes. The UI BFF serves the page and PROXIES `/api/inbox/*` here,
attaching the shared reviewer token server-side, so the browser never reaches this service
directly and never sees the token. It is a separate process because the BFF's charter is
chat-session JWTs ONLY, while this service holds the neo4j + couchbase + MCP + embedding
WRITE plane.

AUTH (§3): an env FLAG (`REVIEW_INBOX_ENABLED`) plus a shared-secret `X-Reviewer-Token`
compared CONSTANT-TIME to `REVIEWER_TOKEN`, both enforced BEFORE the inbox is touched. This
is NOT a `column_scope` JWT and this service never mints a token.

DEGRADE (§5/§6): in offline dev mode list/reject/retract still work honestly, but an approve
that would need to LAND is refused with 503 — the service never fakes a `validated`.

Run: `uv run python scripts/run_inbox_service.py`
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from data_agent.runtime.blueprint.models import BlueprintParseError

from ..candidate.memory_candidate_store import InMemoryCandidateStore
from ..candidate.models import CandidateStatus
from ..promotion.scheduler import PromotionScheduler
from .completion import (
    CompletionInputError,
    CompletionRaceError,
    CompletionResult,
    CompletionUnavailableError,
    ParameterizationCompleter,
)
from .inbox import InboxTransitionError, ReviewInbox, _NoOpProbe, _ZeroHitCounts
from .models import InboxItem


class PromoteRequest(BaseModel):
    """The optional PROMOTE request body (contract §Promote).

    The human may refine the knowledge `doc_id` (the candidate's is non-semantic) and `title`;
    both are typed `str | None` so FastAPI 422s a malformed value before it can reach the emitted
    YAML. `id` is NEVER accepted here — it must equal the landing node id verbatim.
    """

    doc_id: str | None = None
    title: str | None = None


class CompleteParameterizationRequest(BaseModel):
    """The fail-to-review COMPLETE body.

    `entries` is the parameterization the reviewer wrote: APPENDED to what the model already
    produced by default (the `totality_violation` case), or REPLACING the whole array with
    `replace=true` (the `rule_predicate_mismatch` case, where an entry is wrong and no append can
    fix it). Typed as loosely as the payload it becomes: every entry goes through the SAME
    readers and the SAME D97 totality walk as model output, so validating its shape twice would
    give the reviewer two error vocabularies for one mistake, only one of which names the fix.
    """

    entries: list[dict[str, Any]] = []
    replace: bool = False

_logger = logging.getLogger(__name__)

WritePlaneMode = Literal["full", "offline"]

# The statuses the list surface exposes: the live review queue, the durable rejected
# archive, (Phase-3) the VALIDATED set of auto-landed learning nodes awaiting a
# human verify/promote, and the fail-to-review work list. Every validated candidate in
# the store is `source='learning'` by construction (the MCP canon never enters
# `learning_candidates`), so `status=validated` IS the promotable-learning listing. Any
# other value is a 400 — the inbox never lets a caller enumerate arbitrary lifecycle
# states.
#
# `needs_parameterization` is a candidate the judge passed on merit whose
# parameterization form could not be filled in; its rows are completed, not adjudicated
# (`docs/decisions/learning-declined-candidate-review.md`).
#
# `promoted` is listable for one reason: `promote` RE-EMITS idempotently from that state
# (`inbox.py::promote`), so the YAML behind an abandoned or lost PR is always
# recoverable — but only by a caller who can still FIND the row. Withholding the listing
# left that affordance reachable by curl and by nothing else, which is the same shape of
# invisible loss the archive listing was built to end.
_LISTABLE_STATUSES = frozenset(
    {
        CandidateStatus.IN_REVIEW,
        CandidateStatus.REJECTED,
        CandidateStatus.VALIDATED,
        CandidateStatus.NEEDS_PARAMETERIZATION,
        CandidateStatus.PROMOTED,
    }
)


# --- projections (contract §2a) ----------------------------------------------


def _inbox_item_to_wire(item: InboxItem) -> dict[str, Any]:
    """Project an `InboxItem` to the EXACT wire shape.

    The real fields plus the envelope `status`; no invented `confidence`/`drift`. `payload_view`
    is the already-redacted dict (D17) rendered verbatim — the raw entity values never cross this
    boundary.
    """
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
        # Phase-3: expose the human-approval flag so the UI can tell a VERIFIED validated
        # learning node (promotable) from an auto-landed one. False for every review-queue
        # / archive / auto-landed row.
        "verified": item.verified,
        # Plan §4: `"user_corrected"` when a human corrected the artifact this candidate
        # was promoted from and the cron routed it back here, else null. Distinct from
        # `reason` (the routing CATEGORY) — see `InboxItem.route_reason`. The UI should
        # render it prominently: the approve path re-runs static validation and the golden
        # replay, and NEITHER can see the value error the user reported, so this is the
        # only warning a reviewer gets.
        "route_reason": item.route_reason,
        # Plan §4: the review score AND its three axes, not just the composite. A bare
        # number would make the queue's ordering unfalsifiable — the axes are what let a
        # reviewer (or an operator debugging a suspicious order) see that a row is high
        # because it is genuinely novel rather than because nothing could be measured.
        # `*_measured` distinguishes a real zero from an absent stamp; the UI is expected
        # to render an unmeasured axis as unknown rather than as a low score.
        "score": item.score.to_doc(),
        # Fail-to-review only (null on every other row): the decline the reviewer is being
        # asked to fix. The DETAIL — which names predicates and their literal values — is
        # carried only when the persisted leakage verdict is a clean `pass`;
        # `InboxItem.decline_view` owns that rule and states why the wire surface is
        # narrower than the store.
        "decline": item.decline_view(),
    }


def _action_result(env: Any) -> dict[str, Any]:
    """The minimal `ActionResult` (§2a): the new store status, `reason=null` on success.

    Deliberately NOT the full envelope — the UI just refreshes the list.
    """
    return {
        "candidate_id": env.candidate_id,
        "type": env.type,
        "status": env.status,
        "reason": None,
    }


def _completion_result(result: CompletionResult) -> dict[str, Any]:
    """The fail-to-review COMPLETE response.

    The same four `ActionResult` fields every other action returns, plus the `outcome` the caller
    branches on and — when the form is still incomplete — the fresh decline. The decline is
    projected through the SAME `InboxItem` rule that governs the list surface, so the withholding
    of an entity-bearing detail cannot differ between the row a reviewer is reading and the
    response to the edit they just made.
    """
    item = InboxItem.from_envelope(result.envelope)
    return {
        "candidate_id": result.envelope.candidate_id,
        "type": result.envelope.type,
        "status": result.envelope.status,
        "reason": None,
        "outcome": result.outcome,
        "decline": item.decline_view(),
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
    # Same class, different plane: this deployment cannot RE-VALIDATE a completed
    # parameterization (no catalog / no pipeline wired), which is infra missing, not a
    # reviewer mistake. 503 so the page says "unavailable here" rather than implying the
    # form was wrong.
    if "completion_unavailable" in message:
        return HTTPException(
            status_code=503, detail="parameterization completion unavailable"
        )
    return HTTPException(status_code=409, detail=message)


# --- default (env-driven) construction ---------------------------------------


def _build_completer(
    learning_settings: Any,
    runtime_settings: Any,
    *,
    candidate_store: Any,
    corpus: Any,
    embedding_client: Any,
) -> ParameterizationCompleter | None:
    """Build the fail-to-review completion plane, or `None` when the catalog cannot be read.

    THE CATALOG COMES FROM THE SAME PLACE THE CONSUMER'S DOES — the frozen `GET /catalog/export`
    snapshot — and that is the load-bearing detail: the completer re-runs the extractor's own
    validation, so a different catalog would accept rule ids the extractor could not, or decline
    ones it would have taken, and the review queue and the loop would be arguing about which
    rules the deployment has. FAIL-OPEN on a missing or unreadable snapshot: no completer, and a
    completion attempt answers 503 instead of re-validating against an empty catalog, which would
    decline every rule-role entry a reviewer wrote and blame them for it. Logged loudly, because
    a reviewer facing that 503 has no other way to learn the cause.
    """
    import json

    from ..extractor.grounding import known_rule_ids_from_catalog, rule_index_from_catalog
    from ..factory import build_write_router_stages

    try:
        path = runtime_settings.catalog_fixture_file()
        with path.open(encoding="utf-8") as fh:
            catalog = json.load(fh)["catalog"]
    except (OSError, ValueError, KeyError, TypeError):
        _logger.warning(
            "inbox service: the semantic catalog snapshot could not be read, so "
            "fail-to-review COMPLETION is disabled (every attempt 503s). It must be the "
            "SAME catalog the extractor is grounded against — point CATALOG_FIXTURE_PATH "
            "at a GET /catalog/export dump.",
            exc_info=True,
        )
        return None

    from data_agent.catalog.loader import build_sqlglot_schema_from_catalog

    return ParameterizationCompleter(
        store=candidate_store,
        known_rules=known_rule_ids_from_catalog(catalog),
        rule_index=rule_index_from_catalog(catalog),
        # The BLUEPRINT half of the frozen write-router order. `needs_parameterization`
        # is a blueprint-only status (its decline reasons are blueprint-only checks), so
        # the two target-specific stages would do nothing but demand collaborators this
        # process has no other use for — see `build_write_router_stages`.
        stages=build_write_router_stages(
            learning_settings,
            candidate_store=candidate_store,
            blueprint_corpus=corpus,
            catalog_schema=build_sqlglot_schema_from_catalog(catalog),
            embedder=embedding_client,
            include_target_specific=False,
        ),
    )


def _build_inbox_from_env() -> tuple[ReviewInbox, WritePlaneMode, Any]:
    """Build the `ReviewInbox` for a standalone run. Returns `(inbox, mode, driver)`.

    When every full-plane port is configured, wires the fully-activated write plane via
    `build_promotion_write_plane`; otherwise falls back to OFFLINE dev mode (an
    `InMemoryCandidateStore` + the default unwired scheduler), where list/reject/retract work and
    a landing-approve honestly 503s. The neo4j driver is returned so the caller can close it on
    shutdown (None in offline mode).
    """
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
        # A blank tenant claim is as disabling as a blank mint credential: the MCP
        # rejects every replay 403 MISSING_TENANT_CLAIM. Gate on it here so the
        # deployment falls into the LOGGED offline posture rather than starting a
        # write plane whose verification gate can never run.
        and runtime_settings.tenant_client_code.strip()
        and runtime_settings.tenant_proc_center.strip()
        and runtime_settings.tenant_jti.strip()
        and runtime_settings.neo4j_url
        and runtime_settings.neo4j_username
        and runtime_settings.neo4j_password
        and runtime_settings.embedding_api_url
    )
    # The human `approve` path re-runs golden replay, so this service replays as a
    # tenant too. Logged in EVERY posture for the same reason the scheduler does it: a
    # wrong tenant filters to zero rows instead of erroring, so it is invisible in the
    # outcome and recoverable only from a line like this one.
    _logger.info(
        "approve-path golden replay runs AS tenant clientcode=%s proc_center=%s jti=%s "
        "(deployment config TENANT_*; any blank one forces the offline dev mode)",
        runtime_settings.tenant_client_code.strip() or "<unset>",
        runtime_settings.tenant_proc_center.strip() or "<unset>",
        runtime_settings.tenant_jti.strip() or "<unset>",
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
        # NO `corpus_status` here, deliberately: offline dev mode has no
        # `learning_corpus` store at all (its RBAC creds are part of what
        # `write_plane_ready` tests), so there is no artifact to stamp. A reject still
        # transitions the candidate; only the corpus-side stamp is absent, which is the
        # correct shape for a mode that has no corpus.
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
    from ..promotion.token_minter import HttpTokenMinter, TenantClaims

    candidate_store = CouchbaseCandidateStore(learning_settings)
    corpus = CouchbaseBlueprintCorpus(learning_settings)
    embedding_client = HttpEmbeddingClient(
        url=runtime_settings.embedding_api_url,
        api_key=runtime_settings.embedding_api_key,
        model=runtime_settings.embedding_model,
        timeout_seconds=runtime_settings.embedding_timeout_seconds,
    )
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
            # The human `approve` path re-runs golden replay through this minter. The
            # tenant it replays as is deployment config (TENANT_*, shared verbatim with
            # ui/server.py) — a reviewer's approve is not a caller-authority read; see
            # `TenantClaims` for the argument and for what it does NOT prove.
            tenant=TenantClaims(
                clientcode=runtime_settings.tenant_client_code,
                proc_center=runtime_settings.tenant_proc_center,
                jti=runtime_settings.tenant_jti,
            ),
        ),
        neo4j_driver=neo4j_driver,
        embedding_client=embedding_client,
        model_id=runtime_settings.embedding_model,
        # The fail-to-review completion plane: re-validation + the blueprint half of the
        # write router, over the SAME candidate store this inbox reads.
        completer=_build_completer(
            learning_settings,
            runtime_settings,
            candidate_store=candidate_store,
            corpus=corpus,
            embedding_client=embedding_client,
        ),
        # PriorArt Slice 2 — THE process where humans actually reject. `reject` and
        # `retract` reach the scheduler through THIS service, not through
        # `run_learning_scheduler.py`, so omitting this made the whole
        # "rejected artifacts stop surfacing as live prior art" prerequisite a no-op in
        # deployment — silently, because `_stamp_corpus_status` returns without logging
        # when no writer is wired.
        #
        # The SAME `corpus` object already passed as `hit_counts`
        # (`CouchbaseBlueprintCorpus` duck-types both ports): the terminal stamp and the
        # promotion guard's count must address the same artifacts, and a split would let
        # a reject kill one store while the guard read another.
        corpus_status=corpus,
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

    When *inbox* is None, build it from env: the full write plane when configured, else the
    offline dev fallback. Tests inject their own `ReviewInbox` plus an explicit *write_plane*.
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

        Flag off ⇒ 404: the surface does not exist. Token not configured ⇒ 503, FAIL CLOSED — an
        unset `REVIEWER_TOKEN` must NOT collapse to a passing `compare_digest("", "")`, which would
        admit an empty `X-Reviewer-Token` header and expose the whole write plane. Missing token ⇒
        401; mismatch ⇒ 403, constant-time compared with no length oracle.
        """
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
        """List the review queue, or with `?status=rejected` the durable archive.

        The BFF already validates `status`, but validate defensively here too — an out-of-set value
        is a 400, not a pass-through to `list_by_status`, which would happily enumerate any status.
        """
        selected = status if status is not None else CandidateStatus.IN_REVIEW
        if selected not in _LISTABLE_STATUSES:
            # Message DERIVED from the allowlist: it named three statuses while the set
            # held four, which is how a listable status stays invisible to whoever reads
            # the error instead of the code.
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {sorted(_LISTABLE_STATUSES)}.",
            )
        # The durable, unbounded terminal archives (rejected, promoted) list NEWEST-first
        # so the LIMIT caps OLD history rather than present rows — for `promoted` that is
        # what makes "re-emit the YAML I promoted an hour ago" a top-of-list operation.
        # The review queue + the validated (Phase-3 promotable) listing keep ASC (oldest
        # first — FIFO drain). Chosen explicitly by the caller, per the contract.
        order = (
            "desc"
            if selected in (CandidateStatus.REJECTED, CandidateStatus.PROMOTED)
            else "asc"
        )
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

    @app.post("/inbox/{candidate_id}/complete", dependencies=guard)
    async def complete(
        candidate_id: str, body: CompleteParameterizationRequest | None = None
    ) -> dict[str, Any]:
        """FAIL-TO-REVIEW COMPLETE: the reviewer supplies the missing parameterization entries.

        The candidate RE-VALIDATES in full and, if it passes, re-runs the write-router pipeline.
        Requires `status=needs_parameterization`. A STILL-INCOMPLETE FORM ANSWERS 200, not 4xx, with
        `outcome="declined"` and the fresh decline: the reviewer sent a well-formed attempt, and the
        pipeline's answer is the RESULT they need in order to make the next one — mapping it to a 409
        would put the one sentence that names the fix into an error banner. The other codes keep
        their usual meanings: 404 unknown id, 409 wrong status, 422 an `entries` value that is not a
        parameterization array at all, 503 no validation plane in this deployment.
        """
        req = body or CompleteParameterizationRequest()
        try:
            result = await inbox.complete_parameterization(
                candidate_id, entries=req.entries, replace_all=req.replace
            )
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        except CompletionUnavailableError as exc:
            raise HTTPException(
                status_code=503, detail="parameterization completion unavailable"
            ) from exc
        except CompletionRaceError as exc:
            # 409, like every other "the row is not in the state you think it is" — but
            # with the reason surfaced verbatim, because the reviewer did nothing wrong
            # and the only useful next step is to re-read the row.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CompletionInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _completion_result(result)

    @app.post("/inbox/{candidate_id}/verify", dependencies=guard)
    async def verify(candidate_id: str) -> dict[str, Any]:
        """Phase-3 VERIFY: a human vouches for an auto-landed validated learning node.

        Flips `verified=true` on the landed node + envelope; requires `status=validated`. The
        response carries `node_stamped` — False when the neo4j write did not land (fail-open) — so
        the UI can prompt a re-verify.
        """
        try:
            env, node_stamped = await inbox.verify(candidate_id)
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        result = _action_result(env)
        result["node_stamped"] = node_stamped
        return result

    @app.post("/inbox/{candidate_id}/promote", dependencies=guard)
    async def promote(
        candidate_id: str, body: PromoteRequest | None = None
    ) -> dict[str, Any]:
        """Phase-3 PROMOTE: emit the MCP-format YAML for a MANUAL PR into the MCP corpus repo.

        The first promote (from `validated`, requiring `verified=true`) also moves the candidate to
        `promoted`; a re-promote re-emits the same YAML with no status move. Optional body:
        `doc_id`/`title` knowledge refinements — `id` can NEVER be overridden. Returns the YAML plus
        suggested PR metadata; no git here.
        """
        req = body or PromoteRequest()
        try:
            emit = await inbox.promote(
                candidate_id, doc_id=req.doc_id, title=req.title
            )
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        except (ValueError, BlueprintParseError) as exc:
            # A malformed/non-landable validated candidate can't be serialized to MCP YAML
            # (no generalization, an empty knowledge statement, or a malformed structure).
            # This is an unprocessable candidate, not a client error — 422, fail-loud
            # (never a 500 with a stack trace), mirroring the service's mapped style.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return emit.to_wire()

    if driver is not None:

        @app.on_event("shutdown")
        async def _close_driver() -> None:
            await driver.close()

    return app
