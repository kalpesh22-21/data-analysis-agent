"""The ONE canonical-JSON serialization convention (D96 §5).

**The byte output may never change.** Every digest built on it is PERSISTED (Couchbase
corpus, Neo4j nodes, learning-queue idempotency records) and never re-derived, so a change
to `sort_keys`/`separators`/`ensure_ascii` re-keys every stored artifact.
`tests/test_canonical_json.py` pins the exact hex digests of fixed payloads.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Canonical JSON (D96 §5): sorted keys, UTF-8, no insignificant whitespace.

    Deterministic across processes and runs; the exact bytes are frozen (see module docstring).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
