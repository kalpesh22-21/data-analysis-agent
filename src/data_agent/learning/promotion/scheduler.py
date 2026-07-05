"""promotion/scheduler.py — the S9 promotion scheduler (D29/D30, §7.2).

A STANDALONE background process, NOT a `CandidateStage`. It is cron-scanned state
(D29 "cron-scanned state, not queued"): each cycle it reads
`learning_candidates.list_by_status(...)`, runs golden replay + the D43 drift
probes, and advances `status` + stamps `drift` (Contract E). It shares the envelope
contract with the write-router stages but NOT their pipeline seam (§7.2), so it
parallelizes cleanly against the S4 fixture.

Contract E state machine (the edges this scheduler owns):

    candidate ─(static ok AND golden-replay pass AND deps resolved
                AND (hit_count ≥ T OR human approval))──────────▶ validated
    candidate ─(any guard fails / single session / dep unresolved)─▶ candidate  (hold)
    in_review ─(human approve)──────────────────────────────────▶ validated
    in_review ─(human reject)───────────────────────────────────▶ rejected
    validated ─(drift probes clean)─────────────────────────────▶ validated  (drift=clean)
    validated ─(drift suspect OR replay fails OR user correction)─▶ candidate  (demote + review flag)

Load-bearing guards (D29/D98):
  * **Replay alone NEVER promotes** (D98 layer iii): a single-session candidate
    (`hit_count < T`, no human approval) with a GREEN replay STAYS `candidate`.
    Replay verifies structure, not values — there is no value oracle (D17/D98).
  * **`depends_on` guard** (§11.6): a candidate whose `depends_on` references an
    unresolved artifact stays `candidate` — never promotes until it resolves.
  * **Human-gated targets** (`global_knowledge`/`schema_edit`, D58a/D18): T = ∞;
    the auto path never promotes them (they land via the S7 inbox → human
    approve). `user_knowledge` auto-commits in its OWN writer (S8), not here.

Fail-closed throughout: one bad candidate must not abort the cycle (mirrors the
sweeper's per-item guard); the kill-switch is read FRESH every cycle (D58c).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from ..candidate.generalization import BlueprintGeneralization
from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.redaction import entity_spans, strip_entity_bearing
from ..candidate.store import CandidateStore
from ..candidate.verdicts import DriftStamp, LeakageVerdict
from ..config import learning_enabled
from .drift import drift_from_replay, user_correction_stamp
from .models import (
    BLUEPRINT_TYPE,
    HUMAN_GATED_TYPES,
    CandidateDecision,
    DependencyResolver,
    HitCountReader,
    LandingWriter,
    PromotionPolicy,
    PromotionSweep,
    WarehouseProbe,
)
from .replay import ReplayOutcome, golden_replay

_logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self._store = store
        self._probe = probe
        self._hit_counts = hit_counts
        self._policy = policy or PromotionPolicy()
        self._deps = dependency_resolver
        self._landing_writer = landing_writer
        self._require_landing = require_landing
        self._clock = clock

    @property
    def store(self) -> CandidateStore:
        """The candidate store this scheduler reads + CAS-writes. Exposed read-only
        so a composition root can pin the inbox to the SAME instance (a split-brain
        store would let the inbox read one store while this scheduler writes another
        — stale-envelope approve)."""
        return self._store

    # -- the cron cycle -------------------------------------------------------

    async def run_once(self) -> PromotionSweep:
        """One scan: advance eligible `candidate`s, re-check every `validated`.

        Kill-switch FIRST (D58c): disabled ⇒ no scan, no transition. Read uncached,
        per cycle. A per-candidate error is logged and skipped (the whole cycle
        never aborts on one bad candidate — sweeper parity)."""
        if not learning_enabled():
            return PromotionSweep(disabled=True)

        # Snapshot BOTH status lists up front, before any transition writes: a
        # candidate promoted this cycle must not be re-scanned as `validated` in
        # the SAME cycle (it lands in the store at `validated` immediately). The
        # next cycle re-checks it via the drift probes.
        candidates = await self._store.list_by_status(
            CandidateStatus.CANDIDATE, limit=self._policy.scan_limit
        )
        validated = await self._store.list_by_status(
            CandidateStatus.VALIDATED, limit=self._policy.scan_limit
        )

        decisions: list[CandidateDecision] = []
        for env in candidates:
            decisions.append(await self._guard(self._advance_candidate, env))
        for env in validated:
            decisions.append(await self._guard(self._recheck_validated, env))

        return PromotionSweep(decisions=tuple(decisions))

    async def _guard(self, fn, env: CandidateEnvelope) -> CandidateDecision:
        try:
            return await fn(env)
        except Exception:  # noqa: BLE001 - one bad candidate must not abort the cycle
            _logger.exception(
                "promotion scheduler failed for candidate %s; leaving it unchanged",
                env.candidate_id,
            )
            return self._hold(env, "error")

    # -- candidate → validated (the auto-promotion edge) ----------------------

    async def _advance_candidate(self, env: CandidateEnvelope) -> CandidateDecision:
        # Demote-direction CONVERGENCE re-assert (Slice 3 §9.4, review BLOCKER 1). A
        # DEMOTED blueprint (`drift.status == "suspect"`, now back in the `candidate`
        # scan) re-stamps its landed node ineligible EVERY cycle here — because the
        # demote edge's write-back is fail-open (a transient neo4j failure at demote
        # leaves the node `validated`/`clean`, i.e. still RECALLABLE, and the
        # coalesce-default recall filter does NOT catch an un-stamped node). The
        # clean-branch re-assert only scans `validated`, so it never revisits this
        # now-`candidate` envelope — THIS is the re-assert that closes the loop: it is
        # idempotent (one MATCH/SET) and converges the node to non-recallable once neo4j
        # recovers. If this blueprint goes on to RE-PROMOTE below, the land overwrites
        # the stamp with `validated`/`clean`, so running it up front is safe.
        if env.type == BLUEPRINT_TYPE and env.drift.status == "suspect":
            await self._retract_corpus(
                env, status=env.status, drift_status="suspect"
            )

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

        # Guard 3 — golden replay must pass (structure, not values — D98).
        replay = await golden_replay(env, probe=self._probe)
        if not replay.passed:
            return self._hold(env, f"replay_failed:{replay.reason}")

        # Guard 4 — hit_count ≥ T OR human approval. A GREEN replay with a
        # single-session count is NOT enough (D98 layer iii — replay never promotes
        # on its own): the candidate STAYS candidate.
        count = await self._read_hit_count(env)
        if count < self._policy.blueprint_hit_threshold:
            return self._hold(env, "below_hit_threshold")

        # Guard 5 — landing gate (§3/§4). A blueprint that passes EVERY guard above
        # (including the now-REAL replay gate) still must not promote to `validated`
        # until it can LAND in the neo4j retrieval corpus — otherwise it would be
        # `validated` but never recallable (the silent gap). When `require_landing` is
        # set but no writer is wired (the dormant state), HOLD `landing_unavailable`.
        if self._landing_gate_blocks():
            return self._hold(env, "landing_unavailable")

        # All guards pass → promote, stamping a fresh clean drift (the passing
        # replay IS the live grain_integrity probe), so it is immediately
        # silent-eligible. With a real landing writer wired, LAND into the neo4j
        # retrieval corpus FIRST, then CAS `validated` (§3.1); a landing failure HOLDS
        # `landing_failed` and the candidate stays `candidate` (not landed ⇒ not
        # validated). Without a writer (require_landing off), promote directly (today's
        # baseline behavior — nothing to land into).
        drift = drift_from_replay(replay, now=self._clock())
        if self._landing_writer is not None:
            # Capture the entity spans S5 identified BEFORE the strip (D17 last gate) —
            # `_land_and_promote` strips, which blanks `entity_scan`, so the forbidden
            # spans must be read from the PRE-strip envelope here.
            return await self._land_and_promote(
                env, drift, action="promote", forbidden_spans=entity_spans(env)
            )
        promoted = replace(env, status=CandidateStatus.VALIDATED, drift=drift)
        await self._store.put(promoted)
        return CandidateDecision(
            candidate_id=env.candidate_id,
            type=env.type,
            action="promote",
            from_status=env.status,
            to_status=CandidateStatus.VALIDATED,
            reason=None,
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
        """Apply a human `in_review` decision (Contract D / Contract E): approve →
        validated, reject → rejected. This is the ONE caller-driven promotion path
        (not the cron scan) and the SINGLE implementation of the approve transition —
        `ReviewInbox.approve` delegates here so EVERY approve enforces the identical
        invariants (R4): the entity strip, the current-status guard, the `depends_on`
        guard, and (for a replayable blueprint) the static + replay guards.

        Approve invariants, in order:
          1. current status MUST be `in_review` (a mis-routed approve never mutates).
          2. strip entity-bearing payload + audit spans BEFORE `validated` (D17/Q3).
          3. `depends_on` must be resolved (§11.6/Q2) — a human cannot promote a
             blueprint whose required schema_edit has not landed.
          4. a REPLAYABLE blueprint (has a generalization) STILL passes static +
             golden replay (human approval substitutes for the hit-count threshold,
             NOT for structural integrity — D98). A non-replayable target
             (knowledge/schema, or a blueprint with no template) approves directly.
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

        # Capture the entity spans S5 identified BEFORE the strip blanks them (D17 last
        # gate) — the landing writer's tripwire needs the PRE-strip spans (§3.3).
        forbidden_spans = entity_spans(env)

        # Guard 2 — entity strip on the promotion boundary (D17). Done up front so no
        # entity-bearing payload or audit span can cross into a validated state.
        env = strip_entity_bearing(env)

        # Guard 3 — depends_on must resolve (§11.6) regardless of promotion path (Q2).
        if not await self._deps_resolved(env):
            return CandidateDecision(
                env.candidate_id, env.type, "hold", env.status, env.status,
                reason="approve_blocked_depends_on_unresolved",
            )

        # Guard 4 — structural guards. A BLUEPRINT is always replayable: its
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
                    env, drift, action="approve", forbidden_spans=forbidden_spans
                )
        else:
            # Genuinely non-replayable target (pre-gated knowledge/schema) →
            # human-authoritative, no live drift probe (Phase 2).
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
        """A user correction is a NEGATIVE signal (D29/D43): demote a `validated`
        artifact to `candidate` + review flag. Idempotent for a non-validated
        candidate (a no-op hold)."""
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
        """Retract a promoted artifact `validated → retired` (the inbox leak/drift PULL,
        §11.4) — the SINGLE implementation `ReviewInbox.retract` delegates to, so the
        highest-stakes human edge (pulling a LEAKED blueprint from recall) enforces the
        same corpus write-back as every other demote edge (review BLOCKER 2).

        Stamps the landed node `retired` (fail-open, BEFORE the store retire so a crash
        between leaves the node un-recallable — the safe direction), then writes
        `retired` to the store. Idempotent for a non-validated env (a no-op hold — a
        mis-routed retract never mutates). Physical index removal + the D25 exposure
        trace remain S10; the STAMP here is what makes recall exclude the leaked node
        NOW (the recall filter drops any non-`validated` status)."""
        if env.status != CandidateStatus.VALIDATED:
            return self._hold(env, "not_validated")
        retired = replace(env, status=CandidateStatus.RETIRED)
        await self._retract_corpus(
            retired, status=CandidateStatus.RETIRED, drift_status=env.drift.status
        )
        await self._store.put(retired)
        return CandidateDecision(
            env.candidate_id, env.type, "retire", env.status,
            CandidateStatus.RETIRED, reason=None,
        )

    # -- helpers --------------------------------------------------------------

    def _generalization(self, env: CandidateEnvelope) -> BlueprintGeneralization | None:
        gen_doc = env.payload.get("generalization")
        if not isinstance(gen_doc, dict):
            return None
        try:
            return BlueprintGeneralization.from_doc(gen_doc)
        except (KeyError, TypeError):
            return None

    def _landing_gate_blocks(self) -> bool:
        """§4 dormant gate: `require_landing` is set but NO landing writer is wired. A
        blueprint can PASS the replay gate but must not promote to `validated` when
        there is nowhere to land it (it would be `validated` yet never recallable —
        the silent gap). True ⇒ HOLD (`landing_unavailable`). With a real writer
        present this is False, and `_land_and_promote` runs the land-then-status
        sequence instead. Default OFF — today's callers are unchanged."""
        return self._require_landing and self._landing_writer is None

    async def _land_and_promote(
        self,
        env: CandidateEnvelope,
        drift: DriftStamp,
        *,
        action: str,
        forbidden_spans: tuple[str, ...],
    ) -> CandidateDecision:
        """The SINGLE land-then-status sequence for the `→ validated` edge (§3.1),
        shared by the auto (`_advance_candidate`) and human-approve paths.

        Order is LOAD-BEARING: land into the neo4j retrieval corpus FIRST, then write
        `status = validated` to the candidate store. Invariant "not landed ⇒ not
        validated":
          * a landing failure → HOLD `landing_failed`; the candidate is NOT written
            `validated` (it stays `candidate`/`in_review`, never a half state), so the
            next cycle retries;
          * a crash BETWEEN land and the status write is safe — the next cycle
            re-lands idempotently (MERGE by the deterministic id) then writes status.

        The status write is a plain store upsert, not a compare-and-set: S9 assumes a
        SINGLE promotion writer (the cron scan and the human-approve path both serialize
        through this scheduler over the shared store, §7.2), so no CAS is needed; a
        second concurrent writer is out of scope (and would need one).

        The fresh `drift` is stamped on the env BEFORE landing so the landed seed's
        `drift_status`, the crash-retry re-land, and the status write all carry the SAME
        fresh stamp (review S1 / §8.1) — never the stale pre-promotion `unchecked`/
        `suspect` value.

        The entity strip runs on THIS edge (D17, §3.3): the human path already stripped
        (Guard 2), the auto path only CHECKED `entity_scan` was clean — so strip here
        (idempotent) makes BOTH edges land an entity-free seed. *forbidden_spans* (the
        spans S5 identified, captured by the caller BEFORE the strip) drive the writer's
        last-gate defense, which RAISES if the strip regressed and let one through."""
        from_status = env.status
        # Stamp the fresh drift BEFORE landing so the landed seed carries it (§8.1);
        # strip is idempotent (the human path already stripped at Guard 2).
        landed = replace(strip_entity_bearing(env), drift=drift)
        assert self._landing_writer is not None  # guarded by the caller
        try:
            await self._landing_writer.land(landed, forbidden_spans=forbidden_spans)
        except Exception:  # noqa: BLE001 - any landing failure HOLDS; never a half state
            _logger.warning(
                "landing failed for candidate %s; holding (status stays %s, retried next cycle)",
                landed.candidate_id,
                from_status,
                exc_info=True,
            )
            return CandidateDecision(
                landed.candidate_id, landed.type, "hold", from_status, from_status,
                reason="landing_failed",
            )
        await self._store.put(replace(landed, status=CandidateStatus.VALIDATED))
        return CandidateDecision(
            landed.candidate_id, landed.type, action, from_status,
            CandidateStatus.VALIDATED, reason=None,
        )

    async def _retract_corpus(
        self, env: CandidateEnvelope, *, status: str, drift_status: str
    ) -> None:
        """FAIL-OPEN corpus write-back (S9-activation Slice 3, §8.6): stamp the landed
        neo4j node's recall-eligibility (`status`/`drift_status`) via the landing
        writer, keyed by the same deterministic landing id.

        The store transition is SOURCE-OF-TRUTH and must SUCCEED even if this write-back
        fails, so EVERY exception is swallowed and logged LOUDLY (never re-raised) — a
        demote/reject/correction is never blocked by a neo4j hiccup. The recall filter's
        coalesce default is fail-OPEN for an UN-stamped node (a node that never got
        stamped still reads `validated`/`clean` ⇒ recallable), so the filter alone is NOT
        a backstop for a FAILED demote write-back. Convergence is provided by the
        per-cycle RE-ASSERTS instead:
          * a DEMOTED blueprint (`drift.status == "suspect"`) re-stamps its node
            ineligible every `_advance_candidate` cycle (the demote-direction re-assert);
          * a still-validated blueprint's clean `_recheck_validated` re-stamps it
            `validated`/`clean` (the self-heal direction).
        So a transient failure at demote is retried each subsequent cycle and converges
        the node to non-recallable once neo4j recovers.

        No writer wired (the dormant Slice-1 state, `landing_writer is None`) ⇒ nothing
        ever landed ⇒ nothing to retract ⇒ a no-op. Idempotent for a never-landed /
        already-retracted node (the writer's MATCH-by-id matches nothing)."""
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
                "retried every subsequent cycle by the per-cycle re-assert (a demoted "
                "blueprint re-stamps in the candidate scan; a validated one in the clean "
                "rescan), converging the node to non-recallable once neo4j recovers.",
                env.candidate_id,
                status,
                drift_status,
                exc_info=True,
            )

    async def _deps_resolved(self, env: CandidateEnvelope) -> bool:
        """True iff every `depends_on` ref resolves (§11.6). No deps ⇒ trivially
        resolved. Deps present but NO resolver injected ⇒ fail-closed (unresolved):
        never promote a candidate whose dependencies cannot be verified."""
        if not env.depends_on:
            return True
        if self._deps is None:
            return False
        for ref in env.depends_on:
            if not await self._deps.is_resolved(ref):
                return False
        return True

    async def _read_hit_count(self, env: CandidateEnvelope) -> int:
        """Read `hit_count` from the LANDED corpus artifact via the injected
        reader, keyed by the S6 `canonical_key`. No dedup verdict yet (S6 not run)
        ⇒ no canonical_key ⇒ count 0 (cannot promote by count; human approval is
        the alternative path)."""
        if env.dedup is None or not env.dedup.canonical_key:
            return 0
        return await self._hit_counts.hit_count(env.dedup.canonical_key)

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
        """Periodic loop (the entrypoint). *sleep* is injected (`asyncio.sleep`) so
        it is unit-drivable. A transient error must not kill the daemon — log +
        retry next interval (sweeper parity)."""
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a transient store/probe error must
                # not kill the daemon; log and retry next interval.
                _logger.exception("promotion cycle failed; retrying next interval")
            await sleep(self._policy.promotion_interval_seconds)


__all__ = ["PromotionScheduler", "ReplayOutcome"]
