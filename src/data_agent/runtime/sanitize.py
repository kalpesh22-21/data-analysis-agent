"""sanitize.py — structural sanitisation of model-facing interpolated text (H2).

ONE implementation, shared by every producer of a model-facing message that
interpolates text the runtime did not author:

  - `retrieval/render.py` — retrieved blueprint/knowledge/user-memory cards.
  - `context/assembly.py` — the `analysisState` block's intent descriptions
    (03 §D), which are MODEL-authored text derived from the user's question and
    therefore re-enter model context on every round-trip.

The rule: text may not be able to forge the STRUCTURE of the message it lands
in. A newline in a description could otherwise fabricate a new "## System"
section, a fake instruction, or a fake bullet in the intent list. So whitespace
controls collapse to single spaces, other C0/C1 control characters (incl. NUL)
are dropped, runs of whitespace collapse, and each field is length-capped.
Deterministic — the same input always renders byte-identically, so a D45 resume
rebuilds the same request.

WHY IT LIVES AT THE RUNTIME ROOT rather than at `runtime/context/sanitize.py`
(where 03 §D puts it): `context/__init__.py` eagerly imports `context.assembly`,
which imports `retrieval.render`. A `retrieval/render.py` that imported anything
under `runtime.context` would therefore run `context/__init__` -> `assembly` ->
`from ...retrieval.render import render_retrieved_context` against a
half-initialised `render` module and fail at import time. This module is a leaf
with no `data_agent` imports at all, so both callers can depend on it in one
direction — the same reason `loop/read_guard.py` was extracted.
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
    """Structurally sanitise one interpolated field: map whitespace controls to
    spaces, drop other C0/C1 control chars (incl. NUL), collapse runs of
    whitespace, strip, and cap length. Deterministic."""
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
