# context — the D50 assembly pipeline (load -> D44 filter -> D46 budget/compact -> inject).
from .assembly import AssembledContext, ContextAssembler
from .budget import CompactionResult, Summarizer, SummaryCache, compact_trail, render_messages
from .llm_summarizer import build_llm_summarizer
from .scope_filter import compute_scope_hash, filter_trail, is_entry_in_scope

__all__ = [
    "AssembledContext",
    "CompactionResult",
    "ContextAssembler",
    "SummaryCache",
    "Summarizer",
    "build_llm_summarizer",
    "compact_trail",
    "compute_scope_hash",
    "filter_trail",
    "is_entry_in_scope",
    "render_messages",
]
