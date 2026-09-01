"""Adversarial QA on the non-blueprint payload readers (`validation.py::_PAYLOAD_READERS`).

The builder's `test_validation.py` pins the shapes that were actually observed. These
probe the ones NOBODY observed yet, on the rule this plane keeps re-learning: a guard
written from the INTENT of a field rather than from the READ downstream performs is a
guard that matches on every input except the one that ships.

Three questions, in order:

  1. WHITESPACE AND TYPE. `""`, `"   "`, `123`, `["a"]` all mean "no statement" to
     `knowledge_seed_from_candidate` (or, worse, mean something to `UserKnowledgeRecord`
     that nobody intended). Every one of them must decline at INTAKE, where the model can
     still be re-asked, rather than at APPROVE, where only a human can.
  2. THE CLOSED SET IS CLOSED REGARDLESS OF VALUE. `global_knowledge`'s key set is a
     LEAKAGE rule, not a tidiness one, so the refusal cannot depend on what the off-contract
     key happens to hold — a number or a boolean today is a string after one prompt edit.
  3. CORRECTABILITY PARITY. A malformed knowledge payload has to be re-askable on exactly
     the same terms a malformed blueprint is, all the way through the real correction loop
     — otherwise the whole point of moving the check to intake (the model can fix it) is
     lost and it is just an earlier dead end.

Everything here goes through the PUBLIC `to_candidate` (or the extractor above it), never
the private readers: the readers are only reachable via a `require`/`optional` chain and a
try/except belt, and a test that calls them directly proves nothing about what a candidate
actually gets.

Slug: PA-non-blueprint-payload-adversarial.
"""

from __future__ import annotations

import pytest

from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import (
    REASON_MALFORMED,
    to_candidate,
)

from .helpers import (
    KEEP_VERDICT,
    blueprint_raw,
    emit_extractor,
    evidence_item,
    make_extractor,
    make_summary,
    scripted_turn,
)


def _raw(ctype: str, payload) -> dict:
    """One non-blueprint envelope carrying *payload*, with evidence present so the D31
    gate is never what declined."""
    return {
        "type": ctype,
        "confidence": 0.9,
        "evidence": [evidence_item()],
        "rationale": "worth learning",
        "proposed_action": "new",
        "entity_self_check": {"contains_entities": False, "found": []},
        "payload": payload,
    }


def _validate(ctype: str, payload):
    return to_candidate(_raw(ctype, payload), make_summary(), known_rules=frozenset())


def _decline(ctype: str, payload) -> Decline:
    out = _validate(ctype, payload)
    assert isinstance(out, Decline), f"expected a Decline, got {type(out).__name__}"
    return out


GOOD_STATEMENT = "an active employee is one with no termination date"


# --- (1) the statement: every "present but unusable" shape --------------------------


_UNUSABLE = [
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="spaces"),
    pytest.param("\t\n  \r\n", id="whitespace-only"),
    pytest.param("  ", id="non-breaking-spaces"),
    pytest.param(123, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param(["a fact"], id="list"),
    pytest.param({"text": "a fact"}, id="object"),
]


@pytest.mark.parametrize("ctype", ["global_knowledge", "user_knowledge", "schema_edit"])
@pytest.mark.parametrize("statement", _UNUSABLE)
def test_an_unusable_statement_declines_for_every_type(ctype, statement) -> None:
    """One matrix rather than three lists, because the property is the same for all
    three: `statement` is the artifact's only identifying text, and a coercion (`as_text`
    would accept `123`) merely moves the same failure past a human approve.

    `\\u00a0` is in the list deliberately: `str.strip()` DOES strip it, and a
    hand-rolled `== ""` check would not have.
    """
    payload = {"statement": statement}
    if ctype == "schema_edit":
        # The other three required fields present, so `statement` is what declines.
        payload |= {
            "edit_kind": "add_rule",
            "patch": "rules:\n  - id: x",
            "target_catalog": "payroll",
        }
    out = _decline(ctype, payload)

    assert out.reason == REASON_MALFORMED
    assert out.correctable
    assert "candidate.payload.statement" in out.detail
    # ENTITY-FREE: the arriving VALUE never appears, only its JSON type.
    assert "a fact" not in out.detail


