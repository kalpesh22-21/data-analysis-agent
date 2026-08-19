#!/usr/bin/env python
"""run_learning_scheduler — the learning-loop PROMOTION SCHEDULER entrypoint (D29, §7.2).

A SEPARATE cron-scanned process (D29 "cron-scanned state, not queued"), DISTINCT from
the consumer daemon: each cycle it reads `learning_candidates` by status, runs golden
replay + the D43 drift probes, and advances `status` (Contract E). It parallelizes
against the write-router, so it runs as its own process — `run_forever` here, or
`run_once` from an external cron trigger.

Composed via `build_promotion_plane`, which pins ONE shared candidate store across the
scheduler and the review inbox (a split store would let the inbox read a stale envelope
from one store while the scheduler CAS-writes another). The durable
`CouchbaseBlueprintCorpus` doubles as the `HitCountReader` port — one source of truth
for the cross-session `hit_count` the promotion guard reads.

DORMANT + fail-closed by default:
  * The D58c kill-switch is read FRESH every cycle; disabled ⇒ no scan, no transition.
  * The REAL golden-replay probe + dependency resolver are wired only when the write
    plane is configured (`MCP_URL` + `TOKEN_SERVICE_URL` + `TOKEN_ISSUER_API_KEY`); the
    probe runs the D56 grain probe through the MCP `runQuery` choke point under a
    per-blueprint JWT scoped to the blueprint's `uses` (D57 reuse). Otherwise the
    fail-closed DEFERRED stubs stay (probe raises ⇒ clean `probe_unavailable` hold;
    resolver reports unresolved), so the scheduler runs safely and promotes nothing.
  * Auto-promotion INTO RETRIEVAL stays GATED: with no corpus-landing writer,
    `require_landing=True` + `landing_writer=None` makes a blueprint that passes the
    replay gate HOLD `landing_unavailable`, rather than validate one that never becomes
    recallable. The human `approve` path for a BLUEPRINT holds the same way; only the
    human-gated targets (global_knowledge/schema_edit) approve without a replay.

Environment: `RuntimeSettings` (COUCHBASE_*, MCP_URL, TOKEN_SERVICE_URL,
TOKEN_ISSUER_API_KEY) + `LearningSettings` (LEARNING_CANDIDATES_*, LEARNING_CORPUS_*).
Traced to the Phoenix `learning-loop` project.

Usage:
    uv run python scripts/run_learning_scheduler.py
"""

from __future__ import annotations

import asyncio
import logging

from data_agent.daemon import run_daemon
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import get_learning_settings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.entrypoint import configure_daemon_process
from data_agent.learning.factory import build_promotion_plane, build_promotion_write_plane
from data_agent.learning.promotion.models import ProbeResult
from data_agent.learning.promotion.token_minter import HttpTokenMinter, TenantClaims
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.real_client import RealMCPClient
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient

_logger = logging.getLogger(__name__)


