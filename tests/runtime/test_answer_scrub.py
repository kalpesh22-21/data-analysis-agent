"""The answer-prose scrub (ISSUES I1), at the unit.

TWO HALVES, and the SILENT one is the dangerous half. That an identifier gets
redacted is loud — it shows up as a marker in an answer. That ordinary business
prose survives BYTE-IDENTICALLY is silent, and a regression there does not fail
visibly: it quietly replaces the numbers and department names a correct answer is
made of with "[schema detail withheld]", which is a worse outcome than the
disclosure the rule exists to prevent. So the clean table below is the longer of
the two, and every entry in it is a sentence a correct answer would really write.

The specific trap it guards is the DOTTED rule. The obvious pattern for
`database.table` also matches "…people. Engineering…" if it tolerates whitespace
around the dot — which the progress summarizer's `_STRUCTURAL_DOTTED`
deliberately does (it is a reject-the-whole-line detector, where a false positive
costs one static fallback line) and which a scrubber must not (a false positive
there eats the sentence boundary out of the user's answer).
"""

from __future__ import annotations

import pytest

from data_agent.runtime.answer_scrub import (
    SAVED_ANALYSIS_MARKER,
    SCHEMA_DETAIL_MARKER,
    scrub_answer_prose,
)

# This turn read one catalogued table. `department` is a column name AND an
# ordinary English word, which is the whole reason detection is by form.
PROVENANCE = frozenset(
    {
        ("dbpcm_warehouse.employee", "department"),
        ("dbpcm_warehouse.employee", "EmployeeCode"),
    }
)


# --- the silent half: business prose survives byte-identically --------------


@pytest.mark.parametrize(
    "prose",
    [
        # THE SENTENCE-BOUNDARY CANARY. A dotted rule that allows whitespace around
        # the dot eats this one, and the answer loses its sentence break.
        "Sales has 3 people. Engineering has 2.",
        "3.2 percent",
        "e.g. in Q1",
        "well-known cost-per-hire",
        # A bare column name IS the business word for the thing. The catalog has 15
        # like it; a membership test would redact every one.
        "The Department column is free text.",
        "Headcount is 412.",
        # A quoted VALUE, not a quoted identifier — the distinction the
        # double-quoted arm is provenance-gated for.
        'Employees in the "Sales" department: 12.',
        "U.S. employees rose 4.5% in Q1.2026 vs. last year (i.e. year-over-year).",
        "No employees match that filter.",
        "Overtime is paid at 1.5x. Regular hours are capped at 40.",
        # DOTTED VALUES. Both wear the qualified-name shape and both are things
        # the answer is FOR, so both have an explicit carve-out.
        "Email jane.smith@acme.com for access.",
        "Attach sales_data.csv and report_2026.csv to the ticket.",
        # A CODE IS NOT A CORPUS ID. `KN-95` and `BP-1042` are plausible cell
        # values; rewriting them to "[saved analysis]" would not be a redaction
        # but a false claim that a saved analysis was used.
        "KN-95 masks: 412 units on hand.",
        "Part BP-1042 shipped on the 3rd.",
    ],
)
def test_business_prose_is_returned_byte_identical(prose: str) -> None:
    scrubbed, count = scrub_answer_prose(prose, provenance=PROVENANCE)
    assert scrubbed == prose
    assert count == 0


# --- the loud half: identifier shapes are redacted --------------------------


