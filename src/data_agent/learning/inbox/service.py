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
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from data_agent.runtime.blueprint.models import BlueprintParseError

from ..candidate.memory_candidate_store import InMemoryCandidateStore
from ..candidate.models import CandidateStatus
from ..mint import (
    MAX_QUESTION_CHARS,
    MintConflictError,
    MintInputError,
    MintRequest,
    MintResponseError,
    MintUnavailableError,
)
from ..observability import get_learning_tracer
from ..promotion.scheduler import PromotionScheduler
from ..revise import ForbiddenTemplateEditError, ReviserUnavailableError
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
    # §C.5 — the ACCEPTED SQL, when the reviewer is applying an assistant REWRITE of it. Empty
    # (or equal to the query already on the candidate) means the ordinary path: this field is
    # additive, and a client that never sends it behaves exactly as before.
    #
    # Non-empty and DIFFERENT switches the whole operation: the accepted SQL is replaced, the
    # snapshot is stamped `authored=True`, `replace` is forced true (every old entry describes
    # the old query), and the candidate can never auto-land. Typed as loosely as `entries` and
    # for the same reason — the checks that decide whether this is a usable query are the ones
    # downstream that will READ it, and a second vocabulary here would give the reviewer two
    # error messages for one mistake.
    sql: str = ""


class TrialRunRequest(BaseModel):
    """The trial-run body: one value per slot the template references.

    Typed as loosely as the values it binds — they are literals a reviewer chose, and the
    template binder validates them the same way a live `runBlueprint` would. A schema here
    would be a second vocabulary for the same mistake.
    """

    bindings: dict[str, Any] = {}
    # ⚠ THE REVIEWER'S OWN WAREHOUSE TOKEN, borrowed for one request. This service never mints
    # one for a trial — see `ReviewInbox.trial_run`. It is used and dropped: never persisted,
    # never logged, and never echoed back on the response.
    token: str = ""


class ApproveRequest(BaseModel):
    """The approve body: the reviewer's warehouse token, borrowed for the golden replay.

    Used and dropped — never persisted, never logged, never echoed on the response.
    """

    token: str = ""


class AttestScanRequest(BaseModel):
    """The leakage-override body: WHY this finding is a false positive.

    `note` is required and non-blank at the route, not optional-with-a-default. An override
    with no stated reason is not an audit trail — and this is the one action on the surface
    that lets a human step past a D17 gate, so the record of why has to exist before it does.
    """

    note: str = ""


class ReviseParameterizationRequest(BaseModel):
    """The REVISE body: a reviewer's sentence about what is wrong.

    One field, and free text, because that is the whole interface: the reviewer says what they
    would say to a colleague ("the register_type predicate spans two rules — inline it") and the
    reviser turns it into entries. Nothing here reaches a store; the proposal comes back for the
    reviewer to apply through `complete`, which stays the only write path.

    `allow_sql` is the §C.5 opt-in and the ONE reason there is a second field. DEFAULT FALSE and
    never inferred from the feedback text: it licenses the assistant to return a replacement
    query, which costs the candidate its provenance and its ability to auto-land, and a licence
    like that has to be something a reviewer TICKED rather than something a sentence implied.
    """

    feedback: str = ""
    allow_sql: bool = False


class MintBlueprintRequest(BaseModel):
    """The MINTING body: what an expert knows about a question they can already answer.

    Deliberately loose about `steps` and `assumptions` — they are prose, and the model is the
    thing that reads them. What IS pinned is `tables`, because the drafting brief's closed column
    list is built from it, and `sql_mode`, because it decides whether the model is offered a tool
    that can write SQL at all. Both are re-checked in `MintRequest.__post_init__`, which is the
    validator that matters; this class exists so FastAPI can parse the body, not so it can be the
    second place the rules live.
    """

    question: str = ""
    tables: list[str] = []
    steps: list[str] = []
    assumptions: list[str] = []
    sql: str = ""
    sql_mode: str = "none"
    # ⚠ THE COMPOSITE DAG. Its absence made multi-step minting UNREACHABLE over HTTP without
    # failing anything: pydantic drops an undeclared field, so `model_dump()` never carried the
    # steps, `MintRequest.from_doc` saw none, and every composite submission was quietly minted
    # as a single blueprint. The page sent them; nothing received them.
    #
    # Typed loosely on purpose — `MintNode` owns the real validation (output-name grammar,
    # backward-only edges, the node cap derived from `check_dag`), and a second schema here
    # would be a second vocabulary for the same mistake.
    nodes: list[dict[str, Any]] = []


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
        # The generalized template pre-split into text / `{slot}` parts, so the card can render
        # slot chips without re-spelling `SLOT_TOKEN` in JS. Derived from the REDACTED
        # `payload_view` (see `InboxItem._template_parts`) — it carries inline literals, so it
        # is exactly as entity-sensitive as the payload beside it and is redacted by the same
        # pass. `[]` for every candidate without a template.
        "template_parts": [dict(part) for part in item.template_parts],
        # The S4 parameterization judge's verdict (design §D), or null when it did not run.
        # Part of the EXACT wire shape for the same reason `decline` is: a client cannot
        # branch on a field it cannot know exists. In phase D-1 this is the ONLY thing the
        # judge does that a human ever sees, and the reviewer's agree/disagree with it — via
        # the ordinary approve/reject they were going to make anyway — is half the
        # measurement the whole phase exists to collect.
        "param_judge": item.param_judge.to_doc() if item.param_judge is not None else None,
        # The reviewer override, when one is in force. ENTITY-FREE by construction — a
        # digest, a timestamp, a count and the reviewer's own note; never a span, which is
        # the value being withheld. Null when absent OR when a stored attestation no longer
        # binds to the current finding, so the card can never show "attested" for a verdict
        # that has since changed.
        "leakage_attestation": (
            item.leakage_attestation.to_doc()
            if item.leakage_attestation is not None
            else None
        ),
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
        # §C.5 — whether THIS request replaced the accepted SQL. Off the result, not derived
        # from the payload: a candidate rewritten yesterday carries the same `sql_rewrite`
        # record as one rewritten just now, and what the surface has to confirm is what the
        # reviewer just did.
        "sql_rewritten": result.sql_rewritten,
    }


