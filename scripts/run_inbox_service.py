#!/usr/bin/env python
"""run_inbox_service — the review-inbox HTTP service entrypoint (UI Slice 2, §3).

A SMALL, dedicated FastAPI process (Option A) that mounts ONE `ReviewInbox` over the
promotion write plane and exposes the reviewer routes. The UI BFF (`ui/server.py`)
proxies `/api/inbox/*` here, attaching the shared `X-Reviewer-Token` server-side.

DORMANT by default: every route 404s unless `REVIEW_INBOX_ENABLED=1`, and each
mutating call requires the `X-Reviewer-Token` header to match `REVIEWER_TOKEN`. When
the full write plane (couchbase + MCP + token + neo4j + embedding) is NOT configured,
the service runs in OFFLINE dev mode (list/reject/retract work; a landing-approve
honestly 503s).

**Importing this module builds NOTHING.** The app is constructed inside the running
event loop (`_serve`).

The reason is that `create_inbox_app`'s full-plane branch composes durable infra, and
`acouchbase.Cluster(...)` REFUSES to be constructed without a running loop — it raises
`RuntimeError: Event loop is not running` (SDK 4.6.2), which import-time construction
turned into "the full write plane cannot start at all, on every deploy that provisions
it correctly" (ISSUES.md H7). The `Cluster` is no longer built in
`CouchbaseCandidateStore.__init__` — the Tier-3 lazy Couchbase seam (`b7b21c1`) moved
it behind the first `_ensure_connected()` — so the crash is not reachable from here
today. It is one refactor away from returning: a store that reverts to connecting
eagerly, or any new full-plane collaborator that builds a cluster/driver in `__init__`,
resurrects it verbatim and only in the configuration that matters. Building the app at
module scope (which is what "so `uvicorn scripts.run_inbox_service:app` also works"
bought) is the property that made a lazily-fixed constructor the only thing standing
between a deploy and a crash, so that second way in is gone.

Anything that wants an ASGI factory should point at the real one, which has always been
importable and is now the ONLY entry: `uvicorn data_agent.learning.inbox.service:create_inbox_app
--factory` (uvicorn calls a `--factory` app inside its own running loop, so acouchbase is
happy). Note that path skips `configure_daemon_process` — prefer the script.

Usage:
    REVIEW_INBOX_ENABLED=1 REVIEWER_TOKEN=... uv run python scripts/run_inbox_service.py
"""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from data_agent.learning.config import LearningSettings
from data_agent.learning.entrypoint import configure_daemon_process
from data_agent.learning.inbox.service import create_inbox_app

_logger = logging.getLogger(__name__)


async def _serve(host: str, port: int) -> None:
    """Build the app IN the loop, then serve it.

    `uvicorn.run(app, ...)` would be equivalent for the app object, but it takes an app
    that already exists — and there is nowhere outside a running loop to build this one.
    Driving `uvicorn.Server` directly is the smallest shape that puts `create_inbox_app`
    on the right side of `asyncio.run`, and it keeps everything about the served app
    identical: uvicorn still runs the app's lifespan (the `shutdown` hook that closes the
    neo4j driver) and still installs its own SIGTERM/SIGINT handlers, so this process
    needs none of `data_agent.daemon` — unlike the four non-HTTP workers.
    """
    app = create_inbox_app()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    await server.serve()
    # Parity with `uvicorn.run()`, which ends in exactly this check: driving `Server`
    # by hand is otherwise identical, but it drops the one thing `run()` does AFTER
    # serving. A failed lifespan `startup` hook does not raise out of `serve()` — uvicorn
    # logs "Application startup failed. Exiting." and RETURNS NORMALLY — so without this
    # a process that never served a request would exit 0, and Kubernetes would mark the
    # pod Completed instead of restarting it. `started` stays True once the boot
    # succeeded (shutdown never clears it), so an ordinary SIGTERM/SIGINT still falls
    # through here.
    #
    # Unreachable today: this app registers only a `shutdown` hook, so startup cannot
    # fail. It is the guard for the first `startup` hook anyone adds — a warm-up that
    # pings couchbase, say, which is the shape of the H7 failure. Note a bind clash is
    # NOT this path: `Config.bind_socket` calls `sys.exit(1)` itself, under `run()` too.
    if not server.started:
        raise SystemExit(3)  # uvicorn.main.STARTUP_FAILURE — parity with uvicorn.run()


def main() -> None:
    # The SAME startup preamble the sweeper/consumer/scheduler run. This process is a
    # learning-plane daemon like the others and had NEITHER half of it: no tracing
    # posture line and no LEARNING_* typo warning, so a mistyped variable here applied
    # the shipped default in total silence. With `OTLP_ENDPOINT` unset (the default)
    # `configure_learning_tracing` still builds a NO-OP provider, so this changes
    # nothing for an operator who has not asked for tracing.
    #
    # It now runs on EVERY path into this service, because there is only one (see the
    # module docstring). Sync and loop-free, so it stays out here, ahead of the loop.
    configure_daemon_process("inbox", LearningSettings(), _logger)
    # Loopback by default: the service holds the write plane and its only intended
    # caller is the co-located BFF proxy — bind wider (0.0.0.0) only deliberately.
    host = os.environ.get("INBOX_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("INBOX_SERVICE_PORT", "8100"))
    try:
        asyncio.run(_serve(host, port))
    except KeyboardInterrupt:
        # uvicorn (0.49) captures SIGINT itself, shuts down, and then RE-RAISES it out
        # of `serve()` (`Server.capture_signals` calls `signal.raise_signal` on the way
        # out). `uvicorn.run()` swallows that KeyboardInterrupt; so must we, or a dev's
        # Ctrl-C prints a traceback over an otherwise clean shutdown.
        pass


if __name__ == "__main__":
    main()
