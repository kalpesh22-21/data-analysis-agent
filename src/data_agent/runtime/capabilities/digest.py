"""Compact, sanitized digests of WHAT a UI capability shows and HOW it is presented.

Safety contract: only provider-authored HUMAN LABELS are used — presentation
title/fields, metadata.gql[].mapping[].description/fieldName, navigation
metadata.arguments.links[].description/webPage, and the component name from
preamble_url / metadata.widgetName. Raw GraphQL text, gql query/link entries, raw
field paths, and internal redirect links are NEVER read here. Every label passes
through sanitize_text and all lists are capped against prompt bloat — excess items
are silently dropped: the digest is a selection hint, not the payload contract.
"""

from collections.abc import Mapping
from typing import Any

from data_agent.runtime.sanitize import sanitize_text

MAX_DIGEST_ITEMS = 8
_MAX_LABEL_CHARS = 120
_MAX_NAME_CHARS = 60
_MAX_PRESENTATION_CHARS = 240

_KIND_LABELS = {"navigation": "navigation", "data_widget": "data widget"}

# Component-name suffix → short human hint. On a hint hit the component name is
# KEPT in parentheses (the provider's own identifier stays debuggable).
_COMPONENT_HINTS: tuple[tuple[str, str], ...] = (
    ("card", "card"),
    ("table", "table"),
    ("grid", "table"),
    ("chart", "chart"),
    ("graph", "chart"),
    ("form", "form"),
    ("button", "button link"),
    ("link", "button link"),
    ("modal", "dialog"),
    ("dialog", "dialog"),
)

# Schemes that must never survive into a model-facing label even wrapped in a
# `scheme:rest` shape (XSS/unsafe-URI families). A denylist for actively-dangerous
# schemes, not scheme validation of the (untrusted) preamble itself.
_UNSAFE_SCHEMES = frozenset({"javascript", "data", "vbscript", "file"})


def _looks_url_shaped(value: str) -> bool:
    """Absolute-URL and protocol-relative shapes (`scheme://…`, `//…`)."""
    return "://" in value or value.startswith("//")


def _looks_url_or_path_like(value: str) -> bool:
    """Recognizable URL/path shapes are addresses, not navigation titles."""
    return (
        _looks_url_shaped(value)
        or value.startswith("/")
        or value.startswith(("./", "../"))
    )


def _component_name(raw: Any) -> str | None:
    """Extract a component name from a preamble URL like `ember:EmployeeCard`.

    A `scheme:name` shape yields `name`; a bare name is used as-is after structural
    sanitization. URL-shaped values are REJECTED, never rendered. Sanitize BEFORE
    the shape checks: control chars are dropped by sanitize_text, so
    `java\\x00script:…` checked raw would pass the `.isalpha()` scheme gate and only
    reassemble into `javascript:…` after cleaning; checking the already-sanitized
    value closes that smuggle.
    """
    if not isinstance(raw, str):
        return None
    component = sanitize_text(raw, _MAX_NAME_CHARS)
    if not component or _looks_url_shaped(component):
        return None
    scheme, separator, rest = component.partition(":")
    if separator and scheme.isalpha() and rest:
        if scheme.casefold() in _UNSAFE_SCHEMES:
            return None
        component = rest
    if any(char in component for char in ":/\\"):
        return None
    return component or None


def presentation_detail_label(kind, *, preamble_url=None, widget_name=None) -> str | None:
    """The full `kind — detail` presentation label, or None when no component is
    known — a bare kind repeats what the card line already shows."""
    component = _component_name(preamble_url) or _component_name(widget_name)
    if component is None:
        return None
    base = _KIND_LABELS.get(kind, str(kind))
    lowered = component.casefold()
    hint = next(
        (label for suffix, label in _COMPONENT_HINTS if lowered.endswith(suffix)),
        None,
    )
    detail = f"{hint} ({component})" if hint else component
    return sanitize_text(f"{base} — {detail}", _MAX_PRESENTATION_CHARS)


def presentation_label(kind, *, preamble_url=None, widget_name=None) -> str:
    """One-line "how it will be presented" label, e.g. `data widget — card
    (EmployeeCard)`; just the kind label when no component is known."""
    return presentation_detail_label(
        kind, preamble_url=preamble_url, widget_name=widget_name
    ) or _KIND_LABELS.get(kind, str(kind))