@pytest.mark.parametrize("ctype", ["global_knowledge", "user_knowledge"])
def test_the_decline_names_the_json_type_that_arrived(ctype) -> None:
    """The single most useful fact in a corrective message, and the reason `optional`
    refuses to normalize a falsy value into "absent"."""
    assert "an array arrived" in _decline(ctype, {"statement": ["a fact"]}).detail
    assert "a number arrived" in _decline(ctype, {"statement": 123}).detail
    assert "a boolean arrived" in _decline(ctype, {"statement": True}).detail


@pytest.mark.parametrize("ctype", ["global_knowledge", "user_knowledge"])
def test_absent_and_wrong_type_are_different_sentences(ctype) -> None:
    """`ShapeError` splits them on purpose: "you omitted it" and "you sent the wrong
    shape" ask the model for different edits."""
    absent = _decline(ctype, {"scope": "employee"}).detail
    wrong = _decline(ctype, {"statement": 123}).detail

    assert "absent or null" in absent
    assert "absent or null" not in wrong


@pytest.mark.parametrize("ctype", ["global_knowledge", "user_knowledge"])
def test_an_explicit_null_statement_is_treated_as_absent(ctype) -> None:
    """`require` maps null onto absent, because every mandatory field here is one the
    caller goes on to USE."""
    assert "absent or null" in _decline(ctype, {"statement": None}).detail


# --- (1b) the statements that MUST pass --------------------------------------------


def test_a_unicode_statement_passes_and_is_forwarded_byte_for_byte() -> None:
    """A non-ASCII fact is a fact. The readers CHECK; they never rewrite — so a
    normalization here would silently alter text a human is about to attest to."""
    text = "un employé actif n'a pas de date de fin — 従業員 ✅ (​zwsp)"
    out = _validate("global_knowledge", {"statement": text})

    assert isinstance(out, ExtractedCandidate)
    assert out.payload["statement"] == text


def test_a_very_long_statement_passes_untruncated() -> None:
    """There is no length gate, and there should not be one HERE: this is the landed
    chunk's whole text, not a log line or prompt text, and truncating it would land a
    half-sentence. (The bounded-rendering rules apply to DECLINE messages, not to
    accepted payloads.)"""
    text = GOOD_STATEMENT + ". " + ("supporting detail. " * 2000)
    out = _validate("global_knowledge", {"statement": text})

    assert isinstance(out, ExtractedCandidate)
    assert out.payload["statement"] == text


def test_a_statement_with_leading_and_trailing_whitespace_passes_unstripped() -> None:
    """`.strip()` decides USABILITY; it does not edit the value. The mapper strips again
    when it builds the seed, so nothing downstream depends on this reader doing it."""
    out = _validate("global_knowledge", {"statement": f"  {GOOD_STATEMENT}  "})

    assert isinstance(out, ExtractedCandidate)
    assert out.payload["statement"] == f"  {GOOD_STATEMENT}  "


def test_a_statement_containing_control_characters_still_passes() -> None:
    """Sanitizing belongs to the DECLINE path (`_quoted`), not to accepted payloads —
    the accepted text goes to a human card and a vector index, neither of which is a
    terminal. Pinned so a future "tidy the statement here" is a deliberate decision."""
    out = _validate("global_knowledge", {"statement": f"a fact\nwith a newline\t{GOOD_STATEMENT}"})

    assert isinstance(out, ExtractedCandidate)


