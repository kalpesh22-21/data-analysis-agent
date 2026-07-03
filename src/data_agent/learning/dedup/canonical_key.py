"""The D48 canonical hard key — the S4→S6 coupling point (Contract C §3).

The key is the SHA-256 of the canonical JSON of EXACTLY, in this order:

    [ resolves , uses_rules , result_grain , canonical_ast_norm ]

- `resolves`            — `payload.resolves` (S3), dict; canonical JSON sorts its keys.
- `uses_rules`          — `generalization.uses_rules` (S4), sorted explicitly here.
- `result_grain`        — `generalization.result_grain` (S4), a `{columns, verifiable}`
                          dict; a defensive cross-check, subsumed by the AST (D48 N4).
- `canonical_ast_norm`  — `generalization.canonical_ast_norm` (S4). **OPAQUE.** S6
                          NEVER re-parses it — a minor sqlglot bump can re-render it,
                          and the whole point of the freeze is that S4 produces the
                          exact string S6 hashes. Treat it as a byte-stable token.

`canonical_json` mirrors the D96 §5 convention (`sort_keys`, no insignificant
whitespace, UTF-8, `ensure_ascii=False`) so the hash is deterministic across
processes and Python runs. The returned key carries a `sha256:` prefix, matching
the corpus `canonical_key` convention.

Single-writer-per-key by construction: two candidates with identical
`(resolves, uses_rules, result_grain, canonical_ast_norm)` hash to the SAME key, so
the hard layer is a deterministic equality test — the real cross-process
Redis-lock / partitioned-queue leg (Layer 2 race-safety) is Wave-3 and does not
change this function.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


def _canonical_json(obj: Any) -> str:
    """Canonical JSON (D96 §5): sorted keys, UTF-8, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_canonical_key(
    resolves: Mapping[str, Any],
    uses_rules: Iterable[str],
    result_grain: Mapping[str, Any],
    canonical_ast_norm: str,
) -> str:
    """Return the D48 `sha256:`-prefixed canonical key. Inputs/order are FROZEN
    (Contract C §3); do not add, drop, or reorder members."""
    parts = [
        dict(resolves),
        sorted(uses_rules),
        dict(result_grain),
        canonical_ast_norm,
    ]
    digest = hashlib.sha256(_canonical_json(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
