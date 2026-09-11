from __future__ import annotations

from typing import Any

from data_agent.runtime.sanitize import sanitize_text

from .client import CapabilityClient, CapabilityKind, CapabilityPrefetch
from .router import PrefetchRouter

_KINDS: dict[str, tuple[CapabilityKind, ...]] = {
    # CL action-bearing tools are registered as data widgets, so action searches
    # must include both registry kinds.
    "action_navigation": ("navigation", "data_widget"),
    "data": ("data_widget",),
    "both": ("navigation", "data_widget"),
    "ambiguous": ("navigation", "data_widget"),
}


async def prefetch_capabilities(
    client: CapabilityClient, query: str, router: PrefetchRouter | None = None
) -> CapabilityPrefetch:
    route = (router or PrefetchRouter()).route(query)
    cards = await client.search(query, _KINDS[route], limit=5)
    return CapabilityPrefetch(route=route, cards=tuple(cards))


def render_capability_prefetch(prefetch: CapabilityPrefetch) -> dict[str, Any] | None:
    if not prefetch.cards:
        return None
    lines = [
        "[Available UI options for this request]",
        f"Request route: {prefetch.route}",
        "Load a suitable option with getCapabilityTool before presenting it.",
        "For a concrete action, prefer an option whose matched actions explicitly cover "
        "it over a general page-navigation option.",
    ]
    for card in prefetch.cards:
        lines.append(
            f"- {sanitize_text(card.tool_name, 200)} ({card.kind}): "
            f"{sanitize_text(card.summary, 500)}"
        )
        for label, values in (
            ("questions", card.matched_questions),
            ("actions", card.matched_actions),
            ("data", card.matched_data_points),
        ):
            if values:
                lines.append(
                    f"  {label}: " + "; ".join(sanitize_text(value, 300) for value in values)
                )
    return {"role": "user", "content": "\n".join(lines)}


def route_uses_data_prefetch(route: str) -> bool:
    return route in {"data", "both", "ambiguous"}


__all__ = ["prefetch_capabilities", "render_capability_prefetch", "route_uses_data_prefetch"]
