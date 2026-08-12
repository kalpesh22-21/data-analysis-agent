"""retrieval — the Phase-1 D7/D8 retrieval pipeline package (design brick).

Slice 1 scope: the recall+rerank CORE over an in-memory `VectorIndex` fake,
with the three degrade-not-fail paths and the `ContextAssembler` pre-injection
integration. No neo4j, no model-facing search tools, no offline indexing job
(those are Slice 2/3, design §4.3).

See `docs/decisions/retrieval-pipeline-design.md`.
"""

from __future__ import annotations

from .models import (
    Candidate,
    CandidateKind,
    KnowledgeHit,
    RetrievedContext,
    SlotSummary,
    ThinCard,
    UserMemoryItem,
)
from .pipeline import RetrievalPipeline
from .render import render_retrieved_context
from .user_memory import NullUserMemoryProvider, UserMemoryProvider
from .vector_index import FakeVectorIndex, VectorIndex

__all__ = [
    "Candidate",
    "CandidateKind",
    "FakeVectorIndex",
    "KnowledgeHit",
    "NullUserMemoryProvider",
    "RetrievalPipeline",
    "RetrievedContext",
    "SlotSummary",
    "ThinCard",
    "UserMemoryItem",
    "UserMemoryProvider",
    "VectorIndex",
    "render_retrieved_context",
]
