"""ADVERSARIAL: the fail-to-review sub-documents read back from a store humans can
write through `cbq`.

`candidate/decline.py` states the posture — "NORMALIZE, do not trust" — and the two
`from_doc` classmethods are the only enforcement of it. Everything below feeds them the
shapes a hand edit, a foreign writer or a half-finished migration actually produces:
wrong types at every level, missing keys, nulls, nesting, and text far larger than any
model would emit. The contract under test is the same one three times over:

  * a shape that is not ours reads as ABSENT (`None`), never as a coerced empty value and
    never as an exception — this parsing runs inside a queue worker and inside the inbox
    projection, and a raise in either is an outage;
  * ABSENT and PRESENT-BUT-UNREADABLE must not collapse, because `decline`'s presence is
    what marks a row as a form to complete (`derive_inbox_reason`) and `revalidation`'s
    absence is what makes the completion path REFUSE rather than re-validate against an
    invented summary;
  * whatever does survive is of the declared type, because the next reader is an f-string
    on its way to a reviewer's browser.

These tests were added by QA over the builder's slice; the builder's own round-trip test
lives in `test_contracts_wave0.py` and covers the happy path plus two junk values.
"""

from __future__ import annotations

import json

import pytest

from data_agent.learning.candidate.decline import (
    DeclineBlock,
    EvidencePointer,
    ValidationSnapshot,
)
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.inbox.models import InboxItem
from data_agent.learning.summary.models import AnswerSql

# Every non-dict a JSON document can hold at a key that is supposed to hold an object.
_NOT_A_DICT = [None, "totality_violation", 42, 3.5, True, ["a"], (), ""]

# The two fields the completion path READS, in their good shape. Every per-field snapshot
# test below supplies them and varies exactly one thing, because the guard is now
# AGGREGATE as well as per-field: `ValidationSnapshot.from_doc` normalizes each field
# tolerantly and then reports the whole snapshot MISSING if what survived cannot be
# re-validated against (no SQL to walk, or no citation). Without a good baseline every
# test here would trip that aggregate and stop testing its own field.
_GOOD_SQL = {"tc1": ["SELECT 1 FROM t WHERE a = 'b'"]}
_GOOD_EVIDENCE = [{"turn_ref": 0, "tool_call_ref": "tc1"}]


def _snapshot_doc(**overrides: object) -> dict:
    doc: dict = {
        "session_id": "s1",
        "sql_by_ref": dict(_GOOD_SQL),
        "evidence": list(_GOOD_EVIDENCE),
    }
    doc.update(overrides)
    return doc


# --- DeclineBlock.from_doc -------------------------------------------------------------


@pytest.mark.parametrize("doc", _NOT_A_DICT)
def test_a_decline_that_is_not_an_object_reads_as_absent(doc: object) -> None:
    assert DeclineBlock.from_doc(doc) is None


@pytest.mark.parametrize("reason", [None, 42, {"reason": "x"}, ["totality_violation"], ""])
def test_a_decline_without_a_usable_reason_reads_as_absent(reason: object) -> None:
    """`reason` is the whole actionable content of the block. A block that reads back
    without one would put a row in front of a reviewer with nothing to act on — which is
    the failure the slice exists to fix, wearing the opposite mask."""
    assert DeclineBlock.from_doc({"reason": reason, "detail": "something"}) is None


@pytest.mark.parametrize("detail", [None, 42, {"a": 1}, ["line"], True])
def test_a_non_string_detail_reads_as_empty_not_as_a_repr(detail: object) -> None:
    """The detail is rendered into a reviewer-facing wire field. A dict here must not
    reach a browser as `{'a': 1}` — the same rule `route_reason` already follows."""
    block = DeclineBlock.from_doc({"reason": "totality_violation", "detail": detail})
    assert block is not None
    assert block.detail == ""


@pytest.mark.parametrize(
    "attempted", [None, "2", 2.5, {"n": 2}, [2], True, False]
)
def test_a_non_integer_correction_count_reads_as_zero(attempted: object) -> None:
    """Booleans included, deliberately: `True` is an `int` in Python, and "the model was
    corrected once" is not what a stored `true` means."""
    block = DeclineBlock.from_doc(
        {"reason": "totality_violation", "corrections_attempted": attempted}
    )
    assert block is not None
    assert block.corrections_attempted == 0


