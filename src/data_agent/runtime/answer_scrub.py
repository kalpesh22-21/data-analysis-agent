"""answer_scrub.py — the last-mile scrub of USER-FACING ANSWER PROSE (ISSUES I1).

The prompt already tells the model to answer metadata questions in business terms
and never to name a database, a table, a column, or a saved analysis (1885a8f).
A prompt is a request. This is the enforcement: whatever the model wrote, the
prose that reaches the user carries no identifier-shaped token and no corpus id.

WHAT IS *NOT* SCRUBBED, deliberately — the STRUCTURED payload. `sql_executed`,
`answer_sql`, `answer_tables[].sql`, `blueprint_use`, `verification` and
`provenance` stay FULLY transparent (the same-day I2 decision): those fields are
the audit surface a reviewer needs to check the answer, they are rendered as
labelled machine detail rather than as the agent's voice, and redacting them
would destroy the D56 transparency contract. Only the PROSE — the sentences the
agent speaks — is scrubbed. So an answer may say "[schema detail withheld]" while
the SQL panel beside it shows the very identifier that was withheld, and that is
the intended shape: the agent describes what it did in business terms, and the
machinery it used is inspectable on demand rather than narrated.

DETECTION IS BY IDENTIFIER *FORM*, NEVER BY WORD MEMBERSHIP. The obvious
implementation — "redact any token that is a column name in this turn's
provenance" — destroys the answers this rule exists to produce: the catalog
carries bare-word columns (`department`, `amount`, `status`, `name`, ...), and a
correct business answer MUST be able to say "department" in a sentence. So the
rules below match SHAPES that ordinary English does not produce (a corpus id, a
dotted qualification, a snake_case token, a quoted identifier) and never a word
list.

NEVER REFUSES, NEVER NUDGES, ALWAYS COMPLETES. Unlike the answer-shape gate (05
§J) this does not hand the round back: it redacts in place and the turn finishes.
A gate that can be exhausted has an honour-system hole at the end of its
allowance; a scrub does not.

WHY IT LIVES AT THE RUNTIME ROOT, and why it imports nothing from `data_agent`:
the same reason as `sanitize.py` and `loop/read_guard.py` — a leaf with no
package imports can be depended on from anywhere (the loop today, a history
projection or a transport tomorrow) without an import cycle, and can be unit
tested without constructing a loop.

DETERMINISM AND SAFETY. One left-to-right pass of one compiled alternation, so
the same input always produces the same output (a D45 resume re-renders the same
answer) and overlapping candidates cannot double-count: `warehouse.employee_id`
is ONE redaction (the qualified arm wins at that position), not two, and so is
the three-part `warehouse.employee_master.pay_check`. The replacement markers are
themselves unmatchable by every rule — no dot, no underscore, no `bp-`/`kn-`
prefix, no quotes — so a marker can never be scrubbed into another marker even if
this function is applied twice. The scan is linear in the length of the text (no
nested quantifiers, no backtracking blowup); no cap is imposed here because
truncating a user's answer would be a worse failure than scanning it. For
reference on the caps that DO apply upstream:
`composite/answer_with_table.py::clean_answer_text` truncates the
`answerWithTable` prose at 20_000 characters, while the no-tool-calls exit's
`assistant_text` is uncapped and arrives as the model produced it.

DOTTED *VALUES* ARE THE HARD CASE, and the carve-outs are deliberately NARROW.
A dot between two identifier-shaped words is usually a schema qualification, but
two kinds of ordinary CELL VALUE wear the same shape, and both are things an
answer exists to deliver rather than things it must withhold:

  - an EMAIL ADDRESS (`jane.smith@acme.com`) — recognised by the adjacent `@`,
    which is why R2 carries a lookbehind and a lookahead for it. Both halves are
    spared: the local part is the match followed by `@`, the domain part the
    match preceded by it.
  - a DATA-FILE NAME (`sales_data.csv`, `report_2026.csv`) — recognised by a
    final segment that is a common data-file extension. This is a whole ARM of
    the alternation rather than a lookaround on R2, because the underscored rule
    R3 would otherwise eat `sales_data` off the front of the filename one
    character before R2 ever looked at the dot. The arm consumes the entire
    filename and returns it verbatim, so it is the ONLY place the extension
    carve-out has to be expressed.

KNOWN EATEN — false positives accepted with open eyes, not oversights:

  | input                  | output                        |
  |------------------------|-------------------------------|
  | `intranet.company.com` | `[schema detail withheld]`    |
  | `bp-active_headcount`  | `bp-[schema detail withheld]` |

A URL or hostname is a dotted identifier by SHAPE and neither carve-out reaches
it — it has no `@` and `.com` is not a data-file extension. Accepted rather than
carved out: URLs are rare in answer prose, and an internal hostname is closer to
infrastructure detail than to the answer a user asked for, so losing it costs
little.

The second is the BP-UNDERSCORE HYBRID. R1's segments are alphanumeric and its
trailing word boundary cannot sit in front of an underscore, so R1 declines the
whole token, R3 takes the tail, and the result carries the SCHEMA marker plus a
surviving `bp-` prefix instead of "[saved analysis]". The id itself is still
gone; only the marker's claim and the stub prefix are wrong.

OUT OF SCOPE — UNICODE LOOKALIKES. Every rule is ASCII, so a full-width `．` or a
Cyrillic `а` inside an identifier passes straight through; the model is not the
adversary here, the identifiers it is quoting are ASCII, and an evasion would
have to be authored deliberately.
"""