@pytest.mark.parametrize(
    ("prose", "expected", "expected_count"),
    [
        # R2, qualified: ONE redaction, not two — the qualified arm wins at that
        # position, so the snake_case half is not counted a second time.
        (
            "I read dbpcm_warehouse.employee to get this.",
            f"I read {SCHEMA_DETAIL_MARKER} to get this.",
            1,
        ),
        # R1, corpus id: a DIFFERENT marker, because "a saved analysis was used" is
        # a true and useful thing to say while its id is not.
        (
            "I ran bp-active-headcount-by-department for you.",
            f"I ran {SAVED_ANALYSIS_MARKER} for you.",
            1,
        ),
        # R3, snake_case, twice in one sentence.
        (
            "Joined employee_master on check_detail.",
            f"Joined {SCHEMA_DETAIL_MARKER} on {SCHEMA_DETAIL_MARKER}.",
            2,
        ),
        # R4, the backtick arm: a bare word that only its QUOTING marks as an
        # identifier. Unconditional — a backtick is ClickHouse identifier quoting
        # and has no business in prose spoken to a user.
        (
            "The `employee` table has 412 rows.",
            f"The {SCHEMA_DETAIL_MARKER} table has 412 rows.",
            1,
        ),
        # R4, the double-quoted arm: the SAME word as the byte-identical canary
        # above ("The Department column is free text.") — quoted, and confirmed by
        # this turn's provenance to be a column.
        (
            'The "department" column is free text.',
            f"The {SCHEMA_DETAIL_MARKER} column is free text.",
            1,
        ),
        # Two corpus ids, one of them upper-cased: the model is quoting an id off a
        # card, and "BP-" discloses exactly as much as "bp-".
        (
            "See kn-pay-period-definition and BP-hires-per-month.",
            f"See {SAVED_ANALYSIS_MARKER} and {SAVED_ANALYSIS_MARKER}.",
            2,
        ),
        # A model that pasted its SQL into the prose still loses the identifier.
        (
            "I ran SELECT count() FROM employee_master.",
            f"I ran SELECT count() FROM {SCHEMA_DETAIL_MARKER}.",
            1,
        ),
        # THREE PARTS, ONE MARKER. A two-segment dotted rule redacts only
        # `dbpcm_warehouse.employee` and leaves `.EmployeeCode` dangling behind
        # the marker — the column survives with its dot attached, which is the
        # most natural way for a model to write a fully-qualified name.
        (
            "I read dbpcm_warehouse.employee.EmployeeCode for this.",
            f"I read {SCHEMA_DETAIL_MARKER} for this.",
            1,
        ),
        # The same repetition group collapses a three-part name whose parts are
        # THEMSELVES snake_case into ONE redaction and ONE count — not a marker
        # plus two more from R3 picking over the remains.
        (
            "Joined warehouse.employee_master.pay_check here.",
            f"Joined {SCHEMA_DETAIL_MARKER} here.",
            1,
        ),
        # KNOWN EATEN, asserted so the behaviour is a decision and not a
        # surprise: a hostname is a dotted identifier by shape, and neither the
        # `@` nor the file-extension carve-out reaches it.
        (
            "The dashboard lives at intranet.company.com today.",
            f"The dashboard lives at {SCHEMA_DETAIL_MARKER} today.",
            1,
        ),
    ],
)
def test_identifier_shapes_are_redacted(prose: str, expected: str, expected_count: int) -> None:
    scrubbed, count = scrub_answer_prose(prose, provenance=PROVENANCE)
    assert scrubbed == expected
    assert count == expected_count


# --- the dotted-VALUE carve-outs --------------------------------------------


def test_a_data_file_name_survives_the_underscored_rule_too() -> None:
    """THE INTERACTION, not just the dotted rule. `sales_data.csv` is at risk
    from TWO arms and the file-extension carve-out has to beat both: R2 sees a
    qualified name, and R3 sees `sales_data` one character EARLIER than R2 sees
    the dot — so a carve-out expressed as a lookaround on R2 alone would still
    hand the user "[schema detail withheld].csv". The carve-out is therefore its
    own arm, and it consumes the whole filename."""
    scrubbed, count = scrub_answer_prose(
        "I loaded sales_data.csv; report_2026.csv is next.", provenance=PROVENANCE
    )
    assert scrubbed == "I loaded sales_data.csv; report_2026.csv is next."
    assert count == 0


def test_the_extension_carve_out_is_a_filename_not_a_suffix() -> None:
    """The carve-out is word-bounded, so it cannot be used as a suffix to smuggle
    an identifier past the scrub: only a name that ENDS at a known data-file
    extension is spared."""
    scrubbed, count = scrub_answer_prose("Read data.csvfile and employee_master.json_col.")
    assert scrubbed == f"Read {SCHEMA_DETAIL_MARKER} and {SCHEMA_DETAIL_MARKER}."
    assert count == 2


def test_both_halves_of_an_email_address_survive() -> None:
    """An address is a cell value the answer exists to deliver, and BOTH halves
    are dotted-name shaped: the local part is a match followed by `@`, the domain
    part a match preceded by one. So the carve-out is a lookbehind AND a
    lookahead, not one of them."""
    scrubbed, count = scrub_answer_prose(
        "Contact jane.smith@acme.com or ops.team@corp.example.", provenance=PROVENANCE
    )
    assert scrubbed == "Contact jane.smith@acme.com or ops.team@corp.example."
    assert count == 0


def test_a_corpus_id_needs_a_letter_but_a_one_letter_id_is_enough() -> None:
    """The R1 letter requirement is about the difference between an ID and a
    CODE, not about length: `bp-a` is a (short) corpus id and still goes, while
    `KN-95` is a mask grade and must not be relabelled "[saved analysis]" — a
    marker that claims something FALSE about the turn is worse than the token it
    replaced."""
    scrubbed, count = scrub_answer_prose("bp-a covers KN-95 and BP-1042.")
    assert scrubbed == f"{SAVED_ANALYSIS_MARKER} covers KN-95 and BP-1042."
    assert count == 1


