"""ADVERSARIAL: what the re-validation snapshot promises, and the one promise it does
not keep.

The completion path re-validates against a `SessionSummary` rebuilt from
`ValidationSnapshot` — the session itself is gone, under its own TTL and its own access
boundary. Everything therefore turns on one property: **the reconstruction must reach the
same verdict the original summary would have.** The builder pinned the happy direction of
that (`test_consumer_fail_to_review.py::test_a_snapshot_and_the_original_validate_
identically`). This file pins the DEGRADED directions, which is where a snapshot-based
design actually fails:

  * a ref the snapshot cannot resolve must DECLINE, not pass — a re-validation that
    walked no SQL would wave through a plan that covers nothing;
  * a citation the snapshot cannot resolve is NOT checked on either path, and pinning
    that stops a future reader from assuming a resolution gate that has never existed;
  * and a snapshot that came back from the store DAMAGED must be reported as missing
    rather than as empty — which `candidate/decline.py` stated as its rule and did not
    implement. FIXED: the guard is now derived from the three things the completion path
    READS (the identity, the SQL map it walks, at least one citation) rather than from the
    key that names the record, so a damaged snapshot refuses with `completion_unavailable`
    instead of blaming the model's proven-live SQL. The tests below were the bug; they are
    now the pins.
"""

from __future__ import annotations

import pytest

from data_agent.learning.candidate.decline import ValidationSnapshot
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateEnvelope,
    CandidateStatus,
    build_declined_envelope,
    mint_review_candidate_id,
)
from data_agent.learning.extractor.models import Decline, ExtractedCandidate
from data_agent.learning.extractor.validation import to_candidate
from data_agent.learning.inbox import ParameterizationCompleter, ReviewInbox
from data_agent.learning.inbox.completion import CompletionUnavailableError

from .extractor.helpers import (
    PAYROLL_SQL,
    blueprint_raw,
    make_summary,
    make_tool_call,
    make_turn,
    param_slot,
    payroll_parameterization,
)

CID = mint_review_candidate_id("hash-ratio", 0)
_MISSING_REGION = payroll_parameterization()[:3]
_HUMAN_ENTRIES = [
    param_slot(
        "region", slot_type="entity", required=False, optional_pattern="TRUE", value="NA"
    )
]


def _summary(*, tool_calls=None):
    return make_summary(
        tool_calls=tool_calls if tool_calls is not None
        else (make_tool_call(ref="tc1", sql=PAYROLL_SQL),),
        turns=(make_turn(0),),
        content_hash="hash-ratio",
    )


def _declined(raw: dict, *, summary=None) -> CandidateEnvelope:
    from dataclasses import replace

    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="region = 'NA' has no entry",
            correctable=True,
            corrections_attempted=2,
            correction_history=(),
            raw_payload=raw,
        ),
        summary if summary is not None else _summary(),
        candidate_id=CID,
    )
    return replace(
        env,
        entity_scan={
            "result": "pass",
            "hits": [],
            "scanned_fields": ["intent"],
            "scanner": "regex+ner",
        },
    )


async def _inbox(env: CandidateEnvelope):
    store = InMemoryCandidateStore()
    await store.put(env)
    return (
        ReviewInbox(
            store,
            completer=ParameterizationCompleter(
                store=store, known_rules=frozenset(), rule_index=None, stages=()
            ),
        ),
        store,
    )


# --- refs the snapshot cannot resolve -----------------------------------------------------


async def test_a_source_ref_the_snapshot_never_carried_declines_rather_than_passing() -> None:
    """The sharp edge of a snapshot-based re-validation: if the accepted SQL cannot be
    resolved, the D97 totality walk has NOTHING to check the plan against — and a walk
    over nothing trivially succeeds. It must decline instead, which it does, and the
    reason names the resolution failure rather than the reviewer's entries."""
    ghost = blueprint_raw(parameterization=_MISSING_REGION, source_refs=("tc-ghost",))
    inbox, store = await _inbox(_declined(ghost))

    result = await inbox.complete_parameterization(CID, entries=_HUMAN_ENTRIES)

    assert result.outcome == "declined"
    assert result.decline is not None
    assert result.decline.reason == "unrewritable_sql"
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_a_session_whose_tool_call_carried_no_sql_declines_the_completion() -> None:
    """The same hole reached from the session side: a summary whose only tool call is a
    non-SQL one (`answerWithTable`, `updateAnalysisState`) snapshots an EMPTY `sql_by_ref`,
    and the completion must refuse to bless a plan it cannot check. Pinned because "empty
    map" is the shape a partial write also produces."""
    raw = blueprint_raw(parameterization=_MISSING_REGION, source_refs=("tc1",))
    no_sql = _summary(
        tool_calls=(make_tool_call(ref="tc1", sql=None, tool_name="answerWithTable"),)
    )
    inbox, _ = await _inbox(_declined(raw, summary=no_sql))

    result = await inbox.complete_parameterization(CID, entries=_HUMAN_ENTRIES)

    assert result.outcome == "declined"
    assert result.decline is not None
    assert result.decline.reason == "unrewritable_sql"