# --- (2) the closed key set is closed regardless of the VALUE behind the key --------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(42, id="number"),
        pytest.param(False, id="false"),
        pytest.param(True, id="true"),
        pytest.param(None, id="null"),
        pytest.param([], id="empty-list"),
        pytest.param({}, id="empty-object"),
        pytest.param("", id="empty-string"),
    ],
)
def test_an_off_contract_key_is_refused_whatever_it_holds(value) -> None:
    """The check is `set(raw) - _GLOBAL_KNOWLEDGE_KEYS`, i.e. KEY-BASED, and that is the
    property worth pinning: the objection is that the key EXISTS, because the S5 gate
    scans a fixed field list and anything else is an unscanned surface. A value-sensitive
    check (skip it if it is falsy, skip it if it is not text) would let today's `count: 0`
    through and then admit tomorrow's `count: "employees in dept 0420"` after one prompt
    edit, having never been reviewed as a decision."""
    out = _decline("global_knowledge", {"statement": GOOD_STATEMENT, "definition": value})

    assert out.reason == REASON_MALFORMED
    assert out.correctable
    assert "'definition'" in out.detail


def test_a_null_valued_off_contract_key_is_still_refused() -> None:
    """Stated on its own because `optional`/`require` both read null as "not stated" —
    the closed-set check does NOT, and the difference matters: a model that emits every
    declared property with a null placeholder (a live behaviour) would otherwise train
    the reviewer to ignore the key list."""
    assert _decline("global_knowledge", {"statement": GOOD_STATEMENT, "intent": None})


def test_the_off_contract_key_list_is_bounded_and_sorted() -> None:
    """It is prompt text before it is a log line. Ten unknown keys must not produce a
    ten-item paragraph, and the order has to be stable or two runs of the same defect
    read as two different defects."""
    payload = {"statement": GOOD_STATEMENT} | {f"k{i:02d}": "x" for i in range(10)}
    detail = _decline("global_knowledge", payload).detail

    listed = [f"k{i:02d}" for i in range(10) if f"'k{i:02d}'" in detail]
    assert listed == [f"k{i:02d}" for i in range(6)]  # first six, in sorted order


def test_an_off_contract_key_name_is_sanitized_before_it_becomes_prompt_text() -> None:
    """The key name is MODEL-AUTHORED text out of an entity-bearing session, and it is
    interpolated into a checklist of lines the checker wrote. A newline in it would forge
    a line."""
    out = _decline(
        "global_knowledge",
        {"statement": GOOD_STATEMENT, "bad\nkey": "x"},
    )

    # The rendered token carries no line break, so it cannot pose as a checker line.
    assert "'bad key'" in out.detail or "'bad\\nkey'" in out.detail
    assert "\nkey" not in out.detail


def test_a_non_string_key_degrades_to_a_decline_rather_than_crashing() -> None:
    """Unreachable from JSON, reachable from a hand-fed or replayed payload — and
    `sorted()` over mixed types plus `_quoted(<int>)` would both raise straight out of
    `to_candidate`, the one function documented never to raise (an escape here
    dead-letters the whole session). The BELT must catch it."""
    out = _decline("global_knowledge", {"statement": GOOD_STATEMENT, 7: "x"})

    assert out.reason == REASON_MALFORMED
    # NOT correctable: no field could be named, so a re-ask would be asking the model to
    # guess at the price of a full extraction prompt.
    assert not out.correctable


def test_a_payload_of_only_contract_keys_with_empty_strings_declines_on_statement() -> None:
    """All five names right, every value blank. The keys pass the closed-set check, so
    the decline has to come from the CONTENT check — otherwise "shaped correctly" would
    be enough to land an empty chunk."""
    out = _decline(
        "global_knowledge",
        {"statement": "", "knowledge_type": "", "scope": "", "related_terms": [], "structured": {}},
    )

    assert "candidate.payload.statement" in out.detail


def test_blank_optional_labels_beside_a_real_statement_pass() -> None:
    """The mirror: `knowledge_type`/`scope` are LABELS the mapper defaults away
    (`title` falls back to None on a blank scope), so blanking them is not a defect and
    declining them would reject candidates the consumer handles fine."""
    out = _validate(
        "global_knowledge",
        {"statement": GOOD_STATEMENT, "knowledge_type": "", "scope": "   "},
    )

    assert isinstance(out, ExtractedCandidate)