from __future__ import annotations

import re

# A corpus id is INTERNAL BOOKKEEPING, not an answer. "I ran
# bp-active-headcount-by-department" tells the user nothing they can act on and
# leaks the shape of the corpus; the marker keeps the FACT that a saved analysis
# was used (which is honest, and which `blueprint_use` reports precisely in the
# structured payload) without the id.
SAVED_ANALYSIS_MARKER = "[saved analysis]"

# The one event this rule produces, emitted by its callers and ONLY when
# something was actually redacted (a clean answer is silent, so the event rate is
# the disclosure rate). Its payload is a COUNT and an exit label — never a
# matched token, never the prose: a matched token may be a column name, which is
# deliberately not on the D25 attribute allowlist.
ANSWER_PROSE_REDACTED_EVENT = "loop_answer_prose_redacted"

# Everything else — a qualified name, a snake_case token, a quoted identifier —
# is physical schema. The marker is deliberately visible rather than a silent
# deletion: a user reading "the [schema detail withheld] table" can tell that the
# agent withheld something and ask, where a silent drop would read as a typo.
SCHEMA_DETAIL_MARKER = "[schema detail withheld]"

# R1 — CORPUS ID. `bp-...` / `kn-...` with hyphen-joined alphanumeric segments,
# which is the shape every id in the corpus actually has
# (`bp-active-headcount-by-department`, `kn-pay-period-definition`). The prefix is
# matched case-insensitively because the model is quoting an id from a card it
# was shown, not echoing bytes, and "BP-hires-per-month" is the same disclosure.
#
# AT LEAST ONE LETTER MUST FOLLOW THE PREFIX (the lookahead). Without it `KN-95`
# and `BP-1042` — a mask grade and a part number, both perfectly plausible CELL
# VALUES an answer is for — are rewritten to "[saved analysis]", which is not a
# redaction but a FALSE CLAIM: it tells the user a saved analysis was used when
# none was. A corpus id is words joined by hyphens; a code is digits.
_R1_CORPUS_ID = (
    r"(?P<corpus>\b(?:[Bb][Pp]|[Kk][Nn])-(?=[A-Za-z0-9-]*[A-Za-z])"
    r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b)"
)

# R0 — DATA-FILE NAME, a CARVE-OUT ARM: it matches so that nothing else can, and
# returns its match verbatim. `sales_data.csv` is a value a user handed us, not
# schema we are hiding. It must sit AHEAD of R2 and R3 in the alternation and it
# must consume the WHOLE filename, because R3 starts one character earlier than
# R2 does on `sales_data.csv` and would take `sales_data` before the dot was ever
# considered. The extension list is closed on purpose (a data file the agent can
# plausibly be handed), word-bounded so `data.csvfile` is not a filename, and
# case-insensitive so `.CSV` is the same carve-out.
_R0_DATA_FILE = (
    r"(?P<datafile>\b[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*"
    r"\.(?i:csv|tsv|xlsx|json|parquet)\b)"
)

# R2 — QUALIFIED NAME. `database.table`, `table.column`, and — via the REPETITION
# group — the three-part `database.table.column` as ONE redaction. Matching
# exactly two segments would leave the third dangling
# (`dbpcm_warehouse.employee.EmployeeCode` -> `[schema detail withheld].EmployeeCode`),
# which leaks the column and is the most natural way for a model to write a
# fully-qualified name.
#
# EVERY SEGMENT MUST BE 2+ CHARACTERS and start with a letter or underscore,
# which is what keeps "e.g." / "i.e." / "U.S." / "Q1.2026" / "3.2 percent" out of
# it. The `@` lookaround spares email addresses (see the module docstring).
#
# NO `\s*` AROUND THE DOT, and this is the whole reason the progress
# summarizer's `_STRUCTURAL_DOTTED` is NOT reused here (that pattern allows
# whitespace around the dot, which is right for a REJECT-THE-LINE detector and
# catastrophic for a scrubber: "Sales has 3 people. Engineering has 2." would
# lose the sentence boundary to a marker). A schema qualification is written
# tight; a sentence boundary is not.
_R2_QUALIFIED = (
    r"(?P<qualified>(?<!@)\b[A-Za-z_][A-Za-z0-9_$]+"
    r"(?:\.[A-Za-z_][A-Za-z0-9_$]+)+\b(?!@))"
)

