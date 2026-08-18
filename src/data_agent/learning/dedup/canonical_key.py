"""The D48 canonical hard key — the S4→S6 coupling point (Contract C §3).

The SHA-256 of the canonical JSON of EXACTLY, in this order:
`[resolves, uses_rules, result_grain, canonical_ast_norm]`. `uses_rules` is a rule SET
(deduplicated + sorted here, QA-Q5) and `result_grain.columns` is order-normalized (QA-Q6),
so listing a rule or a column twice, or in another order, cannot mint a different key.
`canonical_ast_norm` is OPAQUE — S6 NEVER re-parses it, because a minor sqlglot bump can
re-render it and the whole point of the freeze is that S4 produces the exact string S6
hashes. The serializer is the SHARED `data_agent.canonical.canonical_json`, so the digest is
deterministic across processes AND comparable with the runtime-side `structural_key`
digests. Two candidates with identical inputs hash to the SAME key, which is what makes the
hard layer a deterministic equality test.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from data_agent.canonical import canonical_json as _canonical_json


def _normalized_grain(result_grain: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize `result_grain` so semantically-identical grains hash equal (QA-Q6).

    `columns` is a SET of grain columns, but `canonical_json` sorts dict keys while preserving
    LIST order, so two grains with the same columns in a different order would otherwise mint
    different keys.
    """
    grain = dict(result_grain)
    columns = grain.get("columns")
    if isinstance(columns, (list, tuple)):
        grain["columns"] = sorted(columns)
    return grain


def compute_canonical_key(
    resolves: Mapping[str, Any],
    uses_rules: Iterable[str],
    result_grain: Mapping[str, Any],
    canonical_ast_norm: str,
) -> str:
    """The D48 `sha256:`-prefixed canonical key. Inputs and order are FROZEN (Contract C §3).

    Do not add, drop or reorder members. `uses_rules` is DEDUPLICATED and sorted (QA-Q5), so a
    blueprint referencing the same catalog rule from two locators hashes equal to one that lists
    it once; `result_grain.columns` is likewise order-normalized (QA-Q6).
    """
    parts = [
        dict(resolves),
        sorted(set(uses_rules)),
        _normalized_grain(result_grain),
        canonical_ast_norm,
    ]
    digest = hashlib.sha256(_canonical_json(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
