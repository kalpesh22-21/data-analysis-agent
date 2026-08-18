"""Blueprint scope pre-filter — drop candidates whose USES ⊄ scope.

The READ-PATH analogue of `context/scope_filter.py` (which filters the session TRAIL, D44):
both keep the model from reasoning over anything outside `column_scope`. Here it drops any
blueprint candidate whose transitive USES set — the `db.table.column` strings the
blueprint's DAG reads, precomputed on the node at write time (D60) — is not a subset of the
current scope.

Semantics mirror the trail filter's D80(b) convention exactly: an empty `column_scope` is
allow-all; a non-empty one is an allowlist (`candidate.uses <= column_scope`); and
`uses is None` (undetermined) DROPS fail-closed, the same posture as D44's
`provenance is None`.

KNOWLEDGE candidates are entity-agnostic and carry `uses=None` by construction — they are
NOT passed to this filter; the pipeline scope-filters blueprints only.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Candidate


def is_blueprint_in_scope(candidate: Candidate, column_scope: frozenset[str]) -> bool:
    """Return True iff *candidate*'s transitive USES set is within *column_scope*."""
    if candidate.uses is None:
        return False  # undetermined USES → fail-closed (design §2)
    if not column_scope:
        return True  # allow-all (D80(b))
    return candidate.uses <= column_scope


def filter_blueprints_by_scope(
    candidates: Sequence[Candidate], column_scope: frozenset[str]
) -> list[Candidate]:
    """Return the subsequence of blueprint *candidates* within *column_scope*.
        Order-preserving; pure, no I/O.
    """
    return [c for c in candidates if is_blueprint_in_scope(c, column_scope)]


__all__ = ["filter_blueprints_by_scope", "is_blueprint_in_scope"]
