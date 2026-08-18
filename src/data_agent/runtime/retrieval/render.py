"""render.py — `RetrievedContext` -> one model-facing user message.

Pure formatting over an already-cut, already-ordered block, producing the single
`{"role": "user", "content": ...}` message the assembler prepends before history.
Deterministic (same block, same string), so a D45 resume renders byte-identically.

`role: "user"`, not `system`, so the base prompt stays the SOLE `role: "system"` message: a
second system message would compete with the base instructions on the OpenAI-compatible
endpoints that honor only the first-or-last one. A leading prefix marks the block as
retrieved prior-context so the model never mistakes it for the current question.

Trust boundary (H2): even though the corpus is written offline and passes the write-time
leakage gate — so its CONTENT is trusted — the retrieved TEXT must not be able to forge the
message's STRUCTURE, since a newline in an intent, chunk or slot string could otherwise
fabricate a section header or a fake instruction. Every interpolated field is therefore
structurally sanitised (control characters collapsed or dropped) and length-capped, and the
sanitisation is deterministic. When less-trusted learned content lands, this trust posture
must be reconfirmed.

Redaction is by construction: the input carries no question, no `column_scope` and no JWT —
only blueprint ids/intents/slot summaries, knowledge chunks and user-memory items.
"""

from __future__ import annotations

from typing import Any

from data_agent.runtime.sanitize import sanitize_text

from .models import RetrievedContext, ThinCard

# Marks the block as retrieved prior-context under the `user` role, so the model
# reads it as candidate reference material rather than the current question.
_USER_CONTEXT_PREFIX = "[Retrieved context — candidate blueprints/knowledge for this question]\n\n"

_HEADER = (
    "Relevant context retrieved for this request "
    "(pre-injected; you may ignore anything not helpful):"
)

# Per-field length caps (H2). Card fields (intent / slots_summary / title /
# user-memory) are short by design; knowledge chunks are longer prose. Chosen as
# structural safety bounds, NOT a token-budget mechanism (M4 full D46 budget
# accounting is deferred to Slice 2 — these caps only partially mitigate it).
_MAX_FIELD_CHARS = 500
_MAX_CHUNK_CHARS = 2000

# The sanitiser itself now lives in `runtime/sanitize.py` (Release 1, 03 §D): the
# `analysisState` context block interpolates MODEL-authored intent descriptions
# into a model-facing message and needs the identical structural guarantee, so
# there is ONE implementation rather than two that can drift. This module-local
# alias keeps every call site below unchanged.
_sanitize = sanitize_text


# Indent for the enrichment sub-lines rendered under a card's bullet.
_CARD_DETAIL_INDENT = "  "


def _card_detail_lines(card: ThinCard) -> list[str]:
    """Render one card's enrichment fields as indented sub-lines.

        Every interpolated piece goes through `_sanitize` (H2) for the same reason the intent
        does: these strings come from the same corpus, so a newline in a slot name, a resolved
        column or a grain entry could forge a bullet or a fake section header inside the
        pre-injected block.

        Per-field capping does not bound a CARD, so the slot COLLECTION is capped upstream and
        the overflow is rendered here as `(+K more)` — the model must never be told a 12-slot
        blueprint has 6.
    """
    lines: list[str] = []
    if card.slots:
        rendered: list[str] = []
        for slot in card.slots:
            name = _sanitize(slot.name, _MAX_FIELD_CHARS)
            slot_type = _sanitize(slot.type, _MAX_FIELD_CHARS)
            requirement = "required" if slot.required else "optional"
            qualifier = f"{slot_type}, {requirement}" if slot_type else requirement
            rendered.append(f"{name} ({qualifier})")
        more = f" (+{card.slots_omitted} more)" if card.slots_omitted > 0 else ""
        lines.append(f"{_CARD_DETAIL_INDENT}slots: {', '.join(rendered)}{more}")
    if card.resolves:
        # Sorted so the block is byte-identical across a re-derived resume
        # regardless of the stored map's iteration order (design §6).
        pairs = "; ".join(
            f"{_sanitize(term, _MAX_FIELD_CHARS)} -> {_sanitize(column, _MAX_FIELD_CHARS)}"
            for term, column in sorted(card.resolves.items())
        )
        lines.append(f"{_CARD_DETAIL_INDENT}resolves: {pairs}")
    if card.result_grain:
        grain = ", ".join(_sanitize(column, _MAX_FIELD_CHARS) for column in card.result_grain)
        lines.append(f"{_CARD_DETAIL_INDENT}result grain: {grain}")
    return lines


def render_retrieved_context(context: RetrievedContext) -> dict[str, Any] | None:
    """Render *context* to one `user`-role message, or `None` when it is empty.

        `None` means "prepend nothing": the assembler then behaves exactly as if retrieval had
        not run, keeping the empty-retrieval path byte-identical to the unconfigured path.
    """
    if context.is_empty():
        return None

    lines: list[str] = [_HEADER]

    if context.thin_cards:
        lines.append("")
        lines.append("Candidate blueprints (analysis templates you can run):")
        for card in context.thin_cards:
            intent = _sanitize(card.intent, _MAX_FIELD_CHARS)
            slots_text = _sanitize(card.slots_summary, _MAX_FIELD_CHARS)
            card_id = _sanitize(card.id, _MAX_FIELD_CHARS)
            slots = f" [slots: {slots_text}]" if slots_text else ""
            lines.append(f"- {card_id}: {intent}{slots}")
            lines.extend(_card_detail_lines(card))

    if context.knowledge_hits:
        lines.append("")
        lines.append("Relevant knowledge:")
        for hit in context.knowledge_hits:
            title_text = _sanitize(hit.title, _MAX_FIELD_CHARS) if hit.title else ""
            prefix = f"{title_text}: " if title_text else ""
            lines.append(f"- {prefix}{_sanitize(hit.text, _MAX_CHUNK_CHARS)}")

    if context.user_memory:
        lines.append("")
        lines.append("What we remember about you:")
        for item in context.user_memory:
            kind = _sanitize(item.kind, _MAX_FIELD_CHARS)
            lines.append(f"- ({kind}) {_sanitize(item.text, _MAX_FIELD_CHARS)}")

    return {"role": "user", "content": _USER_CONTEXT_PREFIX + "\n".join(lines)}


__all__ = ["render_retrieved_context"]
