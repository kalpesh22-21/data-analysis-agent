#!/usr/bin/env python
"""run_learning_scheduler — the learning-loop PROMOTION SCHEDULER entrypoint (D29, §7.2).

The S9 promotion scheduler is a SEPARATE cron-scanned process (D29 "cron-scanned
state, not queued"), DISTINCT from the consumer daemon (D96 §g). Each cycle it reads
`learning_candidates` by status, runs golden replay + the D43 drift probes, and
advances `status` (Contract E). It is NOT a consumer stage — it parallelizes against
the write-router — so it runs as its own process, launched here via
`scheduler.run_forever(sleep=asyncio.sleep)` (or `run_once` from an external cron
trigger).

Composed via the Wave-3 composition-root `build_promotion_plane`, which pins ONE
shared candidate store across the scheduler and the review inbox (a split store would
let the inbox read a stale envelope from one store while the scheduler CAS-writes
another). The durable `CouchbaseBlueprintCorpus` doubles as the `HitCountReader` port
(it is the one source of truth for the cross-session `hit_count` the promotion guard
reads — the same artifacts S6 seeds + increments).

DORMANT + fail-closed by default:
  * The D58c kill-switch is read FRESH every cycle inside `run_once`; disabled ⇒ no
    scan, no transition.
  * The real `WarehouseProbe` (golden-replay SQL against ClickHouse) and the
    `DependencyResolver` (neo4j `depends_on` resolution) are DEFERRED infra with no
    production client yet. Until they land, this entrypoint wires fail-closed stubs:
    the probe raises (`golden_replay` catches it → a clean `probe_unavailable` hold, so
    every blueprint HOLDS on the replay gate — NOT a raise), the resolver reports
    unresolved (⇒ dependent candidates HOLD). The scheduler therefore runs safely but
    AUTO-PROMOTES NO BLUEPRINT. The human `approve` path shares that replay gate for a
    BLUEPRINT (a blueprint approve is likewise blocked `probe_unavailable`, degrading
    cleanly, never a raise); only the human-gated targets (global_knowledge/schema_edit)
    approve without a replay and so remain fully approvable via the S7 inbox. Wiring the
    real probe/resolver is the follow-on that activates count-based auto-promotion (and
    blueprint approval).

Environment: `RuntimeSettings` (COUCHBASE_*) + `LearningSettings`
(LEARNING_CANDIDATES_*, LEARNING_CORPUS_*). Traced to the Phoenix `learning-loop`
project.

Usage:
    uv run python scripts/run_learning_scheduler.py
"""

from __future__ import annotations

import asyncio
import logging

from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.factory import build_promotion_plane
from data_agent.learning.observability import configure_learning_tracing, get_learning_tracer
from data_agent.learning.promotion.models import ProbeResult
from data_agent.runtime.observability.tracing import set_global_tracer_provider

_logger = logging.getLogger(__name__)


class _DeferredWarehouseProbe:
    """Fail-closed `WarehouseProbe` stub (real ClickHouse golden-replay deferred).

    Raising here makes the scheduler's per-candidate guard HOLD every blueprint on
    the replay gate (never a crash — `run_once` catches per item), so no candidate
    auto-promotes until a real probe is wired."""

    async def run(self, sql: str, *, grain_columns: tuple[str, ...]) -> ProbeResult:
        # `golden_replay` catches this and returns a clean `probe_unavailable` hold —
        # the scheduler never sees the raise (no per-candidate traceback spam), the
        # human approve path never surfaces a 500.
        raise NotImplementedError(
            "no warehouse probe wired; golden replay is deferred — every blueprint "
            "holds on the replay gate (no auto-promotion, no blueprint approval)"
        )


class _DeferredDependencyResolver:
    """Fail-closed `DependencyResolver` stub (real neo4j resolution deferred). Reports
    every `depends_on` ref UNRESOLVED, so a dependent candidate holds — the safe
    direction (never promote against an unresolved dependency)."""

    async def is_resolved(self, ref: str) -> bool:
        return False


async def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    learning_settings = LearningSettings()

    provider = configure_learning_tracing(
        otlp_endpoint=learning_settings.otlp_endpoint,
        service_name=learning_settings.learning_service_name,
    )
    set_global_tracer_provider(provider)
    _ = get_learning_tracer(provider)

    candidates_ready = bool(
        learning_settings.learning_candidates_username
        and learning_settings.learning_candidates_password
    )
    corpus_ready = bool(
        learning_settings.learning_corpus_username and learning_settings.learning_corpus_password
    )
    if not (candidates_ready and corpus_ready):
        # NON-zero exit (not 0) so a supervisor (systemd/k8s) ALERTS on a misprovisioned
        # deploy rather than treating the immediate exit as a clean shutdown.
        _logger.error(
            "promotion scheduler NOT started: candidates_ready=%s corpus_ready=%s — "
            "provision learning_candidates AND learning_corpus (their RBAC users) to "
            "run the scheduler",
            candidates_ready,
            corpus_ready,
        )
        return 1

    candidate_store = CouchbaseCandidateStore(learning_settings)
    corpus = CouchbaseBlueprintCorpus(learning_settings)

    # build_promotion_plane pins ONE candidate store across the scheduler + inbox.
    # The corpus is the HitCountReader (cross-session hit_count source of truth).
    scheduler, _inbox = build_promotion_plane(
        learning_settings,
        candidate_store=candidate_store,
        probe=_DeferredWarehouseProbe(),
        hit_counts=corpus,
        dependency_resolver=_DeferredDependencyResolver(),
    )

    _logger.info(
        "learning promotion scheduler starting (auto-promotion DORMANT until a real "
        "warehouse probe + dependency resolver are wired; kill-switch honored per cycle)"
    )
    await scheduler.run_forever(sleep=asyncio.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