def _map_transition_error(exc: InboxTransitionError) -> HTTPException:
    """Map an `InboxTransitionError` to the contract §2/§6 status codes.

      * message contains "not found"          → 404 (unknown id)
      * a HELD approve for a malformed payload → 409 (repair in the store, or reject)
      * a HELD approve for landing-unavailable → 503 ("landing plane unavailable")
      * anything else                          → 409 (illegal transition / held
                                                 approve — reason surfaced verbatim)
    """
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    # BEFORE the 503 branch, and the order is the whole point: `landing_invalid` is a
    # DETERMINISTIC landing refusal — the candidate's payload cannot be mapped onto a seed
    # — so calling it "landing plane unavailable" would send the reviewer to check a plane
    # that is perfectly healthy and invite an approve-retry that can never succeed. (It
    # would also match the 503 test below if that ever grew a looser substring; keeping
    # this first makes the intended precedence explicit rather than incidental.) 409: the
    # request cannot be satisfied in the candidate's current state. The detail names the
    # two actions that exist — repair the stored payload (there is no payload-edit
    # endpoint; the store-side repair script is the tool) or reject — because "revise"
    # would point at the parameterization reviser, which cannot touch payload keys.
    if "landing_invalid" in message:
        return HTTPException(
            status_code=409,
            detail=(
                "candidate payload cannot be landed (malformed for its type); repair it "
                "in the store or reject it — approving again will not help"
            ),
        )
    # `landing_entity_leak` gets NO branch: it falls through to the 409-verbatim default,
    # which is right — the reason names the D17 tripwire that fired, and that is exactly
    # what the reviewer and the log both need to see.
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


@dataclass(frozen=True)
class _JudgeDeps:
    """The two collaborators the completion plane's judges share: a model client and an audit store.

    ⚠ BUILT ONCE, PASSED IN. Each judge used to construct its own, which meant a deployment with
    both switched on opened TWO Couchbase audit connections and TWO model clients in a process
    that needs one of each — and, worse, wrote its two kinds of verdict about the same candidate
    through two different store objects. The consumer has always shared them (`build_learning_
    consumer` builds one of each and hands them to both `build_coverage_judge` and
    `build_param_judge`); this is the same shape at the second composition root.

    LAZY: `build` returns `None` when nothing needs them, so a deployment with both judges off —
    the default — opens neither connection.
    """

    model_client: Any
    audit_store: Any

    @classmethod
    def build(cls, learning_settings: Any) -> _JudgeDeps | None:
        """The pair, or `None` when they cannot be made. Never raises."""
        api_key = getattr(learning_settings, "learning_extractor_api_key", "")
        if not api_key:
            _logger.warning(
                "inbox service: a completion-path judge is enabled but no extractor API key is "
                "configured — human-completed, minted and SQL-rewritten candidates will skip it"
            )
            return None
        try:
            from data_agent.runtime.model.openai_client import build_openai_model_client

            from ..audit.couchbase_audit_store import CouchbaseAuditStore

            return cls(
                model_client=build_openai_model_client(
                    api_key=api_key,
                    model=getattr(learning_settings, "learning_extractor_model", ""),
                    base_url=getattr(learning_settings, "learning_extractor_base_url", ""),
                ),
                audit_store=CouchbaseAuditStore(learning_settings),
            )
        except Exception:  # noqa: BLE001 — a judge may not break the completion plane
            _logger.warning(
                "inbox service: the completion-path judges could not be built; completions "
                "still re-validate and re-run the write router without them",
                exc_info=True,
            )
            return None


