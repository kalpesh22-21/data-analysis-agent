"""`canonical_ast_norm` — the pinned §11.2 sqlglot normalization recipe (D48).

This string is the S6 dedup hash input. It MUST be byte-stable across builds/CI, so
the recipe is exact and the sqlglot version is pinned (`~=30.12`, `pyproject.toml`):
a minor bump can change the render, silently mint a different `canonical_key`, and
degrade a D48 `increment` into a spurious `insert` (a duplicate blueprint).

The stored `sql_template` is BRACE authoring form (`{slot}`, runtime-executable with
zero translation). The recipe converts each `{slot}` → `:slot` FIRST — reusing the
runtime binder's `_SLOT_TOKEN` regex (`runtime/blueprint/template.py`), the SAME
rewrite the executor applies — so the hash input is computed from the identical
`:slot` intermediate the runtime parses. This keeps `canonical_ast_norm` byte-stable
regardless of the stored placeholder surface.

The recipe (schema-free, deterministic — do NOT run the full optimizer or `qualify`,
which need a schema and choke on slot placeholders):

    _SLOT_TOKEN.sub `{slot}` → `:slot`
      → parse_one(dialect="clickhouse")
      → normalize_identifiers   (case-fold)
      → normalize               (canonical boolean form)
      → .sql(dialect="clickhouse", normalize=True, pretty=False)

A `:slot` placeholder parses to a sqlglot `Placeholder` and renders in the ClickHouse
dialect as the stable canonical token `{slot: }`, surviving the round-trip unchanged.

Composite rule (PINNED so the S4 producer and the S6 hasher cannot diverge): for a
composite blueprint (top-level `sql_template` is None), `canonical_ast_norm` is the
per-node normalized templates in ASCENDING `order`, joined by a single `\n`.

**HASH-INPUT ONLY.** The returned string is a hash input; it is never re-parsed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlglot
from sqlglot.optimizer.normalize import normalize
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from ...runtime.blueprint.template import SLOT_TOKEN
from ..candidate.generalization import NodeTemplate

_DIALECT = "clickhouse"


def canonical_ast_norm_one(sql_template: str) -> str:
    """Normalize a single template by the exact §11.2 recipe. Assumes a parseable
    template (the caller has already produced it via the AST rewrite).

    The stored template is BRACE authoring form (`{slot}`); rewrite each `{slot}` →
    `:slot` FIRST (the runtime binder's `SLOT_TOKEN`, reused not reimplemented) so the
    hash input is computed from the identical `:slot` intermediate the runtime parses."""
    colon_template = SLOT_TOKEN.sub(lambda m: f":{m.group(1)}", sql_template)
    ast = sqlglot.parse_one(colon_template, dialect=_DIALECT)
    ast = normalize_identifiers(ast, dialect=_DIALECT)
    ast = normalize(ast)
    return ast.sql(dialect=_DIALECT, normalize=True, pretty=False)


def canonical_ast_norm(
    sql_template: str | None,
    node_templates: Sequence[NodeTemplate] = (),
) -> str:
    """The single- or composite-blueprint canonical string (§11.2 composite rule).

    Single: `sql_template` is the one top-level template.
    Composite: `sql_template` is None; join the per-node normalized templates in
    ascending `order` with a single newline.
    """
    if sql_template is not None:
        return canonical_ast_norm_one(sql_template)
    ordered = sorted(node_templates, key=lambda n: n.order)
    return "\n".join(canonical_ast_norm_one(n.sql_template) for n in ordered)
