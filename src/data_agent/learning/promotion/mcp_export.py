"""promotion/mcp_export.py — the Phase-3 PROMOTE emit (governed-corpus §Promote).

The inbox PROMOTE action turns a VERIFIED learning node into the MCP-format YAML a
human opens a MANUAL PR with into the MCP corpus repo (`clickhouse-api`,
`app/corpus/data/{blueprints,knowledge}/`). This module is that serializer: it REUSES
`blueprint_seed_from_candidate` / `knowledge_seed_from_candidate` to normalize the
candidate (the SAME projection the landing writer uses, so the emitted YAML can never
drift from what actually landed), then projects the seed onto the EXACT MCP field set,
DROPPING the non-MCP provenance fields (`source`, `verified`, `created_by`,
`source_candidate_id`, and knowledge `drift_status`).

The emitted `id` is the LANDING id VERBATIM (`bp::<hash>` / `kn::<hash>`) so a later
reseed of the merged YAML flips THAT SAME neo4j node `learning → mcp` in place instead
of creating a duplicate (the load path MERGEs by `id`).

This module NEVER touches git or the filesystem: it returns the YAML string plus the
suggested PR metadata (filename / target path / branch / commit message) for the human
to open the PR themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from ..candidate.models import CandidateEnvelope
from ..generalize.mapping import (
    blueprint_seed_from_candidate,
    knowledge_seed_from_candidate,
)
from .landing import landing_id


class _BlockDumper(yaml.SafeDumper):
    """A SafeDumper that renders multiline strings as literal block scalars (`|`),
    matching the MCP corpus YAML formatting (Phase-1) for `sql_template` / `text`."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockDumper.add_representer(str, _represent_str)


