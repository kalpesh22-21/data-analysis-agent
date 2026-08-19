#!/usr/bin/env python
"""run_ui_bff — the UI backend-for-frontend entrypoint (`ui/server.py`).

ONE launcher for TWO Deployments, because they are one process: the data-agent chart's
`ui` (the agent chat shell) and the data-agent-learning chart's `inbox-ui` (the reviewer
surface) both ran `uvicorn ui.server:app --host 0.0.0.0 --port 3000`, and the only thing
that makes the second copy a reviewer UI is its env (`REVIEW_INBOX_ENABLED=1` +
`INBOX_SERVICE_URL`). The split stays configuration, not code — this script reads no env
of its own and passes none.

Why not the uvicorn CLI: uvicorn re-raises the SIGTERM it captured once `serve()`
returns, onto the `SIG_DFL` it restored first, so a CLEAN shutdown still killed the pod
by signal at 143 and every rollout looked like a crash (ISSUES.md C3). `run_http_daemon`
chains that re-raise onto a handler installed first and exits 0.

Host and port are COMMAND-LINE flags, mirroring what the chart passed to uvicorn; the
defaults are loopback and the BFF's usual :3000, so a bare local run does not publish on
every interface by accident.

Usage:
    uv run python scripts/run_ui_bff.py --host 0.0.0.0 --port 3000
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from data_agent.http_daemon import run_http_daemon

# `ui/` ships as SOURCE at the repo root (it is not part of the installed package), and
# what used to put it on `sys.path` was the CWD: `uvicorn ui.server:app` was run from
# /app and uvicorn prepends "". `python scripts/run_ui_bff.py` prepends the SCRIPT's
# directory instead, so without this line `import ui.server` fails at boot in the image.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 3000


def create_ui_app() -> Any:
    """Return the BFF app. Handed to uvicorn as a `factory=True` app, so it runs from
    `config.load()` INSIDE `serve()`.

    `ui.server` builds its `app` at module scope, so the import IS the composition and it
    belongs in here: at module scope it would run before any SIGTERM handler exists, and
    a stop arriving in that window is exactly the 143 this launcher exists to prevent.
    The CLI imported it inside `serve()` as well (`Config.load()` →
    `import_from_string`), so this is parity.
    """
    from ui.server import app

    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the data-agent UI backend-for-frontend.")
    parser.add_argument(
        "--host", default=_DEFAULT_HOST, help="interface to bind (default: %(default)s)"
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help="TCP port to bind (default: %(default)s)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # uvicorn's logging config leaves the ROOT logger alone, so without this the line
    # that tells a rollout apart from a crash ("SIGTERM received — ... exiting 0") never
    # reaches the pod log.
    logging.basicConfig(level=logging.INFO)
    return run_http_daemon(
        create_ui_app,
        host=args.host,
        port=args.port,
        logger=_logger,
        process="ui-bff",
        log_level="info",
    )


if __name__ == "__main__":
    raise SystemExit(main())