# --- (2b) related_terms / structured: the fields that fail QUIETLY downstream -------


@pytest.mark.parametrize(
    "terms",
    [
        pytest.param("active, headcount", id="string-not-array"),
        pytest.param({"0": "active"}, id="object-not-array"),
        pytest.param(42, id="number"),
    ],
)
def test_a_non_array_related_terms_declines(terms) -> None:
    """`_collect_text` walks a string, a dict or a list without complaint — it just gets
    the wrong answer silently (`"active, headcount"` lands as ONE term). The type is
    checked against the DECLARED contract precisely because the consumer will not."""
    out = _decline("global_knowledge", {"statement": GOOD_STATEMENT, "related_terms": terms})

    assert "candidate.payload.related_terms" in out.detail


@pytest.mark.parametrize(
    "bad_term",
    [
        pytest.param(123, id="number"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="null"),
        pytest.param(["nested"], id="nested-list"),
        pytest.param({"term": "active"}, id="object"),
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace"),
    ],
)
def test_a_related_terms_array_containing_a_non_string_declines_naming_the_index(
    bad_term,
) -> None:
    """Per-ELEMENT, and the message carries the index: `["active", 123]` is one bad entry
    in an otherwise fine array, and a decline that only said "related_terms" would send
    the model to re-author all of it."""
    out = _decline(
        "global_knowledge",
        {"statement": GOOD_STATEMENT, "related_terms": ["active", bad_term, "headcount"]},
    )

    assert "candidate.payload.related_terms[1]" in out.detail
    assert out.correctable


def test_an_empty_related_terms_array_passes() -> None:
    out = _validate("global_knowledge", {"statement": GOOD_STATEMENT, "related_terms": []})
    assert isinstance(out, ExtractedCandidate)


@pytest.mark.parametrize(
    "structured",
    [
        pytest.param([{"a": "b"}], id="list-of-objects"),
        pytest.param(["a", "b"], id="list-of-strings"),
        pytest.param([], id="empty-list"),
        pytest.param("a=b", id="string"),
        pytest.param(7, id="number"),
    ],
)
def test_a_list_shaped_structured_declines(structured) -> None:
    """A list-shaped `structured` lands its leaves keyed BY INDEX — no error anywhere,
    just a chunk whose supporting detail reads as `structured.0`. The EMPTY list is in
    the list on the same rule `optional` follows: `[]` is a type violation, not "not
    stated", and one type rule with a falsy exemption is a rule nobody remembers."""
    out = _decline("global_knowledge", {"statement": GOOD_STATEMENT, "structured": structured})

    assert "candidate.payload.structured" in out.detail


def test_an_empty_structured_object_passes() -> None:
    out = _validate("global_knowledge", {"statement": GOOD_STATEMENT, "structured": {}})
    assert isinstance(out, ExtractedCandidate)


@pytest.mark.parametrize("scope", [["employee"], {"of": "employee"}, True])
def test_a_container_scope_declines_rather_than_being_str_coerced(scope) -> None:
    """`scope` TITLES the landed chunk. `str(["employee"])` is a Python repr on a review
    card, which is corruption rather than rejection."""
    out = _decline("global_knowledge", {"statement": GOOD_STATEMENT, "scope": scope})

    assert "candidate.payload.scope" in out.detail


def test_a_numeric_scope_is_coerced_not_declined() -> None:
    """The deliberate asymmetry in `as_text`: `str(2025)` is exactly as usable as
    `"2025"`, so a number is accepted for a LABEL field. Pinned so the coercion stays a
    decision rather than an accident — note it does NOT apply to `statement`, which goes
    through `_non_empty_text` instead."""
    out = _validate("global_knowledge", {"statement": GOOD_STATEMENT, "scope": 2025})

    assert isinstance(out, ExtractedCandidate)
    # And the payload is forwarded UNCHANGED — the reader checks, it never rewrites, so
    # the coercion does not leak into what lands.
    assert out.payload["scope"] == 2025
    # Harmless downstream, and only because the mapper re-checks: `title` falls back to
    # None on a non-`str` scope rather than rendering `str(2025)` onto the card.


