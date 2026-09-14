"""Bounded lexical queries and rank fusion; channel scores are not comparable."""

from __future__ import annotations

import re
from dataclasses import replace

from .models import Candidate

BLUEPRINT_TEXT_INDEX = "blueprint_intent_text"
BLUEPRINT_TEXT_INDEX_DDL = (
    "CREATE FULLTEXT INDEX blueprint_intent_text IF NOT EXISTS "
    "FOR (b:Blueprint) ON EACH [b.intent, b.slots_summary]"
)


def keyword_terms(query: str) -> list[str]:
    # Strip Lucene operators/field syntax rather than interpreting user text as code.
    return list(
        dict.fromkeys(
            term
            for term in re.findall(r"[^\W_]+", query.lower(), flags=re.UNICODE)
            if len(term) <= 64
        )
    )[:32]


def keyword_query(query: str) -> str:
    return " OR ".join(f'"{term}"' for term in keyword_terms(query))


def fuse_candidates(semantic: list[Candidate], lexical: list[Candidate]) -> list[Candidate]:
    """Reciprocal rank fusion; preserve a surviving single channel's scores."""
    if not lexical:
        return semantic
    if not semantic:
        return lexical
    candidates, scores = {}, {}
    for channel in (semantic, lexical):
        seen = set()
        for rank, candidate in enumerate(channel, 1):
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            candidates.setdefault(candidate.id, candidate)
            scores[candidate.id] = scores.get(candidate.id, 0.0) + 1 / (60 + rank)
    return [
        replace(candidates[ident], score=scores[ident])
        for ident in sorted(candidates, key=lambda ident: (-scores[ident], ident))
    ]