def test_a_real_integer_correction_count_survives() -> None:
    block = DeclineBlock.from_doc(
        {"reason": "totality_violation", "corrections_attempted": 2}
    )
    assert block is not None and block.corrections_attempted == 2


@pytest.mark.parametrize("history", [None, "first correction", 42, {"0": "first"}, True])
def test_a_correction_history_that_is_not_a_list_reads_as_empty(history: object) -> None:
    """A bare string is the dangerous one: it is iterable, so a reader that trusted it
    would produce a tuple of single CHARACTERS and render a correction round per letter."""
    block = DeclineBlock.from_doc(
        {"reason": "totality_violation", "correction_history": history}
    )
    assert block is not None
    assert block.correction_history == ()


def test_a_partly_malformed_correction_history_keeps_only_the_strings() -> None:
    block = DeclineBlock.from_doc(
        {
            "reason": "totality_violation",
            "correction_history": ["first", None, {"second": 1}, 3, "third", ["fourth"]],
        }
    )
    assert block is not None
    assert block.correction_history == ("first", "third")


def test_a_huge_stored_detail_is_carried_rather_than_crashing() -> None:
    """Bounding happens at the BUILD site (`validation.py::_flattened`/`_quoted`), not
    here — this reader's contract is that it does not raise. Pinned so a future truncation
    added in the wrong layer is a deliberate change rather than a silent one."""
    huge = "x" * 500_000
    block = DeclineBlock.from_doc(
        {"reason": "totality_violation", "detail": huge, "correction_history": [huge]}
    )
    assert block is not None
    assert len(block.detail) == 500_000


def test_extra_unknown_keys_are_ignored() -> None:
    block = DeclineBlock.from_doc(
        {"reason": "totality_violation", "injected": {"deeply": ["nested"]}}
    )
    assert block is not None and block.reason == "totality_violation"


# --- ValidationSnapshot.from_doc ---------------------------------------------------------


@pytest.mark.parametrize("doc", _NOT_A_DICT)
def test_a_snapshot_that_is_not_an_object_reads_as_absent(doc: object) -> None:
    assert ValidationSnapshot.from_doc(doc) is None


@pytest.mark.parametrize("session_id", [None, 42, {"id": "s"}, ["s"], "", True])
def test_a_snapshot_without_a_session_id_reads_as_absent(session_id: object) -> None:
    """ABSENT, not an empty snapshot. An empty `sql_by_ref` would make every
    re-validation decline `unrewritable_sql` — a misdiagnosis blaming the model's SQL for
    a storage fault — so the completion path must be told the snapshot is missing."""
    assert ValidationSnapshot.from_doc({"session_id": session_id, "sql_by_ref": {}}) is None


@pytest.mark.parametrize("raw_map", [None, "tc1", 42, ["tc1"], True])
def test_a_sql_map_that_is_not_an_object_reads_as_missing(raw_map: object) -> None:
    """UPDATED with the derived-guard fix (see `test_fail_to_review_snapshot_parity.py`,
    which pinned this from the reviewer-facing side).

    This used to assert a snapshot with an EMPTY map, on the "normalize per field" rule.
    That normalization is still what happens INSIDE the reader — but an empty map is not a
    survivable result: the completion path walks `sql_by_ref` and an empty one makes every
    re-validation decline `unrewritable_sql`, telling a reviewer that a query which ran
    live could not be rewritten, which is both false and unactionable. So the aggregate is
    judged after the per-field normalization, and a snapshot that cannot supply the SQL
    reads as MISSING — which routes to `completion_unavailable` (503, "this cannot be
    checked")."""
    assert ValidationSnapshot.from_doc(_snapshot_doc(sql_by_ref=raw_map)) is None


