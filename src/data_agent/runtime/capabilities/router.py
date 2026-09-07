from __future__ import annotations

import re
from typing import Literal

PrefetchRoute = Literal["action_navigation", "data", "both", "ambiguous"]

_ACTION_NAVIGATION_PATTERNS = (
    r"\b(?:open|navigate|visit|manage|create|change|update|edit|promote|demote|offboard|move)\b",
    r"\b(?:take|send) me to\b",
    r"\bwhere (?:is|can|do)\b",
)
_DATA_PATTERNS = (
    r"\b(?:how many|count|total|average|compare|trend|report)\b",
    r"\b(?:show|list|find|display)\b",
    r"\b(?:profile|details|information|status)\b",
)


class PrefetchRouter:
    def route(self, query: str) -> PrefetchRoute:
        text = query.casefold()
        action_navigation = any(
            re.search(pattern, text) for pattern in _ACTION_NAVIGATION_PATTERNS
        )
        data = any(re.search(pattern, text) for pattern in _DATA_PATTERNS)
        if action_navigation and data:
            return "both"
        if action_navigation:
            return "action_navigation"
        if data:
            return "data"
        return "ambiguous"


__all__ = ["PrefetchRoute", "PrefetchRouter"]