def _judge_deps_if_needed(learning_settings: Any) -> _JudgeDeps | None:
    """`_JudgeDeps` when EITHER completion-path judge is switched on, else `None`.

    The `if needed` is what keeps `build` lazy without either judge having to know about the
    other's switch.
    """
    wanted = getattr(learning_settings, "learning_param_judge_enabled", False) or getattr(
        learning_settings, "learning_judge_enabled", False
    )
    return _JudgeDeps.build(learning_settings) if wanted else None


def _build_completion_param_judge(
    learning_settings: Any, deps: _JudgeDeps | None = None
) -> Any:
    """The S4 parameterization judge for the COMPLETION path, or `None`.

    ⚠ THE COMPLETION PATH NEEDS ITS OWN, and leaving it out is not a small omission. Design
    §D.1 puts the stage in both callers, and `build_write_router_stages` says so in its own
    docstring — because phase D-1 is a MEASUREMENT and its population must be the population it
    claims to measure. A judge wired only into the consumer would score extracted blueprints and
    never human-completed ones, which is precisely the biased sample the measurement is supposed
    to avoid.

    LAZY AND FAIL-SOFT. Nothing here is constructed unless the judge is switched on, so a
    deployment with it off (the default) gains no Couchbase audit connection and no model client
    in this process. Every missing precondition degrades to `None` with a log line, because a
    completion that re-validates is worth strictly more than an observation about it.
    """
    if not getattr(learning_settings, "learning_param_judge_enabled", False):
        return None
    deps = deps if deps is not None else _JudgeDeps.build(learning_settings)
    if deps is None:
        _logger.warning(
            "inbox service: the parameterization judge is enabled but its model client and "
            "audit store could not be built — human-completed candidates will be MISSING "
            "from the phase-D-1 dataset"
        )
        return None
    from ..factory import build_param_judge

    return build_param_judge(
        learning_settings,
        model_client=deps.model_client,
        audit_store=deps.audit_store,
    )


def _build_reviser(learning_settings: Any, runtime_settings: Any) -> Any:
    """Build the LLM typing aid for the fail-to-review form, or `None`.

    FAIL-SOFT at every precondition, and that is the difference between this and
    `_build_completer`: without a completer a reviewer CANNOT clear a form (so its absence 503s
    loudly); without a reviser they simply type the entries themselves, exactly as they did
    before this existed. So a missing key, a missing catalog or an unreadable one all degrade to
    "no assistant", logged at INFO rather than WARNING.

    The catalog is the GROUNDING (rule ids and bindable columns) and comes from the SAME frozen
    snapshot the completer uses — a reviser offering rule ids the completer's validator does not
    recognize would propose entries that are rejected on apply, which is worse than proposing
    none.
    """
    if not getattr(learning_settings, "learning_revise_enabled", False):
        _logger.info(
            "inbox service: LLM-assisted revision is OFF (LEARNING_REVISE_ENABLED=false); "
            "the parameterization form still accepts entries directly"
        )
        return None
    api_key = getattr(learning_settings, "learning_extractor_api_key", "")
    if not api_key:
        _logger.info(
            "inbox service: LLM-assisted revision is enabled but no extractor API key is "
            "configured — no assistant; the form still works"
        )
        return None

    import json

    from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
    from data_agent.runtime.model.openai_client import build_openai_model_client

    from ..extractor.grounding import known_rule_ids_from_catalog
    from ..revise import BlueprintReviser

    try:
        path = runtime_settings.catalog_fixture_file()
        with path.open(encoding="utf-8") as fh:
            catalog = json.load(fh)["catalog"]
    except (OSError, ValueError, KeyError, TypeError):
        _logger.info(
            "inbox service: the semantic catalog snapshot could not be read, so "
            "LLM-assisted revision is disabled (an ungrounded reviser would invent rule ids "
            "and column bindings the validator then rejects)",
            exc_info=True,
        )
        return None

    model = (
        getattr(learning_settings, "learning_revise_model", "")
        or getattr(learning_settings, "learning_extractor_model", "")
    )
    _logger.info("inbox service: LLM-assisted revision is ON (model=%s)", model)
    # The tracer the daemon preamble already installed globally (`configure_daemon_process`
    # calls OTel's `set_tracer_provider`, which is what `set_global_tracer_provider` aliases).
    # Read from the global rather than threaded down from `main()`, because
    # `create_inbox_app` is built inside the event loop and this is the only consumer on the
    # path. With no OTLP endpoint the preamble installs a NO-OP provider, so an untraced
    # deployment is unchanged rather than special-cased.
    from opentelemetry import trace as _otel_trace

    tracer = get_learning_tracer(_otel_trace.get_tracer_provider())
    return BlueprintReviser(
        model_client=build_openai_model_client(
            api_key=api_key,
            model=model,
            base_url=getattr(learning_settings, "learning_extractor_base_url", ""),
        ),
        known_rules=known_rule_ids_from_catalog(catalog),
        catalog_schema=build_sqlglot_schema_from_catalog(catalog),
        timeout_seconds=getattr(learning_settings, "learning_revise_timeout_seconds", 30.0),
        model=model,
        tracer=tracer,
        # ⚠ The verbose payload on `learning.revise` is a human's free text plus model prose
        # about the UNREDACTED accepted SQL. Same switch, same posture, same D51 standard as
        # every other entity-bearing span on this plane.
        trace_verbose=getattr(learning_settings, "learning_trace_verbose", False),
    )