def test_only_well_formed_sql_entries_survive_the_map() -> None:
    """Per-REF tolerance: one unreadable ref must not cost the others, and a ref whose
    SQL list is empty (or all whitespace) must not read back as a ref with no queries —
    the totality walk would then have nothing to check the plan against and pass it."""
    snap = ValidationSnapshot.from_doc(
        _snapshot_doc(
            sql_by_ref={
                "tc1": ["SELECT 1", None, 42, "  ", "SELECT 2"],
                "tc2": "SELECT 3",  # not a list
                "tc3": [],  # empty after filtering
                "tc4": ["   "],  # whitespace only
                5: ["SELECT 4"],  # non-string key
                "tc5": ["SELECT 5"],
            }
        )
    )
    assert snap is not None
    assert snap.sql_by_ref == {"tc1": ("SELECT 1", "SELECT 2"), "tc5": ("SELECT 5",)}


@pytest.mark.parametrize("evidence", [None, "tc1", 42, {"turn_ref": 0}, True])
def test_evidence_that_is_not_a_list_reads_as_missing(evidence: object) -> None:
    """UPDATED with the derived-guard fix, by the same argument as the SQL map above: a
    snapshot with zero citations makes the completion decline `no_evidence` — telling the
    reviewer their form cites nothing, which they can never fix because the completion
    body carries no citations at all."""
    assert ValidationSnapshot.from_doc(_snapshot_doc(evidence=evidence)) is None


def test_only_well_formed_evidence_pointers_survive() -> None:
    """A citation is a (turn_ref, tool_call_ref) PAIR; a half-read one is not a pointer.
    `True` is rejected explicitly — `isinstance(True, int)` would otherwise make it
    turn 1."""
    snap = ValidationSnapshot.from_doc(
        _snapshot_doc(
            evidence=[
                {"turn_ref": 0, "tool_call_ref": "tc1"},
                {"turn_ref": True, "tool_call_ref": "tc2"},
                {"turn_ref": "0", "tool_call_ref": "tc3"},
                {"turn_ref": 1, "tool_call_ref": 42},
                {"turn_ref": 1},
                "tc4",
                None,
                {"turn_ref": 2, "tool_call_ref": "tc5", "quote": "an injected quote"},
            ]
        )
    )
    assert snap is not None
    assert snap.evidence == (
        EvidencePointer(0, "tc1"),
        EvidencePointer(2, "tc5"),
    )


@pytest.mark.parametrize("accepted", [None, 42, {"signal": "x"}, ["x"], True])
def test_a_non_string_acceptance_signal_reads_as_absent(accepted: object) -> None:
    """`accepted_signal=None` is the D34 "no acceptance" case, which re-validation
    DECLINES on. Coercing junk to a truthy string here would manufacture an acceptance
    the session never carried."""
    snap = ValidationSnapshot.from_doc(_snapshot_doc(accepted_signal=accepted))
    assert snap is not None
    assert snap.accepted_signal is None


@pytest.mark.parametrize("field_name", ["user_id", "trace_id", "content_hash"])
@pytest.mark.parametrize("value", [None, 42, {"a": 1}, ["x"], True])
def test_the_scalar_string_fields_normalize_to_empty(field_name: str, value: object) -> None:
    snap = ValidationSnapshot.from_doc(_snapshot_doc(**{field_name: value}))
    assert snap is not None
    assert getattr(snap, field_name) == ""


def test_a_junk_snapshot_still_rebuilds_a_summary_rather_than_raising() -> None:
    """The whole point of normalizing instead of raising: what comes out has to be
    USABLE. A snapshot rescued from a half-junk document must still produce a
    `SessionSummary` the validation path can walk (it will decline it — that is the
    correct answer — but it will not explode inside the completion request)."""
    snap = ValidationSnapshot.from_doc(
        _snapshot_doc(
            user_id=42,
            sql_by_ref={"tc1": ["SELECT 1", None], "tc2": 42},
            evidence=[{"turn_ref": 0, "tool_call_ref": "tc1"}, "junk"],
        )
    )
    assert snap is not None
    rebuilt = snap.to_summary()
    assert rebuilt.session_id == "s1"
    assert rebuilt.user_id == ""
    assert rebuilt.answer_sqls == (AnswerSql(tool_call_ref="tc1", sql="SELECT 1", blueprint_id=None),)
    assert rebuilt.turns == () and rebuilt.tool_calls == ()


