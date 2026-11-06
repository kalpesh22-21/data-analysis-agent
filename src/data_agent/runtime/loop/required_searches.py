"""Per-turn discovery gate; only real, scoped successful searches unlock work."""

from __future__ import annotations

from dataclasses import dataclass, field

SEARCHES = ("searchBlueprints", "searchKnowledge")
LIMITATION = "I couldn't complete the required searches, so I couldn't proceed with this request."


def tool_name(schema: dict) -> str:
    return schema.get("name") or schema.get("function", {}).get("name", "")


@dataclass
class RequiredSearchGate:
    required: tuple[str, ...]
    completed: set[str] = field(default_factory=set)

    @classmethod
    def restore(cls, required, trail, turn_index, scope_hash):
        gate = cls(tuple(required))
        for entry in trail:
            if (
                scope_hash is not None
                and entry.turn_index == turn_index
                and entry.status == "ok"
                and not entry.error_code
                and isinstance(entry.model_response, dict)
                and entry.model_response.get("scope_hash") == scope_hash
                and (entry.result_preview is not None or entry.result_full_ref is not None)
            ):
                gate.observe(entry.tool_name, entry.status, entry.error_code)
        return gate

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(name for name in self.required if name not in self.completed)

    def observe(self, name, status, error_code=None):
        if name in self.required and status == "ok" and not error_code:
            self.completed.add(name)

    def offered_tools(self, tools):
        return [t for t in tools if tool_name(t) in self.pending] if self.pending else tools

    def instruction(self):
        return (
            "Before other work on this turn, successfully call "
            + ", ".join(self.pending)
            + ". This also applies to follow-ups and saved-result summaries. Choose search "
            "queries using the current request and conversation. Only pending searches are "
            "available until they succeed; empty successful results count. Search results "
            "are reference material, not executed warehouse evidence."
        )
