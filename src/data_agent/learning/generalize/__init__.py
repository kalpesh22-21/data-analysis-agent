"""Slice 4 — the deterministic generalize + AST-rewrite + static-validate stage.

S3 emits a blueprint PLAN (`extractor/models.py::BlueprintPayload`): it classifies
each literal predicate of the ACCEPTED SQL as `slot | rule | inline` (D97 totality)
but never re-emits SQL (D35). S4 turns that plan + the accepted SQL (read from the
session's tool trail) into a `BlueprintGeneralization` (Contract A,
`candidate/generalization.py`) merged under `payload["generalization"]`:

  * `sql_template` — the accepted SQL AST-rewritten to `:slot` placeholders
    (role=slot), literals kept (role=inline), rule predicates dropped + recorded in
    `uses_rules` (role=rule). Composite → one `NodeTemplate` per `composes[*].order`.
  * `uses` — byte-exact `database.table.column` scope keys, from the D69/D87
    provenance extractor (reused, never reimplemented).
  * `result_grain` — the D56 teeth, from `result_signature.grain`.
  * `static_validation` — the dry-run stamp; ANY false check ⇒ `fail_to_review`
    (D52/D97), an in-band value S7 routes on, never a raised error nor an
    auto-promote.
  * `canonical_ast_norm` — the pinned §11.2 sqlglot render, the S6 hash input.
    HASH-INPUT ONLY — never re-parsed.

NO LLM here: every step is a deterministic function of the plan + the accepted SQL
+ the catalog. Un-rewritable / unparseable SQL and `when`-bearing composites are
rejected to review (`fail_to_review`), never a guessed template.
"""

from __future__ import annotations

from .builder import generalize_blueprint
from .canonical import canonical_ast_norm
from .mapping import blueprint_from_generalization
from .rewrite import RewriteError, rewrite_sql_to_template
from .stage import GeneralizeStage

__all__ = [
    "GeneralizeStage",
    "RewriteError",
    "blueprint_from_generalization",
    "canonical_ast_norm",
    "generalize_blueprint",
    "rewrite_sql_to_template",
]
