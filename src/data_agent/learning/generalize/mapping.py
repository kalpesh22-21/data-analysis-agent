"""S3 PLAN + S4 `BlueprintGeneralization` → runtime `Blueprint` (§1 mapping table).

The whole point of Contract A: a validated candidate promotes (Slice 9) 1:1 onto
`runtime/blueprint/models.py::Blueprint` with ZERO field invention. This function is
that mapping — proving the round-trip in tests and reused by S9 at promotion:

  id ← minted (caller) · intent/resolves/slots ← S3 PLAN ·
  uses_rules/sql_template/result_grain ← S4 generalization ·
  composes ← S3 `ComposeNodePlan` ⨝ S4 `NodeTemplate.sql_template` (by `order`).
"""

from __future__ import annotations

from typing import Any

from ...runtime.blueprint.models import Blueprint
from ..candidate.generalization import BlueprintGeneralization


def blueprint_from_generalization(
    payload: dict[str, Any],
    generalization: BlueprintGeneralization,
    *,
    id: str,
) -> Blueprint:
    """Assemble the runtime `Blueprint` from the S3 payload + the S4 generalization.

    Raises `BlueprintParseError` (from `Blueprint.parse`) if any mapped field is
    malformed — the round-trip contract (`S4-payload-maps-to-runtime-blueprint`)
    asserts this succeeds with no missing field for a valid candidate.
    """
    slots = [
        p["slot"]
        for p in payload.get("parameterization", []) or []
        if p.get("role") == "slot" and p.get("slot")
    ]

    template_by_order = {n.order: n.sql_template for n in generalization.node_templates}
    composes: list[dict[str, Any]] = []
    for node in payload.get("composes", []) or []:
        mapped = dict(node)
        mapped["sql_template"] = template_by_order.get(node.get("order"))
        composes.append(mapped)

    return Blueprint.parse(
        id=id,
        intent=payload.get("intent", ""),
        resolves=payload.get("resolves"),
        slots=slots or None,
        uses_rules=list(generalization.uses_rules),
        sql_template=generalization.sql_template,
        composes=composes or None,
        result_grain=generalization.result_grain.to_doc(),
    )
