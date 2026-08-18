"""The startup preamble every learning-plane daemon runs before it composes anything.

`configure_daemon_process` runs, in order, `logging.basicConfig`, the learning
TracerProvider plus its installation as the process global, `log_tracing_status` and
`warn_unrecognized_learning_env_vars`. The last two turn a SILENT misconfiguration into a
startup line: an empty `otlp_endpoint` yields a no-op provider, and `extra="ignore"` drops
a typo'd `LEARNING_*` var. Settings and infra composition stay with the callers.
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

    *process* names the daemon in the startup lines; *logger* is the CALLER's module logger, so
    the diagnostics are attributed to the entrypoint an operator is looking at. The provider is
    installed either way, so callers that thread a tracer keep it and the rest may ignore it.
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
