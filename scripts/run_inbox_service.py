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

from data_agent.learning.inbox.service import create_inbox_app

# Built at import time so `uvicorn scripts.run_inbox_service:app` also works.
app = create_inbox_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Loopback by default: the service holds the write plane and its only intended
    # caller is the co-located BFF proxy — bind wider (0.0.0.0) only deliberately.
    host = os.environ.get("INBOX_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("INBOX_SERVICE_PORT", "8100"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
