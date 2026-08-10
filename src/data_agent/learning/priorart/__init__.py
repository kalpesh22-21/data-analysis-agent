"""priorart — the cross-tier "does this already exist?" read (plan §2).

`index.py` holds the port + the Layer-1 fake, `models.py` the entity-free card, and
`neo4j_index.py` the real reader (whose Cypher deliberately drops recall's `source`
trust gate — read that module's docstring before touching it).

`neo4j_index` is NOT re-exported here: importing it pulls in `runtime.blueprint`'s
sqlglot machinery, and the port + fake must stay importable by the unit suite with no
such cost. Import the concrete reader from its own module at the composition root.
"""

from .index import (
    InMemoryPriorArtIndex,
    PriorArtIndex,
    PriorArtUnavailableError,
    token_overlap,
)
from .models import (
    MODEL_MISMATCH_PENALTY,
    TERMINAL_STATUSES,
    TIER_LEARNING,
    TIER_MCP,
    TIER_UNSOURCED,
    PriorArtCard,
    PriorArtKind,
    PriorArtTier,
)

__all__ = [
    "MODEL_MISMATCH_PENALTY",
    "TERMINAL_STATUSES",
    "TIER_LEARNING",
    "TIER_MCP",
    "TIER_UNSOURCED",
    "InMemoryPriorArtIndex",
    "PriorArtCard",
    "PriorArtIndex",
    "PriorArtKind",
    "PriorArtTier",
    "PriorArtUnavailableError",
    "token_overlap",
]
