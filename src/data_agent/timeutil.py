"""The wall-clock stamp both planes write onto persisted documents.

TZ-AWARE UTC is the load-bearing part: these strings are read back, compared and sorted
(`context/assembly.py`, `promotion/scheduler.py::_parse_clock`, candidate-store scans), and
a naive stamp or a `Z` suffix compares unequal to its siblings. NOT a clock seam — callers
needing one take an injectable `clock` and default to this; do not add freezing hooks here.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """The current UTC instant as a tz-aware ISO-8601 string (`...+00:00`).

    Microsecond resolution and NOT monotonic — an NTP step can move it backwards, so a caller
    needing a total order must not rely on it alone.
    """
    return datetime.now(UTC).isoformat()
