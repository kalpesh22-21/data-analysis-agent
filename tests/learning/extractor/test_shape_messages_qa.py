"""Every decline a model could act on has to say what was wrong and what was expected.

MOTIVATION, from ten real analyst sessions: triage kept all ten, prior art worked, the
judge dropped three duplicates, and ZERO candidates were mined — because in every case
the model's ANALYSIS was right and its PACKAGING was wrong. Two of the failures are
reproduced verbatim below:

  * `gpt-4.1` flattened the envelope (payload fields at the top level, no `type`, no
    `payload` wrapper) and emitted `parameterization` as an object. Declined:
    `unknown candidate type`.
  * `gpt-5.5` got the envelope right and wrote `result_signature.grain` as PROSE where
    the schema wants an object. Declined: `bad blueprint payload: 'str' object has no
    attribute 'get'` — a raw interpreter message, actionable by nobody.

This file pins the properties that make a decline usable rather than any particular
sentence: it NAMES the dotted path, it says the SHAPE required, it never quotes the
offending value, and it is never interpreter-ese. The last three are asserted as a
SWEEP over mutations rather than one test per field, because the failure this replaces
was precisely a guard that covered only the paths someone happened to exercise — a
per-field test suite would have the same hole as a per-field message table.

Slug: PA-actionable-shape-declines.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.shape import (
    ShapeError,
    as_array,
    as_flag,
    as_int,
    as_number,
    as_object,
    as_text,
)
from data_agent.learning.extractor.validation import REASON_MALFORMED, to_candidate

from .helpers import blueprint_raw, make_summary, param_slot, payroll_parameterization

# A value no field of a well-formed candidate would need, planted in the OFFENDING
# position of each mutation. Stands in for the warehouse literals a real candidate is
# full of (a department name, an employee id) — a decline is not inside the D51 audit
# boundary that evidence quotes live in, and it becomes prompt text on the corrective
# turn, so leaking one is a real leak.
CANARY = "Zzyzx-Consolidated-Payroll-Unit-77"

# Interpreter-ese: the shapes a raw `str(exc)` takes. Any of these in a decline detail
# means an exception message reached the message, which is the defect this file guards.
_INTERPRETER_PHRASES = (
    "object has no attribute",
    "object is not subscriptable",
    "indices must be integers",
    "not iterable",
    "takes no arguments",
    "unhashable type",
    "argument must be",
    "Traceback",
)

_PATH = re.compile(r"candidate(\.[a-z_]+|\[\d+\])+|composes\[\d+\]")


def _validate(raw: Any) -> ExtractedCandidate | Decline:
    return to_candidate(raw, make_summary(), known_rules=frozenset())


def _decline(raw: Any) -> Decline:
    out = _validate(raw)
    assert isinstance(out, Decline), f"expected a Decline, got {type(out).__name__}"
    return out


# --- the two live failures ---------------------------------------------------------


def test_the_flattened_envelope_names_the_envelope_gpt_41() -> None:
    """`gpt-4.1`, verbatim: the payload's fields at the TOP level, no `type` and no
    `payload` wrapper. "unknown candidate type" told it nothing — the mistake was not
    the type it chose, it was that it skipped the envelope."""
    flattened = blueprint_raw()["payload"]
    flattened["evidence"] = [{"turn_ref": 0, "tool_call_ref": "tc1", "quote": "q"}]

    out = _decline(flattened)

    assert out.reason == REASON_MALFORMED
    assert out.correctable is True
    assert "candidate.type" in out.detail
    # The three facts a model needs to fix it: the closed set, the word ENVELOPE, and
    # where the fields it did send actually belong.
    assert "blueprint" in out.detail
    assert "ENVELOPE" in out.detail
    assert "payload" in out.detail


def test_parameterization_as_an_object_says_array_gpt_41() -> None:
    """The other half of the same `gpt-4.1` response. An OBJECT here used to be either
    silently normalized to `[]` (when empty ⇒ a wrong `totality_violation`, which is
    substantive and therefore never re-asked) or a `TypeError` about string indices."""
    raw = blueprint_raw()
    raw["payload"]["parameterization"] = {"department": {"role": "slot"}}

    out = _decline(raw)

    assert out.reason == REASON_MALFORMED
    assert "candidate.payload.parameterization" in out.detail
    assert "ARRAY" in out.detail
    assert "an object arrived" in out.detail


def test_result_signature_grain_as_prose_names_grain_and_columns_gpt_55() -> None:
    """`gpt-5.5`, verbatim — the whole reason this slice exists. The old message was
    `bad blueprint payload: 'str' object has no attribute 'get'`."""
    raw = blueprint_raw(
        result_signature={
            "shape": [{"column": "org_unit", "type": "String"}],
            "grain": (
                "one row per organizational unit above the company-wide per-employee "
                "benchmark"
            ),
            "invariants": [],
        }
    )

    out = _decline(raw)

    assert out.reason == REASON_MALFORMED
    assert out.correctable is True
    assert "candidate.payload.result_signature.grain" in out.detail
    assert "columns" in out.detail
    assert "verifiable" in out.detail
    assert "a string arrived" in out.detail
    # The old message, and the shape of every message like it, must be gone.
    assert "has no attribute" not in out.detail
    # And the model's own prose must not be quoted back at it.
    assert "organizational unit" not in out.detail


# --- the sweep: the properties, over every shape confusion we can name -------------


def _mut(path: str, value: Any) -> dict[str, Any]:
    """`blueprint_raw()` with one dotted *path* replaced by *value*."""
    raw = blueprint_raw()
    node: Any = raw
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    node[parts[-1]] = value
    return raw


# One entry per untrusted READ in `validation.py`, not one per field name someone
# thought of. Each plants the canary where the wrong-shaped value goes.
_MUTATIONS: dict[str, dict[str, Any]] = {
    "type-unknown": _mut("type", CANARY),
    "type-object": _mut("type", {"kind": CANARY}),
    "confidence-prose": _mut("confidence", CANARY),
    "confidence-bool": _mut("confidence", True),
    "evidence-object": _mut("evidence", {"turn_ref": 0, "quote": CANARY}),
    "evidence-item-scalar": _mut("evidence", [CANARY]),
    "evidence-item-no-quote": _mut("evidence", [{"turn_ref": 0, "tool_call_ref": CANARY}]),
    "evidence-turn-ref-prose": _mut(
        "evidence", [{"turn_ref": CANARY, "tool_call_ref": "tc1", "quote": "q"}]
    ),
    "entity-self-check-string": _mut("entity_self_check", CANARY),
    "entity-self-check-found-string": _mut(
        "entity_self_check", {"contains_entities": True, "found": CANARY}
    ),
    "entity-self-check-flag-string": _mut(
        "entity_self_check", {"contains_entities": "yes", "found": [CANARY]}
    ),
    "depends-on-string": _mut("depends_on", CANARY),
    "payload-array": _mut("payload", [CANARY]),
    "payload-string": _mut("payload", CANARY),
    "intent-absent": _mut("payload.intent", None),
    "intent-object": _mut("payload.intent", {"text": CANARY}),
    "kind-absent": _mut("payload.kind", None),
    "accepted-signal-absent": _mut("payload.accepted_signal", None),
    "source-refs-string": _mut("payload.source_tool_call_refs", CANARY),
    "source-ref-object": _mut("payload.source_tool_call_refs", [{"ref": CANARY}]),
    "parameterization-object": _mut("payload.parameterization", {"a": CANARY}),
    "parameterization-string": _mut("payload.parameterization", CANARY),
    "param-entry-scalar": _mut("payload.parameterization", [CANARY]),
    "param-locator-absent": _mut("payload.parameterization", [{"role": "inline", "why": CANARY}]),
    "param-locator-string": _mut(
        "payload.parameterization", [{"locator": CANARY, "role": "inline", "why": "w"}]
    ),
    "param-locator-column-absent": _mut(
        "payload.parameterization",
        [{"locator": {"table": "payroll.payroll_fact", "value": CANARY}, "role": "inline",
          "why": "w"}],
    ),
    "param-role-absent": _mut(
        "payload.parameterization",
        [{"locator": {"table": "t.t", "column": "c", "value": CANARY}}],
    ),
    "slot-string": _mut(
        "payload.parameterization",
        [{"locator": {"table": "t.t", "column": "c", "value": "v"}, "role": "slot",
          "slot": CANARY}],
    ),
    "slot-name-absent": _mut(
        "payload.parameterization",
        [{"locator": {"table": "t.t", "column": "c", "value": "v"}, "role": "slot",
          "slot": {"type": "entity", "binds_to": CANARY}}],
    ),
    "slot-required-string": _mut(
        "payload.parameterization",
        [{"locator": {"table": "t.t", "column": "c", "value": "v"}, "role": "slot",
          "slot": {"name": "n", "type": "entity", "binds_to": "t.t.c", "required": CANARY}}],
    ),
    "slot-binds-to-array": _mut(
        "payload.parameterization",
        [{"locator": {"table": "t.t", "column": "c", "value": "v"}, "role": "slot",
          "slot": {"name": "n", "type": "entity", "binds_to": [CANARY], "required": True}}],
    ),
    "slot-enum-values-string": _mut(
        "payload.parameterization",
        [{"locator": {"table": "t.t", "column": "c", "value": "v"}, "role": "slot",
          "slot": {"name": "n", "type": "enum", "binds_to": "t.t.c", "required": True,
                   "enum_values": CANARY}}],
    ),
    "result-signature-string": _mut("payload.result_signature", CANARY),
    "result-signature-grain-prose": _mut(
        "payload.result_signature", {"shape": [], "grain": CANARY, "invariants": []}
    ),
    "result-signature-grain-columns-string": _mut(
        "payload.result_signature",
        {"shape": [], "grain": {"columns": CANARY, "verifiable": True}, "invariants": []},
    ),
    "result-signature-grain-verifiable-string": _mut(
        "payload.result_signature",
        {"shape": [], "grain": {"columns": [], "verifiable": CANARY}, "invariants": []},
    ),
    "result-signature-shape-scalar": _mut(
        "payload.result_signature",
        {"shape": [CANARY], "grain": {"columns": [], "verifiable": True}, "invariants": []},
    ),
    "result-signature-invariants-string": _mut(
        "payload.result_signature",
        {"shape": [], "grain": {"columns": [], "verifiable": True}, "invariants": CANARY},
    ),
    "composes-string": _mut("payload.composes", CANARY),
    "composes-node-scalar": _mut("payload.composes", [CANARY]),
    "composes-order-prose": _mut("payload.composes", [{"order": CANARY}]),
    "composes-node-kind-prose": _mut(
        "payload.composes", [{"order": 0, "node_kind": CANARY}]
    ),
    "composes-feeds-from-scalar": _mut(
        "payload.composes", [{"order": 0, "feeds_from": CANARY}]
    ),
    "composes-feeds-entry-prose": _mut(
        "payload.composes", [{"order": 0, "feeds_from": [CANARY]}]
    ),
    "composes-consumes-scalar": _mut(
        "payload.composes", [{"order": 0, "consumes": CANARY}]
    ),
    "composes-requires-approval-scalar": _mut(
        "payload.composes", [{"order": 0, "requires_approval": CANARY}]
    ),
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_every_shape_confusion_declines_correctably(name: str) -> None:
    """A wrong JSON type is a PACKAGING mistake, so it must reach the correctable side
    — the side the extractor is allowed to re-ask about. A shape mistake that lands as
    a substantive decline (`no_evidence`, `totality_violation`) is the misdiagnosis
    this slice is about: it reads as a judgement on the model's analysis and it is
    terminal."""
    out = _decline(_MUTATIONS[name])
    assert out.reason == REASON_MALFORMED, out.detail
    assert out.correctable is True, out.detail


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_no_decline_message_quotes_the_offending_value(name: str) -> None:
    """The entity-freedom invariant, swept. The message is fed to a model and recorded
    on the decline; the value that caused it is model-authored from an entity-bearing
    session."""
    out = _decline(_MUTATIONS[name])
    assert CANARY not in out.detail


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_no_decline_message_is_interpreter_ese(name: str) -> None:
    """No `str(exc)` may reach a decline. This is the property, not the individual
    sentences: a new reader that forgets and falls back to the belt fails here."""
    for phrase in _INTERPRETER_PHRASES:
        assert phrase not in out_detail(name), phrase


def out_detail(name: str) -> str:
    return _decline(_MUTATIONS[name]).detail


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_every_decline_message_names_a_path_and_a_requirement(name: str) -> None:
    """Self-containment: a dotted path from the candidate root, and enough of a
    sentence to say what was wanted. Length is a crude proxy for the second half, but
    it catches the failure mode exactly — `"unknown candidate type"` is 22 characters
    and names nothing."""
    detail = out_detail(name)
    assert _PATH.search(detail), detail
    assert len(detail) > 40, detail


# --- the line between shape and judgement ------------------------------------------


@pytest.mark.parametrize(
    ("name", "raw", "reason"),
    [
        ("no-evidence", blueprint_raw(evidence=[]), "no_evidence"),
        (
            "unknown-rule",
            blueprint_raw(
                parameterization=[
                    p if p["locator"]["column"] != "record_type"
                    else {"locator": {"table": "payroll.payroll_fact",
                                      "column": "record_type", "value": "EARNING"},
                          "role": "rule", "rule_id": "no_such_rule"}
                    for p in payroll_parameterization()
                ]
            ),
            "missing_rule",
        ),
        (
            "totality",
            blueprint_raw(
                parameterization=[
                    p for p in payroll_parameterization()
                    if p["locator"]["column"] != "region"
                ]
            ),
            "totality_violation",
        ),
        (
            "malformed-optional-pattern",
            blueprint_raw(
                parameterization=[
                    param_slot("region", required=False, value="NA",
                               optional_pattern="region = ((( bad sql"),
                    *[p for p in payroll_parameterization()
                      if p["locator"]["column"] != "region"],
                ]
            ),
            "role_inconsistent",
        ),
    ],
)
def test_a_judgement_about_content_is_never_correctable(
    name: str, raw: dict[str, Any], reason: str
) -> None:
    """The line, asserted from the other side. Each of these consults something OUTSIDE
    the candidate — the catalog's rule ids, the accepted SQL's predicates, a SQL parser
    — or asks for content the candidate does not contain. Re-asking any of them is
    talking a model into a candidate it was right to decline:

      * `no_evidence` is the D31 primary guard; re-asking invites an invented citation.
      * `missing_rule` is the §7 pairing hook — "no such rule exists" is the OUTPUT a
        human acts on, and pressure to name any id that passes destroys it.
      * `totality_violation` would ask the model to model a filter it decided to drop.
      * a malformed `optional_pattern` is fixed with SQL, and the extractor's contract
        is plan-not-SQL — its message also quotes the offending fragment, which must
        not become prompt text."""
    out = _decline(raw)
    assert out.reason == reason, name
    assert out.correctable is False


@pytest.mark.parametrize(
    ("name", "params", "needle"),
    [
        (
            "inline-wrote-reason-not-why",
            [
                {"locator": {"table": "payroll.payroll_fact", "column": "record_type",
                             "value": "EARNING"},
                 "role": "inline", "reason": "defines the metric 'earnings'"},
                *[p for p in payroll_parameterization()
                  if p["locator"]["column"] != "record_type"],
            ],
            "named `why`",
        ),
        (
            "slot-type-outside-the-enum",
            [
                param_slot("department", slot_type="banana", value="0420"),
                *[p for p in payroll_parameterization()
                  if p["locator"]["column"] != "department"],
            ],
            "exactly one of",
        ),
        (
            "rule-role-with-no-id",
            [
                {"locator": {"table": "payroll.payroll_fact", "column": "record_type",
                             "value": "EARNING"}, "role": "rule"},
                *[p for p in payroll_parameterization()
                  if p["locator"]["column"] != "record_type"],
            ],
            "no rule_id",
        ),
        (
            "enum-slot-with-no-values",
            [
                param_slot("department", slot_type="enum", value="0420"),
                *[p for p in payroll_parameterization()
                  if p["locator"]["column"] != "department"],
            ],
            "enum_values",
        ),
        (
            "optional-slot-with-no-pattern",
            [
                param_slot("region", required=False, optional_pattern=None, value="NA"),
                *[p for p in payroll_parameterization()
                  if p["locator"]["column"] != "region"],
            ],
            "optional_pattern",
        ),
    ],
)
def test_a_role_obligation_the_candidate_took_on_itself_is_correctable(
    name: str, params: list[dict[str, Any]], needle: str
) -> None:
    """The other half of the line, and the one a LIVE run forced.

    `gpt-5.5` classified a status filter as inline, wrote a perfectly good
    justification, and put it under `reason` instead of `why`. The old decline —
    "inline role without 'why'", a terminal `role_inconsistent` — read as a judgement
    about the model's analysis when the analysis was right and the KEY NAME was wrong.

    Every case here is the same species: a required field or a closed enum that follows
    mechanically from a role or type the candidate CHOSE FOR ITSELF. Nothing outside the
    candidate is consulted, so the fix is a change of expression, not of decision. The
    reason code stays `role_inconsistent` — it still means what it meant — but the
    decline is re-askable."""
    out = _decline(blueprint_raw(parameterization=params))
    assert out.reason == "role_inconsistent", name
    assert out.correctable is True, out.detail
    assert needle in out.detail, out.detail
    assert "candidate.payload.parameterization[" in out.detail


def test_a_valid_candidate_still_extracts_unchanged() -> None:
    """The regression floor for the whole rewrite: the readers must not have made the
    happy path stricter."""
    assert isinstance(_validate(blueprint_raw()), ExtractedCandidate)


# --- shape.py: the readers, and why each one is not `isinstance(x, Iterable)` -------


def test_as_array_refuses_a_string_because_iterating_one_is_a_silent_misread() -> None:
    """The reason `as_array` cannot be a duck-typed iterable check. `enum_values:
    "NA,EU"` iterated cleanly into seven single-character values and reached the
    corpus with no error anywhere — worse than a raise, because nothing looked wrong."""
    with pytest.raises(ShapeError):
        as_array("NA,EU", at="candidate.x", requirement="an array")
    assert as_array([], at="candidate.x", requirement="an array") == []


def test_as_flag_refuses_the_string_false() -> None:
    """`bool("false")` is True, and the three fields that go through `as_flag`
    (`required`, `verifiable`, `contains_entities`) are exactly the ones where
    inverting the answer is silent."""
    with pytest.raises(ShapeError):
        as_flag("false", at="candidate.x", requirement="true or false")
    assert as_flag(False, at="candidate.x", requirement="true or false") is False


def test_as_number_refuses_a_bool_but_accepts_a_numeric_string() -> None:
    """`float(True)` is 1.0 — a maximum-confidence claim conjured from a type
    confusion. `float("0.9")` is the same number a model meant."""
    with pytest.raises(ShapeError):
        as_number(True, at="candidate.confidence", requirement="a number")
    assert as_number("0.9", at="candidate.confidence", requirement="a number") == 0.9


def test_as_int_mirrors_the_node_index_rules() -> None:
    """Same domain as `validation.py::_node_index`, deliberately: a numeric string is
    accepted, a bool and a fractional float are not."""
    assert as_int("3", at="candidate.x", requirement="an integer") == 3
    assert as_int(3.0, at="candidate.x", requirement="an integer") == 3
    for bad in (True, 3.5, [3], None):
        with pytest.raises(ShapeError):
            as_int(bad, at="candidate.x", requirement="an integer")


def test_as_text_coerces_a_number_but_never_a_container() -> None:
    """`str(2025)` is what the model meant; `str({...})` is a Python repr landing in
    the corpus as an intent nobody can parse — a corruption, not a rejection."""
    assert as_text(2025, at="candidate.x", requirement="a string") == "2025"
    for bad in ({"a": 1}, ["a"], True):
        with pytest.raises(ShapeError):
            as_text(bad, at="candidate.x", requirement="a string")


def test_the_message_is_built_from_the_check_not_written_beside_it() -> None:
    """The enforcement claim, stated as a test: a reader's message is a by-product of
    its arguments, so there is no way to reject without explaining. `requirement` is a
    mandatory keyword on every reader — omitting it does not run."""
    with pytest.raises(ShapeError) as caught:
        as_object("prose", at="candidate.payload.result_signature.grain",
                  requirement='an object with a "columns" array')
    message = str(caught.value)
    assert "candidate.payload.result_signature.grain" in message
    assert 'an object with a "columns" array' in message
    assert "a string arrived" in message
    assert "prose" not in message

    with pytest.raises(TypeError):
        as_object("prose", at="candidate.x")  # type: ignore[call-arg]


def test_absent_and_wrong_type_are_different_sentences() -> None:
    """"You did not send it" and "you sent the wrong kind of thing" call for different
    fixes, so they are not collapsed into one message."""
    absent = str(ShapeError("candidate.payload.intent", "a sentence", None, absent=True))
    wrong = str(ShapeError("candidate.payload.intent", "a sentence", ["x"]))
    assert "is required" in absent and "absent or null" in absent
    assert "an array arrived" in wrong
    assert absent != wrong
