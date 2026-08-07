"""render.py — RetrievedContext → one model-facing user message (design §3.1).

Pure formatting: given the already-cut, already-ordered `RetrievedContext`,
produce a single `{"role": "user", "content": ...}` message the assembler
prepends before history. Deterministic (same block → same string), so a D45
resume that re-derives the same block renders byte-identically (design §6).

Non-system role: the block is rendered under `role: "user"` (not `system`) so
the base prompt (`context/assembly.py`) stays the SOLE `role: "system"` message.
A second system message here would compete with the base instructions on the
OpenAI-compatible endpoints that honor only the first-or-last system message —
the same anti-pattern the compaction-summary demotion closed. A leading prefix
marks the block as retrieved prior-context so the model never mistakes it for
the current user question.

Trust boundary (H2 — structural sanitisation, as-built): this block is
interpolated into a model-facing message alongside the real conversation. Even
though the corpus is written offline and passes the write-time leakage gate (so
its CONTENT is trusted), the retrieved TEXT must not be able to forge the
message's STRUCTURE — a newline in an intent/chunk/slot string could otherwise
fabricate a new "## System" section or a fake instruction/bullet. So every
interpolated text field is structurally sanitised before it is placed in the
message: newlines/tabs and
other C0/C1 control characters (incl. NUL) are collapsed to single spaces (or
dropped), and each field is length-capped (`_MAX_FIELD_CHARS` for card fields,
`_MAX_CHUNK_CHARS` for knowledge chunks) so one 100 KB card cannot dominate the
prompt. Sanitisation is deterministic (resume still renders byte-identically).
Content itself remains trusted per the leakage gate; when Track-B (less-trusted,
learned) content lands in Slice 2 this trust posture must be reconfirmed. See
the design doc's trust-boundary as-built note.

Redaction is by construction: the input carries no question, no `column_scope`,
and no JWT — only blueprint ids/intents/slot summaries, knowledge chunks, and
user-memory items. The renderer emits exactly those (sanitised), nothing more.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from .models import RetrievedContext

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

# Whitespace controls collapsed to a single space (so words never merge).
_WS_CONTROLS = frozenset("\t\n\r\v\f")


def _sanitize(text: str, max_chars: int) -> str:
    """Structurally sanitise one interpolated field (H2): map whitespace
    controls to spaces, drop other C0/C1 control chars (incl. NUL), collapse
    runs of whitespace, strip, and cap length. Deterministic."""
    out: list[str] = []
    for ch in text:
        if ch in _WS_CONTROLS:
            out.append(" ")
        elif unicodedata.category(ch) == "Cc":  # other control chars incl. \x00
            continue
        else:
            out.append(ch)
    # Collapse whitespace runs + strip (all remaining whitespace is now spaces).
    collapsed = " ".join("".join(out).split())
    return collapsed[:max_chars]


def render_retrieved_context(context: RetrievedContext) -> dict[str, Any] | None:
    """Render *context* to one `user`-role message, or `None` when it is empty.

    `None` means "prepend nothing" — the assembler then behaves exactly as if
    retrieval had not run, keeping the empty-retrieval path byte-identical to
    the unconfigured path.
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
