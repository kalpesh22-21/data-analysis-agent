"""The wall-clock stamp both planes write onto persisted documents.

One line of code, seven copies, and the reason to collapse them is the FORMAT rather
than the duplication: `datetime.now(UTC).isoformat()` is what every `ts`,
`created_at`, `last_activity`, `committed_at` and `judged_at` in the session store,
the candidate store, the user store, and the judge is written as — and those strings
are READ BACK and compared. `context/assembly.py` merges the message and tool-trail
streams by `(turn_index, ts, stream_rank)`; `promotion/scheduler.py::_parse_clock`
runs `datetime.fromisoformat` over the value; the candidate store orders scans by it.
A copy that drifted to `utcnow()` (naive, no offset) or to a `Z` suffix would compare
unequal to its siblings and sort wrong against them, silently.

TZ-AWARE UTC is the load-bearing part: `isoformat()` on an aware datetime emits the
`+00:00` offset, and `fromisoformat` reads it back aware. A naive stamp round-trips to
a naive datetime and raises on comparison with an aware one.

NOT a clock seam. Several callers take an injectable `clock: Callable[[], str]` and
use this as the DEFAULT; tests pass their own. This function is the production clock,
not the injection point — do not add freezing/mocking hooks here.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """The current UTC instant as a tz-aware ISO-8601 string (`...+00:00`).

    Microsecond resolution, and NOT guaranteed monotonic — an NTP step can move it
    backwards. Callers that need a total order must not rely on it alone (see
    `context/assembly.py`, which sorts by `turn_index` first, and
    `composite/analysis_state.py`, which uses append order)."""
    return datetime.now(UTC).isoformat()
