"""`GeneralizeStage` — the S4 `CandidateStage` (the write-router pipeline seam).

First stage in the frozen order `generalize (S4) → leakage (S5) → dedup (S6) →
writer (S7)` (`learning/stage.py`, D102 §7.1). It reads the freshly-extracted
blueprint envelope + the session's tool trail (accepted SQL), computes the
`BlueprintGeneralization`, and merges it under `payload["generalization"]` — an
ADDITIVE payload key; it mutates no S3 field.

`control` is always `continue`: a `fail_to_review` outcome is an IN-BAND value on
the payload (`static_validation.outcome`) that the S7 writer routes on — S4 never
drops it and never raises (D52/D97). Non-blueprint envelopes pass straight through.

Wiring (NOT done here — a one-line registration at the composition root, D102 §7.1):
    stages=(GeneralizeStage(catalog_schema=load_catalog_from_dir(...)), ...)
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..candidate.models import CandidateEnvelope
from ..stage import StageContext, StageResult
from .builder import generalize_blueprint


@dataclass(frozen=True)
class GeneralizeStage:
    """Deterministic (NO LLM) generalize + AST-rewrite + static-validate stage.

    `catalog_schema`: the D69 `database.table` → `{column: type}` catalog the
    provenance extractor qualifies against (injected at the composition root)."""

    catalog_schema: dict[str, dict[str, str]]
    stage_id: str = "generalize"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        if env.type != "blueprint":
            return StageResult(envelope=env, control="continue")

        sql_by_ref: dict[str, str | None] = {
            tc.tool_call_ref: tc.sql for tc in ctx.summary.tool_calls
        }
        generalization = generalize_blueprint(env.payload, sql_by_ref, self.catalog_schema)

        new_payload = dict(env.payload)
        new_payload["generalization"] = generalization.to_doc()
        enriched = replace(env, payload=new_payload)
        return StageResult(envelope=enriched, control="continue")
