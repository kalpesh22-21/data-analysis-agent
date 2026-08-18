"""`canonical_ast_norm` — the pinned §11.2 sqlglot normalization recipe (D48).

This string is the S6 dedup hash input, so it MUST be byte-stable across builds and CI: the
sqlglot version is pinned because a minor bump can change the render, silently mint a
different `canonical_key`, and degrade a D48 `increment` into a spurious `insert`. The
recipe itself lives in `runtime/blueprint/structural_key.py` and this is the learning-side
adapter over it — the D58c no-import invariant forbids the request path from importing
`data_agent.learning.*`, so there is one definition, imported downward. HASH-INPUT ONLY:
the returned string is never re-parsed.
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

    Single: `sql_template` is the one top-level template. Composite: `sql_template` is None, and
    the per-node normalized templates are joined in ascending `order` by a single newline.
    Projects the S4 `NodeTemplate` value objects onto the shared `(order, template)` pairs the
    runtime normalizer takes, so the learning plane and the canon seeder run the IDENTICAL
    recipe over the IDENTICAL join.
    """
    return blueprint_canonical_ast_norm(
        sql_template, [(n.order, n.sql_template) for n in node_templates]
    )


__all__ = ["canonical_ast_norm", "canonical_ast_norm_one"]
