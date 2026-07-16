#!/usr/bin/env python
"""Reconstruct one session's journey through the learning loop from durable stores.

Reads ONLY the durable Couchbase stores (session / candidate / audit) — no Phoenix,
no Redis, no network beyond those stores. Gives a human ONE view of one session's
lifecycle instead of correlating three daemon terminals by three different keys.

READ-ONLY: never writes, never creates a session doc. A missing session, a session
that never entered the learning loop, a missing audit store, or missing evidence
refs are all reported gracefully (no traceback).

Optionally (`--export-phoenix`) it ALSO projects the reconstructed lifecycle into
Phoenix as one synthetic per-session trace (project `learning-sessions`). That is
additive: the text/JSON report still prints to STDOUT unchanged; the export's status
messaging goes to STDERR, and any export failure degrades to a warning (never blanks
the report). Since the 2026-07-15 D25 amendment the export is VERBOSE (entity-bearing:
intent/template/resolves/rationale/quote) BY DEFAULT, because
`LearningSettings.learning_trace_verbose` now defaults True (the effective posture is
`--verbose-trace or learning_trace_verbose`). The `learning-sessions` project is
therefore entity-bearing and MUST be access-controlled like the audit/session store;
set `LEARNING_TRACE_VERBOSE=false` to restore the shape-only telemetry posture.

Usage:
    uv run python scripts/learning_trace.py <session_id> \
        [--json] [--no-color] [--export-phoenix] [--verbose-trace]

Exit codes:
    0  the session was found
    1  the session was not found
    2  an infrastructure error (a store failed to connect)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.trace import (
    SessionTrace,
    reconstruct_session_trace,
    render_session_trace,
    render_session_trace_json,
)
from data_agent.learning.trace.project import (
    build_session_export_provider,
    export_session_trace,
)
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.observability.tracing import get_tracer
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

_logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="learning_trace",
        description="Reconstruct one session's journey through the learning loop.",
    )
    parser.add_argument("session_id", help="The session id to trace.")
    parser.add_argument(
        "--json", action="store_true", help="Emit the trace as JSON instead of a report."
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color in the text report."
    )
    parser.add_argument(
        "--export-phoenix",
        action="store_true",
        help="Also project the lifecycle into Phoenix as one synthetic session trace.",
    )
    parser.add_argument(
        "--verbose-trace",
        action="store_true",
        help="Emit entity-bearing content on the exported trace (see --export-phoenix).",
    )
    return parser.parse_args()


def _export_to_phoenix(
    trace: SessionTrace,
    session_id: str,
    learning_settings: LearningSettings,
    *,
    verbose_flag: bool,
) -> None:
    """Project *trace* into the Phoenix `learning-sessions` project (STDERR status
    only). Never raises into the caller: a missing endpoint or a collector/export
    failure degrades to a one-line STDERR warning so the report is never affected."""
    if not learning_settings.otlp_endpoint:
        print(
            "learning_trace: --export-phoenix requested but LearningSettings.otlp_endpoint "
            "is empty; nothing exported",
            file=sys.stderr,
        )
        return
    verbose = verbose_flag or learning_settings.learning_trace_verbose
    try:
        provider = build_session_export_provider(
            otlp_endpoint=learning_settings.otlp_endpoint, session_id=session_id
        )
        flushed = False
        try:
            tracer = get_tracer(provider, "learning-sessions")
            trace_id = export_session_trace(trace, tracer, verbose=verbose)
            # ESSENTIAL: the BatchSpanProcessor won't export before process exit
            # without an explicit flush; capture its bool so we never claim success
            # on a timed-out flush (a dead collector can make this block ~30-60s on
            # the exporter's internal retry — acceptable; the exit code is unchanged).
            flushed = provider.force_flush()
        finally:
            provider.shutdown()  # releases the exporter regardless of flush outcome
    except Exception as exc:  # noqa: BLE001 — export must never blank the report.
        print(
            f"learning_trace: --export-phoenix failed ({type(exc).__name__}): {exc}; "
            "report unaffected",
            file=sys.stderr,
        )
        return
    if trace_id is None:
        print(
            "learning_trace: --export-phoenix: session not found; nothing projected",
            file=sys.stderr,
        )
    elif not flushed:
        print(
            "learning_trace: --export-phoenix: flush timed out — spans may not have "
            "reached Phoenix",
            file=sys.stderr,
        )
    else:
        print(
            f"learning_trace: exported to Phoenix project 'learning-sessions' — "
            f"trace {trace_id} (verbose={verbose})",
            file=sys.stderr,
        )


async def _main() -> int:
    args = _parse_args()
    # Keep stdout clean for the report — send library chatter to stderr at WARNING+.
    logging.basicConfig(level=logging.WARNING)

    runtime_settings = get_runtime_settings()
    learning_settings = LearningSettings()

    session_store = CouchbaseSessionStore(runtime_settings)
    # H1: the read-only guarantee for the REAL store hinges on `_get_doc` existing
    # (reconstruct prefers it; the public get_session_with_cas CREATES the doc on
    # miss — couchbase_store.py:222-227). If a future refactor renames `_get_doc`,
    # fail LOUD here rather than let reconstruct silently fall back to the
    # write-capable accessor. (The injectable-fake fallback in reconstruct stays.)
    if not callable(getattr(session_store, "_get_doc", None)):
        raise RuntimeError(
            "CouchbaseSessionStore no longer exposes _get_doc; refusing to fall back to "
            "create-on-miss get_session_with_cas (would violate read-only)."
        )

    candidate_store = CouchbaseCandidateStore(learning_settings)
    audit_store = None
    if learning_settings.learning_audit_username and learning_settings.learning_audit_password:
        from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore

        audit_store = CouchbaseAuditStore(learning_settings)
    else:
        _logger.warning("learning_audit RBAC creds absent — evidence quotes will not be resolved")

    stores = [session_store, candidate_store]
    if audit_store is not None:
        stores.append(audit_store)

    # The acouchbase Cluster connects lazily; force the connect BEFORE the first
    # read so a bad endpoint/credential fails here rather than mid-reconstruction
    # (mirrors scripts/demo_learning_e2e_openai.py). M2: a connect failure is an
    # INFRA error — report it in one line on stderr, close every already-connected
    # cluster, and exit 2 (distinct from 0=found / 1=not-found), never a raw
    # traceback (the candidate/audit creds default to "" — a common misconfig).
    connected: list[object] = []
    try:
        for store in stores:
            await store._cluster.on_connect()
            connected.append(store)
    except Exception as exc:  # noqa: BLE001 — surface a clean infra message, not a traceback.
        failing = type(stores[len(connected)]).__name__
        print(f"learning_trace: failed to connect {failing}: {exc}", file=sys.stderr)
        for store in connected:
            await store._cluster.close()
        return 2

    try:
        trace: SessionTrace = await reconstruct_session_trace(
            args.session_id, session_store, candidate_store, audit_store
        )
        if args.json:
            print(render_session_trace_json(trace))
        else:
            use_color = not args.no_color and sys.stdout.isatty()
            print(render_session_trace(trace, use_color=use_color))
        if args.export_phoenix:
            _export_to_phoenix(
                trace, args.session_id, learning_settings, verbose_flag=args.verbose_trace
            )
    finally:
        for store in stores:
            await store._cluster.close()

    return 0 if trace.session is not None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
