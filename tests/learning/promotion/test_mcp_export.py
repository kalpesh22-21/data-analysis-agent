"""Phase-3 PART E — the PROMOTE emit serializes the EXACT MCP-format YAML.

The emitted YAML must (a) carry an `id` equal to the landing node's id VERBATIM (so a
reseed flips THAT SAME node `learning → mcp`), (b) match the MCP field set, and (c) DROP
the non-MCP provenance fields (`source`/`verified`/`created_by`/`source_candidate_id`,
and knowledge `drift_status`). Asserted for BOTH a blueprint and a knowledge chunk.
"""

from __future__ import annotations

from dataclasses import replace

import yaml

from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.candidate.verdicts import DriftStamp
from data_agent.learning.promotion.landing import landing_id
from data_agent.learning.promotion.mcp_export import build_promotion_emit

from .helpers import make_blueprint_candidate, with_type

KEY = "sha256:single-bp"

# Fields that MUST NEVER appear in the emitted MCP YAML (non-MCP provenance/trust).
_DROPPED = {"source", "verified", "created_by", "source_candidate_id"}

# The EXACT MCP-allowed field sets (the in-repo stand-in for the cross-repo round-trip):
# a stray emit field fails CI here. Blueprint `drift_status` is allowed; knowledge is a
# closed five-field set (its `drift_status` is NON-MCP and dropped).
# `window_anchor` is MCP-canon (J7; hand-authored in clickhouse-api) and emitted only
# when the candidate declares one — see test_window_anchor_j7c.py for its pins.
_BLUEPRINT_ALLOWED = {
    "id", "intent", "slots_summary", "status", "drift_status", "catalog_sha",
    "uses", "slots", "uses_rules", "result_grain", "window_anchor", "sql_template",
    "composes", "resolves",
}
_KNOWLEDGE_ALLOWED = {"id", "title", "doc_id", "status", "text"}


def _blueprint(*, grain_verifiable: bool = False):
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, canonical_key=KEY,
        grain_verifiable=grain_verifiable,
    )
    return replace(env, verified=True, drift=DriftStamp(status="clean"))


def _knowledge():
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        "global_knowledge",
    )
    payload = dict(env.payload)
    payload["statement"] = "line one\nline two"  # multiline → block scalar
    payload["scope"] = "fiscal calendar"
    return replace(env, payload=payload, verified=True)


# --- blueprint -----------------------------------------------------------------


def test_blueprint_emit_id_matches_landing_node_verbatim() -> None:
    env = _blueprint()
    emit = build_promotion_emit(env)
    doc = yaml.safe_load(emit.yaml)
    assert doc["id"] == landing_id(env) == f"bp::{KEY}"


def test_blueprint_emit_field_set_matches_mcp_and_drops_non_mcp() -> None:
    emit = build_promotion_emit(_blueprint())
    doc = yaml.safe_load(emit.yaml)
    # The MCP core is always present; no non-MCP provenance field leaks through.
    assert {"id", "intent", "slots_summary", "status", "drift_status",
            "catalog_sha", "uses"} <= set(doc)
    assert doc["status"] == "validated"
    assert doc["catalog_sha"] == ""  # a learning candidate has none
    assert _DROPPED.isdisjoint(doc)


def test_blueprint_emit_metadata() -> None:
    env = _blueprint()
    emit = build_promotion_emit(env)
    assert emit.target_path == "app/corpus/data/blueprints/"
    assert emit.suggested_branch == f"learning/promote/{landing_id(env)}"
    assert emit.filename.endswith(".yaml")
    assert "check_corpus_parity.py" in emit.note


# --- normalized shapes (match hand-authored canon) -----------------------------


def test_blueprint_emit_keys_are_subset_of_mcp_allowed_set() -> None:
    """Contract lock (in-repo stand-in for the cross-repo round-trip): every emitted key
    is in the MCP-allowed set, and no non-MCP provenance field ever appears."""
    doc = yaml.safe_load(build_promotion_emit(_blueprint()).yaml)
    assert set(doc) <= _BLUEPRINT_ALLOWED
    assert _DROPPED.isdisjoint(doc)


