# observability — D23/D24/D25/D61 as one instrumentation (design §7).
#
# One instrumentation, two consumers: tracing.py (OTel/Phoenix spans) and
# progress.py (UI-facing SSE progress events) share the same stage
# boundaries and the same redaction.py rules. `tracing` is imported as a
# submodule (not flattened here) since app.py calls several of its functions
# together (configure_tracing/instrument_openai/get_tracer/span helpers).
from . import tracing
from .progress import ProgressEmitter, ProgressEvent, combine_observers, to_progress_event
from .redaction import hash_scope, mask_sql, redact_tool_args, tool_span_args

__all__ = [
    "ProgressEmitter",
    "ProgressEvent",
    "combine_observers",
    "hash_scope",
    "mask_sql",
    "redact_tool_args",
    "to_progress_event",
    "tool_span_args",
    "tracing",
]
