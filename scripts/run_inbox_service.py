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

Usage:
    REVIEW_INBOX_ENABLED=1 REVIEWER_TOKEN=... uv run python scripts/run_inbox_service.py
"""

from __future__ import annotations

import logging
import os

import uvicorn

from data_agent.learning.config import LearningSettings
from data_agent.learning.entrypoint import configure_daemon_process
from data_agent.learning.inbox.service import create_inbox_app

_logger = logging.getLogger(__name__)

# Built at import time so `uvicorn scripts.run_inbox_service:app` also works.
app = create_inbox_app()


def main() -> None:
    # The SAME startup preamble the sweeper/consumer/scheduler run. This process is a
    # learning-plane daemon like the others and had NEITHER half of it: no tracing
    # posture line and no LEARNING_* typo warning, so a mistyped variable here applied
    # the shipped default in total silence. With `OTLP_ENDPOINT` unset (the default)
    # `configure_learning_tracing` still builds a NO-OP provider, so this changes
    # nothing for an operator who has not asked for tracing.
    #
    # NOTE: it does NOT run under `uvicorn scripts.run_inbox_service:app`, which
    # bypasses `main()` entirely — the `app` above is built at import time for exactly
    # that entry path. That gap is pre-existing and is a property of having two ways in.
    configure_daemon_process("inbox", LearningSettings(), _logger)
    # Loopback by default: the service holds the write plane and its only intended
    # caller is the co-located BFF proxy — bind wider (0.0.0.0) only deliberately.
    host = os.environ.get("INBOX_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("INBOX_SERVICE_PORT", "8100"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