def test_a_deeply_nested_junk_document_does_not_recurse() -> None:
    """A hostile writer nesting 2000 objects into a field neither reader descends. The
    readers are flat by construction; this pins that they stay that way."""
    nested: object = "bottom"
    for _ in range(2000):
        nested = {"deeper": nested}
    assert DeclineBlock.from_doc({"reason": "totality_violation", "detail": nested}) == (
        DeclineBlock(reason="totality_violation")
    )
    # The nested value is not descended into — it is simply not a usable map, so the
    # snapshot reads as missing (no `RecursionError`, which is the whole point).
    assert ValidationSnapshot.from_doc(_snapshot_doc(sql_by_ref=nested)) is None


# --- the envelope that carries them ------------------------------------------------------


def _doc(**overrides: object) -> dict:
    """A minimal persisted candidate document, as `from_doc` receives it."""
    doc: dict = {
        "candidate_id": "candidate::hash-x::review-0",
        "type": "blueprint",
        "status": CandidateStatus.NEEDS_PARAMETERIZATION,
        "payload": {"intent": "ratio of deductions to earnings"},
        "content_hash": "hash-x",
        "entity_scan": {"result": "pending", "hits": []},
    }
    doc.update(overrides)
    return doc


@pytest.mark.parametrize(
    "decline, revalidation",
    [
        ("totality_violation", ["tc1"]),
        (None, None),
        (42, 42),
        ([{"reason": "totality_violation"}], {"session_id": 5}),
        ({"detail": "no reason key"}, {"sql_by_ref": {"tc1": ["SELECT 1"]}}),
        (True, False),
    ],
)
def test_a_candidate_doc_with_junk_stamps_parses_with_both_absent(
    decline: object, revalidation: object
) -> None:
    """The envelope reader is where a bad document meets the queue worker. Neither field
    may raise, and neither may fabricate — a snapshot with no `session_id` is missing, not
    empty."""
    env = CandidateEnvelope.from_doc(_doc(decline=decline, revalidation=revalidation))
    assert env.decline is None
    assert env.revalidation is None
    assert env.status == CandidateStatus.NEEDS_PARAMETERIZATION


def test_a_readable_decline_survives_an_unreadable_snapshot_and_vice_versa() -> None:
    """The two stamps are written together but must READ independently: a row with a
    decline and no snapshot is exactly the case the completion path refuses with
    `completion_unavailable`, and collapsing either into the other would turn that honest
    refusal into a re-validation against an invented summary."""
    only_decline = CandidateEnvelope.from_doc(
        _doc(decline={"reason": "totality_violation"}, revalidation="junk")
    )
    assert only_decline.decline is not None and only_decline.revalidation is None

    only_snapshot = CandidateEnvelope.from_doc(
        _doc(decline="junk", revalidation=_snapshot_doc())
    )
    assert only_snapshot.decline is None and only_snapshot.revalidation is not None


def test_the_inbox_projection_of_a_junk_row_neither_raises_nor_leaks() -> None:
    """The end of the blast radius. A row rescued from a hostile document still has to
    project — and the withholding rule still has to hold on it, because an unsettled scan
    is exactly what a hand-written document has."""
    env = CandidateEnvelope.from_doc(
        _doc(
            decline={"reason": "totality_violation", "detail": {"nope": 1}},
            revalidation="junk",
            judge="junk",
            session_signals="junk",
            novelty=["junk"],
            route_reason={"nope": 1},
        )
    )
    item = InboxItem.from_envelope(env)
    view = item.decline_view()
    assert view is not None
    assert view["reason"] == "totality_violation"
    assert view["detail"] == ""
    assert view["detail_withheld"] is True
    assert view["judge_verdict"] == "" and view["judge_covered_by"] == ""
    assert item.route_reason is None
    # And the whole projection is JSON-serializable — no dataclass or repr survives into
    # the wire shape.
    json.dumps(view)
