"""The D48 canonical hard key — the S4→S6 coupling point (Contract C §3).

The key is the SHA-256 of the canonical JSON of EXACTLY, in this order:

    [ resolves , uses_rules , result_grain , canonical_ast_norm ]

- `resolves`            — `payload.resolves` (S3), dict; canonical JSON sorts its keys.
- `uses_rules`          — `generalization.uses_rules` (S4), a rule SET: deduplicated
                          + sorted here (QA-Q5) so listing a rule twice cannot mint a
                          different key.
- `result_grain`        — `generalization.result_grain` (S4), a `{columns, verifiable}`
                          dict; a defensive cross-check, subsumed by the AST (D48 N4).
                          `columns` is order-normalized (sorted) here (QA-Q6).
- `canonical_ast_norm`  — `generalization.canonical_ast_norm` (S4). **OPAQUE.** S6
                          NEVER re-parses it — a minor sqlglot bump can re-render it,
                          and the whole point of the freeze is that S4 produces the
                          exact string S6 hashes. Treat it as a byte-stable token.

The serializer is the SHARED `data_agent.canonical.canonical_json` (D96 §5:
`sort_keys`, no insignificant whitespace, UTF-8, `ensure_ascii=False`) so the hash
is deterministic across processes and Python runs AND comparable with the
`structural_key` digests minted on the runtime side. The returned key carries a
`sha256:` prefix, matching the corpus `canonical_key` convention.

Single-writer-per-key by construction: two candidates with identical
`(resolves, uses_rules, result_grain, canonical_ast_norm)` hash to the SAME key, so
the hard layer is a deterministic equality test — the real cross-process
Redis-lock / partitioned-queue leg (Layer 2 race-safety) is Wave-3 and does not
change this function.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from data_agent.canonical import canonical_json as _canonical_json


def _normalized_grain(result_grain: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize `result_grain` so semantically-identical grains hash equal
    (QA-Q6). `columns` is a SET of grain columns — `canonical_json` sorts dict keys
    but preserves LIST order, so two grains with the same columns in a different
    order would otherwise mint different keys. Sort the columns array explicitly."""
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
    """Return the D48 `sha256:`-prefixed canonical key. Inputs/order are FROZEN
    (Contract C §3); do not add, drop, or reorder members.

    `uses_rules` is a rule SET: it is DEDUPLICATED and sorted (QA-Q5) so a blueprint
    that references the same catalog rule from two locators hashes equal to one that
    lists it once. `result_grain.columns` is likewise order-normalized (QA-Q6)."""
    parts = [
        dict(resolves),
        sorted(set(uses_rules)),
        _normalized_grain(result_grain),
        canonical_ast_norm,
    ]
    digest = hashlib.sha256(_canonical_json(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
