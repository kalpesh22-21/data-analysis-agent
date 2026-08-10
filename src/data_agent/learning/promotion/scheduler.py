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

**Cost + fairness (the two properties that make the cron survivable at scale).**
This is a daemon, not a one-shot, so every per-candidate cost is multiplied by 288
cycles a day, forever:

  * **The golden replay is rate-limited, the cheap guards are not.** One replay costs
    a JWT mint plus two live warehouse queries. Guards 0-2 (`entity_scan`, static
    validation, `depends_on`) are pure field reads / one resolver call and keep running
    on every examination, so a dependency that lands mid-day is still noticed the next
    time the candidate comes up. Only Guard 3 consults
    `PromotionPolicy.replay_recheck_interval_seconds`: inside that window the STORED
    D43 verdict is reused (`reusable_replay_verdict`), outside it the probe runs for
    real. Without this a candidate that can never clear Guard 4 was fully replayed
    every 5 minutes forever — 576 warehouse queries a day, each one re-deriving a
    verdict that had not changed.
  * **The scan window ROTATES.** Both status reads are ordered by `last_scanned_at`
    (never-scanned FIRST), and every examined candidate gets its cursor stamped. The
    old `created_at ASC` read handed the bounded `scan_limit` window to the same oldest
    rows forever, so once more than `scan_limit` candidates were parked in a hold, a
    newly extracted candidate was NEVER examined — no error, no metric, the loop simply
    stopped making progress on anything new.

A consequence worth stating plainly, because several comments below used to say "every
cycle": the corpus re-asserts and the cheap guards now run on every EXAMINATION, which
is every cycle while the backlog fits in `scan_limit` and once per rotation period
beyond that. That is strictly better than the previous behaviour (where the overflow was
examined NEVER), but it is a rotation guarantee, not a per-cycle one.

