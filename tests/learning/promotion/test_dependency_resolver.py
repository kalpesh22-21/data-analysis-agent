"""Layer-1 — the real `CandidateStoreDependencyResolver` (§11.6/D35).

`S9-resolver-fail-closed`: a `depends_on` ref resolves to True ONLY when the sibling
candidate exists AND is `validated`; a missing / non-validated candidate, or a store
error, resolves to False (never fail-open — an unverifiable dependency HOLDS).
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.promotion.dependency_resolver import (
    CandidateStoreDependencyResolver,
)

from .helpers import make_blueprint_candidate


async def test_validated_dependency_resolves_true() -> None:
    store = InMemoryCandidateStore()
    dep = make_blueprint_candidate(status=CandidateStatus.VALIDATED)
    await store.put(dep)
    resolver = CandidateStoreDependencyResolver(store)

    assert await resolver.is_resolved(dep.candidate_id) is True


async def test_missing_dependency_fails_closed() -> None:
    resolver = CandidateStoreDependencyResolver(InMemoryCandidateStore())
    assert await resolver.is_resolved("candidate::does-not-exist") is False


@pytest.mark.parametrize(
    "status",
    [
        CandidateStatus.EXTRACTED,
        CandidateStatus.CANDIDATE,
        CandidateStatus.IN_REVIEW,
        CandidateStatus.REJECTED,
    ],
)
async def test_non_validated_dependency_fails_closed(status: str) -> None:
    store = InMemoryCandidateStore()
    dep = make_blueprint_candidate(status=status)
    await store.put(dep)
    resolver = CandidateStoreDependencyResolver(store)

    assert await resolver.is_resolved(dep.candidate_id) is False


async def test_store_error_fails_closed() -> None:
    """A store read that raises is an UNVERIFIABLE dependency ⇒ False (never a promote
    against an unverifiable dependency)."""

    class _RaisingStore:
        async def get(self, candidate_id: str):
            raise RuntimeError("store unreachable")

    resolver = CandidateStoreDependencyResolver(_RaisingStore())
    assert await resolver.is_resolved("candidate::anything") is False
