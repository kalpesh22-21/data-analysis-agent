"""Current-turn Help Center availability and actual document evidence."""
from dataclasses import dataclass
from typing import Any

HELP_UNAVAILABLE_TEXT = "I can't provide verified product instructions from the information available right now."
HELP_TOOLS = frozenset({"searchHelpCenter", "getHelpCenterDocument"})

@dataclass
class HelpGrounding:
    unavailable: bool = False
    document_fetched: bool = False

    def observe(self, name: str, status: str, payload: Any) -> None:
        if name not in HELP_TOOLS:
            return
        if status != "ok":
            self.unavailable = True
            return
        data = payload if isinstance(payload, dict) else {}
        if name == "getHelpCenterDocument":
            content = data.get("content")
            if data.get("found") is True and isinstance(content, str) and content.strip():
                self.document_fetched = True
            else:
                self.unavailable = True
        elif data.get("count") == 0 or data.get("documents") == []:
            self.unavailable = True

    def needs_decline(self, evidence: set[str], has_display: bool) -> bool:
        return (self.unavailable and not self.document_fetched and not has_display
                and not evidence.intersection({"runQuery", "runBlueprint"}))
