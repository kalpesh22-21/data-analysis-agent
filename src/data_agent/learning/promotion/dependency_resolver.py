"""promotion/dependency_resolver.py — the real S9 `depends_on` resolver (§11.6/D35).

A `depends_on` entry is the **candidate_id of a sibling candidate** (per
`extractor/models.py::CandidateHeader.depends_on`) — e.g. a blueprint blocked on a
`schema_edit(add_rule)` emitted in the same session (the D35 missing-rule pairing).
It is NOT a raw schema-rule string or a neo4j node id.

A dependency is "resolved" once its OWN candidate reaches `validated`: for a blueprint
dep that means it landed; for a `schema_edit` it means a human approved it through the
inbox (D18 — the human approve only fires post-PR-merge, so `validated` implies the
catalog rule is live). Resolution therefore reads the SAME candidate store the
scheduler is already pinned to (no split-brain, no new infra) rather than a neo4j
probe (S9-design §2.2).

FAIL-CLOSED (the `S9-resolver-fail-closed` invariant, §2.3): a missing candidate, a
non-`validated` candidate, OR a store error all resolve to `False` (an unverifiable
dependency is always "not resolved" → the dependent candidate HOLDS). Never fail-open.
"""

from __future__ import annotations

import logging

from ..candidate.models import CandidateStatus
from ..candidate.store import CandidateStore

_logger = logging.getLogger(__name__)


class CandidateStoreDependencyResolver:
    """Resolves a `depends_on` ref against the shared candidate store (§2.2)."""

    def __init__(self, store: CandidateStore) -> None:
        self._store = store

    async def is_resolved(self, ref: str) -> bool:
        """True IFF the referenced sibling candidate EXISTS and is `validated`.
        Missing / non-validated / store-error ⇒ `False` (fail-closed, §2.3)."""
        try:
            env = await self._store.get(ref)
        except Exception:  # noqa: BLE001 - a store error is an UNVERIFIABLE dependency
            # An unverifiable dependency is never "resolved" — never promote against it.
            _logger.warning("dependency resolver store read failed for ref %s; unresolved", ref)
            return False
        return env is not None and env.status == CandidateStatus.VALIDATED


__all__ = ["CandidateStoreDependencyResolver"]
