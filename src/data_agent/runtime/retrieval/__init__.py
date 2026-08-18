"""retrieval — the D7/D8 retrieval pipeline package.

The recall + rerank core over a `VectorIndex`, its three degrade-not-fail paths, the
`ContextAssembler` pre-injection integration, the neo4j-backed index, and the three
model-facing read tools.

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
