"""promotion/scheduler.py — the S9 promotion scheduler (D29/D30, §7.2).

A STANDALONE cron-scanned process, NOT a `CandidateStage`: each cycle it lists candidates
by status, runs golden replay + the D43 drift probes, and advances `status` + stamps `drift`
(Contract E). THE SCHEDULER IS A ROUTER, NOT A PROMOTER — the auto path ends at `in_review`,
and `→ validated` happens ONLY through `apply_human_decision`, which is therefore the only
edge that writes content to the corpus. Fail-closed throughout: one bad candidate never
aborts the cycle, and the kill-switch is read FRESH every cycle (D58c). The golden replay is
rate-limited (the cheap guards are not), and the scan window ROTATES by `last_scanned_at`,
so a bounded `scan_limit` cannot starve newly extracted candidates.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime

from data_agent.timeutil import now_iso as _now_iso

from ..candidate.generalization import BlueprintGeneralization
from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.redaction import entity_spans, strip_entity_bearing
from ..candidate.store import CandidateStore
from ..candidate.verdicts import DriftStamp, LeakageVerdict
from ..config import learning_enabled
from ..observability import context_from_traceparent, land_span, promote_span
from .drift import drift_from_replay, reusable_replay_verdict, user_correction_stamp
from .landing import landing_id
from .models import (
    BLUEPRINT_TYPE,
    HUMAN_GATED_TYPES,
    CandidateDecision,
    CorpusStatusWriter,
    DependencyResolver,
    HitCountReader,
    LandingWriter,
    PromotionPolicy,
    PromotionSweep,
    RecurrenceCountReader,
    WarehouseProbe,
)
from .replay import ReplayOutcome, golden_replay

_logger = logging.getLogger(__name__)

# The artifact types the learning loop LANDS into the neo4j retrieval corpus: a
# blueprint (`:Blueprint`) and a global_knowledge chunk (`:KnowledgeChunk`, UI Slice 2).
# `schema_edit`/`user_knowledge` never land here, so a demote of them has nothing to
# re-stamp. GOVERNED CORPUS (Phase 2): a landed node is stamped `source='learning'` and
# is NOT recallable regardless of status/drift (the recall trust gate serves only
# `source='mcp'`), so the demote/re-stamp keeps the STAGING node's status coherent for a
# future Phase-3 promotion — it is no longer what gates the node out of live recall.
_LANDED_TYPES: frozenset[str] = frozenset({BLUEPRINT_TYPE, "global_knowledge"})


def _parse_clock(now_iso: str) -> datetime | None:
    """The injected clock's string as a tz-AWARE datetime, or `None` if it is not one.

    `clock` is a `Callable[[], str]`, so its output is not a guaranteed ISO-8601 string; a
    malformed string and a non-string both mean "no usable now", which every caller treats as
    "cannot judge freshness, run the real probe" — the pre-rate-limit behaviour.
    """
    if not isinstance(now_iso, str):
        return None
    try:
        parsed = datetime.fromisoformat(now_iso)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# The one value `CandidateEnvelope.route_reason` currently takes: this candidate is on
# its way back to a human because a USER CORRECTED the artifact it was promoted from.
#
# Deliberately a narrow vocabulary rather than a free-text slot. The field is RENDERED to
# a reviewer, and "route_reason" is the kind of name that attracts every subsequent
# diagnostic string somebody wants in the inbox — at which point a human is reading
# whatever the last author felt like writing.
ROUTE_REASON_USER_CORRECTED = "user_corrected"


def _is_user_correction_stamp(drift: DriftStamp) -> bool:
    """True iff this drift stamp is the one `user_correction_stamp` writes.

    Identified STRUCTURALLY (`suspect` + no probes + no failed probe) rather than by a marker
    field, because that is exactly what the stamp IS. It has to be read BEFORE Guard 3, which
    replays and overwrites `drift` with a `clean` verdict — and a passing replay is the NORMAL
    outcome here, because a correction is about a VALUE while the replay is structure-only by
    design (D98).
    """
    return drift.status == "suspect" and not drift.probes and drift.failed_probe is None


def _entity_scan_is_actionable(env: CandidateEnvelope) -> bool:
    """True iff this candidate's leakage verdict is one the D17 machinery can ACT on.

    DERIVED FROM THE OPERATION, not from a field name: both entity defenses on the approve path
    consume `entity_spans(env)` — the strip REMOVES those spans, the landing writer's tripwire
    RE-CHECKS they are gone — so an EMPTY span set disables both layers, and the only question
    is whether the emptiness is EXPLAINED. The rule is therefore neither "settled" nor "clean"
    but EITHER A CLEAN PASS, OR A FINDING THAT LOCALIZES ITSELF: a settled finding with no hits
    and an unsettled `pending` scan are the same failure in different clothes. The first is
    reachable with the shipped scanner — `gate._decide` returns `reroute`/`quarantine` without
    consulting whether `hits` is empty. Deliberately a DIFFERENT predicate from
    `_entity_scan_is_clean`, which the AUTOMATIC edge uses because nobody is looking there.
    """
    scan = env.entity_scan
    if not LeakageVerdict.is_settled(scan):
        return False
    if scan.get("result") == "pass":
        return True
    return bool(entity_spans(env))


def _entity_scan_is_clean(env: CandidateEnvelope) -> bool:
    """True iff the S5 leakage gate SETTLED a `pass` verdict (R5). An unsettled
    (`pending`) scan or any non-`pass` result fails closed — never auto-promotable."""
    scan = env.entity_scan
    return LeakageVerdict.is_settled(scan) and scan.get("result") == "pass"


class PromotionScheduler:
    """The cron-scanned promotion scheduler. Stateless per cycle; every dependency
    is injected so Layer-1 fakes and the live stack use the identical path."""

    def __init__(
        self,
        store: CandidateStore,
        *,
        probe: WarehouseProbe,
        hit_counts: HitCountReader,
        # The SOFT (intent-similarity) recurrence count (plan §4). Optional: absent ⇒ the
        # recurrence term reads 0, which at the shipped `recurrence_weight = 0.0` is
        # arithmetically identical to having one wired. Pass the SAME object as
        # `hit_counts` (`CouchbaseBlueprintCorpus` duck-types both) — the two counts are
        # summed, so they must address the same artifacts.
        recurrence_counts: RecurrenceCountReader | None = None,
        policy: PromotionPolicy | None = None,
        dependency_resolver: DependencyResolver | None = None,
        # S9-activation landing writer + gate (§3/§4). A blueprint becomes RECALLABLE
        # only when it LANDS in the neo4j retrieval corpus. Slice 2 wires a REAL
        # `landing_writer`: on the `→ validated` edge the scheduler LANDS FIRST, then
        # CAS-writes `status = validated` (`_land_and_promote`) — "not landed ⇒ not
        # validated" (§3.1). When `require_landing` is set but NO writer is wired
        # (the Slice-1 dormant state), a blueprint that passes EVERY guard (incl. the
        # real replay gate) HOLDS `landing_unavailable` instead of promoting — a real
        # probe with no landing writer would validate a never-recallable blueprint (the
        # silent gap). Both default OFF so today's callers are unchanged.
        landing_writer: LandingWriter | None = None,
        require_landing: bool = False,
        # PriorArtIndex Slice 2 — the corpus-artifact status write-back. The TERMINAL
        # transitions (reject/retract) stamp the `learning_corpus` artifact so the dedup
        # prior-art read stops surfacing an idea a human explicitly declined. Optional
        # and fail-open: absent ⇒ a no-op, exactly as before this slice, so no existing
        # caller changes behaviour. `CouchbaseBlueprintCorpus` duck-types it, which is
        # why the entrypoint can pass the SAME object it already passes as `hit_counts`.
        corpus_status: CorpusStatusWriter | None = None,
        clock: Callable[[], str] = _now_iso,
        tracer: object | None = None,
        trace_verbose: bool = False,
    ) -> None:
        self._store = store
        self._probe = probe
        self._hit_counts = hit_counts
        self._recurrence_counts = recurrence_counts
        self._policy = policy or PromotionPolicy()
        self._deps = dependency_resolver
        self._landing_writer = landing_writer
        self._require_landing = require_landing
        self._corpus_status = corpus_status
        self._clock = clock
        # Tracer seam (mirrors the consumer's): when wired, the promote/land edges
        # emit `learning.promote`/`learning.land` spans STARTED under the candidate's
        # propagated trace context, so a session's promotion CONTINUES its ONE trace.
        # `trace_verbose` (D25 gate) toggles the entity-bearing attrs. No tracer ⇒
        # nullcontext no-op (baseline behavior byte-identical).
        self._tracer = tracer
        self._trace_verbose = trace_verbose

    @property
    def store(self) -> CandidateStore:
        """The candidate store this scheduler reads + CAS-writes, exposed read-only.

        So a composition root can pin the inbox to the SAME instance: a split-brain store would let
        the inbox read one store while this scheduler writes another (stale-envelope approve).
        """
        return self._store

    @property
    def policy(self) -> PromotionPolicy:
        """The policy this scheduler routes on, exposed read-only for the same reason `store` is.

        `build_promotion_plane` pins the inbox to it, so the threshold that puts work into the
        review queue and the cutoff that decides whether a human ever sees that work can never end
        up configured independently.
        """
        return self._policy

    # -- the cron cycle -------------------------------------------------------

    async def run_once(self) -> PromotionSweep:
        """One scan: advance eligible `candidate`s, re-check every `validated`.

        Kill-switch FIRST (D58c), read uncached per cycle: disabled ⇒ no scan, no transition. A
        per-candidate error is logged and skipped, so the cycle never aborts on one bad candidate.
        """
        if not learning_enabled():
            return PromotionSweep(disabled=True)

        # Snapshot BOTH status lists up front, before any transition writes: a
        # candidate promoted this cycle must not be re-scanned as `validated` in
        # the SAME cycle (it lands in the store at `validated` immediately). The
        # next cycle re-checks it via the drift probes.
        # ROTATING read (`order_by="last_scanned_at"`, never-scanned FIRST) — see the
        # module docstring. With `created_at ASC` a permanently-held candidate held its
        # slot in the bounded window for its entire lifetime, so a store with more than
        # `scan_limit` held candidates silently stopped examining new ones.
        candidates = await self._store.list_by_status(
            CandidateStatus.CANDIDATE,
            limit=self._policy.scan_limit,
            order_by="last_scanned_at",
        )
        validated = await self._store.list_by_status(
            CandidateStatus.VALIDATED,
            limit=self._policy.scan_limit,
            order_by="last_scanned_at",
        )

        decisions: list[CandidateDecision] = []
        for env in candidates:
            decisions.append(await self._guard(self._advance_candidate, env))
        for env in validated:
            decisions.append(await self._guard(self._recheck_validated, env))

        return PromotionSweep(decisions=tuple(decisions))

    async def _guard(self, fn, env: CandidateEnvelope) -> CandidateDecision:
        """Run one per-candidate handler, then advance its scan cursor — ALWAYS.

        The cursor write is in the `finally` so a candidate that raises does not keep its cursor,
        stay pinned at the front of the rotation, and re-raise every cycle forever. Stamping AFTER
        the handler is what makes it clobber-free: `touch_scanned` writes the single cursor path on
        whatever the CURRENT stored document is, so it cannot undo the handler's own `put` or revert
        a concurrent S7 inbox transition. A store hiccup here is logged and swallowed — a cursor is
        bookkeeping and must not mask the real decision.
        """
        try:
            return await fn(env)
        except Exception:  # noqa: BLE001 - one bad candidate must not abort the cycle
            _logger.exception(
                "promotion scheduler failed for candidate %s; leaving it unchanged",
                env.candidate_id,
            )
            return self._hold(env, "error")
        finally:
            await self._mark_scanned(env)

    async def _mark_scanned(self, env: CandidateEnvelope) -> None:
        """Advance this candidate's rotation cursor (fail-quiet — see `_guard`)."""
        try:
            await self._store.touch_scanned(env.candidate_id, self._clock())
        except Exception:  # noqa: BLE001 - a cursor write must never fail the cycle
            _logger.warning(
                "scan-cursor write failed for candidate %s; it keeps its old position "
                "in the rotation and will be re-examined next cycle",
                env.candidate_id,
                exc_info=True,
            )

    # -- candidate → validated (the auto-promotion edge) ----------------------

    async def _advance_candidate(self, env: CandidateEnvelope) -> CandidateDecision:
        # Demote-direction CONVERGENCE re-assert (Slice 3 §9.4, review BLOCKER 1). A
        # DEMOTED landed artifact, now back in the `candidate` scan, re-stamps its landed
        # node ineligible on EVERY examination here — because the demote edge's write-back
        # is fail-open (a transient neo4j failure at demote leaves the node
        # `validated`/`clean`, i.e. still RECALLABLE, and the coalesce-default recall
        # filter does NOT catch an un-stamped node). The clean-branch re-assert only scans
        # `validated`, so it never revisits this now-`candidate` envelope — THIS is the
        # re-assert that closes the loop: it is idempotent (one MATCH/SET) and converges
        # the node to non-recallable once neo4j recovers. If a blueprint goes on to
        # RE-PROMOTE below, the land overwrites the stamp with `validated`/`clean`, so
        # running it up front is safe.
        #
        # The trigger is the STATUS, not the drift stamp. It used to be
        # `drift.status == "suspect"`, and that coupling was a silent hole: a user
        # correction demotes with `suspect`/`probes=()`, Guard 3 rightly refuses to reuse
        # that as a replay verdict and probes for real, and the replay PASSES (a
        # correction is about values, not structure — D98) — so the stamp becomes `clean`
        # and the re-assert switched itself off for ever. If neo4j had been down for the
        # demote and the blueprint sits below the hit threshold, nothing would ever
        # re-land it and the node would stay recallable indefinitely while the store said
        # `candidate`. Every envelope reaching this scan IS `candidate` by construction,
        # and a candidate-status landed node must be non-recallable whatever drift says,
        # so the status alone is the correct and un-defeatable condition.
        #
        # Cost: one idempotent Cypher per examined landed-type candidate — the same class
        # as the clean-side self-heal, which is likewise kept outside the replay rate
        # limit (only the warehouse probe is expensive). A never-landed candidate is a
        # MATCH-miss no-op.
        #
        # UI Slice 2 (knowledge convergence gap): global_knowledge LANDS too, and a
        # user-correction demote of a validated knowledge chunk re-enters THIS scan — but
        # it returns `hold: human_gated_target` below and `_recheck_validated` skips
        # non-blueprints, so without this it would NEVER re-stamp and a transiently-failed
        # write-back would leave the chunk recallable for ever.
        # `_retract_corpus`/`update_status` dispatch the Cypher by `env.type`, so this
        # re-asserts the `:KnowledgeChunk` node too.
        if env.type in _LANDED_TYPES:
            await self._retract_corpus(
                env, status=env.status, drift_status=env.drift.status
            )

        # Capture the USER-CORRECTION signal NOW, before Guard 3 replays and overwrites
        # `drift` with a `clean` verdict. A passing replay is the EXPECTED outcome here,
        # not an edge case: the correction was about a VALUE and the replay is
        # structure-only by design (D98). After Guard 3 nothing on the envelope remembers
        # this happened.
        #
        # Why it has to reach the REVIEWER and not just the sweep log: routing to review
        # is what stops the artifact silently re-landing, which makes a human the ONLY
        # thing now standing between a corrected blueprint and the corpus. The approve
        # path re-runs static validation and the golden replay, and NEITHER can see a
        # value error. Without this the reviewer adjudicates the exact artifact a user
        # flagged with strictly LESS information than the machine had a moment earlier.
        corrected = _is_user_correction_stamp(env.drift)

        # Human-gated / non-blueprint targets never auto-promote here (D58a/D18):
        # global_knowledge + schema_edit are T=∞ (S7 inbox → human approve);
        # user_knowledge auto-commits in S8. Leave them as candidate.
        if env.type != BLUEPRINT_TYPE:
            reason = (
                "human_gated_target" if env.type in HUMAN_GATED_TYPES else "not_auto_promotable"
            )
            return self._hold(env, reason)

        # Guard 0 — defense-in-depth (R5): the leakage gate must have SETTLED a
        # `pass`. An unsettled (`pending`) or non-`pass` entity_scan must never
        # auto-promote into a retrievable state — fail closed (D17/D58).
        if not _entity_scan_is_clean(env):
            return self._hold(env, "entity_scan_not_pass")

        # Guard 1 — static validation must be `ok` (S4 stamp; D52/D97).
        gen = self._generalization(env)
        if gen is None or gen.static_validation.outcome != "ok":
            return self._hold(env, "static_not_ok")

        # Guard 2 — `depends_on` unresolved ⇒ stays candidate (§11.6). Checked
        # BEFORE the (more expensive) replay so an un-landed dependency short-circuits.
        if not await self._deps_resolved(env):
            return self._hold(env, "depends_on_unresolved")

        # Guard 3 — golden replay must pass (structure, not values — D98). This is the
        # ONLY expensive guard (a JWT mint + two live warehouse queries), so it is the
        # only one that is rate-limited: within `replay_recheck_interval_seconds` the
        # stored D43 verdict is REUSED rather than re-derived. The guards above stay
        # unconditional — they are field reads, and gating them would delay picking up
        # a dependency that resolved five minutes ago for no saving at all.
        drift = self._reusable_drift(env)
        if drift is None:
            replay = await golden_replay(env, probe=self._probe)
            drift = drift_from_replay(replay, now=self._clock())
            # PERSIST the verdict even though nothing is promoting yet. This is what
            # makes the rate limit real: without a stored stamp the next cycle has
            # nothing to reuse and pays for the identical probe again. It also upgrades
            # what the reviewer sees — a candidate parked below the hit threshold now
            # carries the evidence that its template still executes.
            #
            # `stamp_drift`, NOT `put`: this is bookkeeping on a candidate S9 is not
            # transitioning, so it must not rewrite fields S9 does not own, must not
            # renew the 90-day retention TTL (which would make a parked candidate
            # immortal), and must not RESURRECT a document `supersede` deleted between
            # this cycle's scan read and now. A full-envelope upsert would do all three.
            #
            # Safe against the readers of `drift`: `silent_eligible` requires
            # `status == validated`, which a candidate by definition is not, and the
            # demote-direction re-assert above is keyed on STATUS, not on this stamp —
            # deliberately, because a passing replay overwriting a correction's `suspect`
            # with `clean` would otherwise switch that convergence loop off for good.
            env = replace(env, drift=drift)
            await self._store.stamp_drift(env.candidate_id, drift)
            if not replay.passed:
                # The LIVE reason verbatim (`no_generalization` / `bind_failed` /
                # `probe_unavailable` / the D56 verdict): far more diagnostic than the
                # probe id the stamp can carry, and unchanged from before.
                return self._hold(env, f"replay_failed:{replay.reason}")
        elif drift.status != "clean":
            # A DISTINCT tag, not the live one: an operator reading this must be able
            # to tell "the warehouse just said no" from "we are still holding yesterday's
            # no and did not ask again", because only the second one has a maximum age.
            return self._hold(env, "replay_failed:cached_suspect")

        # Guard 4 — CORROBORATION ≥ T. Below it the candidate STAYS `candidate` and is
        # re-examined on the next rotation, accruing hits and soft recurrences until it
        # crosses (or until its 90-day retention expires).
        corroboration = await self._corroboration(env)
        if corroboration < self._policy.blueprint_hit_threshold:
            return self._hold(env, "below_hit_threshold")

        # All guards pass → ROUTE TO A HUMAN (plan §4). NOT `validated`: see the module
        # docstring. Landing is deliberately NOT attempted here and the landing gate is
        # deliberately NOT consulted — `in_review` is not a recallable state, so there is
        # nothing to land and nothing for "not landed ⇒ not validated" to protect. The
        # gate still guards the `→ validated` edge inside `apply_human_decision`, which
        # is now the only edge that reaches it; a deployment with no landing writer fills
        # its inbox and then honestly refuses each approve (a 503 in the inbox service)
        # rather than silently parking the queue at `candidate` with no explanation.
        #
        # The fresh `drift` is stamped on the way through (the passing replay IS the live
        # grain_integrity probe), so the human sees evidence the template still executes
        # and the approve path can reuse the verdict inside the re-check window. When the
        # verdict was REUSED, `drift` carries its ORIGINAL `last_drift_check_at` —
        # deliberately not re-dated: moving the timestamp forward would claim a probe
        # that did not run.
        #
        # `route_reason` is STICKY: `env.route_reason` is carried forward when this pass
        # saw no correction, so a later clean cycle cannot erase the mark. A negative
        # signal that a subsequent success wipes is not a signal — it is the same shape of
        # bug as the erasure this whole routing change was written to fix.
        routed = replace(
            env,
            status=CandidateStatus.IN_REVIEW,
            drift=drift,
            route_reason=(
                ROUTE_REASON_USER_CORRECTED if corrected else env.route_reason
            ),
        )
        # Keep the LANDED node's stamp coherent with the store for a candidate that was
        # previously validated and has cycled back round (a demote, then a re-route).
        # `in_review` is non-recallable exactly like `candidate`, so this changes no
        # access decision — it keeps the two records from disagreeing about which state
        # the artifact is in, and it is the same fail-open idempotent Cypher the
        # demote-direction re-assert at the top of this method issues.
        if env.type in _LANDED_TYPES:
            await self._retract_corpus(
                routed, status=CandidateStatus.IN_REVIEW, drift_status=drift.status
            )
        with self._promote_scope(env, "route"):
            await self._store.put(routed)
        return CandidateDecision(
            candidate_id=env.candidate_id,
            type=env.type,
            action="route",
            from_status=env.status,
            to_status=CandidateStatus.IN_REVIEW,
            # The sweep's own copy. `reason` on a non-hold decision has been `None`
            # everywhere until now; a re-routed correction is the first thing worth saying
            # about a SUCCESSFUL transition, and carrying it here makes the rate countable
            # in telemetry without joining back to the envelope.
            reason=ROUTE_REASON_USER_CORRECTED if corrected else None,
        )

    # -- validated re-check (drift probes; demote on suspect/replay-fail) ------

    async def _recheck_validated(self, env: CandidateEnvelope) -> CandidateDecision:
        # Only a replayable (blueprint) artifact has a live grain_integrity probe.
        # A non-blueprint validated artifact (knowledge) has no template to replay
        # in Phase 1 → leave untouched (its Phase-2 probes are catalog/rule, stubbed).
        if env.type != BLUEPRINT_TYPE:
            return CandidateDecision(
                env.candidate_id, env.type, "skip", env.status, env.status,
                reason="not_replayable",
            )

        # Drift re-check is the SAME expensive probe as the promotion gate, so it obeys
        # the same rate limit. A `validated` blueprint whose stored verdict is still
        # inside `replay_recheck_interval_seconds` keeps that verdict and is NOT
        # re-probed — otherwise every validated blueprint costs two live warehouse
        # queries every five minutes for as long as it stays validated.
        #
        # The self-heal re-assert below deliberately stays OUTSIDE the rate limit: it is
        # one idempotent Cypher, and it is the loop that repairs a landed node whose
        # demote/land write-back transiently failed. Slowing a convergence guarantee to
        # save a cheap write would be the wrong trade — only the probe is expensive.
        reused = self._reusable_drift(env)
        if reused is not None and reused.status == "clean":
            await self._retract_corpus(
                env, status=CandidateStatus.VALIDATED, drift_status=reused.status
            )
            return CandidateDecision(
                env.candidate_id, env.type, "drift_clean", env.status, env.status,
                reason="replay_fresh",
            )

        # No reusable verdict (never probed, expired, or a stamp that is not a replay
        # verdict at all — e.g. a user correction) ⇒ probe for real. A stale-but-clean
        # stamp and a `suspect` one both land here: `suspect` on a validated artifact is
        # not reused to demote, because a demotion retracts a live recallable artifact
        # and must rest on a probe run NOW, not on a cached no.
        replay = await golden_replay(env, probe=self._probe)
        drift = drift_from_replay(replay, now=self._clock())
        if replay.passed:
            # Clean drift → stays validated; re-stamp clean+fresh (silent-eligible).
            refreshed = replace(env, drift=drift)
            await self._store.put(refreshed)
            # Slice 3 self-heal (§8.6): RE-ASSERT the landed node's recall-eligibility
            # stamp every clean rescan. Belt-and-suspenders — a demote's write-back that
            # transiently failed (fail-open below) leaves a stale node; the recall filter
            # keeps it out meanwhile, and this periodic re-assert repairs the stamp once
            # the blueprint is validated + clean again, so a transient neo4j failure can
            # never leave the corpus permanently out of sync with the store.
            await self._retract_corpus(
                refreshed, status=CandidateStatus.VALIDATED, drift_status=drift.status
            )
            return CandidateDecision(
                env.candidate_id, env.type, "drift_clean", env.status, env.status,
                reason=None,
            )
        # Suspect drift OR replay fail → demote to candidate + review flag. The
        # review flag is carried by `status=candidate` + `drift.status=suspect`
        # (naming the failed probe) — S9 owns only `status` + `drift` (D102).
        demoted = replace(env, status=CandidateStatus.CANDIDATE, drift=drift)
        # Slice 3 (§8.6): write the demote back to the landed neo4j node FIRST (so a
        # crash between here and the store write leaves the node un-recallable — the
        # SAFE direction), then the authoritative store demote. The corpus write-back
        # FAILS OPEN (see `_retract_corpus`): the store demote must never be blocked.
        await self._retract_corpus(
            demoted, status=CandidateStatus.CANDIDATE, drift_status=drift.status
        )
        await self._store.put(demoted)
        return CandidateDecision(
            env.candidate_id, env.type, "demote", env.status,
            CandidateStatus.CANDIDATE, reason=f"drift_suspect:{drift.failed_probe}",
        )

    # -- caller-driven transitions (human review / user correction) -----------

    async def apply_human_decision(
        self, env: CandidateEnvelope, decision: str
    ) -> CandidateDecision:
        """Apply a human `in_review` decision: approve → validated, reject → rejected.

        THE ONE caller-driven promotion path and the SINGLE implementation of the approve
        transition — `ReviewInbox.approve` delegates here so EVERY approve enforces the identical
        invariants (R4). Since plan §4 it is also the ONLY edge that writes content to the corpus,
        so every guard below is the sole enforcement of what it checks. `apply_retract`,
        `apply_verify` and `apply_promote` all require `status == validated`, which only this
        transition produces, so they INHERIT these guarantees rather than carrying copies.

        Approve invariants, in order:
          1. current status MUST be `in_review` (a mis-routed approve never mutates).
          2. the leakage verdict must be ACTIONABLE — a clean `pass`, or a finding that LOCALIZES
             itself; an empty span set switches off the strip AND the landing tripwire at once.
          3. strip entity-bearing payload + audit spans BEFORE `validated` (D17/Q3).
          4. `depends_on` must be resolved (§11.6/Q2).
          5. a REPLAYABLE blueprint STILL passes static + golden replay — human approval substitutes
             for the corroboration threshold, NOT for structural integrity (D98).

        Guards 1-4 are TYPE-AGNOSTIC and run before the blueprint/knowledge split, which is what
        makes the leakage guard cover the `global_knowledge` landing too. REJECT is deliberately
        ungated: it writes no content, and it is the ONLY action available for a candidate whose
        scan never settled.
        """
        if decision == "reject":
            rejected = replace(env, status=CandidateStatus.REJECTED)
            # Slice 3 (§8.6): retract the landed node so a reject is not recallable.
            # A reject usually fires from `in_review` (never landed → an idempotent
            # no-op), but a previously-landed blueprint CAN be rejected here, so the
            # write-back is meaningful; fail-open, store reject is source-of-truth.
            await self._retract_corpus(
                rejected,
                status=CandidateStatus.REJECTED,
                drift_status=env.drift.status,
            )
            # PriorArt Slice 2: kill the CORPUS ARTIFACT too. The node write-back above
            # only reaches neo4j, and a rejected candidate's `learning_corpus` artifact
            # is what the dedup soft layer reads — leave it live and the same declined
            # idea keeps surfacing as prior art, and keeps accruing hits, for ever.
            await self._stamp_corpus_status(env, CandidateStatus.REJECTED)
            await self._store.put(rejected)
            return CandidateDecision(
                env.candidate_id, env.type, "reject", env.status,
                CandidateStatus.REJECTED, reason=None,
            )
        if decision != "approve":
            return self._hold(env, f"unknown_decision:{decision}")

        # Guard 1 — current-status guard: approve only from in_review.
        if env.status != CandidateStatus.IN_REVIEW:
            return CandidateDecision(
                env.candidate_id, env.type, "hold", env.status, env.status,
                reason="approve_not_in_review",
            )

        # Guard 2 — the leakage verdict must be one the D17 machinery can ACT on:
        # a clean `pass`, or a finding that localizes itself. See
        # `_entity_scan_is_actionable` for the derivation.
        #
        # THIS GUARD IS THE PRECONDITION OF THE TWO LINES BELOW IT, which is why it sits
        # here rather than beside the other structural guards. Both defenses read
        # `entity_spans(env)`: the capture feeds the landing writer's last-gate tripwire
        # and `strip_entity_bearing` removes the same spans from the payload. When that
        # set is empty for any reason OTHER than "the scan found nothing", both layers
        # become no-ops at once — silently — and the raw payload crosses into `validated`
        # and into a corpus the agent recalls from.
        #
        # It matters now in a way it did not before plan §4. The automatic edge has always
        # carried `_entity_scan_is_clean` as its Guard 0, so `_advance_candidate` refuses
        # these envelopes; but that edge no longer lands anything, and approve is the ONLY
        # remaining path into the corpus. The one door left open was the unguarded one.
        # Found by QA, whose three strict-xfails are now the passing assertions in
        # `test_nothing_auto_lands_qa.py`.
        #
        # ACTIONABLE, NOT CLEAN — a deliberate asymmetry with the cron edge:
        #
        #   * A human MAY approve over a LOCALIZED finding. D58b routes 100% of leakage
        #     near-misses here precisely so a person decides; demanding a clean pass would
        #     make `reason=leakage_near_miss` a permanently un-approvable dead end, an
        #     inbox row no action but reject could ever clear. A localized finding is also
        #     the state in which the machinery WORKS — the spans exist, so the strip
        #     removes them and the tripwire verifies the strip — and the reviewer is
        #     genuinely informed: `_leakage_view` shows the real result and each hit's
        #     field and kind.
        #   * NOBODY may approve over an UNSETTLED scan. Unsettled means nobody looked,
        #     machine or human, and the reviewer cannot supply the judgement the machine
        #     did not — they are not even shown the gap, because `_leakage_view` renders
        #     an unsettled scan as `result="pass"` (with `scanner="unsettled"`). A human
        #     "deciding with their eyes open" is in fact reading a PASS for a scan that
        #     never ran.
        #   * NOBODY may approve over a finding with NO SPANS either, and this is the one
        #     an "eyes open" argument is most likely to talk itself past. The reviewer DOES
        #     see a non-`pass` result — but with zero hits, so neither they nor the strip
        #     can act on it. It is a scanner asserting a leak it could not locate.
        #
        # CONSEQUENCE, stated rather than discovered later: both refused shapes are
        # REJECT-ONLY for a human. There is no re-scan action in the inbox, so the only
        # terminal move is to decline. That matches what the writer already calls an
        # unsettled candidate (`_entity_scan_unsettled` routes it `fail_to_review`) and it
        # is the fail-closed direction — the alternative is landing text nobody has
        # cleared into a corpus the agent recalls from.
        if not _entity_scan_is_actionable(env):
            return CandidateDecision(
                env.candidate_id, env.type, "hold", env.status, env.status,
                reason="approve_blocked_entity_scan_not_actionable",
            )

        # Capture the entity spans S5 identified BEFORE the strip blanks them (D17 last
        # gate) — the landing writer's tripwire needs the PRE-strip spans (§3.3). Now
        # guaranteed to reflect a real verdict by Guard 2.
        forbidden_spans = entity_spans(env)

        # Guard 3 — entity strip on the promotion boundary (D17). Done up front so no
        # entity-bearing payload or audit span can cross into a validated state.
        env = strip_entity_bearing(env)

        # Guard 4 — depends_on must resolve (§11.6) regardless of promotion path (Q2).
        if not await self._deps_resolved(env):
            return CandidateDecision(
                env.candidate_id, env.type, "hold", env.status, env.status,
                reason="approve_blocked_depends_on_unresolved",
            )

        # Guard 5 — structural guards. A BLUEPRINT is always replayable: its
        # `generalization` MUST parse, else static + replay cannot run and human
        # approval would silently substitute for them (D98 forbids). Fail CLOSED on a
        # missing/malformed generalization (S3) — only genuinely NON-blueprint targets
        # (knowledge/schema) approve directly without a replay.
        if env.type == BLUEPRINT_TYPE:
            gen = self._generalization(env)
            if gen is None:
                return CandidateDecision(
                    env.candidate_id, env.type, "hold", env.status, env.status,
                    reason="approve_blocked_no_generalization",
                )
            if gen.static_validation.outcome != "ok":
                return CandidateDecision(
                    env.candidate_id, env.type, "hold", env.status, env.status,
                    reason="approve_blocked_static_not_ok",
                )
            replay = await golden_replay(env, probe=self._probe)
            if not replay.passed:
                return CandidateDecision(
                    env.candidate_id, env.type, "hold", env.status, env.status,
                    reason=f"approve_blocked_replay:{replay.reason}",
                )
            # Landing gate (§3.1/§4): a human approve of a BLUEPRINT also produces
            # `validated`, so it too must LAND to be recallable. When `require_landing`
            # is set but no writer is wired (dormant), a blueprint approve HOLDS at
            # in_review with a clear reason — the replay gate has already run + passed.
            # Non-blueprint human-gated targets (below) are unaffected.
            if self._landing_gate_blocks():
                return CandidateDecision(
                    env.candidate_id, env.type, "hold", env.status, env.status,
                    reason="approve_blocked_landing_unavailable",
                )
            drift = drift_from_replay(replay, now=self._clock())
            # With a real writer wired, LAND FIRST then write `validated` (§3.1) — the
            # SAME land-then-status invariant the auto edge uses. `env` was already
            # stripped (Guard 2); `_land_and_promote` re-strips idempotently. The
            # forbidden spans were captured PRE-strip above. A landing failure HOLDS
            # `landing_failed`, leaving the candidate at `in_review`.
            if self._landing_writer is not None:
                return await self._land_and_promote(
                    env,
                    drift,
                    action="approve",
                    forbidden_spans=forbidden_spans,
                    verified=True,  # human-approved landing → verified
                )
        else:
            # A pre-gated global_knowledge approve ALSO produces `validated`, so it too
            # must LAND to be recallable (UI Slice 2 §1.1 row 6) — otherwise the flagship
            # "approve → retrievable" action is a silent no-op. Route it through the SAME
            # type-agnostic land-then-status machinery the blueprint edge uses: LAND FIRST
            # into the neo4j retrieval corpus, THEN write `validated` (a landing failure
            # HOLDS `landing_failed`, never a fake validate). When `require_landing` is set
            # but no writer is wired (dormant), HOLD honestly — never a fake validate for a
            # never-recallable chunk. `forbidden_spans` were captured PRE-strip above.
            if env.type == "global_knowledge" and self._landing_writer is not None:
                return await self._land_and_promote(
                    env,
                    DriftStamp(),
                    action="approve",
                    forbidden_spans=forbidden_spans,
                    verified=True,  # human-approved landing → verified
                )
            if env.type == "global_knowledge" and self._landing_gate_blocks():
                return CandidateDecision(
                    env.candidate_id, env.type, "hold", env.status, env.status,
                    reason="approve_blocked_landing_unavailable",
                )
            # Genuinely non-landing target (schema_edit) or a truly dormant dev config
            # with require_landing off → human-authoritative direct validate, no live
            # drift probe (Phase 2). Nothing to land into (baseline behavior).
            drift = DriftStamp()

        approved = replace(env, status=CandidateStatus.VALIDATED, drift=drift)
        await self._store.put(approved)
        return CandidateDecision(
            env.candidate_id, env.type, "approve", env.status,
            CandidateStatus.VALIDATED, reason=None,
        )

    async def apply_user_correction(
        self, env: CandidateEnvelope
    ) -> CandidateDecision:
        """A user correction is a NEGATIVE signal (D29/D43): demote `validated` → `candidate`.

        Idempotent for a non-validated candidate (a no-op hold). The correction STICKS only because
        the next cycle routes the demoted candidate to `in_review` instead of back to `validated` —
        every guard still passes (a correction is about a VALUE and the replay is structure-only by
        design, D98), so before the routing change it was silently re-promoted within minutes. It
        does NOT make the correction a durable property of the ARTIFACT: there is no `corrected_at`,
        so a second correction repeats the cycle rather than escalating.
        """
        if env.status != CandidateStatus.VALIDATED:
            return self._hold(env, "not_validated")
        demoted = replace(
            env,
            status=CandidateStatus.CANDIDATE,
            drift=user_correction_stamp(now=self._clock()),
        )
        # Slice 3 (§8.6): write the demote back to the landed node FIRST (safe
        # direction on a crash), then the authoritative store demote. Fail-open —
        # a corpus-write failure must never block a user correction.
        await self._retract_corpus(
            demoted,
            status=CandidateStatus.CANDIDATE,
            drift_status=demoted.drift.status,
        )
        await self._store.put(demoted)
        return CandidateDecision(
            env.candidate_id, env.type, "demote", env.status,
            CandidateStatus.CANDIDATE, reason="user_correction",
        )

    async def apply_retract(self, env: CandidateEnvelope) -> CandidateDecision:
        """Retract a promoted artifact `validated → retired` (the inbox leak/drift PULL, §11.4).

        The SINGLE implementation `ReviewInbox.retract` delegates to. Stamps the landed node
        `retired` (fail-open, BEFORE the store retire, so a crash between leaves the node
        un-recallable — the safe direction), then writes `retired` to the store. Idempotent for a
        non-validated env. Physical index removal remains S10; the STAMP is what makes recall
        exclude the leaked node NOW.
        """
        if env.status != CandidateStatus.VALIDATED:
            return self._hold(env, "not_validated")
        retired = replace(env, status=CandidateStatus.RETIRED)
        await self._retract_corpus(
            retired, status=CandidateStatus.RETIRED, drift_status=env.drift.status
        )
        # PriorArt Slice 2: a retracted artifact is dead prior art too — a LEAKED
        # blueprint pulled from recall must not come back as "we already have this".
        await self._stamp_corpus_status(env, CandidateStatus.RETIRED)
        await self._store.put(retired)
        return CandidateDecision(
            env.candidate_id, env.type, "retire", env.status,
            CandidateStatus.RETIRED, reason=None,
        )

    async def apply_verify(
        self, env: CandidateEnvelope
    ) -> tuple[CandidateDecision, bool]:
        """VERIFY a `validated` learning node — the SINGLE implementation `ReviewInbox.verify` uses.

        Flips `verified → true` on BOTH the landed neo4j node AND the candidate-store envelope, and
        RETURNS whether the node write actually landed (`node_stamped`) so the caller can surface a
        re-verify prompt. The node is stamped FIRST: a crash between the two writes is the SAFE
        direction, and the node write FAILS OPEN so a neo4j hiccup never blocks the verify — the
        resulting gap is REPORTED rather than silent, and a re-verify converges the two. Status
        stays `validated`; verify is a flag flip, not a lifecycle transition, and does NOT move the
        node into the trusted MCP recall partition.
        """
        if env.status != CandidateStatus.VALIDATED:
            return self._hold(env, "not_validated"), False
        node_stamped = await self._verify_corpus(env)
        verified = replace(env, verified=True)
        await self._store.put(verified)
        return (
            CandidateDecision(
                env.candidate_id, env.type, "verify", env.status, env.status, reason=None,
            ),
            node_stamped,
        )

    async def apply_promote(self, env: CandidateEnvelope) -> CandidateDecision:
        """PROMOTE a verified learning node to the terminal `promoted` state (Phase-3).

        The store-side move `ReviewInbox.promote` delegates to AFTER it has emitted the MCP-format
        YAML. Requires `validated` + `verified` (fail-loud hold otherwise). Purely a candidate-store
        transition: it touches neither neo4j nor git, so the node stays `source='learning'` and
        excluded until a human merges the emitted YAML PR.
        """
        if env.status != CandidateStatus.VALIDATED:
            return self._hold(env, "not_validated")
        if not env.verified:
            return self._hold(env, "not_verified")
        promoted = replace(env, status=CandidateStatus.PROMOTED)
        await self._store.put(promoted)
        return CandidateDecision(
            env.candidate_id, env.type, "promote_emit", env.status,
            CandidateStatus.PROMOTED, reason=None,
        )

    # -- helpers --------------------------------------------------------------

    def _reusable_drift(self, env: CandidateEnvelope) -> DriftStamp | None:
        """`env.drift` when it is a golden-replay verdict still inside the re-check window.

        Else `None`, meaning "pay for a real replay". Both details fail SAFE in the direction of
        spending money rather than fabricating a verdict: the window is
        `min(replay_recheck_interval_seconds, drift_freshness_seconds)`, enforced at the point of
        use so a misconfiguration can only make the scheduler probe MORE often; and an unparseable
        or naive clock yields `None`, degrading to always-replay rather than to "everything looks
        fresh", which would silently disable the structural gate.
        """
        now = _parse_clock(self._clock())
        if now is None:
            return None
        window = min(
            self._policy.replay_recheck_interval_seconds,
            self._policy.drift_freshness_seconds,
        )
        if reusable_replay_verdict(env.drift, now=now, window_seconds=window) is None:
            return None
        return env.drift

    def _generalization(self, env: CandidateEnvelope) -> BlueprintGeneralization | None:
        gen_doc = env.payload.get("generalization")
        if not isinstance(gen_doc, dict):
            return None
        try:
            return BlueprintGeneralization.from_doc(gen_doc)
        except (KeyError, TypeError):
            return None

    def _blueprint_intent(self, env: CandidateEnvelope) -> str | None:
        """The learned blueprint intent for the verbose promote/land span (entity-bearing, D25-gated).

        `None` for a non-blueprint or payload-less env.
        """
        payload = env.payload
        intent = payload.get("intent") if isinstance(payload, dict) else None
        return intent if isinstance(intent, str) and intent else None

    def _canonical_key(self, env: CandidateEnvelope) -> str | None:
        return env.dedup.canonical_key if env.dedup is not None else None

    def _promote_scope(self, env: CandidateEnvelope, action: str):
        """The `learning.promote` span (session-trace continuation via the candidate's
        traceparent) OR a `nullcontext` when no tracer is wired."""
        if self._tracer is None:
            return nullcontext(None)
        # Double-gate (defense-in-depth, mirroring the consumer): pass the entity-
        # bearing attrs ONLY when verbose is on, so the D25 gate does not rest solely
        # on `_verbose_attrs` dropping them downstream.
        verbose = self._trace_verbose
        return promote_span(
            self._tracer,
            session_id=env.source_session,
            candidate_id=env.candidate_id,
            action=action,
            context=context_from_traceparent(env.traceparent),
            verbose=verbose,
            blueprint_id=landing_id(env) if verbose else None,
            blueprint_intent=self._blueprint_intent(env) if verbose else None,
            canonical_key=self._canonical_key(env) if verbose else None,
        )

    def _land_scope(self, env: CandidateEnvelope):
        """The `learning.land` span, nested UNDER the open promote span (so it takes
        the ambient context, not the remote parent again). Nullcontext when untraced."""
        if self._tracer is None:
            return nullcontext(None)
        verbose = self._trace_verbose  # double-gate, see `_promote_scope`
        return land_span(
            self._tracer,
            session_id=env.source_session,
            candidate_id=env.candidate_id,
            verbose=verbose,
            blueprint_id=landing_id(env) if verbose else None,
            blueprint_intent=self._blueprint_intent(env) if verbose else None,
            canonical_key=self._canonical_key(env) if verbose else None,
        )

    def _landing_gate_blocks(self) -> bool:
        """§4 dormant gate: `require_landing` is set but NO landing writer is wired.

        A blueprint can PASS the replay gate but must not become `validated` when there is nowhere
        to land it — it would be `validated` yet never recallable. True ⇒ HOLD
        (`landing_unavailable`). Consulted ONLY on the human-approve edge: the auto path lands
        nothing, so gating it there would park candidates with a reason about a landing nobody asked
        for. A deployment with no writer fills its inbox and refuses each approve honestly (503).
        """
        return self._require_landing and self._landing_writer is None

    async def _land_and_promote(
        self,
        env: CandidateEnvelope,
        drift: DriftStamp,
        *,
        action: str,
        forbidden_spans: tuple[str, ...],
        verified: bool = False,
    ) -> CandidateDecision:
        """The SINGLE land-then-status sequence for the `→ validated` edge (§3.1).

        Order is LOAD-BEARING: land into the neo4j retrieval corpus FIRST, then write
        `status = validated`. Invariant "not landed ⇒ not validated" — a landing failure HOLDs
        `landing_failed` and never writes a half state, and a crash between the two is safe because
        the next cycle re-lands idempotently (MERGE by the deterministic id) then writes status.

        The status write is a plain upsert, not a CAS: S9 assumes a SINGLE promotion writer, since
        the cron scan and the human-approve path both serialize through this scheduler over the
        shared store. The fresh `drift` is stamped BEFORE landing so the seed, the crash-retry
        re-land and the status write all carry the SAME stamp. The entity strip runs on THIS edge
        too (idempotent), and *forbidden_spans* — the spans S5 identified, captured by the caller
        BEFORE the strip — drive the writer's last-gate defense, which RAISES if the strip
        regressed. *verified* is stamped onto BOTH the node and the envelope so the two never
        diverge.
        """
        from_status = env.status
        # Stamp the fresh drift BEFORE landing so the landed seed carries it (§8.1);
        # strip is idempotent (the human path already stripped at Guard 2).
        landed = replace(strip_entity_bearing(env), drift=drift)
        assert self._landing_writer is not None  # guarded by the caller
        # The promote span continues the session's trace (from the candidate's
        # traceparent), with the land span nested under it. Verbose attrs are read
        # from the PRE-strip `env` (D25-gated intent/canonical_key); `landed` is
        # already entity-stripped.
        with self._promote_scope(env, action):
            try:
                with self._land_scope(env):
                    await self._landing_writer.land(
                        landed, forbidden_spans=forbidden_spans, verified=verified
                    )
            except Exception:  # noqa: BLE001 - any landing failure HOLDS; never a half state
                _logger.warning(
                    "landing failed for candidate %s; holding (status stays %s, "
                    "retried next cycle)",
                    landed.candidate_id,
                    from_status,
                    exc_info=True,
                )
                return CandidateDecision(
                    landed.candidate_id, landed.type, "hold", from_status, from_status,
                    reason="landing_failed",
                )
            # Stamp `verified` on the store envelope to MATCH the landed node (Phase-3):
            # the node is the recall source of truth, this copy lets the inbox tell a
            # human-verified landing from an auto one without a neo4j read.
            await self._store.put(
                replace(landed, status=CandidateStatus.VALIDATED, verified=verified)
            )
        return CandidateDecision(
            landed.candidate_id, landed.type, action, from_status,
            CandidateStatus.VALIDATED, reason=None,
        )

    async def _retract_corpus(
        self, env: CandidateEnvelope, *, status: str, drift_status: str
    ) -> None:
        """FAIL-OPEN corpus write-back: stamp the landed node's recall-eligibility (§8.6).

        The store transition is SOURCE-OF-TRUTH and must succeed even if this fails, so EVERY
        exception is swallowed and logged LOUDLY. The recall filter's coalesce default is fail-OPEN
        for an un-stamped node, so the filter is NOT a backstop for a FAILED demote write-back;
        convergence comes from the RE-ASSERTS, which fire on every EXAMINATION of the artifact (the
        demote direction from `_advance_candidate`, the self-heal direction from a clean
        `_recheck_validated`) and are deliberately NOT keyed on the drift STAMP — a re-assert whose
        trigger a later write can erase is not a convergence guarantee. Both sit outside the replay
        rate limit: a re-assert is one idempotent Cypher. No writer wired ⇒ nothing landed ⇒ no-op.
        """
        if self._landing_writer is None:
            return
        try:
            await self._landing_writer.update_status(
                env, status=status, drift_status=drift_status
            )
        except Exception:  # noqa: BLE001 - fail-OPEN: a corpus write must never block a demote
            _logger.warning(
                "corpus status write-back FAILED for candidate %s (status=%s, "
                "drift_status=%s); the store transition proceeds (fail-open). This is "
                "retried on every subsequent EXAMINATION by the re-asserts (a demoted "
                "artifact re-stamps in the candidate scan; a validated one in the clean "
                "rescan), converging the node to non-recallable once neo4j recovers.",
                env.candidate_id,
                status,
                drift_status,
                exc_info=True,
            )

    async def _stamp_corpus_status(self, env: CandidateEnvelope, status: str) -> None:
        """FAIL-OPEN `learning_corpus` artifact status write-back (PriorArt Slice 2).

        Called ONLY on the two terminal edges (reject, retract), keyed by the S6 `canonical_key`:
        no dedup verdict means S6 never ran, so there is no artifact and nothing to stamp — a clean
        no-op, since a human-approved candidate that skipped S6 is a supported path. Fail-open
        because the candidate-store transition is source of truth. Unlike the neo4j write-back there
        is deliberately NO periodic re-assert: the terminal edges are one-shot human actions, and
        the worst case of a lost stamp is one extra candidate reaching a human.
        """
        if self._corpus_status is None:
            return
        key = self._canonical_key(env)
        if not key:
            return
        try:
            await self._corpus_status.set_status(key, status)
        except Exception:  # noqa: BLE001 - fail-OPEN: never block a terminal transition
            _logger.warning(
                "learning_corpus status write-back FAILED for candidate %s "
                "(canonical_key=%s, status=%s); the store transition proceeds "
                "(fail-open). The artifact stays visible to the dedup prior-art read, "
                "so this declined idea can resurface as prior art until it is stamped.",
                env.candidate_id,
                key[:23],
                status,
                exc_info=True,
            )

    async def _verify_corpus(self, env: CandidateEnvelope) -> bool:
        """FAIL-OPEN corpus verify write-back (Phase-3): flip the landed node's `verified` flag.

        RETURNS whether a landed node was actually stamped — False when no writer is wired, no node
        was landed, OR the write failed — so `apply_verify` can report a re-verify prompt. The
        envelope write is source-of-truth, so every exception is swallowed and logged: a neo4j
        hiccup never blocks a human verify.
        """
        if self._landing_writer is None:
            return False
        try:
            return await self._landing_writer.mark_verified(env)
        except Exception:  # noqa: BLE001 - fail-OPEN: a corpus write must never block verify
            _logger.warning(
                "corpus verify write-back FAILED for candidate %s; the envelope verify "
                "proceeds (fail-open) and reports node_stamped=False. The node stays "
                "unverified until a re-verify succeeds; recall is unaffected (a learning "
                "node is never recallable).",
                env.candidate_id,
                exc_info=True,
            )
            return False

    async def _deps_resolved(self, env: CandidateEnvelope) -> bool:
        """True iff every `depends_on` ref resolves (§11.6). No deps ⇒ trivially resolved.

        Deps present but NO resolver injected ⇒ fail-closed: never promote a candidate whose
        dependencies cannot be verified.
        """
        if not env.depends_on:
            return True
        if self._deps is None:
            return False
        for ref in env.depends_on:
            if not await self._deps.is_resolved(ref):
                return False
        return True

    async def _corroboration(self, env: CandidateEnvelope) -> float:
        """How much evidence there is that this idea is worth a human's time (plan §4).

        `max(hit_count, 1) + recurrence_weight * recurrence_count`. THE FLOOR OF 1 closes a hole the
        threshold change opens rather than loosening anything: `_read_hit_count` returns 0 when S6
        could not mint a `canonical_key`, and at T=1 such a candidate would hold FOREVER — the one
        subclass that could never reach a human. It changes no outcome above 1, because a keyed
        artifact is seeded at `hit_count = 1` and the counter never decreases, and an unkeyable
        candidate reads zero on BOTH counters so the floor cannot combine with a recurrence term.
        The recurrence term is DORMANT at the shipped weight of 0.0 and is read anyway, so the path
        is exercised from the first deploy. A `float`, not an `int`: `recurrence_weight` is
        fractional by design, and an int return would floor away the tuning it exists to express.
        """
        hits = await self._read_hit_count(env)
        weight = self._policy.recurrence_weight
        if weight <= 0.0:
            return float(max(hits, 1))
        return max(hits, 1) + weight * await self._read_recurrence_count(env)

    async def _read_hit_count(self, env: CandidateEnvelope) -> int:
        """Read `hit_count` from the LANDED corpus artifact, keyed by the S6 `canonical_key`.

        No dedup verdict yet (S6 not run) ⇒ no canonical_key ⇒ count 0 — see `_corroboration`.
        """
        if env.dedup is None or not env.dedup.canonical_key:
            return 0
        return await self._hit_counts.hit_count(env.dedup.canonical_key)

    async def _read_recurrence_count(self, env: CandidateEnvelope) -> int:
        """The SOFT paraphrase count for this candidate's artifact, or 0.

        Fail-soft on ANY reader failure: this is a dormant secondary signal, and a corpus hiccup
        must not hold a candidate its PRIMARY count already corroborates. The hard count is
        deliberately not treated this way — it can only raise the sum, so swallowing its failure
        would be swallowing the gate.
        """
        if self._recurrence_counts is None:
            return 0
        if env.dedup is None or not env.dedup.canonical_key:
            return 0
        try:
            return await self._recurrence_counts.recurrence_count(
                env.dedup.canonical_key
            )
        except Exception:  # noqa: BLE001 - a dormant signal may never block a candidate
            _logger.warning(
                "recurrence-count read failed for candidate %s; treating it as 0 "
                "(the corroboration gate falls back to the hard hit count)",
                env.candidate_id,
                exc_info=True,
            )
            return 0

    def _hold(self, env: CandidateEnvelope, reason: str) -> CandidateDecision:
        return CandidateDecision(
            candidate_id=env.candidate_id,
            type=env.type,
            action="hold",
            from_status=env.status,
            to_status=env.status,
            reason=reason,
        )

    # -- the daemon loop ------------------------------------------------------

    async def run_forever(self, *, sleep) -> None:
        """Periodic loop (the entrypoint); *sleep* is injected so it is unit-drivable.

        A transient error must not kill the daemon — log and retry next interval.
        """
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a transient store/probe error must
                # not kill the daemon; log and retry next interval.
                _logger.exception("promotion cycle failed; retrying next interval")
            await sleep(self._policy.promotion_interval_seconds)


__all__ = ["PromotionScheduler", "ReplayOutcome"]
