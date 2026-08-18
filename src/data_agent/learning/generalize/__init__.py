"""Slice 4 — the deterministic generalize + AST-rewrite + static-validate stage.

Turns the S3 blueprint PLAN plus the accepted SQL into a `BlueprintGeneralization` merged
under `payload["generalization"]`: the `{slot}` `sql_template`, the byte-exact `uses` scope
keys from the reused D69/D87 provenance extractor, the D56 `result_grain`, the
`static_validation` dry-run stamp, and the pinned `canonical_ast_norm` (hash input only, never
re-parsed). NO LLM: every step is a deterministic function of the plan, the SQL and the
catalog. Un-rewritable SQL and any failing check route to `fail_to_review` — an in-band value
S7 acts on, never a raised error and never a guessed template.
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
