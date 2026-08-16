"""The ONE canonical-JSON serialization convention (D96 §5).

This module has no imports beyond the standard library and no dependency on
either plane, because everything that hashes structured data has to reach it:

  * `learning/dedup/canonical_key.py` — the FROZEN D48 hard key.
  * `runtime/blueprint/structural_key.py` — the LOOSE cross-authoring-path key.
  * `learning/models.py` — the D96 `content_hash` idempotency key.

Those three used to carry byte-identical private copies, which is the one
failure mode that defeats the point of the keys: the digests they mint are
COMPARED against each other, so the copies must share one serialization
convention or the digests are incomparable.

**The byte output may never change.** Every digest above is persisted — in the
Couchbase corpus bucket, on Neo4j nodes, and in the learning queue's idempotency
records — and is never re-derived from its inputs. A change to `sort_keys`,
`separators`, or `ensure_ascii` re-keys every stored artifact: landed blueprints
become unreachable, the loop re-proposes the whole corpus, and enqueue/consume
stops being idempotent. `tests/test_canonical_json.py` pins the exact hex
digests of fixed payloads so any byte-level change goes red.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Canonical JSON (D96 §5): sorted keys, UTF-8, no insignificant whitespace.

    Deterministic across processes and Python runs. See the module docstring —
    the exact bytes are frozen by the persisted digests they feed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