def test_blueprint_drift_status_is_pinned_clean() -> None:
    """`drift_status` is pinned `clean` for deterministic canon (a validated candidate is
    always clean), not echoed from the mutable seed drift."""
    env = replace(_blueprint(), drift=DriftStamp(status="unchecked"))
    doc = yaml.safe_load(build_promotion_emit(env).yaml)
    assert doc["drift_status"] == "clean"


def test_blueprint_slots_strip_null_optional_keys() -> None:
    """Emitted slots carry the compact `name/type/binds_to/required` shape — null-valued
    optional keys (`optional_pattern`/`enum_values`) are stripped to match canon."""
    doc = yaml.safe_load(build_promotion_emit(_blueprint()).yaml)
    for slot in doc["slots"]:
        assert None not in slot.values()
        # A non-null optional (the `region` slot's `optional_pattern`) is KEPT.
    region = next(s for s in doc["slots"] if s["name"] == "region")
    assert region.get("optional_pattern") == "TRUE"


def test_blueprint_verifiable_grain_emits_bare_column_list() -> None:
    """A verifiable grain with columns emits the BARE list (canon shape), not the
    `{columns, verifiable}` dict."""
    doc = yaml.safe_load(build_promotion_emit(_blueprint(grain_verifiable=True)).yaml)
    assert doc["result_grain"] == ["department"]


def test_blueprint_empty_verifiable_grain_is_omitted() -> None:
    """A verifiable grain with NO columns is a semantically-absent grain — the field is
    OMITTED entirely (never `result_grain: {columns: [], verifiable: true}`)."""
    env = _blueprint()
    payload = dict(env.payload)
    gen = dict(payload["generalization"])
    gen["result_grain"] = {"columns": [], "verifiable": True}
    payload["generalization"] = gen
    env = replace(env, payload=payload)
    doc = yaml.safe_load(build_promotion_emit(env).yaml)
    assert "result_grain" not in doc


def test_blueprint_non_verifiable_grain_keeps_dict() -> None:
    """A NON-verifiable grain keeps the `{columns, verifiable}` dict (the probe is off;
    the shape is preserved for a later reseed)."""
    doc = yaml.safe_load(build_promotion_emit(_blueprint()).yaml)
    assert doc["result_grain"] == {"columns": [], "verifiable": False}


# --- knowledge -----------------------------------------------------------------


def test_knowledge_emit_id_matches_landing_node_verbatim() -> None:
    env = _knowledge()
    emit = build_promotion_emit(env)
    doc = yaml.safe_load(emit.yaml)
    assert doc["id"] == landing_id(env) == f"kn::{KEY}"


def test_knowledge_emit_exact_field_set_and_drops_non_mcp() -> None:
    emit = build_promotion_emit(_knowledge())
    doc = yaml.safe_load(emit.yaml)
    # Knowledge YAML is EXACTLY the five allowed fields (drift_status dropped too).
    assert set(doc) == _KNOWLEDGE_ALLOWED
    assert doc["status"] == "validated"
    assert "drift_status" not in doc
    assert _DROPPED.isdisjoint(doc)


def test_knowledge_emit_keys_are_subset_of_mcp_allowed_set() -> None:
    """Contract lock (in-repo stand-in for the cross-repo round-trip): no stray field."""
    doc = yaml.safe_load(build_promotion_emit(_knowledge()).yaml)
    assert set(doc) <= _KNOWLEDGE_ALLOWED
    assert _DROPPED.isdisjoint(doc)


def test_knowledge_emit_honours_doc_id_and_title_overrides() -> None:
    env = _knowledge()
    emit = build_promotion_emit(env, doc_id="hr-policy-fiscal", title="Fiscal start")
    doc = yaml.safe_load(emit.yaml)
    assert doc["doc_id"] == "hr-policy-fiscal"
    assert doc["title"] == "Fiscal start"
    # The id is NEVER overridable — it stays the landing node id.
    assert doc["id"] == landing_id(env)


def test_knowledge_multiline_text_uses_block_scalar() -> None:
    emit = build_promotion_emit(_knowledge())
    # A literal block scalar (`|`) is used for the multiline text (Phase-1 formatting).
    assert "text: |" in emit.yaml
    # And it still round-trips to the exact value.
    assert yaml.safe_load(emit.yaml)["text"].startswith("line one\nline two")
