"""Structural sanitisation of model-facing interpolated text (H2).

One implementation, shared by every producer of a model-facing message that interpolates
text the runtime did not author (`retrieval/render.py`, `context/assembly.py`). The rule:
that text may not forge the STRUCTURE of the message it lands in — a newline could
otherwise fabricate a section header, a fake instruction, or a fake bullet. Whitespace
controls become spaces, other C0/C1 controls are dropped, runs collapse, fields are
capped. Deterministic, so a D45 resume rebuilds the same request byte-for-byte.

Must stay a LEAF with no `data_agent` imports: `context/__init__` eagerly imports
`context.assembly`, which imports `retrieval.render`, so importing anything under
`runtime.context` from here is a circular import at module load.
"""

from __future__ import annotations

import unicodedata

# The shared per-field cap for short fields (card intents, slot summaries,
# user-memory items, intent descriptions). Longer prose (knowledge chunks) passes
# its own larger cap.
MAX_FIELD_CHARS = 500

# Whitespace controls collapsed to a single space (so words never merge).
_WS_CONTROLS = frozenset("\t\n\r\v\f")


def sanitize_text(text: str, max_chars: int) -> str:
    """Structurally sanitise one interpolated field: whitespace controls to spaces, other
        C0/C1 controls (incl. NUL) dropped, runs of whitespace collapsed, stripped, capped.
    """
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


__all__ = ["MAX_FIELD_CHARS", "sanitize_text"]