def test_a_citation_that_resolves_nowhere_is_treated_the_same_on_both_paths() -> None:
    """PARITY over the case a reader will assume is checked and is not.

    D31's evidence gate is STRUCTURAL — "did the model cite anything" — and neither
    `to_candidate` path resolves a `turn_ref` or a `tool_call_ref` against the summary.
    That is why the snapshot can carry pointers and an explicit `QUOTE_WITHHELD` marker
    without weakening anything: there was no resolution to weaken. This test states that
    plainly, so a future change that starts resolving citations against the summary has to
    confront the reconstructed summary's EMPTY transcript deliberately rather than
    discovering it as a mass of `no_evidence` declines in the review queue."""
    original = _summary()
    rebuilt = ValidationSnapshot.from_summary(original).to_summary()
    ghost_citation = blueprint_raw(source_refs=("tc1",))
    ghost_citation["evidence"] = [
        {"turn_ref": 99, "tool_call_ref": "tc-does-not-exist", "quote": "invented"}
    ]

    before = to_candidate(ghost_citation, original, known_rules=frozenset(), rule_index=None)
    after = to_candidate(ghost_citation, rebuilt, known_rules=frozenset(), rule_index=None)

    assert isinstance(before, ExtractedCandidate)
    assert isinstance(after, ExtractedCandidate)
    assert after.payload == before.payload


def test_the_reconstruction_carries_no_transcript_and_says_so() -> None:
    """The fields the snapshot deliberately drops, asserted as EMPTY rather than left
    unstated: an empty transcript is the honest shape for a value that was never stored,
    and a reconstruction that filled them with plausible content would be the one thing
    worse — a summary that looks complete and is not."""
    rebuilt = ValidationSnapshot.from_summary(_summary()).to_summary()

    assert rebuilt.turns == ()
    assert rebuilt.tool_calls == ()
    assert rebuilt.blueprint_usages == ()
    assert rebuilt.askuser_exchanges == ()
    assert rebuilt.failed_fixed_sql == ()
    assert rebuilt.scope_ref == ""


# --- FIXED: the guard `candidate/decline.py` describes is now the one it implements -------


@pytest.mark.parametrize("junk", ["tc1", 42, ["tc1"], {"tc1": "SELECT 1"}, {"tc1": []}])
def test_a_snapshot_whose_sql_map_is_unreadable_must_read_as_missing(junk: object) -> None:
    """DERIVE THE GUARD FROM THE READ. What the completion path needs from a snapshot is
    (a) the accepted SQL and (b) at least one citation. A snapshot that cannot supply the
    first is not a snapshot, whatever else survived in the document — and `None` here is
    already wired to the right behaviour end to end (`CompletionUnavailableError` → 503,
    "this cannot be checked"), which is why the fix is one condition and not a new path."""
    snapshot = ValidationSnapshot.from_doc(
        {
            "session_id": "sess-1",
            "user_id": "user-1",
            "content_hash": "hash-ratio",
            "accepted_signal": "no_correction",
            "sql_by_ref": junk,
            "evidence": [{"turn_ref": 0, "tool_call_ref": "tc1"}],
        }
    )
    assert snapshot is None


@pytest.mark.parametrize("junk", ["tc1", 42, [{"turn": 0}], [None]])
def test_a_snapshot_whose_citations_are_unreadable_must_read_as_missing(junk: object) -> None:
    snapshot = ValidationSnapshot.from_doc(
        {
            "session_id": "sess-1",
            "user_id": "user-1",
            "content_hash": "hash-ratio",
            "accepted_signal": "no_correction",
            "sql_by_ref": {"tc1": ["SELECT 1 FROM t WHERE a = 'b'"]},
            "evidence": junk,
        }
    )
    assert snapshot is None


async def test_a_damaged_snapshot_refuses_instead_of_blaming_the_models_sql() -> None:
    doc = _declined(
        blueprint_raw(parameterization=_MISSING_REGION, source_refs=("tc1",))
    ).to_doc()
    doc["revalidation"]["sql_by_ref"] = "the map did not survive the store"
    inbox, _ = await _inbox(CandidateEnvelope.from_doc(doc))

    with pytest.raises(CompletionUnavailableError):
        await inbox.complete_parameterization(CID, entries=_HUMAN_ENTRIES)