# --- the provenance parameter ----------------------------------------------


def test_the_double_quoted_arm_is_the_only_one_that_needs_provenance() -> None:
    """`provenance=None` is what the PAUSE exits pass, so what it costs must be
    known and small: the double-quoted arm goes quiet (a quoted bare word is
    indistinguishable from a quoted value without it) and nothing else changes."""
    prose = 'Read dbpcm_warehouse.employee, `headcount`, "department", and ran bp-a.'

    with_provenance, with_count = scrub_answer_prose(prose, provenance=PROVENANCE)
    without_provenance, without_count = scrub_answer_prose(prose, provenance=None)

    assert with_provenance == (
        f"Read {SCHEMA_DETAIL_MARKER}, {SCHEMA_DETAIL_MARKER}, "
        f"{SCHEMA_DETAIL_MARKER}, and ran {SAVED_ANALYSIS_MARKER}."
    )
    assert with_count == 4
    # The ONE difference: the quoted bare word is left alone, and the other three
    # rules are untouched by the missing provenance.
    assert without_provenance == (
        f'Read {SCHEMA_DETAIL_MARKER}, {SCHEMA_DETAIL_MARKER}, "department", '
        f"and ran {SAVED_ANALYSIS_MARKER}."
    )
    assert without_count == 3


def test_a_quoted_identifier_that_is_not_bare_is_caught_by_the_other_rules() -> None:
    """The quoted arm is deliberately narrow — BARE tokens only — and this is why
    it can afford to be: a dotted or snake_case token inside quotes is caught one
    character later by the rules that need no quoting at all. The quotes are left
    behind as decoration; the identifier is gone either way."""
    scrubbed, count = scrub_answer_prose('Read `employee_master` and "db.table".', provenance=None)
    assert scrubbed == f"Read `{SCHEMA_DETAIL_MARKER}` and \"{SCHEMA_DETAIL_MARKER}\"."
    assert count == 2


def test_a_quoted_value_survives_even_when_provenance_is_rich() -> None:
    """The failure mode the provenance gate exists to prevent: a cell value the
    answer is FOR, quoted for emphasis, eaten as if it were a column."""
    scrubbed, count = scrub_answer_prose(
        'The largest is "Engineering" with 128 people.', provenance=PROVENANCE
    )
    assert scrubbed == 'The largest is "Engineering" with 128 people.'
    assert count == 0


# --- totality ---------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, ""])
def test_absent_prose_passes_through(empty: str | None) -> None:
    """An absent answer has nothing to disclose — and `None` must stay `None`, or
    the no-tool-calls exit's append guard (`persist_text or None`) changes meaning."""
    assert scrub_answer_prose(empty) == (empty, 0)


def test_a_scrubbed_answer_is_never_scrubbed_into_nothing() -> None:
    """Every replacement is a non-empty marker, so a prose-only-identifiers answer
    still leaves a message behind rather than silently becoming an empty bubble."""
    scrubbed, count = scrub_answer_prose("employee_master")
    assert scrubbed == SCHEMA_DETAIL_MARKER
    assert count == 1


def test_the_markers_are_themselves_unscrubbable() -> None:
    """MARKER-IN-MARKER. The output is fed back in: a marker that were itself
    identifier-shaped would compound on every pass (and this function is one
    refactor away from being applied twice — the loop calls it at five exits)."""
    once, first = scrub_answer_prose(
        "I read dbpcm_warehouse.employee via bp-x.", provenance=PROVENANCE
    )
    twice, second = scrub_answer_prose(once, provenance=PROVENANCE)
    assert twice == once
    assert (first, second) == (2, 0)


def test_it_is_deterministic_and_linear_on_a_large_answer() -> None:
    """No cap is applied here (truncating a user's answer would be the worse
    failure), so the scan must stay well-behaved on the largest prose that can
    reach it — `clean_answer_text` caps the `answerWithTable` arm at 20_000 chars
    and the no-tool-calls arm is uncapped."""
    prose = ("Sales has 3 people. Engineering has 2. " * 2_000) + "employee_master"
    first, first_count = scrub_answer_prose(prose, provenance=PROVENANCE)
    second, second_count = scrub_answer_prose(prose, provenance=PROVENANCE)

    assert first == second
    assert (first_count, second_count) == (1, 1)
    assert first.endswith(SCHEMA_DETAIL_MARKER)
