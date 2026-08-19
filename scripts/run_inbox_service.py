#!/usr/bin/env python
"""run_inbox_service — the review-inbox HTTP service entrypoint (UI Slice 2, §3).

A SMALL, dedicated FastAPI process that mounts ONE `ReviewInbox` over the promotion
write plane and exposes the reviewer routes. The UI BFF (`ui/server.py`) proxies
`/api/inbox/*` here, attaching the shared `X-Reviewer-Token` server-side.

DORMANT by default: every route 404s unless `REVIEW_INBOX_ENABLED=1`, and each mutating
call requires the `X-Reviewer-Token` header to match `REVIEWER_TOKEN`. When the full
write plane (couchbase + MCP + token + neo4j + embedding) is NOT configured, the service
runs in OFFLINE dev mode (list/reject/retract work; a landing-approve honestly 503s).

**Importing this module builds NOTHING.** `create_inbox_app` is handed to the shared
HTTP daemon wrapper as a FACTORY and called inside the running event loop — never at
module scope, where a full-plane collaborator that connects in `__init__` becomes a
crash on exactly the deploys that provision it correctly (see docs/cleanup/WORKLOG.md
#14). Anything that wants an ASGI factory should point at the real one:
`uvicorn data_agent.learning.inbox.service:create_inbox_app --factory` — note that path
skips `configure_daemon_process`, so prefer the script.

Usage:
    REVIEW_INBOX_ENABLED=1 REVIEWER_TOKEN=... uv run python scripts/run_inbox_service.py
"""

from __future__ import annotations

import logging
import os

from data_agent.http_daemon import run_http_daemon
from data_agent.learning.config import get_learning_settings
from data_agent.learning.entrypoint import configure_daemon_process
from data_agent.learning.inbox.service import create_inbox_app

_logger = logging.getLogger(__name__)


def main() -> int:
    # The SAME startup preamble the sweeper/consumer/scheduler run. This process is a
    # learning-plane daemon like the others and had NEITHER half of it: no tracing
    # posture line and no LEARNING_* typo warning, so a mistyped variable here applied
    # the shipped default in total silence. With `OTLP_ENDPOINT` unset (the default)
    # `configure_learning_tracing` still builds a NO-OP provider, so this changes
    # nothing for an operator who has not asked for tracing.
    #
    # It now runs on EVERY path into this service, because there is only one (see the
    # module docstring). Sync and loop-free, so it stays out here, ahead of the loop.
    configure_daemon_process("inbox", get_learning_settings(), _logger)
    # Loopback by default: the service holds the write plane and its only intended
    # caller is the co-located BFF proxy — bind wider (0.0.0.0) only deliberately.
    host = os.environ.get("INBOX_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("INBOX_SERVICE_PORT", "8100"))
    # `run_http_daemon`, not `uvicorn.run`/a bare `Server.serve()`: it builds the app IN
    # the loop (the reason this script exists — `acouchbase.Cluster` refuses to be
    # constructed without one) AND it chains uvicorn's SIGTERM re-raise so a rollout
    # exits 0 instead of dying by signal at 143 (C3). See `data_agent/http_daemon.py`;
    # the startup-failure code and the swallowed Ctrl-C moved there verbatim.
    return run_http_daemon(
        create_inbox_app, host=host, port=port, logger=_logger, process="inbox"
    )


if __name__ == "__main__":
    raise SystemExit(main())