def test_a_numerically_scoped_payload_still_lands_with_no_title() -> None:
    """The reason the coercion above is safe, asserted against the real mapper rather
    than assumed: intake tolerates it, and landing does not act on it."""
    from dataclasses import replace

    from data_agent.learning.generalize.mapping import knowledge_seed_from_candidate

    from ..promotion.helpers import make_blueprint_candidate, with_type

    env = replace(
        with_type(make_blueprint_candidate(), "global_knowledge"),
        payload={"statement": GOOD_STATEMENT, "scope": 2025},
    )
    assert knowledge_seed_from_candidate(env, id="k::1").title is None


# --- (2c) user_knowledge / schema_edit: the open-key-set types ----------------------


def test_user_knowledge_keeps_an_open_key_set_but_still_type_checks() -> None:
    """Open because the target is entity-BEARING by contract and per-user scoped. That is
    a decision about UNKNOWN keys only — the KNOWN ones are still checked."""
    ok = _validate(
        "user_knowledge",
        {"statement": "I mean the NA region", "anything_at_all": {"nested": [1, 2]}},
    )
    assert isinstance(ok, ExtractedCandidate)

    bad = _decline("user_knowledge", {"statement": "I mean NA", "structured": ["a"]})
    assert "candidate.payload.structured" in bad.detail


@pytest.mark.parametrize(
    "field,alias",
    [
        ("edit_kind", "edit_type"),
        ("proposed_yaml", "patch"),
    ],
)
def test_a_wrong_typed_first_precedence_alias_declines_immediately_not_silently(
    field, alias
) -> None:
    """`_first_non_empty` mirrors `from_payload`'s `a or b` chain IN ITS ORDER
    (`edit_kind or edit_type`, `proposed_yaml or patch`) — but an `or` SKIPS a
    wrong-typed first alias silently, and the model would never learn which of the two
    names it got wrong. So a present-but-wrong-typed FIRST-precedence alias declines ON
    THAT ALIAS rather than falling through to its partner — the writer would have
    SELECTED the wrong-typed value."""
    payload = {
        "statement": "define active_employee",
        "edit_kind": "add_rule",
        "patch": "rules:\n  - id: x",
        "target_catalog": "payroll",
        field: {"nested": "object"},
        alias: "a perfectly good value",
    }
    out = _decline("schema_edit", payload)

    assert f"candidate.payload.{field}" in out.detail
    assert out.correctable


def test_a_wrong_typed_second_alias_beside_a_usable_first_passes() -> None:
    """The mirror, forced by matching the writer's precedence: `from_payload` reads
    `proposed_yaml or patch`, so a usable `proposed_yaml` means the wrong-typed `patch`
    beside it is never read by anything. Declining it would refuse a payload the writer
    handles correctly — the exact inversion the first QA round caught (a wrong-typed
    `proposed_yaml` sailing past a valid `patch` that the writer would NOT select)."""
    out = _validate(
        "schema_edit",
        {
            "statement": "define active_employee",
            "edit_kind": "add_rule",
            "target_catalog": "payroll",
            "proposed_yaml": "a perfectly good value",
            "patch": {"nested": "object"},
        },
    )

    assert isinstance(out, ExtractedCandidate)


def test_a_blank_first_alias_falls_through_to_the_second() -> None:
    """The other half of the same rule: `""` IS what `or` skips, so intake must skip it
    too or it would decline a payload the writer handles correctly."""
    out = _validate(
        "schema_edit",
        {
            "statement": "define active_employee",
            "edit_kind": "",
            "edit_type": "add_rule",
            "patch": "   ",
            "proposed_yaml": "rules:\n  - id: x",
            "target_catalog": "",
            "target": {"database": "payroll"},
        },
    )

    assert isinstance(out, ExtractedCandidate)