def _build_minter(
    learning_settings: Any,
    runtime_settings: Any,
    *,
    completer: Any,
    prior_art: Any = None,
) -> Any:
    """Build the hand-authoring plane, or `None` when it cannot be grounded.

    THREE THINGS ARE REQUIRED and each absence is a different kind of no:

      * a COMPLETER — the minter writes through it, so without one it could only file review
        rows carrying no generalization and an unsettled scan, which every downstream guard
        refuses. That is a row a reviewer cannot act on and cannot distinguish from an unread
        one, so the honest answer is to offer no page at all;
      * an API KEY — every mode calls a model, `exact` included (something has to classify the
        literals), so unlike the reviser there is no degraded typing-aid version of this;
      * the CATALOG — the drafting brief's closed column list is what stops a model inventing
        a column the rewrite then rejects, blaming the expert for a table they never chose.

    FAIL-OPEN and logged, exactly like `_build_reviser`: no minter means `POST /inbox/mint`
    answers 503 and the page says so, rather than the service refusing to start.
    """
    api_key = getattr(learning_settings, "learning_extractor_api_key", "") or getattr(
        runtime_settings, "openai_api_key", ""
    )
    if completer is None or not api_key:
        _logger.info(
            "inbox service: blueprint MINTING is off (%s)",
            "no completion plane" if completer is None else "no model API key",
        )
        return None

    import json

    from data_agent.runtime.model.openai_client import build_openai_model_client

    from ..extractor.grounding import known_rule_ids_from_catalog
    from ..mint import BlueprintMinter

    try:
        path = runtime_settings.catalog_fixture_file()
        with path.open(encoding="utf-8") as fh:
            catalog = json.load(fh)["catalog"]
    except (OSError, ValueError, KeyError, TypeError):
        _logger.info(
            "inbox service: the semantic catalog snapshot could not be read, so blueprint "
            "MINTING is disabled (an ungrounded draft invents columns the validator rejects)",
            exc_info=True,
        )
        return None

    model = (
        learning_settings.learning_mint_model
        or getattr(learning_settings, "learning_revise_model", "")
        or getattr(learning_settings, "learning_extractor_model", "")
    )
    _logger.info("inbox service: blueprint MINTING is ON (model=%s)", model)
    return BlueprintMinter(
        model_client=build_openai_model_client(
            api_key=api_key,
            model=model,
            base_url=getattr(learning_settings, "learning_extractor_base_url", ""),
        ),
        completer=completer,
        known_rules=known_rule_ids_from_catalog(catalog),
        catalog_columns=_catalog_columns(catalog),
        # The duplicate WARNING. Optional by design: absent, the page still mints — which is
        # the right degrade for something that was never a gate.
        prior_art=prior_art,
        timeout_seconds=learning_settings.learning_mint_timeout_seconds,
    )


def _catalog_columns(catalog: Any) -> tuple[str, ...]:
    """Every `database.table.column` the catalog declares, flattened and sorted.

    DERIVED FROM `build_sqlglot_schema_from_catalog` rather than by walking the catalog shape
    again. The first version of this function walked a `catalog["tables"]` list that does not
    exist — the catalog is keyed BY `database.table` — and it did not fail: it returned an empty
    tuple, which is a legal value meaning "no grounding available", so the minting page would
    have shipped with its column list silently switched off. Reusing the canonical reader makes
    that class of mistake impossible, because the same projection already feeds the rewrite's
    column-scope check.
    """
    from data_agent.catalog.loader import build_sqlglot_schema_from_catalog

    try:
        schema = build_sqlglot_schema_from_catalog(catalog)
    except (AttributeError, TypeError):
        _logger.info("inbox service: catalog columns could not be projected", exc_info=True)
        return ()
    return tuple(
        sorted(
            f"{table}.{column}"
            for table, columns in schema.items()
            for column in (columns or {})
        )
    )


# --- default (env-driven) construction ---------------------------------------


