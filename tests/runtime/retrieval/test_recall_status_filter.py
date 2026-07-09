"""Layer-1 — the blueprint recall-eligibility filter (S9-activation Slice 3, §8.6).

The retraction backstop: `_BLUEPRINT_RECALL_QUERY` filters recall on the landed node's
`status`/`drift_status` so a demoted / rejected / drift-suspect blueprint is NOT
recalled. The load-bearing COMPAT contract is the `coalesce(...)` default: an ABSENT
property (or `status='validated'` + `drift_status='clean'`) stays recallable, so the
existing hand-authored seed corpus is byte-unchanged; only an EXPLICIT
`candidate`/`retired`/`rejected` status OR a `suspect` drift is excluded.

The real neo4j filter runs server-side (Cypher `WHERE`), so this Layer-1 test locks the
query STRUCTURE (the exact coalesce clauses) + a truth table mirroring their semantics;
`tests/integration/test_neo4j_vector_index_live.py` + the Slice-3 live landing test prove
the semantics against real neo4j.

Slugs: `S9-recall-seed-compat`, `S9-recall-filters-ineligible`.
"""

from __future__ import annotations

import re

from data_agent.runtime.retrieval.vector_index import (
    _BLUEPRINT_RECALL_QUERY,
    _KNOWLEDGE_RECALL_QUERY,
)


def _normalize(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def _recall_eligible(status: object, drift_status: object) -> bool:
    """A pure mirror of the Cypher filter's coalesce semantics
    (`coalesce(status,'validated')='validated' AND coalesce(drift_status,'clean')
    <>'suspect'`). Kept in the test (NOT production) precisely because the real filter
    runs in neo4j; this enumerates the intended truth table, and the live test proves
    neo4j agrees."""
    effective_status = status if status is not None else "validated"
    effective_drift = drift_status if drift_status is not None else "clean"
    return effective_status == "validated" and effective_drift != "suspect"


# --- S9-recall-seed-compat (the query carries the fail-open coalesce filter) -------


def test_blueprint_recall_query_carries_coalesce_status_filter() -> None:
    """The exact compat clauses: an absent `status`/`drift_status` coalesces to the
    recallable default, so a seed node with no lifecycle stamp stays recallable."""
    normalized = _normalize(_BLUEPRINT_RECALL_QUERY)
    assert "coalesce(node.status, 'validated') = 'validated'" in normalized
    assert "coalesce(node.drift_status, 'clean') <> 'suspect'" in normalized
    # The parity guard is still first (unchanged).
    assert "node.embedding_model = $expected_model" in normalized


def test_knowledge_recall_query_carries_coalesce_status_filter() -> None:
    """UI Slice 2 §1.1 row 5: global_knowledge now LANDS via the human-approve edge and
    can be RETRACTED (`status=retired`), so the knowledge query MUST carry the same
    fail-open status filter or a retracted chunk is a silent recall no-op. Drift is NOT
    applicable to knowledge (only blueprints replay), so there is no drift_status clause."""
    normalized = _normalize(_KNOWLEDGE_RECALL_QUERY)
    assert "coalesce(node.status, 'validated') = 'validated'" in normalized
    assert "drift_status" not in normalized
    # The parity guard is still first (unchanged).
    assert "node.embedding_model = $expected_model" in normalized


def test_seed_compat_absent_and_validated_clean_are_recallable() -> None:
    """A hand-authored seed node with NO lifecycle props, or the explicit
    `validated`/`clean` the fixtures + loop-landed nodes carry, stays recallable."""
    assert _recall_eligible(None, None) is True  # bare seed node — no props
    assert _recall_eligible("validated", None) is True  # status only
    assert _recall_eligible(None, "clean") is True  # drift only
    assert _recall_eligible("validated", "clean") is True  # fully stamped


# --- S9-recall-filters-ineligible (demoted / rejected / suspect excluded) ----------


def test_ineligible_status_or_suspect_drift_excluded() -> None:
    assert _recall_eligible("candidate", "clean") is False  # demoted
    assert _recall_eligible("retired", "clean") is False
    assert _recall_eligible("rejected", "clean") is False
    assert _recall_eligible("validated", "suspect") is False  # drift-suspect demote
    assert _recall_eligible("candidate", "suspect") is False  # both
