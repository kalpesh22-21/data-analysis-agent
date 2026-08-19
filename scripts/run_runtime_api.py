#!/usr/bin/env python
"""run_runtime_api — the agent runtime HTTP API entrypoint (the chart's `runtime` workload).

Serves the SAME app, composed the same way, that the Deployment used to start as
`uvicorn data_agent.runtime.app:create_app --factory --host 0.0.0.0 --port 8000`. What
changes is who owns `serve()`: under the uvicorn CLI no repo code brackets it, so the
SIGTERM uvicorn captured is re-raised onto the restored `SIG_DFL` AFTER a successful
graceful shutdown and the pod exits 143 — every rollout read as a crash (ISSUES.md C3).
`run_http_daemon` chains that re-raise onto a handler installed FIRST and exits 0.

Nothing about the app changes and nothing new is required of the environment: this reads
the same `RuntimeSettings` (i.e. the same envFrom/env the chart already supplies), binds
the same interface and port, and runs one worker in one process, exactly as before.

Host and port are COMMAND-LINE flags, mirroring the ones the chart used to pass to
uvicorn, so the pod spec still says out loud which interface it publishes on. The
defaults are uvicorn's own (loopback, :8000) — a bare local run does not put the API on
every interface by accident.

Usage:
    uv run python scripts/run_runtime_api.py --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from data_agent.http_daemon import run_http_daemon

_logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def create_runtime_app() -> Any:
    """Build the runtime app. Handed to uvicorn as a `factory=True` app, so it runs from
    `config.load()` INSIDE `serve()` — in the running loop and inside uvicorn's captured
    -signal region.

    The import is deliberately IN here rather than at module scope. Importing
    `data_agent.runtime.app` drags in the whole runtime tree (fastapi, opentelemetry, the
    couchbase extension, sqlglot) and takes seconds; at module scope those seconds run
    before any SIGTERM handler exists at all, which is the same hole `--factory` was
    closing for composition. The CLI had the module import inside `serve()` too — it is
    `Config.load()` that does `import_from_string` — so this is parity, not a new claim.
    """
    from data_agent.runtime.app import create_app

    return create_app()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the data-agent runtime API.")
    parser.add_argument(
        "--host", default=_DEFAULT_HOST, help="interface to bind (default: %(default)s)"
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help="TCP port to bind (default: %(default)s)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # uvicorn's logging config configures the `uvicorn*` loggers and leaves the ROOT
    # logger alone, so without this the one line that tells a rollout apart from a crash
    # ("SIGTERM received — ... exiting 0") is dropped by the last-resort handler at
    # WARNING. Same call the learning daemons make via `configure_daemon_process`.
    logging.basicConfig(level=logging.INFO)
    return run_http_daemon(
        create_runtime_app,
        host=args.host,
        port=args.port,
        logger=_logger,
        process="runtime",
        log_level="info",
    )


if __name__ == "__main__":
    raise SystemExit(main())