# R3 — SNAKE_CASE TOKEN. `employee_master`, `check_detail`. English prose does
# not contain underscores; SQL identifiers are full of them. Minimum four
# identifier characters overall (the lookahead), so a stray `a_b` in prose is not
# worth a marker. A leading-underscore token (`_private`) is deliberately NOT
# matched: the pattern anchors on a letter, and a word-boundary before `_` only
# exists after punctuation, so matching it would mean matching mid-word.
_R3_UNDERSCORED = (
    r"(?P<underscored>\b(?=[A-Za-z][A-Za-z0-9_$]{3})[A-Za-z][A-Za-z0-9$]*_[A-Za-z0-9_$]+\b)"
)

# R4 — QUOTED BARE IDENTIFIER, the arm R2/R3 cannot see: a single-word identifier
# with no dot and no underscore, which is indistinguishable from an English word
# by SHAPE ALONE — `employee`, `department` — and is therefore recognised only by
# its QUOTING. A dotted or underscored token inside quotes is deliberately NOT
# matched here (the inner pattern excludes both): R2/R3 catch it one character
# later and redact the identifier while leaving the quotes, which is why this arm
# can be this narrow.
#
# THE TWO QUOTE CHARACTERS ARE NOT EQUIVALENT, and conflating them is how this
# rule would destroy answers:
#
#   - A BACKTICK is ClickHouse's identifier quoting. It has no business in prose
#     spoken to a user, so a backticked bare token is redacted unconditionally.
#   - A DOUBLE QUOTE is how English quotes a VALUE — `employees in the "Sales"
#     department` — and "Sales" is a cell value the answer exists to deliver, not
#     schema. So a double-quoted token is redacted only when it is confirmed to be
#     a name from this turn's *provenance* (a column, or a table). Values are not
#     in provenance (it holds `(database.table, column)` pairs), so this arm
#     cannot eat one.
_R4_QUOTED = r"(?P<quote>[`\"])(?P<quoted>[A-Za-z][A-Za-z0-9$]*)(?P=quote)"

# ONE alternation, ONE pass. Order matters where two arms can start at the same
# character: the R0 carve-out precedes R2 and R3 so a filename is decided before
# either can bite, and R2 precedes R3 so `warehouse.employee_id` is one qualified
# redaction rather than a qualified prefix plus a snake_case tail.
_SCRUB = re.compile(
    "|".join((_R1_CORPUS_ID, _R4_QUOTED, _R0_DATA_FILE, _R2_QUALIFIED, _R3_UNDERSCORED))
)


def _provenance_names(provenance: frozenset[tuple[str, str]] | None) -> frozenset[str]:
    """The lower-cased BARE names this turn's provenance confirms: every column
    and the table half of each `database.table`. ONLY bare names are collected —
    the one rule that consults this (R4's double-quoted arm) matches a token with
    no dot in it, so a qualified `database.table` entry could never be looked
    up."""
    if not provenance:
        return frozenset()
    names: set[str] = set()
    for qualified, column in provenance:
        names.add(column.lower())
        names.add(qualified.rsplit(".", 1)[-1].lower())
    return frozenset(names)


def scrub_answer_prose(
    text: str | None, *, provenance: frozenset[tuple[str, str]] | None = None
) -> tuple[str | None, int]:
    """Redact identifier-shaped tokens from user-facing prose.

    Returns `(scrubbed_text, redaction_count)`. `None` and `""` pass through
    untouched with a count of 0 — an absent answer has nothing to disclose.
    Deterministic, never raises, and never returns `None` for a non-`None` input
    (every replacement is a non-empty marker, so a scrubbed answer is never
    scrubbed into nothing).

    *provenance* is this turn's `(database.table, column)` USES set — the same
    frozenset that tags the persisted assistant message. It is CONSULTED BY ONE
    ARM ONLY (R4's double-quoted half, so that `the "department" column` is
    redacted while `the "Sales" department` is not) and is optional everywhere:
    with `provenance=None` the double-quoted arm simply never fires and the other
    three rules — which need no knowledge of the turn at all — do the work. The
    pause exits pass `None` for exactly that reason: a pause has no determined
    provenance to consult.
    """
    if not text:
        return text, 0

    confirmed = _provenance_names(provenance)
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        if match.group("corpus") is not None:
            count += 1
            return SAVED_ANALYSIS_MARKER
        if match.group("datafile") is not None:
            # The carve-out arm: matched only so no other arm can, returned as-is
            # and never counted.
            return match.group(0)
        if match.group("quoted") is not None:
            # The double-quoted arm is provenance-gated; the backtick arm is not.
            if match.group("quote") == '"' and match.group("quoted").lower() not in confirmed:
                return match.group(0)
            count += 1
            return SCHEMA_DETAIL_MARKER
        count += 1
        return SCHEMA_DETAIL_MARKER

    return _SCRUB.sub(_replace, text), count


__all__ = [
    "ANSWER_PROSE_REDACTED_EVENT",
    "SAVED_ANALYSIS_MARKER",
    "SCHEMA_DETAIL_MARKER",
    "scrub_answer_prose",
]
