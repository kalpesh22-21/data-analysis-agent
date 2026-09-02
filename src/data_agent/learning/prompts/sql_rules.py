"""Rules every prompt that lets a model WRITE SQL owes it.

⚠ ONE SENTENCE PER DETERMINISTIC CHECK, stated wherever a model can trip it. The pairing is the
point: a rule stated in a prompt but not checked is advice, and a rule checked but never stated is
a validator complaint about something nobody was told. `DATE_RULE` is the second kind's cure —
`check_no_frozen_date_literal` has refused frozen run dates for a while, and the minting prompts
had no date guidance at all, so a drafted blueprint could freeze the day it was minted, get
stamped `fail_to_review/frozen_date_literal`, and hand the expert a complaint about a rule they
were never shown.

Three callers today: the §C.5 rewrite prompt (`revise/prompt.py`) and minting's DRAFT and
COMPOSITE prompts (`mint/prompt.py`). Deliberately NOT minting's CLASSIFY prompt — in `exact`
mode the expert's query IS the accepted SQL and the model has no field to write any, so telling
it how to write dates would be advice about an action it cannot take, on the one prompt whose
entire job is "you cannot change this query".
"""

from __future__ import annotations

DATE_RULE = """\
NEVER WRITE THE RUN DATE INTO THE QUERY. A blueprint outlives the day it was written, so a \
literal like `toDateTime64('2026-08-28 00:00:00', 6)` sitting in its body makes it answer a \
DIFFERENT question every day it ages — silently. A checker rejects that. Express "now" as \
`today()` / `now()` and measure with `dateDiff`. Two consequences:

  * a TRAILING WINDOW ("the last 6 months") is written against `today()`/`now()`, and the number
    of periods is the only part that varies — the unit stays in the SQL;
  * an EXPLICIT START/END WINDOW IS TWO LITERALS, NOT ONE. Write both bounds as separate
    predicates, each with its own entry. A single "date range" hole is not a thing that can be
    bound.

A SENTINEL FLOOR is fine (`hire_date > '1900-01-01'`): it is a fixed boundary of the data, not \
the day you ran."""

__all__ = ["DATE_RULE"]
