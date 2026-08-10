"""`canonical_ast_norm` — the pinned §11.2 sqlglot normalization recipe (D48).

This string is the S6 dedup hash input. It MUST be byte-stable across builds/CI, so
the recipe is exact and the sqlglot version is pinned (`~=30.12`, `pyproject.toml`):
a minor bump can change the render, silently mint a different `canonical_key`, and
degrade a D48 `increment` into a spurious `insert` (a duplicate blueprint).

**The recipe itself now lives in `runtime/blueprint/structural_key.py`** and this
module is the learning-side adapter over it. The move is load-bearing, not cosmetic:
the MCP-canon corpus seeder (`runtime/retrieval/corpus_loader.py`, imported by the
online hydrator) must derive the SAME normalized string for the cross-tier
`structural_key`, and the D58c no-import invariant forbids the runtime/request path
from importing `data_agent.learning.*`. One definition, imported downward — a second
copy of the recipe would let the two writers drift and defeat the key.

The recipe (schema-free, deterministic — do NOT run the full optimizer or `qualify`,
which need a schema and choke on slot placeholders):

    SLOT_TOKEN.sub `{slot}` → `:slot`
      → parse_one(dialect="clickhouse")
      → normalize_identifiers   (case-fold)
      → normalize               (canonical boolean form)
      → .sql(dialect="clickhouse", normalize=True, pretty=False)

Composite rule (PINNED so the S4 producer and the S6 hasher cannot diverge): for a
composite blueprint (top-level `sql_template` is None), `canonical_ast_norm` is the
per-node normalized templates in ASCENDING `order`, joined by a single `\n`.

**HASH-INPUT ONLY.** The returned string is a hash input; it is never re-parsed.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...runtime.blueprint.structural_key import (
    blueprint_canonical_ast_norm,
    canonical_ast_norm_one,
)
from ..candidate.generalization import NodeTemplate


def canonical_ast_norm(
    sql_template: str | None,
    node_templates: Sequence[NodeTemplate] = (),
) -> str:
    """The single- or composite-blueprint canonical string (§11.2 composite rule).

    Single: `sql_template` is the one top-level template.
    Composite: `sql_template` is None; join the per-node normalized templates in
    ascending `order` with a single newline.

    Projects the S4 `NodeTemplate` value objects onto the shared `(order, template)`
    pairs the runtime normalizer takes, so the learning plane and the canon seeder run
    the IDENTICAL recipe over the IDENTICAL join.
    """
    return blueprint_canonical_ast_norm(
        sql_template, [(n.order, n.sql_template) for n in node_templates]
    )


__all__ = ["canonical_ast_norm", "canonical_ast_norm_one"]