class _DeferredWarehouseProbe:
    """Fail-closed `WarehouseProbe` stub (real ClickHouse golden-replay deferred).

    Raising here makes the scheduler's per-candidate guard HOLD every blueprint on
    the replay gate (never a crash — `run_once` catches per item), so no candidate
    auto-promotes until a real probe is wired."""

    async def run(
        self,
        sql: str,
        *,
        grain_columns: tuple[str, ...],
        column_scope: tuple[str, ...] = (),
    ) -> ProbeResult:
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
    learning_settings = get_learning_settings()
    # The scheduler does not thread a tracer into its components — it relies on the
    # process-global provider this installs — so the return value is discarded.
    configure_daemon_process("scheduler", learning_settings, _logger)

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

    # S9-activation Slice 2: wire the FULLY-ACTIVATED write plane (probe + resolver +
    # corpus-landing writer) as a UNIT when EVERY real port is configured — the MCP +
    # token-mint credentials AND the neo4j + embedding endpoints the landing writer
    # needs. With a real writer present, `require_landing` is ON and a validated
    # blueprint LANDS into the neo4j retrieval corpus (becomes recallable) BEFORE its
    # `validated` status write. Missing ANY port ⇒ keep the fail-closed deferred stubs
    # with no landing writer (auto-promotion stays dormant, fail-closed).
    runtime_settings = RuntimeSettings()
    write_plane_ready = bool(
        runtime_settings.mcp_url
        and runtime_settings.token_service_url
        and runtime_settings.token_issuer_api_key
        # A blank tenant claim is as disabling as a blank mint credential: the MCP
        # rejects every replay 403 MISSING_TENANT_CLAIM before any tool runs. Gate on
        # it so the daemon stays in the LOGGED dormant posture (deferred probe, nothing
        # auto-promotes) instead of running a golden-replay gate that cannot execute.
        and runtime_settings.tenant_client_code.strip()
        and runtime_settings.tenant_proc_center.strip()
        and runtime_settings.tenant_jti.strip()
        and runtime_settings.neo4j_url
        and runtime_settings.neo4j_username
        and runtime_settings.neo4j_password
        and runtime_settings.embedding_api_url
    )
    # State the tenant identity ONCE, at startup, in EVERY posture. A wrong tenant has
    # no observable consequence downstream: row policies FILTER rather than error, so a
    # replay against the wrong tenant matches zero rows and the grain teeth read
    # `0 == 0` and pass — a green that means nothing (see `TenantClaims`). This line is
    # the only place that fact is recoverable, so it is logged whether the write plane
    # is ready or not; blanks print as `<unset>` so a FORGOTTEN claim reads as a missing
    # value rather than as a gap in the message.
    _logger.info(
        "offline golden replay runs AS tenant clientcode=%s proc_center=%s jti=%s "
        "(deployment config TENANT_*; any blank one holds the write plane dormant)",
        runtime_settings.tenant_client_code.strip() or "<unset>",
        runtime_settings.tenant_proc_center.strip() or "<unset>",
        runtime_settings.tenant_jti.strip() or "<unset>",
    )
    neo4j_driver = None
    if write_plane_ready:
        from neo4j import AsyncGraphDatabase

        neo4j_driver = AsyncGraphDatabase.driver(
            runtime_settings.neo4j_url,
            auth=(runtime_settings.neo4j_username, runtime_settings.neo4j_password),
            connection_timeout=runtime_settings.neo4j_timeout_seconds,
            connection_acquisition_timeout=runtime_settings.neo4j_timeout_seconds,
            max_transaction_retry_time=runtime_settings.neo4j_timeout_seconds,
        )
        # build_promotion_write_plane pins ONE candidate store across the scheduler +
        # inbox, wraps the injected infra clients into the three write-plane ports, and
        # flips require_landing ON with the real landing writer present.
        scheduler, _inbox = build_promotion_write_plane(
            learning_settings,
            candidate_store=candidate_store,
            hit_counts=corpus,
            mcp_client=RealMCPClient(runtime_settings.mcp_url),
            token_minter=HttpTokenMinter(
                runtime_settings.token_service_url,
                runtime_settings.token_issuer_api_key,
                # WHICH TENANT the background replay runs as. There is no caller here
                # to resolve it from, so it is deployment config (TENANT_*, the SAME
                # env vars ui/server.py reads). The replay carries no user's authority
                # — it asks a structural question — but a blueprint verified against
                # one tenant's data is verified against that tenant's data only; see
                # `TenantClaims` for the full argument and its limits.
                tenant=TenantClaims(
                    clientcode=runtime_settings.tenant_client_code,
                    proc_center=runtime_settings.tenant_proc_center,
                    jti=runtime_settings.tenant_jti,
                ),
            ),
            neo4j_driver=neo4j_driver,
            embedding_client=HttpEmbeddingClient(
                url=runtime_settings.embedding_api_url,
                api_key=runtime_settings.embedding_api_key,
                model=runtime_settings.embedding_model,
                timeout_seconds=runtime_settings.embedding_timeout_seconds,
            ),
            model_id=runtime_settings.embedding_model,
            # The SAME `corpus` object already passed as `hit_counts`
            # (`CouchbaseBlueprintCorpus` duck-types both ports). The terminal
            # transitions stamp the artifact's `status` so a rejected/retired blueprint
            # stops surfacing as live prior art to the dedup stage; the promotion guard
            # reads the count off those same artifacts, so the two MUST be one object.
            corpus_status=corpus,
        )
    else:
        # Dormant / fail-closed: the deferred stubs auto-promote nothing, and with no
        # landing writer wired `require_landing=False` keeps the scheduler quiet (the
        # deferred probe already HOLDS every blueprint on the replay gate).
        scheduler, _inbox = build_promotion_plane(
            learning_settings,
            candidate_store=candidate_store,
            probe=_DeferredWarehouseProbe(),
            hit_counts=corpus,
            dependency_resolver=_DeferredDependencyResolver(),
            landing_writer=None,
            require_landing=False,
            # Wired even in the dormant posture: the terminal-status stamp is a
            # Couchbase write on a store this process ALWAYS has (the entrypoint
            # refuses to start without it), and it is entirely independent of the
            # neo4j write plane. A human rejecting a candidate through the inbox must
            # kill its corpus artifact whether or not auto-promotion is active.
            corpus_status=corpus,
        )

    _logger.info(
        "learning promotion scheduler starting (write_plane_ready=%s; %s; kill-switch "
        "honored per cycle)",
        write_plane_ready,
        "FULL auto-promotion + neo4j landing ACTIVE"
        if write_plane_ready
        else "dormant deferred stubs (probe_unavailable, no landing)",
    )
    try:
        await scheduler.run_forever(sleep=asyncio.sleep)
    finally:
        if neo4j_driver is not None:
            await neo4j_driver.close()
    return 0


if __name__ == "__main__":
    # `run_daemon`, not `asyncio.run`: PID 1 drops an unhandled SIGTERM, so the rollout
    # would SIGKILL this process mid-cycle and skip the `finally` that closes the neo4j
    # driver pool. See `data_agent/daemon.py`.
    raise SystemExit(run_daemon(_main, logger=_logger, process="promotion scheduler"))
