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
from data_agent.runtime.observability.tracing import (
    instrument_openai,
    set_global_tracer_provider,
)


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
    # AUTO-INSTRUMENT THE OPENAI SDK, which no learning daemon did. Every model call on this
    # plane — the S3 extractor, the coverage judge, the S4 parameterization judge and the §C
    # reviser — produced a CHAIN span describing the DECISION and nothing describing the CALL,
    # so a Phoenix `learning-loop` trace showed a verdict with no prompt, no completion, no
    # token counts and no latency underneath it. The runtime plane has had this since D24;
    # only the learning daemons were missing it.
    #
    # Placed in the SHARED preamble rather than per-daemon so the four processes cannot drift
    # into different tracing postures — and because `OpenAIInstrumentor` is a process-global
    # singleton whose FIRST caller wins, so a second call site with a different `hide_content`
    # would silently no-op rather than error.
    #
    # ⚠ `hide_content` is the INVERSE of `learning_trace_verbose`, which is the mirror
    # `RuntimeSettings.otlp_hide_llm_content` names in its own docstring. Verbose (the shipped
    # default) means the span carries the raw prompt and completion — the accepted SQL with its
    # literals, the reviewer's free text — so this project is ENTITY-BEARING BY DEFAULT and
    # must be access-controlled to the same standard as `learning_audit` (D51). The learning
    # spans already took that posture; this makes the LLM spans agree with them instead of
    # being silently stricter.
    instrument_openai(provider, hide_content=not settings.learning_trace_verbose)
    tracer = get_learning_tracer(provider)
    # An empty OTLP_ENDPOINT builds a NO-OP provider silently; say which it is.
    log_tracing_status(
        logger,
        otlp_endpoint=settings.otlp_endpoint,
        service_name=settings.learning_service_name,
        process=process,
    )
    # Say which content posture is in force. An operator reading a trace with no prompt on it
    # needs to know whether the call was not instrumented or the content was withheld, and
    # those look identical in Phoenix.
    logger.info(
        "learning %s OpenAI spans: %s (LEARNING_TRACE_VERBOSE=%s) — verbose carries the raw "
        "prompt + completion, so this Phoenix project is ENTITY-BEARING and must be "
        "access-controlled like learning_audit (D51)",
        process,
        "CONTENT REVEALED" if settings.learning_trace_verbose else "shape only",
        settings.learning_trace_verbose,
    )
    # `extra="ignore"` accepts a typo'd LEARNING_* var and silently applies the
    # default; say which ones this process is ignoring.
    warn_unrecognized_learning_env_vars(logger)
    return tracer


__all__ = ["configure_daemon_process"]