def _build_completer(
    learning_settings: Any,
    runtime_settings: Any,
    *,
    candidate_store: Any,
    corpus: Any,
    embedding_client: Any,
    prior_art: Any = None,
    judge: Any = None,
    semantic_scanner: Any = None,
    param_judge_deps: Any = None,
) -> ParameterizationCompleter | None:
    """Build the fail-to-review completion plane, or `None` when the catalog cannot be read.

    ⚠ THE COLLABORATORS ARE PART OF THE PARITY, NOT DECORATION ON IT. The stage ORDER has been
    shared with the consumer since `build_write_router_stages` was extracted, but the objects
    handed to those stages were not, and the same pipeline built around different collaborators
    is a different pipeline. Two checks were missing on every completed, minted and (now)
    REWRITTEN candidate:

      * `prior_art` — `dedup/stage.py`'s CROSS-TIER layer returns early without it, so the MCP
        canon and the landed learning tier are invisible and a blueprint a human just finished is
        adjudicated against the `learning_corpus` bucket alone, which this loop seeds itself;
      * `judge` — the S6 layer-3b judged near-miss, which is the one that decides whether a
        soft-similar artifact is a genuine duplicate rather than a neighbour.

    A human-finished blueprint faced STRICTLY LESS evidence than a mined one, which is exactly
    backwards for the path where hand-authored SQL enters. Both are optional and both degrade
    LOUDLY (see `_completion_collaborator_log`) — a completion that re-validates is worth more
    than one refused for want of a graph — but neither degrades silently.

    `semantic_scanner` is passed through for the same reason even though nothing wires a real one
    on either side today: the parity has to be structural, not a coincidence of two `None`s.

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

    _completion_collaborator_log(prior_art=prior_art, judge=judge)
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
            # THE SAME THREE the consumer passes, so the two callers assemble one pipeline
            # rather than two that happen to share an order.
            prior_art=prior_art,
            judge=judge,
            semantic_scanner=semantic_scanner,
            # The SECOND composition root design §D.1 names. See
            # `_build_completion_param_judge` for why the completion path needs one of its
            # own rather than inheriting the consumer's.
            param_judge=_build_completion_param_judge(learning_settings, param_judge_deps),
            include_target_specific=False,
        ),
    )


def _completion_collaborator_log(*, prior_art: Any, judge: Any) -> None:
    """Say, at build time, which dedup layers this completion plane will actually run.

    SEPARATE LINES for the two absences because they need different fixes and have different
    consequences, and NEITHER is an error: a deployment with no graph must still be able to
    complete a form. What must not happen is the degrade being invisible — the symptom of a
    missing cross-tier layer is a duplicate blueprint landing weeks later, which points nowhere
    near the cause.
    """
    if prior_art is None:
        _logger.warning(
            "inbox service: the completion plane has NO prior-art index — S6's CROSS-TIER "
            "layer cannot run, so a completed, minted or SQL-REWRITTEN blueprint is deduped "
            "against the `learning_corpus` bucket ONLY (which this loop seeds itself). The MCP "
            "canon and the landed learning tier are INVISIBLE to it, and the soft layer falls "
            "back to the O(corpus) brute-force scan. The consumer's pipeline has one; this is "
            "the parity gap."
        )
    if judge is None:
        _logger.warning(
            "inbox service: the completion plane has NO coverage judge — S6's layer-3b judged "
            "near-miss cannot run, so a soft-similar artifact is adjudicated on thresholds "
            "alone. A human-finished blueprint therefore faces less evidence than a mined one, "
            "which is backwards for the path hand-authored and rewritten SQL enters."
        )
    if prior_art is not None and judge is not None:
        _logger.info(
            "inbox service: the completion plane runs the FULL write-router evidence set "
            "(cross-tier prior art + the layer-3b coverage judge), matching the consumer's"
        )


def _build_completion_coverage_judge(
    learning_settings: Any, prior_art: Any, deps: _JudgeDeps | None = None
) -> Any:
    """The S6 layer-3b judge for the COMPLETION path, or `None`.

    ⚠ THE SAME ARGUMENT `_build_completion_param_judge` MAKES, for a stage that DECIDES rather
    than observes. The dedup judge is what separates "a genuine duplicate of something the corpus
    already holds" from "a neighbour", and a completion plane without one adjudicates that on
    similarity thresholds alone. A blueprint a human finished — or one whose SQL the assistant
    rewrote — must not be admitted on weaker evidence than one the loop mined.

    LAZY AND FAIL-SOFT, exactly like the param judge: nothing is constructed unless the
    kill-switch is on, and every missing precondition degrades to `None` with a log line, because
    a completion that re-validates is worth strictly more than the extra dedup evidence.
    `build_coverage_judge` owns the preconditions themselves (kill-switch, prior art, audit
    store) so the two composition roots cannot disagree about what "wired" means.
    """
    if not getattr(learning_settings, "learning_judge_enabled", False):
        return None
    if prior_art is None:
        _logger.info(
            "inbox service: the coverage judge is enabled but the completion plane has no "
            "prior-art index — there is nothing for a candidate to be covered BY"
        )
        return None
    deps = deps if deps is not None else _JudgeDeps.build(learning_settings)
    if deps is None:
        _logger.warning(
            "inbox service: the coverage judge is enabled but its model client and audit store "
            "could not be built — completed and rewritten candidates will skip S6's layer-3b"
        )
        return None
    from opentelemetry import trace as _otel_trace

    from ..factory import build_coverage_judge

    return build_coverage_judge(
        learning_settings,
        model_client=deps.model_client,
        judge_client_injected=False,
        audit_store=deps.audit_store,
        prior_art=prior_art,
        # THE GLOBAL PROVIDER the daemon preamble installed, read the same way `_build_reviser`
        # reads it. With no OTLP endpoint that is a NO-OP provider, so an untraced deployment is
        # unchanged rather than special-cased.
        tracer=get_learning_tracer(_otel_trace.get_tracer_provider()),
    )


def _build_inbox_from_env() -> tuple[ReviewInbox, WritePlaneMode, Any]:
    """Build the `ReviewInbox` for a standalone run. Returns `(inbox, mode, driver)`.

    When every full-plane port is configured, wires the fully-activated write plane via
    `build_promotion_write_plane`; otherwise falls back to OFFLINE dev mode (an
    `InMemoryCandidateStore` + the default unwired scheduler), where list/reject/retract work and
    a landing-approve honestly 503s. The neo4j driver is returned so the caller can close it on
    shutdown (None in offline mode).
    """
    from ..config import get_learning_settings

    learning_settings = get_learning_settings()

    from data_agent.runtime.config import get_runtime_settings

    # Vault-aware, matching the consumer and scheduler entrypoints: the ports gated
    # below are sensitive values, and a bare `RuntimeSettings()` reads env only — a
    # Vault-sourced deployment would fall into offline mode with no way to tell why.
    runtime_settings = get_runtime_settings()
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
    from ..priorart.neo4j_index import Neo4jPriorArtIndex
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
    # ONE index, THREE readers: the minter's duplicate check, the completion plane's S6
    # cross-tier layer, and the coverage judge that adjudicates its near-misses. Hoisted for the
    # reason the completer below is — two instances would open two neo4j pools and could
    # disagree about what the corpus contains within one candidate's lifetime.
    prior_art = Neo4jPriorArtIndex(
        driver=neo4j_driver,
        embedding_client=embedding_client,
        expected_model=runtime_settings.embedding_model,
        database=runtime_settings.neo4j_database,
    )
    # ONE model client and ONE audit store for BOTH completion-path judges — built lazily, so a
    # deployment with both switched off (the default) opens neither. Two judges constructing
    # their own meant two Couchbase connections in a process that needs one, and two kinds of
    # verdict about the same candidate written through different store objects.
    judge_deps = _judge_deps_if_needed(learning_settings)
    # HOISTED so the minter can share the EXACT instance rather than build a second one.
    # Two completers would mean two copies of the write-router stages over one store, and a
    # minted candidate adjudicated by a different instance than a reviewed one is a difference
    # nobody would notice until the two disagreed about a dedup verdict.
    completer = _build_completer(
        learning_settings,
        runtime_settings,
        candidate_store=candidate_store,
        corpus=corpus,
        embedding_client=embedding_client,
        # ⚠ THE PARITY WITH THE CONSUMER'S PIPELINE. Without these two the completion plane
        # assembled the same stage ORDER around a strictly weaker evidence set — no cross-tier
        # dedup, no layer-3b judge — for every completed, minted and SQL-REWRITTEN candidate.
        prior_art=prior_art,
        judge=_build_completion_coverage_judge(learning_settings, prior_art, judge_deps),
        param_judge_deps=judge_deps,
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
        neo4j_database=runtime_settings.neo4j_database,
        embedding_client=embedding_client,
        model_id=runtime_settings.embedding_model,
        # The fail-to-review completion plane: re-validation + the blueprint half of the
        # write router, over the SAME candidate store this inbox reads.
        completer=completer,
        # The LLM typing aid for that same form (design §C). Independent of the completer's
        # own switch: it is human-gated and human-committed, so it can be enabled much
        # earlier — and its absence costs a convenience, not a capability.
        reviser=_build_reviser(learning_settings, runtime_settings),
        # The hand-authoring plane. Gated on the completer because it WRITES through it: a
        # minter without one would file review rows that can never be approved.
        minter=_build_minter(
            learning_settings,
            runtime_settings,
            completer=completer,
            # The SAME instance the completion plane's dedup stage and coverage judge hold,
            # built from the SAME neo4j driver and embedding client the rest of this process
            # uses, so the duplicate check reads the corpus the promotion path writes.
            prior_art=prior_art,
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

    @app.get("/inbox/mint/schema", dependencies=guard)
    async def mint_schema() -> dict[str, Any]:
        """The tables and columns the minting form may offer, or `available: false`.

        A 200 either way. "This deployment cannot mint" is a fact about the page, not an error
        the expert caused, and it lets the UI say so in place of the form rather than rendering
        a form whose submit button always fails.
        """
        return inbox.mint_schema()

    @app.post("/inbox/mint/prior_art", dependencies=guard)
    async def mint_prior_art(body: MintBlueprintRequest | None = None) -> dict[str, Any]:
        """What already exists for this question. READS ONLY — nothing is drafted or written.

        Separate from `mint` so the page can warn BEFORE the expert pays for a drafting turn.
        A 200 with an empty list either way: "nothing matched" and "the index is down" are both
        non-events for a warning, and the minter logs the difference.
        """
        req = body or MintBlueprintRequest()
        # CAPPED HERE because this is where the read happens: the question goes straight to an
        # embedding call, and unlike the mint route this one never builds a `MintRequest`, so it
        # inherits none of that model's limits. A megabyte body would become a megabyte embed.
        question = (req.question or "")[:MAX_QUESTION_CHARS]
        return {"prior_art": [dict(c) for c in await inbox.mint_prior_art(question)]}

    @app.post("/inbox/mint", dependencies=guard)
    async def mint(body: MintBlueprintRequest | None = None) -> dict[str, Any]:
        """MINT: draft a hand-authored blueprint onto the review queue.

        NOTHING IS PROMOTED. The result is a candidate the expert then works on with the
        surfaces that already exist — the card, the assistant, the trial run, approve — which is
        why this returns a `candidate_id` rather than a blueprint. A draft that does not
        validate is a 200 with `outcome="declined"` and the validator's complaint, for the same
        reason a still-incomplete completion is: the row exists, the complaint is on it, and the
        next step is on the page rather than in an error banner.

        400 a form that cannot be drafted from, 502 a model that answered off-contract,
        503 no minting plane in this deployment.
        """
        req = body or MintBlueprintRequest()
        try:
            request = MintRequest.from_doc(req.model_dump())
        except MintInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            result = await inbox.mint_blueprint(request)
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        except MintConflictError as exc:
            # 409: the submission is well-formed, but its row already exists.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MintInputError as exc:
            # 400: something about the form itself is wrong and the expert can fix it.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CompletionRaceError as exc:
            # 409, the SAME mapping the completion route gives it. Two concurrent identical
            # submissions can both pass the pre-flight check and both enter the completer; the
            # loser's guarded write raises here, and an unmapped raise would 500 a race the
            # system handled correctly.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MintUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except MintResponseError as exc:
            # 502 and VERBATIM. The expert did nothing wrong and the deployment is not broken —
            # a model answered against a contract this system does not have, and the sentence
            # saying so is more useful than "the assistant failed".
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.to_doc()

    @app.get("/inbox/health", dependencies=guard)
    async def inbox_health() -> dict[str, str]:
        return {"write_plane": app.state.write_plane}

    @app.post("/inbox/{candidate_id}/approve", dependencies=guard)
    async def approve(
        candidate_id: str, body: ApproveRequest | None = None
    ) -> dict[str, Any]:
        """Approve: `in_review -> validated`, replaying the blueprint against the warehouse.

        ⚠ CARRIES THE REVIEWER'S OWN TOKEN. That replay is a real query, and this service mints
        nothing for it — the same posture the trial has, on the gate that actually promotes.
        """
        req = body or ApproveRequest()
        try:
            env = await inbox.approve(candidate_id, token=req.token or "")
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

        §C.5: a non-empty `sql` that DIFFERS from the candidate's accepted SQL applies an
        assistant REWRITE. `replace` is then forced true whatever the body said — a rewrite
        invalidates every existing entry — the candidate becomes hand-authored and can never
        auto-land, and the response carries `sql_rewritten: true`. A `sql` equal to the query
        already on the candidate is not a rewrite and changes nothing.
        """
        req = body or CompleteParameterizationRequest()
        try:
            result = await inbox.complete_parameterization(
                candidate_id,
                entries=req.entries,
                replace_all=req.replace,
                rewritten_sql=req.sql,
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

    @app.post("/inbox/{candidate_id}/revise", dependencies=guard)
    async def revise(
        candidate_id: str, body: ReviseParameterizationRequest | None = None
    ) -> dict[str, Any]:
        """FAIL-TO-REVIEW REVISE: ask the assistant for entries. WRITES NOTHING.

        Returns a PROPOSAL — entries, a replace flag, a rationale and a slot-level diff — which
        the reviewer then applies through `complete`. That two-step is the design (§C.3), not an
        oversight: `complete` stays the ONLY write path into a candidate's payload, so a model's
        output faces the identical `to_candidate` re-validation and write-router stages that a
        hand-typed array faces.

        "The assistant had no suggestion" is a 200 with empty `entries` and a `reason`, for the
        same reason a still-incomplete completion is a 200: the reviewer did nothing wrong, and
        the useful next step belongs on the page rather than in an error banner. 404 unknown id,
        409 wrong status, 422 the model wrote a field this request had no contract for, 503 no
        reviser in this deployment.

        §C.5: with `allow_sql: true` the assistant MAY return a complete replacement query. The
        200 then carries `sql_changed: true`, the `sql` itself, a `caution` to render verbatim,
        and `replace: true` (forced). Still a proposal — nothing is written, and the reviewer
        applies it through `complete`/`apply_revision` with the same `sql`. A rewrite the system
        cannot parse as a read-only SELECT is a 200 with a `reason` and no `sql`, like every
        other "the assistant had no suggestion". A COMPOSITE candidate is a 422 naming the
        reason: its SQL lives on its nodes, so there is no single query to replace.
        """
        req = body or ReviseParameterizationRequest()
        try:
            proposal = await inbox.propose_revision(
                candidate_id, feedback=req.feedback, allow_sql=req.allow_sql
            )
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        except ReviserUnavailableError as exc:
            raise HTTPException(
                status_code=503, detail="LLM-assisted revision unavailable"
            ) from exc
        except ForbiddenTemplateEditError as exc:
            # 422, surfaced VERBATIM. The model worked against a contract this system does not
            # have — the template is DERIVED from the accepted query, never authored — and a
            # reviewer reading that sentence learns something true about the system rather than
            # "the assistant failed".
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return proposal.to_wire()

    @app.post("/inbox/{candidate_id}/apply_revision", dependencies=guard)
    async def apply_revision(
        candidate_id: str, body: CompleteParameterizationRequest | None = None
    ) -> dict[str, Any]:
        """Apply a revision to a candidate already under review (`in_review`).

        Distinct from `complete`, which fills a FORM and appends. This REPLACES, which is both
        the right semantics for correcting a role and idempotent — see
        `ReviewInbox.apply_revision`. The request body's `replace` field is IGNORED here; the
        operation has only one mode by design. The body's `sql` is NOT ignored: a §C.5 rewrite
        applies here exactly as it does on `complete`, and needs no forcing because this verb is
        already replace-only.

        Same outcome vocabulary as `complete`: a revision that still does not validate answers
        200 with `outcome="declined"` and the fresh reason, because that is the result the
        reviewer needs in order to make the next attempt.
        """
        req = body or CompleteParameterizationRequest()
        try:
            result = await inbox.apply_revision(
                candidate_id, entries=req.entries, rewritten_sql=req.sql
            )
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        except CompletionUnavailableError as exc:
            raise HTTPException(
                status_code=503, detail="parameterization completion unavailable"
            ) from exc
        except CompletionRaceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CompletionInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _completion_result(result)

    @app.post("/inbox/{candidate_id}/trial_run", dependencies=guard)
    async def trial_run(
        candidate_id: str, body: TrialRunRequest | None = None
    ) -> dict[str, Any]:
        """TRIAL RUN: execute this blueprint with reviewer-chosen slot values.

        The question a reviewer has before approving — "does it still run, and is the shape
        what I expect, with values I picked" — which nothing answered. The promotion gate
        replays with SYNTHETIC samples, which is how a badly-typed `period` sample went
        unnoticed until it blocked every approve.

        ⚠ STRUCTURE, NEVER ROWS: columns, a row count and a distinct-grain count. That is
        `replay.py`'s rule verbatim, and it holds here for the same reason — this surface is
        access-controlled for redacted CANDIDATES, and returning warehouse rows would turn it
        into a data-browsing surface as a side effect of adding a button.

        A run that could not happen answers 200 with `ok=false` and a machine reason
        (`no_template`, `missing_bindings`, `no_uses_scope`, `warehouse_error`): the reviewer
        did nothing wrong, and the reason is the next step rather than an error banner.
        """
        req = body or TrialRunRequest()
        try:
            result = await inbox.trial_run(
                candidate_id, bindings=req.bindings or {}, token=req.token or ""
            )
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        return result.to_wire()

    @app.post("/inbox/{candidate_id}/attest_scan", dependencies=guard)
    async def attest_scan(
        candidate_id: str, body: AttestScanRequest | None = None
    ) -> dict[str, Any]:
        """LEAKAGE OVERRIDE: a reviewer attests that a finding is a false positive.

        The scanner's verdict is UNCHANGED — the attestation is stored beside it, bound to the
        exact findings it covers, and lapses if they change. It clears one gate (the assistant
        and the decline-detail display); it does NOT make the candidate auto-promotable.

        422 on a blank `note`: this is the only action that steps past a D17 gate, so the
        reason is part of the operation rather than an optional extra. 409 when there is no
        settled non-pass verdict to attest to.
        """
        req = body or AttestScanRequest()
        note = (req.note or "").strip()
        if not note:
            raise HTTPException(
                status_code=422,
                detail=(
                    "a leakage override requires a note saying why the finding is a false "
                    "positive — it is the audit trail for stepping past an entity gate"
                ),
            )
        try:
            env = await inbox.attest_scan(candidate_id, note=note)
        except InboxTransitionError as exc:
            raise _map_transition_error(exc) from exc
        return _action_result(env)

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
