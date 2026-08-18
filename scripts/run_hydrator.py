#!/usr/bin/env python
"""run_hydrator — the singleton neo4j hydrator daemon entrypoint.

Composes a `Hydrator` from real infra (an async neo4j driver + the D71 embedding client
+ the two service-key MCP export clients) and runs the periodic re-seed loop. The
`HYDRATOR_ENABLED` kill-switch is read FRESH at the top of every cycle (inside
`run_once`), so flipping it halts seeding without a restart.

This is a true `replicas:1` singleton (owns the destructive nuke/rebuild), so there is
no distributed lock. When neo4j OR the embedding API is unconfigured, `build_hydrator`
returns `None` and this process IDLES (never crash-loops) — Phase-0 parity.

Environment: `RuntimeSettings` (NEO4J_*, EMBEDDING_*, MCP_URL/MCP_SERVICE_KEY,
HYDRATOR_POLL_INTERVAL_SECONDS, HYDRATOR_ENABLED).

Usage:
    uv run python scripts/run_hydrator.py
"""

from __future__ import annotations

import asyncio
import logging

from data_agent.daemon import run_daemon
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.observability import tracing
from data_agent.runtime.retrieval.hydrator import build_hydrator

_logger = logging.getLogger(__name__)


async def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_runtime_settings()

    tracer_provider = tracing.configure_tracing(
        otlp_endpoint=settings.otlp_endpoint,
        service_name="data-agent-hydrator",
        project_name="data-agent-hydrator",
    )
    tracer = tracing.get_tracer(tracer_provider)

    hydrator = build_hydrator(settings, tracer=tracer)
    if hydrator is None:
        # Unavailable or MISCONFIGURED: neo4j / the embedding API is unconfigured (nothing
        # to seed — Phase-0 parity), OR MCP_SERVICE_KEY is empty (build_hydrator already
        # logged the unmistakable error — refusing to poll with blank creds). IDLE forever
        # rather than exit (a bare exit would crash-loop under a Deployment restart).
        _logger.warning(
            "hydrator not started (prerequisites absent or MCP_SERVICE_KEY unset) — "
            "idling; see the preceding log line for which."
        )
        await asyncio.Event().wait()
        return 0  # unreachable; keeps the signature honest

    _logger.info(
        "hydrator starting (poll_interval=%ss, model=%s)",
        settings.hydrator_poll_interval_seconds,
        settings.embedding_model,
    )
    try:
        await hydrator.run_forever(sleep=asyncio.sleep)
    finally:
        await hydrator.close()
    return 0


if __name__ == "__main__":
    # `run_daemon`, not `asyncio.run`: PID 1 drops an unhandled SIGTERM, so a rollout
    # would SIGKILL this daemon mid-seed and skip `hydrator.close()`. It also makes the
    # IDLE posture above (`asyncio.Event().wait()` forever, when neo4j/embedding are
    # unconfigured) actually STOPPABLE — that wait answers nothing else.
    # See `data_agent/daemon.py`.
    raise SystemExit(run_daemon(_main, logger=_logger, process="hydrator"))
