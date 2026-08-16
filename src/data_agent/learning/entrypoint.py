"""The startup preamble every learning-plane daemon runs before it composes anything.

`configure_daemon_process` is the four things that must happen in EVERY offline
entrypoint, in this order, before any real work:

    logging.basicConfig            so the next three lines are actually visible
    configure_learning_tracing     build the learning-loop TracerProvider
    set_global_tracer_provider     make it the process-global one
    log_tracing_status             SAY whether spans will really leave the process
    warn_unrecognized_learning_env_vars   SAY which LEARNING_* vars are being ignored

The last two are the reason this is a shared function rather than a convention. Both
exist to convert a SILENT misconfiguration into a startup line, and both were added
after the silence had already cost real debugging time:

  * `LearningSettings.otlp_endpoint` defaults to `""`, which `configure_tracing`
    answers with a NO-OP provider — deliberately (zero infra to run the loop), but
    with nothing said. A full day of live runs produced ZERO Phoenix spans, findable
    only by reading source for the env var's name.
  * `LearningSettings` sets `extra="ignore"`, so a typo'd `LEARNING_MAX_DELIVERES` is
    accepted, dropped, and the shipped default silently applies.

An entrypoint that forgets either one does not fail — it just goes quiet, which is
precisely the failure mode. Three daemons carried this block as a copy, and a fourth
(`run_inbox_service`) had neither half; the copies are what made "a new entrypoint
inherits the diagnostics" untrue.

Deliberately NOT in here: settings construction, infra composition, and the tracer's
USE. Callers differ on all three (the consumer and sweeper hold the returned tracer,
the scheduler and inbox service discard it), so the helper returns the tracer and
takes no view.
"""

from __future__ import annotations

import logging

from opentelemetry.trace import Tracer

from data_agent.learning.config import LearningSettings, warn_unrecognized_learning_env_vars
from data_agent.learning.observability import (
    configure_learning_tracing,
    get_learning_tracer,
    log_tracing_status,
)
from data_agent.runtime.observability.tracing import set_global_tracer_provider


def configure_daemon_process(
    process: str,
    settings: LearningSettings,
    logger: logging.Logger,
    *,
    log_level: int = logging.INFO,
) -> Tracer:
    """Run the shared startup preamble for a learning daemon; return its tracer.

    *process* is the daemon's name as it appears in the startup lines (`"consumer"`,
    `"sweeper"`, `"scheduler"`, `"inbox"`) — the only thing that genuinely differs
    between callers. *logger* is the CALLER's module logger, so the diagnostics are
    attributed to the entrypoint an operator is looking at rather than to this module.

    The returned `Tracer` is the learning-loop tracer bound to the newly-installed
    global provider. Callers that thread a tracer into their components keep it; the
    ones that do not may ignore it — the provider is installed either way, which is what
    the module-level `span` helpers pick up.
    """
    logging.basicConfig(level=log_level)
    provider = configure_learning_tracing(
        otlp_endpoint=settings.otlp_endpoint,
        service_name=settings.learning_service_name,
    )
    set_global_tracer_provider(provider)
    tracer = get_learning_tracer(provider)
    # An empty OTLP_ENDPOINT builds a NO-OP provider silently; say which it is.
    log_tracing_status(
        logger,
        otlp_endpoint=settings.otlp_endpoint,
        service_name=settings.learning_service_name,
        process=process,
    )
    # `extra="ignore"` accepts a typo'd LEARNING_* var and silently applies the
    # default; say which ones this process is ignoring.
    warn_unrecognized_learning_env_vars(logger)
    return tracer


__all__ = ["configure_daemon_process"]