def test_a_non_object_schema_edit_target_declines_naming_target() -> None:
    """`target` is read with `.get("database")`, so a string here would raise inside the
    reader rather than decline."""
    out = _decline(
        "schema_edit",
        {
            "statement": "define active_employee",
            "edit_kind": "add_rule",
            "patch": "rules:\n  - id: x",
            "target": "payroll",
        },
    )

    assert "candidate.payload.target" in out.detail


def test_an_empty_schema_edit_payload_declines_rather_than_opening_an_empty_pr() -> None:
    """The whole reason this reader exists: every field `from_payload` reads is DEFAULTED,
    so `{}` opens a pull request with an empty body against a path derived from `""`."""
    out = _decline("schema_edit", {})

    assert out.reason == REASON_MALFORMED
    assert out.correctable


# --- (3) correctability parity with the blueprint path -----------------------------


def _blueprint_malformed() -> Decline:
    """A blueprint decline from the SAME family (a reader failed), as the comparison
    baseline — read off the real validator rather than restated."""
    raw = blueprint_raw()
    raw["payload"]["result_signature"] = {"grain": "one row per department"}
    out = to_candidate(raw, make_summary(), known_rules=frozenset())
    assert isinstance(out, Decline)
    return out


@pytest.mark.parametrize("ctype", ["global_knowledge", "user_knowledge", "schema_edit"])
def test_a_malformed_knowledge_decline_matches_the_blueprint_decline_contract(ctype) -> None:
    """ITEM 3. `Decline.correctable` means "the fix is a change of EXPRESSION, never of
    DECISION" — and a payload keyed `definition` instead of `statement` is the purest
    possible case of that, on any candidate type. So the three new readers must produce a
    decline INDISTINGUISHABLE in contract from the blueprint shape declines that have
    always been re-askable: same reason code, same `correctable`, same type label, and a
    detail that names a full dotted path from the candidate root."""
    baseline = _blueprint_malformed()
    out = _decline(ctype, {"nonsense_key": "x"})

    assert out.reason == baseline.reason == REASON_MALFORMED
    assert out.correctable == baseline.correctable is True
    assert out.type == ctype  # the label a human and a metric group by
    assert out.detail.startswith("candidate.payload")


async def test_the_stuck_payload_is_re_asked_by_the_real_extractor_and_can_be_fixed() -> None:
    """ITEM 3, end to end through the loop that actually matters.

    Intake declining is only half the fix — the value of moving the check here is that
    the MODEL gets told, in the same turn, and can re-emit. Proven through the real
    `LearningExtractor`: the stuck payload goes in, a corrective turn happens, the
    correction names the field, and the repaired candidate is kept.

    (What this does NOT prove: that a real model reads the correction and complies. The
    client is scripted; only a live probe can answer that. See
    `test_correction_loop_qa.py`'s header.)"""
    stuck = _raw(
        "global_knowledge",
        {
            "definition": GOOD_STATEMENT,
            "fact_type": "business_rule",
            "intent": "define active employee",
            "scope": "employee",
        },
    )
    fixed = _raw(
        "global_knowledge",
        {"statement": GOOD_STATEMENT, "knowledge_type": "business_rule", "scope": "employee"},
    )
    extractor = make_extractor([scripted_turn([stuck]), scripted_turn([fixed])])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert extractor._model_client.calls_made == 2
    assert result.corrections == 1
    assert result.declines == ()
    assert len(result.candidates) == 1
    assert result.candidates[0].payload["statement"] == GOOD_STATEMENT

    correction = [
        m
        for m in extractor._model_client.calls[1].messages
        if m.get("role") == "tool"
    ][-1]["content"]
    # The correction names the TYPE (so the model knows which candidate) and the closed
    # set (so it knows what to send instead).
    assert 'type "global_knowledge"' in correction
    assert "statement" in correction
    assert "omit" in correction  # the exit that keeps a correction from being coercion