def card_display_fields(card) -> tuple[str, ...]:
    """What the card will show, from the provider-supplied search-card `presentation`
    block: the card title plus each display field as `name — description`. Empty when
    the provider sent no presentation."""
    presentation = card.presentation
    if presentation is None:
        return ()
    labels: list[str] = []
    seen: set[str] = set()

    def _push(label: str) -> None:
        if label and len(labels) < MAX_DIGEST_ITEMS and label.casefold() not in seen:
            seen.add(label.casefold())
            labels.append(label)

    if presentation.title:
        _push(sanitize_text(presentation.title, _MAX_LABEL_CHARS))
    for field in presentation.fields[:MAX_DIGEST_ITEMS]:
        name = sanitize_text(field.name, _MAX_NAME_CHARS)
        description = sanitize_text(field.description, _MAX_LABEL_CHARS)
        if not (name or description):
            # Both halves sanitized away — skip rather than emit a degenerate
            # literal ` — ` fragment.
            continue
        _push(
            sanitize_text(
                " — ".join(part for part in (name, description) if part), _MAX_LABEL_CHARS
            )
        )
    return tuple(labels)


def _webpage_fallback(raw: Any) -> str | None:
    """Navigation webPage fallback must be a page TITLE, not an address — sanitize
    BEFORE the shape checks (same smuggle as _component_name)."""
    if not isinstance(raw, str):
        return None
    candidate = sanitize_text(raw, _MAX_LABEL_CHARS)
    if not candidate or _looks_url_or_path_like(candidate):
        return None
    scheme, separator, rest = candidate.partition(":")
    if separator and (
        scheme.casefold() in _UNSAFE_SCHEMES or (rest and not rest.startswith(" "))
    ):
        return None
    return candidate


def metadata_data_digest(kind, metadata) -> tuple[str, ...]:
    """What the option will display, from provider-authored metadata. Works over a
    CapabilityDefinition (`definition_data_digest == metadata_data_digest(kind,
    definition.metadata)`) or a raw hydrated-card metadata dict — the answer judge's
    capability_presented section holds the latter, no definition fetch."""
    labels: list[str] = []
    seen: set[str] = set()

    def _push_first(*candidates: Any) -> None:
        if len(labels) >= MAX_DIGEST_ITEMS:
            return
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            label = sanitize_text(candidate, _MAX_LABEL_CHARS)
            if not label or label.casefold() in seen:
                # Sanitized to nothing or already recorded → let the NEXT candidate
                # (the fieldName / webPage fallback) contribute instead.
                continue
            seen.add(label.casefold())
            labels.append(label)
            return

    if kind == "navigation":
        arguments = metadata.get("arguments")
        links = arguments.get("links") if isinstance(arguments, dict) else None
        if isinstance(links, list):
            for link in links[:MAX_DIGEST_ITEMS]:
                if isinstance(link, dict):
                    _push_first(link.get("description"), _webpage_fallback(link.get("webPage")))
        return tuple(labels)

    gql = metadata.get("gql")
    if isinstance(gql, list):
        for entry in gql[:MAX_DIGEST_ITEMS]:
            if not isinstance(entry, dict):
                continue
            mapping = entry.get("mapping")
            if not isinstance(mapping, list):
                continue
            for item in mapping[:MAX_DIGEST_ITEMS]:
                if isinstance(item, dict):
                    _push_first(item.get("description"), item.get("fieldName"))
    return tuple(labels)


def filter_labels_digest(metadata) -> tuple[str, ...]:
    """Human labels for the widget's interactive filters/parameters, from
    metadata.ui_parameters (or a filters sibling) — label/description/name keys only,
    each sanitized, at most MAX_DIGEST_ITEMS."""
    labels: list[str] = []
    seen: set[str] = set()
    raw = metadata.get("ui_parameters")
    if not isinstance(raw, list):
        raw = metadata.get("filters")
    if not isinstance(raw, list):
        return ()
    for item in raw[:MAX_DIGEST_ITEMS]:
        candidate = item
        if isinstance(item, Mapping):
            candidate = item.get("label") or item.get("description") or item.get("name")
        if not isinstance(candidate, str):
            continue
        label = sanitize_text(candidate, _MAX_LABEL_CHARS)
        if not label or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        labels.append(label)
    return tuple(labels)


def widget_label(kind, *, preamble_url=None, widget_name=None) -> str | None:
    """The presentation label for a widget whose KIND may be unknown (the judge
    builds its section from the shipped card, which carries no kind): with a kind,
    exactly presentation_label; without, the bare component name; with nothing, None
    (the caller OMITS the field — degrade-to-absent, never a fabricated label)."""
    if kind in _KIND_LABELS:
        return presentation_label(
            kind,
            preamble_url=preamble_url if isinstance(preamble_url, str) else None,
            widget_name=widget_name if isinstance(widget_name, str) else None,
        )
    return _component_name(preamble_url) or _component_name(widget_name)


def join_digest(labels: tuple[str, ...]) -> str:
    """Join digest labels for a single model-facing line, capped per line."""
    return sanitize_text("; ".join(labels), _MAX_PRESENTATION_CHARS)


def definition_data_digest(definition) -> tuple[str, ...]:
    return metadata_data_digest(definition.kind, definition.metadata)
