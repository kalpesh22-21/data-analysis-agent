"""The corpus seed DTOs + the MCP-export projection that builds them.

Deliberately dependency-light (stdlib only): the offline loader, the online hydrator and the
learning plane all import these, and none of them should pay for the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlueprintSeed:
    """One hand-authored blueprint fixture.

        `uses` MUST be byte-exact `"database.table.column"` scope keys, or the read-path scope
        pre-filter silently drops the blueprint — the corpus's highest-risk contract.
    """

    id: str
    intent: str
    slots_summary: str
    uses: list[str]
    status: str = "validated"
    drift_status: str = "clean"
    catalog_sha: str = ""
    # Provenance (S9-activation Slice 2 review S3). `created_by` distinguishes a
    # hand-authored seed (`"seed"`, the default so existing fixtures stay byte-
    # identical) from a blueprint the LEARNING LOOP landed (`"learning"`), and
    # `source_candidate_id` stamps the originating candidate — so incident response
    # can list/remove everything the loop landed. `source_candidate_id=None` sets no
    # neo4j property (neo4j drops a `SET x = null`), so a fixture node is unchanged.
    created_by: str = "seed"
    source_candidate_id: str | None = None
    # Governed-corpus trust partition (Phase 2). `source` places the node in the
    # TRUSTED MCP-canon partition (`"mcp"`) or the learning STAGING tier
    # (`"learning"`); recall serves ONLY `source="mcp"` (the trust gate in
    # `vector_index._BLUEPRINT_RECALL_QUERY`), and corpus GC only ever touches
    # `source="mcp"` nodes. `verified` is the human-approval flag (Phase-3 triage).
    # Defaults are `mcp`/`True` so the FIXTURE/offline seed path + every existing
    # test produces TRUSTED canon by construction (the fixtures carry no
    # source/verified); the learning landing writer OVERRIDES these to
    # `"learning"`/`False` so a landed node stays out of the trusted recall
    # partition until Phase-3 promotes it.
    source: str = "mcp"
    verified: bool = True
    # The LOOSE cross-authoring-path identity (`runtime/blueprint/structural_key.py`).
    # Empty by DEFAULT: the MCP-canon YAMLs carry a `sql_template`/`composes` and no key,
    # so `dag_properties` DERIVES one from the seed's own templates + grain at write
    # time. The learning landing seed sets it EXPLICITLY (from the same shared helper,
    # over S4's templates) and that explicit value WINS — purely to save a second parse,
    # since both paths run the identical derivation and must agree by construction. A
    # seed whose templates do not normalize lands with NO key at all.
    structural_key: str = ""
    # --- additive full-DAG fields, the runBlueprint brick (OQ-T1, §1.2). All
    # optional-defaulted so existing D87/D88 fixtures still load (no migration).
    # Stored as JSON-string properties on the `:Blueprint` node; unread by recall.
    resolves: dict[str, str] = field(default_factory=dict)
    slots: list[dict[str, Any]] = field(default_factory=list)
    uses_rules: list[Any] = field(default_factory=list)
    sql_template: str | None = None
    composes: list[dict[str, Any]] = field(default_factory=list)
    result_grain: list[str] | dict[str, Any] | None = None
    # J7 — the OPTIONAL window-anchor declaration (`blueprint.models.WINDOW_ANCHORS`:
    # `"data"` | `"calendar"`). Meaningful only for a WINDOWED blueprint; `None` (the
    # default) means the blueprint makes no claim, nothing is stored, and the read path
    # surfaces nothing — so every existing seed and every non-windowed canon YAML is
    # byte-identical to before. Stored as a plain string property (not `*_json`): it is a
    # closed enum, and `validate_blueprint_dag` rejects anything outside the set at
    # WRITE, so a stored value is always renderable.
    window_anchor: str | None = None


@dataclass(frozen=True)
class KnowledgeSeed:
    """One hand-authored global-knowledge fixture (entity-agnostic)."""

    id: str
    text: str
    doc_id: str
    title: str | None = None
    status: str = "validated"
    # Provenance/drift (UI Slice 2 — mirror the blueprint side). `created_by`
    # distinguishes a hand-authored seed (`"seed"`, the default so existing
    # `knowledge.yaml` fixtures stay byte-identical) from a chunk the LEARNING LOOP
    # landed (`"learning"`); `source_candidate_id` stamps the originating candidate.
    # `drift_status` is threaded for parity + so a retraction can stamp it (knowledge
    # recall does not read it — only blueprints replay/drift). `source_candidate_id=
    # None` sets no neo4j property (neo4j drops a `SET x = null`), so a fixture node
    # is unchanged.
    drift_status: str = "clean"
    created_by: str = "seed"
    source_candidate_id: str | None = None
    # Governed-corpus trust partition (Phase 2) — the knowledge-side mirror of the
    # blueprint fields. `source="mcp"` is the TRUSTED canon partition recall serves;
    # `source="learning"` is the staging tier recall ignores. Defaults `mcp`/`True`
    # keep the fixture path + existing tests trusted-by-construction; the landing
    # writer overrides to `learning`/`False`. See `BlueprintSeed.source`.
    source: str = "mcp"
    verified: bool = True


class CorpusLoadError(Exception):
    """Raised on a write-time parity violation (mixed embedding models, §3.3)."""


class DimensionMismatchError(CorpusLoadError):
    """Raised when a pre-existing vector index carries a DIFFERENT embedding dimension than
        the one this run wants to write.

        A `CREATE VECTOR INDEX ... IF NOT EXISTS` silently KEEPS the old (wrong) dimension, so
        the only way to detect a changed embedding model is to introspect `SHOW VECTOR INDEXES`
        and raise LOUD. The singleton hydrator catches this and nukes + rebuilds at the new
        dimension.
    """


_BLUEPRINT_SEED_FIELDS = frozenset(f.name for f in fields(BlueprintSeed))
_KNOWLEDGE_SEED_FIELDS = frozenset(f.name for f in fields(KnowledgeSeed))


def _seed_from_entry(entry_id: str, entry: dict[str, Any], *, kind: str) -> Any:
    """Project one MCP-export entry onto a `BlueprintSeed`/`KnowledgeSeed`, WHITELISTING to
        the dataclass fields.

        The export is a separate repo's canon, so unknown or extra keys are DROPPED rather than
        spread blindly into the constructor. The dict key is the authoritative id.
        `source`/`verified` come through verbatim only when present AS THE RIGHT TYPE; absent OR
        malformed falls back to the dataclass defaults (`mcp`/`True`).

        The malformed fallback matters because the upsert writes `b.source = $source` and neo4j
        REMOVES a property set to null — an explicit `"source": null` produced a SOURCELESS node,
        invisible to recall's fail-closed trust gate but perfectly visible to the prior-art read,
        which deliberately drops that gate. Rather than teach every reader to coalesce, the
        WRITER always stamps: a sourceless node can then only come from a hand edit or a foreign
        writer, which is what `priorart.models.TIER_UNSOURCED` is for.

        Not a trust escalation: everything here came from the canon export over the
        service-key-authenticated route, so trust rests on the TRANSPORT and `source` is
        provenance metadata the export echoes back.
    """
    fields_ = _BLUEPRINT_SEED_FIELDS if kind == "blueprint" else _KNOWLEDGE_SEED_FIELDS
    data = {k: v for k, v in entry.items() if k in fields_}
    data["id"] = entry_id or data.get("id")
    _drop_malformed_trust_stamp(data, entry_id=data["id"], kind=kind)
    return BlueprintSeed(**data) if kind == "blueprint" else KnowledgeSeed(**data)


def _drop_malformed_trust_stamp(
    data: dict[str, Any], *, entry_id: Any, kind: str
) -> None:
    """Coerce a malformed `source`/`verified` so the node write always gets a usable value.
        Mutates *data*.

        THE TWO FIELDS TAKE DIFFERENT FALLBACKS, and the asymmetry is the point.

        `source` must be a NON-EMPTY `str` — `""` would write a property matching neither trust
        partition, a third state nothing handles — so a malformed one is DROPPED and the
        dataclass default (`mcp`) applies. An exporter emitting `"source": null` is
        indistinguishable in trust terms from one omitting the key, which already defaults to
        `mcp`; trust rests on the authenticated transport, not on a field in the payload.

        `verified` must be a real `bool`, and a malformed one is set to `False`, NOT dropped: a
        present `"verified": "false"` plausibly MEANT false, and falling back to the dataclass
        default would silently INVERT it. An ABSENT `verified` still defaults to `True`.
    """
    source = data.get("source")
    if "source" in data and not (isinstance(source, str) and source.strip()):
        _logger.warning(
            "%s corpus entry %r carries a malformed `source` (%r); defaulting to the "
            "trusted `mcp` stamp. Writing it through would REMOVE the property (neo4j "
            "drops null-valued sets) and leave a sourceless node.",
            kind,
            entry_id,
            source,
        )
        del data["source"]
    verified = data.get("verified")
    if "verified" in data and not isinstance(verified, bool):
        _logger.warning(
            "%s corpus entry %r carries a non-boolean `verified` (%r); stamping FALSE "
            "(unverified). Not the dataclass default: a malformed value may well have "
            "meant false, and defaulting would invert it to true.",
            kind,
            entry_id,
            verified,
        )
        data["verified"] = False


def _seeds_from_entries(raw: dict[str, Any], *, kind: str) -> list[Any]:
    """Build seeds from a `{<id>: <entry>}` map, DEGRADE-not-fail per entry.

        A non-dict entry, a falsy id, or one the dataclass constructor rejects is SKIPPED with a
        warning. This is load-bearing: the cache re-arms and retries the SAME export every turn
        on a raised seed, so one malformed entry from the separate MCP repo would otherwise brick
        the corpus indefinitely.

        THE SCOPE OF THAT PROMISE IS NARROWER THAN IT READS: it covers the PROJECTION step only.
        `load_corpus`'s pre-write pass still raises `CorpusLoadError` on the first failure of
        `validate_blueprint_uses`, `resolve_blueprint_references` or `validate_blueprint_dag`,
        aborting everything — deliberately, since those are authoring errors that must not ship
        half-applied. See `_reference_graph` for the case whose blast radius grew.
    """
    seeds: list[Any] = []
    for entry_id, entry in raw.items():
        if not isinstance(entry, dict):
            _logger.warning("skipping non-dict %s corpus entry %r", kind, entry_id)
            continue
        if not entry_id:
            _logger.warning("skipping %s corpus entry with a falsy id", kind)
            continue
        try:
            seeds.append(_seed_from_entry(str(entry_id), entry, kind=kind))
        except Exception:  # noqa: BLE001 - one bad entry is skipped, never fatal to the seed
            _logger.warning(
                "skipping malformed %s corpus entry %r (bad shape/values); the rest of "
                "the corpus still loads",
                kind,
                entry_id,
                exc_info=True,
            )
    return seeds


def corpus_seeds_from_export(
    export: dict[str, Any],
) -> tuple[list[BlueprintSeed], list[KnowledgeSeed]]:
    """Build the seed lists from a combined corpus export dict
        (`{"blueprints": {<id>: <entry>}, "knowledge": {<id>: <entry>}, ...}`) — the online
        analogue of `load_seed_fixtures`.

        Each entry is the verbatim fields the MCP export routes serve, plus the `source`/
        `verified` stamp injected at export time. DEGRADE-not-fail per entry, so a single bad
        entry from the separate MCP repo can never brick the whole corpus seed.
    """
    blueprints = _seeds_from_entries(export.get("blueprints") or {}, kind="blueprint")
    knowledge = _seeds_from_entries(export.get("knowledge") or {}, kind="knowledge")
    return blueprints, knowledge


__all__ = [
    "BlueprintSeed",
    "CorpusLoadError",
    "DimensionMismatchError",
    "KnowledgeSeed",
    "corpus_seeds_from_export",
]