async def test_the_correction_for_a_knowledge_payload_never_echoes_the_refused_text() -> None:
    """The refused key's value is, by definition, the text NOTHING scanned for entities.
    Feeding it back as prompt text would be the leak the closed set exists to prevent —
    committed by the message complaining about it."""
    stuck = _raw(
        "global_knowledge",
        {"statement": GOOD_STATEMENT, "definition": "employees in dept 0420 are active"},
    )
    extractor = make_extractor([scripted_turn([stuck]), scripted_turn([])])

    await extractor.extract(make_summary(), KEEP_VERDICT)

    correction = [
        m
        for m in extractor._model_client.calls[1].messages
        if m.get("role") == "tool"
    ][-1]["content"]
    assert "'definition'" in correction
    assert "0420" not in correction


async def test_a_knowledge_payload_that_stays_malformed_declines_terminally_not_forever() -> None:
    """The budget is shared across every correctable family, so a knowledge payload
    cannot loop the extractor: after the corrections run out it becomes an ordinary
    terminal decline that a human sees, with the reason intact."""
    stuck = _raw("global_knowledge", {"definition": GOOD_STATEMENT})
    extractor = make_extractor(
        [scripted_turn([stuck])] * 6, max_shape_corrections=2
    )

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.candidates == ()
    assert len(result.declines) == 1
    assert result.declines[0].reason == REASON_MALFORMED
    assert result.declines[0].type == "global_knowledge"


async def test_a_good_knowledge_candidate_beside_a_bad_one_is_kept_not_re_asked() -> None:
    """Partial success, on the knowledge path: re-emitting the whole array to fix one
    sibling puts work that already cleared every gate back at risk."""
    good = _raw("global_knowledge", {"statement": "a fact that is fine", "scope": "employee"})
    bad = _raw("global_knowledge", {"definition": GOOD_STATEMENT})
    extractor = make_extractor([scripted_turn([good, bad]), scripted_turn([])])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert len(result.candidates) == 1
    assert result.candidates[0].payload["statement"] == "a fact that is fine"


def test_a_non_object_knowledge_payload_is_a_decline_not_a_crash() -> None:
    """`to_candidate` never raises: an escape leaves the consumer's queue message
    un-acked → reclaim → dead-letter, losing the whole session AND the reason."""
    for payload in ("a statement", ["a statement"], 42, True):
        out = _decline("global_knowledge", payload)
        assert out.reason == REASON_MALFORMED
        assert "candidate.payload" in out.detail


def test_an_absent_knowledge_payload_is_a_decline_not_a_crash() -> None:
    raw = _raw("global_knowledge", {})
    del raw["payload"]
    out = to_candidate(raw, make_summary(), known_rules=frozenset())

    assert isinstance(out, Decline)
    assert out.reason == REASON_MALFORMED


def test_evidence_is_still_the_first_gate_for_a_malformed_knowledge_payload() -> None:
    """ORDER matters: a candidate that cites nothing is refused on D31 grounds and is
    TERMINAL, so the payload check must not overtake it and turn a substantive refusal
    into a re-ask (which would invite an invented citation)."""
    raw = _raw("global_knowledge", {"definition": GOOD_STATEMENT})
    raw["evidence"] = []
    out = to_candidate(raw, make_summary(), known_rules=frozenset())

    assert isinstance(out, Decline)
    assert out.reason == "no_evidence"
    assert not out.correctable


async def test_a_valid_knowledge_candidate_survives_the_whole_extractor_unchanged() -> None:
    """The happy path all of the above is measured against, through the real pipeline —
    the payload the reader accepted is the payload that comes out."""
    payload = {
        "statement": GOOD_STATEMENT,
        "knowledge_type": "business_rule",
        "related_terms": ["active", "headcount"],
        "structured": {"source": "hr policy"},
        "scope": "employee",
    }
    extractor = emit_extractor([_raw("global_knowledge", payload)])

    result = await extractor.extract(make_summary(), KEEP_VERDICT)

    assert result.declines == ()
    assert result.candidates[0].payload == payload