The cursor stamp makes a HOLD a write path, which it previously was not. It is a
narrow sub-document write (`CandidateStore.touch_scanned`), issued AFTER the handler
has finished, precisely so it cannot revert whatever that handler — or a concurrent
S7 inbox transition — wrote to the envelope; see `run_once`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime

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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_clock(now_iso: str) -> datetime | None:
    """The injected clock's string as a tz-AWARE datetime, or `None` if it is not one.

    `clock` is a `Callable[[], str]` injected by the caller (tests pin it to a literal),
    so its output is not a guaranteed ISO-8601 string; `datetime.fromisoformat` raises
    ValueError on a malformed string and TypeError on a non-string. Both mean the same
    thing to every caller here — "no usable now" — and every caller treats that as
    "cannot judge freshness, run the real probe", i.e. the pre-rate-limit behaviour."""
    if not isinstance(now_iso, str):
        return None
    try:
        parsed = datetime.fromisoformat(now_iso)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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

        The cursor write is in the `finally` for the same reason the whole handler is
        wrapped: the cycle must keep its fairness property even for the candidate that
        just blew up. If a candidate that raises kept its cursor, it would stay pinned
        at the front of the rotation and re-raise on every cycle for ever, which is the
        head-of-line starvation this ordering exists to remove — now caused by the one
        row least likely to ever succeed.

        Stamping AFTER the handler (not before, not as part of a re-put of `env`) is
        what makes it clobber-free: `touch_scanned` writes the single cursor path on
        whatever the CURRENT stored document is, so it cannot undo the handler's own
        `put` and cannot revert a concurrent S7 inbox transition. It also cannot fail
        the cycle — a cursor is bookkeeping, so a store hiccup here is logged and
        swallowed rather than being allowed to mask the real decision."""
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

        # All guards pass → promote, stamping the clean drift `drift` already holds
        # (the passing replay IS the live grain_integrity probe), so it is immediately
        # silent-eligible. When the verdict was REUSED, `drift` is the stored stamp with
        # its ORIGINAL `last_drift_check_at` — deliberately not re-dated to now: the
        # blueprint is promoted on evidence gathered at that earlier moment, and moving
        # the timestamp forward would claim a probe that did not run and hand the silent
        # fast path a full trust window it did not earn. The stamp can be at most
        # `replay_recheck_interval_seconds` old, which the policy keeps inside
        # `drift_freshness_seconds`, so a freshly promoted blueprint is still
        # silent-eligible either way.
        #
        # With a real landing writer wired, LAND into the neo4j retrieval corpus FIRST,
        # then CAS `validated` (§3.1); a landing failure HOLDS `landing_failed` and the
        # candidate stays `candidate` (not landed ⇒ not validated). Without a writer
        # (require_landing off), promote directly (today's baseline behavior — nothing
        # to land into).
        if self._landing_writer is not None:
            # Capture the entity spans S5 identified BEFORE the strip (D17 last gate) —
            # `_land_and_promote` strips, which blanks `entity_scan`, so the forbidden
            # spans must be read from the PRE-strip envelope here.
            return await self._land_and_promote(
                env,
                drift,
                action="promote",
                forbidden_spans=entity_spans(env),
                verified=False,  # auto-landed → unverified until a human approves
            )
        with self._promote_scope(env, "promote"):
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
        """VERIFY a `validated` learning node (Phase-3 inbox VERIFY action) — the SINGLE
        implementation `ReviewInbox.verify` delegates to. Flips `verified → true` on BOTH
        the landed neo4j node AND the candidate-store envelope, and RETURNS whether the
        node write actually landed (`node_stamped`) so the caller can surface a re-verify
        prompt when it did not.

        The node is stamped FIRST, then the authoritative envelope is written:
          * a CRASH BETWEEN the two writes is the SAFE direction (node verified, envelope
            not → the candidate still shows verifiable, a re-verify converges it);
          * the node write FAILS OPEN (`_verify_corpus`) so a neo4j hiccup never BLOCKS
            the verify — but that leaves the envelope `verified=true` over an unverified
            node. That gap is NOT silent: it is REPORTED via `node_stamped=False`, and a
            re-verify converges the two (the envelope write is idempotent and
            `mark_verified` re-runs).

        Status stays `validated` — verify is a flag flip, not a lifecycle transition, and
        does NOT move the node into the trusted MCP recall partition (that is the promote
        action's manual-PR reseed). Idempotent for a non-validated env (a no-op hold with
        `node_stamped=False`; a mis-routed verify never mutates)."""
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
        """PROMOTE a verified learning node to the terminal `promoted` state (Phase-3
        inbox PROMOTE action) — the store-side move `ReviewInbox.promote` delegates to
        AFTER it has emitted the MCP-format YAML. Requires `validated` + `verified`
        (fail-loud hold otherwise) and moves `validated → promoted` so the candidate
        drops out of the inbox validated listing (optimistic — the actual `learning→mcp`
        reseed happens when the human merges the emitted YAML PR; if they never do, the
        node stays `source='learning'`/excluded and the candidate stays `promoted`).

        Purely a candidate-store transition: it does NOT touch neo4j (the node stays
        `source='learning'` until the manual PR reseeds it) and does NOT touch git."""
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
        """`env.drift` when it is a golden-replay verdict still inside the re-check
        window, else `None` meaning "pay for a real replay".

        Two things are load-bearing here, both of them fail-SAFE in the direction of
        spending money rather than fabricating a verdict:

          * The window is `min(replay_recheck_interval_seconds,
            drift_freshness_seconds)`. The policy documents the invariant that the
            re-check interval sits inside the trust window; this ENFORCES it at the
            point of use, so a misconfiguration can only make the scheduler probe more
            often — never make it reuse a verdict for longer than its own policy says
            that verdict may be believed.
          * `now` is parsed from the injected clock, and an unparseable/naive clock
            yields `None` (`_is_fresh` requires both sides aware). That degrades to the
            pre-rate-limit behaviour — always replay — which is correct but expensive,
            rather than to "everything looks fresh", which would silently disable the
            structural gate."""
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
        """The learned blueprint intent for the verbose promote/land span (entity-
        bearing, D25-gated). `None` for a non-blueprint / payload-less env."""
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
        verified: bool = False,
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
        last-gate defense, which RAISES if the strip regressed and let one through.

        *verified* (Phase-3) is the human-approval flag stamped onto BOTH the landed
        neo4j node AND the candidate-store envelope so the two never diverge: the
        human-approve path passes `verified=True`, the auto path `verified=False`."""
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
        """FAIL-OPEN corpus write-back (S9-activation Slice 3, §8.6): stamp the landed
        neo4j node's recall-eligibility (`status`/`drift_status`) via the landing
        writer, keyed by the same deterministic landing id.

        The store transition is SOURCE-OF-TRUTH and must SUCCEED even if this write-back
        fails, so EVERY exception is swallowed and logged LOUDLY (never re-raised) — a
        demote/reject/correction is never blocked by a neo4j hiccup. The recall filter's
        coalesce default is fail-OPEN for an UN-stamped node (a node that never got
        stamped still reads `validated`/`clean` ⇒ recallable), so the filter alone is NOT
        a backstop for a FAILED demote write-back. Convergence is provided by the
        RE-ASSERTS instead, which fire on EVERY EXAMINATION of the artifact:
          * a landed-type artifact in the `candidate` scan re-stamps its node with its
            CURRENT status/drift every `_advance_candidate` pass (the demote direction);
          * a still-validated blueprint's clean `_recheck_validated` re-stamps it
            `validated`/`clean` (the self-heal direction).
        Neither is keyed on the drift STAMP — a re-assert whose trigger a later write can
        erase is not a convergence guarantee (see the comment in `_advance_candidate`).
        Both are also outside the golden-replay rate limit: a re-assert is one idempotent
        Cypher, and only the warehouse probe is expensive enough to ration.

        "Every examination" is once per cycle while the backlog fits in `scan_limit`, and
        once per rotation period beyond that (the scan is ordered by `last_scanned_at`, so
        the window round-robins rather than pinning the oldest rows). So a transient
        failure at demote is retried on each subsequent examination and converges the node
        to non-recallable once neo4j recovers.

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

        Called ONLY on the two terminal edges (reject, retract). Keyed by the S6
        `canonical_key`, which is the artifact's identity — no dedup verdict means S6
        never ran, so there is no artifact and nothing to stamp (a clean no-op, not an
        error: a human-approved candidate that skipped S6 is a supported path, OQ-3).

        Fail-open for the same reason `_retract_corpus` is: the candidate-store
        transition is source of truth and a human's reject must never be blocked by a
        Couchbase hiccup. The cost of a lost stamp is bounded and self-correcting in the
        direction that matters — the artifact stays visible as prior art, so the worst
        case is one extra candidate reaching a human, not a bad landing. Unlike the
        neo4j write-back there is deliberately NO periodic re-assert: the terminal edges
        are one-shot human actions with no recurring scan behind them, and inventing a
        convergence loop for a review-noise-grade failure would be more machinery than
        the risk justifies. The warning is the recovery path."""
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
        """FAIL-OPEN corpus verify write-back (Phase-3): flip the landed node's
        `verified` flag true via the landing writer, keyed by the deterministic landing
        id. RETURNS whether a landed node was actually stamped — False when no writer is
        wired, no node was landed, OR the write failed — so `apply_verify` can report a
        re-verify prompt (`node_stamped`). The envelope write in `apply_verify` is
        source-of-truth and must succeed even if this fails, so every exception is
        swallowed + logged (never re-raised) — a neo4j hiccup never blocks a human verify.
        No writer wired (dormant) ⇒ nothing landed ⇒ False. Idempotent for a never-landed
        node (the MATCH-by-id misses → False)."""
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