def _dump(doc: dict[str, Any]) -> str:
    """Serialize *doc* to YAML preserving insertion order (`sort_keys=False`) with block
    scalars for multiline strings — the exact MCP corpus formatting."""
    return yaml.dump(
        doc,
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def _slug(text: str, *, fallback: str) -> str:
    """A filesystem-safe, deterministic slug from an intent/title — lowercased, non
    alphanumerics collapsed to single hyphens, trimmed. Empty ⇒ *fallback*."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or fallback


def _compact_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop null-valued optional keys (`optional_pattern: null`, `enum_values: null`) so
    the emitted slots match hand-authored MCP canon's compact `name/type/binds_to/
    required` shape instead of carrying the generalizer's full nullable slot record."""
    return [{k: v for k, v in slot.items() if v is not None} for slot in slots]


def _result_grain_field(result_grain: Any) -> Any | None:
    """Normalize the generalizer's `{columns, verifiable}` result-grain to the canon
    shape, returning None when the field should be OMITTED entirely:

      * a VERIFIABLE grain with columns → the BARE column list (canon shape,
        e.g. `[Department]`);
      * a VERIFIABLE grain with NO columns → None (a semantically-absent grain is
        omitted, never `{columns: [], verifiable: true}`);
      * a NON-verifiable grain → the `{columns, verifiable}` dict verbatim (the grain
        integrity probe is off, so the shape must be preserved for a later reseed);
      * an already-bare list (or other truthy value) → itself; empty ⇒ None.
    """
    if isinstance(result_grain, dict):
        columns = list(result_grain.get("columns") or [])
        verifiable = bool(result_grain.get("verifiable"))
        if verifiable:
            return columns or None
        return {"columns": columns, "verifiable": verifiable}
    return result_grain or None


@dataclass(frozen=True)
class PromotionEmit:
    """The PROMOTE action's structured result: the MCP-format YAML plus the suggested
    (never-executed) PR metadata. The service does NOT touch git — the human opens the
    PR with these."""

    yaml: str
    filename: str
    target_path: str
    suggested_branch: str
    commit_message: str
    note: str

    def to_wire(self) -> dict[str, str]:
        return {
            "yaml": self.yaml,
            "filename": self.filename,
            "target_path": self.target_path,
            "suggested_branch": self.suggested_branch,
            "commit_message": self.commit_message,
            "note": self.note,
        }


_PARITY_NOTE = (
    "regenerate the corpus SHA sidecars (tools/check_corpus_parity.py --write) in the PR"
)


def _blueprint_doc(env: CandidateEnvelope, node_id: str) -> dict[str, Any]:
    """Project a blueprint candidate onto the EXACT MCP blueprint field set. Reuses
    `blueprint_seed_from_candidate` to normalize, then keeps ONLY the MCP fields (drops
    `source`/`verified`/`created_by`/`source_candidate_id`/`structural_key` — the last
    is re-derived by the reseed from the templates), in the MCP field order.

    This whitelist is hand-written, which is precisely how J7's `window_anchor` was lost
    twice (see `tests/eval/test_harness_field_drift.py`); the field-parity tripwire in
    `test_mcp_export.py` now derives the required key set from `BlueprintSeed` so the
    next field added to the seed cannot be dropped here in silence."""
    seed = blueprint_seed_from_candidate(env, id=node_id)
    doc: dict[str, Any] = {
        "id": seed.id,  # the landing id VERBATIM — a reseed MERGEs this same node
        "intent": seed.intent,
        "slots_summary": seed.slots_summary,
        "status": "validated",
        # A validated candidate is always clean (it passed the golden-replay grain
        # probe); pin `clean` for deterministic canon rather than echoing the mutable
        # `seed.drift_status`.
        "drift_status": "clean",
        "catalog_sha": "",  # a learning candidate has none; the reseed recomputes it
        "uses": list(seed.uses),
    }
    if seed.slots:
        doc["slots"] = _compact_slots(seed.slots)
    if seed.uses_rules:
        doc["uses_rules"] = list(seed.uses_rules)
    result_grain = _result_grain_field(seed.result_grain)
    if result_grain is not None:
        doc["result_grain"] = result_grain
    # J7c — emitted between `result_grain` and `sql_template` to match the hand-authored
    # canon field order (clickhouse-api `app/corpus/data/blueprints/bp-hires-per-month.
    # yaml`). Omitted when absent: `None` means the blueprint makes NO window claim, and
    # a `window_anchor: null` key in canon would read as a declaration that isn't one.
    if seed.window_anchor:
        doc["window_anchor"] = seed.window_anchor
    if seed.sql_template:
        doc["sql_template"] = seed.sql_template
    if seed.resolves:
        doc["resolves"] = dict(seed.resolves)
    if seed.composes:
        doc["composes"] = seed.composes
    return doc


def _knowledge_doc(
    env: CandidateEnvelope, node_id: str, *, doc_id: str | None, title: str | None
) -> dict[str, Any]:
    """Project a knowledge candidate onto the EXACT MCP knowledge field set. Reuses
    `knowledge_seed_from_candidate` to normalize, then keeps ONLY the MCP fields (drops
    `source`/`verified`/`created_by`/`source_candidate_id`/`drift_status`). The human may
    supply a semantic `doc_id` (the candidate's is non-semantic) and a refined `title`."""
    seed = knowledge_seed_from_candidate(env, id=node_id)
    return {
        "id": seed.id,  # the landing id VERBATIM — a reseed MERGEs this same node
        "title": title if title is not None else (seed.title or ""),
        "doc_id": doc_id if doc_id is not None else seed.doc_id,
        "status": "validated",
        "text": seed.text,
    }


def build_promotion_emit(
    env: CandidateEnvelope, *, doc_id: str | None = None, title: str | None = None
) -> PromotionEmit:
    """Build the MCP-format YAML + PR metadata for a verified learning candidate.

    `id` in the YAML is the landing id VERBATIM so a reseed flips the SAME neo4j node
    `learning → mcp`. `doc_id`/`title` are OPTIONAL human refinements for knowledge (the
    caller must NOT allow overriding `id`). Raises `ValueError`/`BlueprintParseError`
    (from the seed builders) for a malformed/non-landable candidate — never a silent
    broken emit."""
    node_id = landing_id(env)
    if env.type == "global_knowledge":
        doc = _knowledge_doc(env, node_id, doc_id=doc_id, title=title)
        subdir = "knowledge"
        slug = _slug(str(doc["title"]), fallback=node_id)
    else:
        doc = _blueprint_doc(env, node_id)
        subdir = "blueprints"
        slug = _slug(doc["intent"], fallback=node_id)
    return PromotionEmit(
        yaml=_dump(doc),
        filename=f"{slug}.yaml",
        target_path=f"app/corpus/data/{subdir}/",
        suggested_branch=f"learning/promote/{node_id}",
        commit_message=f"corpus: promote learning {env.type} {node_id}",
        note=_PARITY_NOTE,
    )


__all__ = ["PromotionEmit", "build_promotion_emit"]
