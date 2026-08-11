"""The verdict as a RECORD (plan §3b) — the doc shape, and the span that counts it.

The typed verdict is the most important thing this slice produces, and not because of
the branch it drives. It is the dataset that decides whether atomic composable
blueprints are worth building at all: a skew to `existing-plus-delta` means the loop
keeps re-deriving near-misses of what it owns and composition pays off; a skew to
`duplicate` means RECALL is failing; a skew to `new` means the corpus is simply young.
Three different projects hang off which one it is, so the field has to survive as a
queryable row and not just as an `if`.

This suite pins the two halves of that:

  * the DOC — flat scalars, a `record_type` discriminator, the threshold that was in
    force, and a normalizing rehydrate (`learning_audit` is a store humans edit with
    `cbq`, and this doc feeds straight back into a drop decision);
  * the SPAN — the DENOMINATOR. The store deliberately holds only verdicts a judge
    actually gave, so the several reasons a session was NOT judged exist only in
    telemetry, and "drops on the span" vs "drops in the bucket" is the cross-check that
    would catch a drop happening without a record.

Slugs:
  * J-record-queryable        — every field the headline queries need, flat.
  * J-record-normalizes       — a hand-edited doc degrades, never raises.
  * J-record-verdict-unknown  — an unrecognized stored verdict can never drop.
  * J-span-shape-only         — no `reason` on the span; it is entity-bearing prose.
  * J-span-denominator        — the skip reasons are counted where the store cannot.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.audit.judgement import (
    COVERAGE_VERDICTS,
    DROPPABLE_VERDICT,
    JUDGE_RECORD_TYPE,
    CoverageAssessment,
    JudgeRecord,
    judgement_fingerprint,
    post_extraction_ref,
    pre_extraction_ref,
)

from .helpers import card, make_judge, make_summary, verdict_turn

_QUERY = "what did Analytics earn in total? SELECT sum(AnnualSalary) AS total FROM dbpcm_warehouse.employee WHERE Department = 'Analytics'"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    exp._provider = provider  # keep the provider alive for the test
    return exp


@pytest.fixture
def tracer(exporter):
    return exporter._provider.get_tracer("learning-loop-test")


def _record(**overrides) -> JudgeRecord:
    base = {
        "judgement_ref": "judgement::pre::hash-1",
        "stage": "pre_extraction",
        "session_id": "sess-1",
        "content_hash": "hash-1",
        "trace_id": "trace-1",
        "assessment": CoverageAssessment(
            verdict="duplicate",
            covered_by="bp::abc",
            covered_by_tier="mcp",
            reason="same total at the same grain",
            confidence=0.93,
        ),
        "outcome": "dropped",
        "threshold": 0.90,
        "best_similarity": 0.85,
        "cards_shown": 3,
        "model": "tiny-judge-1",
        "judged_at": "2026-08-10T12:00:00+00:00",
        "covered_by_known": True,
    }
    base.update(overrides)
    return JudgeRecord(**base)  # type: ignore[arg-type]


# --- the three verdicts are a closed, meaningful set -------------------------------


def test_the_vocabulary_distinguishes_the_three_projects_it_has_to() -> None:
    """A `covered: bool` would have collapsed `duplicate` and `existing-plus-delta`,
    which are the two that matter — the first says retrieval is failing, the second says
    composition would pay off."""
    assert COVERAGE_VERDICTS == ("duplicate", "existing-plus-delta", "new")


def test_only_one_verdict_may_ever_cancel_work() -> None:
    assert DROPPABLE_VERDICT == "duplicate"


def test_the_two_stages_key_differently_and_deterministically() -> None:
    assert pre_extraction_ref("h1") == pre_extraction_ref("h1")
    assert pre_extraction_ref("h1") != pre_extraction_ref("h2")
    assert pre_extraction_ref("h1") != post_extraction_ref("h1")


def test_the_key_binds_content_and_not_position() -> None:
    """The blocker this slice was sent back for. The post-extraction key was
    `candidate_id` = `candidate::<content_hash>::<ordinal>`, and
    `consumer.py::_run_extractor` says in its own comment that a re-extraction can emit
    "a different count/order" — so candidate A landing at ordinal 0 on a redelivery
    would read candidate B's stored verdict, and the audit `reason` would describe B.

    The fingerprint is over what the judge was SHOWN, so different content is a
    different key and a cache MISS (one small model call), never a false hit."""
    assert judgement_fingerprint("post", "h1", "brief A") == judgement_fingerprint(
        "post", "h1", "brief A"
    )
    assert judgement_fingerprint("post", "h1", "brief A") != judgement_fingerprint(
        "post", "h1", "brief B"
    )
    # The STAGE is an input, so the two namespaces cannot collide even on an identical
    # brief — belt and braces alongside the distinct key prefixes.
    assert judgement_fingerprint("pre", "h1", "b") != judgement_fingerprint(
        "post", "h1", "b"
    )
    # Length-prefixed, so a boundary shift cannot collide: ("ab","c") vs ("a","bc").
    assert judgement_fingerprint("ab", "c") != judgement_fingerprint("a", "bc")


# --- the doc -----------------------------------------------------------------------


def test_the_doc_carries_everything_the_headline_queries_need() -> None:
    doc = _record().to_doc()
    # `WHERE record_type = 'judge_verdict'` — evidence snapshots carry no such key at
    # all, so the predicate is exact rather than merely selective.
    assert doc["record_type"] == JUDGE_RECORD_TYPE
    # "we dropped N candidates last quarter"
    assert doc["dropped"] is True
    assert doc["judged_at"] == "2026-08-10T12:00:00+00:00"
    # "and how do the verdicts distribute?" — flat, so `GROUP BY verdict` is one term.
    assert doc["verdict"] == "duplicate"
    assert "assessment" not in doc
    # "would they still drop at 0.95?" — unanswerable without the bar of the day.
    assert doc["threshold"] == 0.90
    assert doc["best_similarity"] == 0.85
    # "which judge said so?" — the model changes underneath the dataset.
    assert doc["model"] == "tiny-judge-1"


async def test_the_row_records_the_score_of_the_artifact_the_verdict_names() -> None:
    """RAISED BY QA, NOW FIXED. The band gates on the BEST card, but the drop is
    authorized by whichever card the model NAMES — so a drop could be taken on an
    artifact scoring 0.05 while the row reported an unrelated 0.85. The row stayed
    auditable (`covered_by` always named the real artifact) but the number stored beside
    the decision was not the number behind it, which is exactly what a later threshold
    retune would be reasoned from."""
    near = card("bp::near", intent="totals by department", similarity=0.85)
    far = card("bp::far", intent="headcount by office", similarity=0.05)
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::far", confidence=0.99)],
        cards=[near, far],
        scores={(_QUERY, "bp::near"): 0.85, (_QUERY, "bp::far"): 0.05},
    )
    await judge.screen_session(make_summary())

    (row,) = audit.judgements
    assert row.best_similarity == pytest.approx(0.85)          # what opened the band
    assert row.authorizing_similarity == pytest.approx(0.05)   # what the verdict named
    assert row.assessment.covered_by == "bp::far"


def test_the_doc_round_trips() -> None:
    record = _record()
    assert JudgeRecord.from_doc(record.to_doc()) == record


def test_a_hand_edited_doc_degrades_rather_than_raising() -> None:
    """`learning_audit` is a store a human can edit with `cbq`, and this doc feeds the
    read-through path that decides whether to re-run the model. A crash there is a
    dead-lettered session; a coerced value is a re-judgement.

    Note what is coerced and what is not: everything HERE is only ever read, so a junk
    value must not throw away a usable verdict. The verdict itself is the exception —
    see the test below."""
    junk = {
        "verdict": "duplicate",
        "covered_by": 7,
        "covered_by_tier": None,
        "reason": {"a": 1},
        "confidence": "high",
        "threshold": True,
        "cards_shown": "three",
        "stage": "sideways",
        "outcome": "exploded",
        "candidate_id": 5,
        "covered_by_known": "yes",
        "covered_by_origin": 3,
        "fingerprint": 9,
        "would_drop": "sure",
        "shadow": 1,
    }
    record = JudgeRecord.from_doc(junk)
    assert record is not None
    assert record.assessment.covered_by == ""
    assert record.assessment.reason == ""
    assert record.assessment.confidence == 0.0
    # `True` is an `int` subclass and would otherwise read back as a threshold of 1.0.
    assert record.threshold == 0.0
    assert record.cards_shown == 0
    assert record.stage == "pre_extraction"
    assert record.outcome == "proceeded"
    assert record.candidate_id is None
    # A string that is not the literal `True` must not read as "the id was known".
    assert record.covered_by_known is False
    assert record.covered_by_origin == ""
    # A corrupt fingerprint can never equal a real one, so the reuse path re-judges.
    assert record.fingerprint == ""
    assert record.would_drop is False
    assert record.shadow is False


def test_an_unrecognized_stored_verdict_is_refused_not_coerced() -> None:
    """The second thing the review caught. An earlier cut coerced an unknown stored
    verdict to `new` — INSIDE the vocabulary — and the gate then refused it only by
    accident (it tests equality against `duplicate`, not membership). Worse, `_judge`
    re-persists a reused assessment, so a corrupted row would have been rewritten as a
    `new` no model ever gave, permanently replacing the original under the same key.
    That breaks this module's own never-fabricate rule at the one place it matters.

    `None` propagates to `read_judgement` as "no record on file"; the judge re-asks."""
    assert JudgeRecord.from_doc({"verdict": "definitely-a-duplicate", "confidence": 1.0}) is None
    assert CoverageAssessment.from_doc({"verdict": None}) is None
    assert CoverageAssessment.from_doc({}) is None
    # And the one droppable verdict still rehydrates, so the refusal is not a blanket.
    kept = CoverageAssessment.from_doc({"verdict": DROPPABLE_VERDICT, "confidence": 0.5})
    assert kept is not None and kept.verdict == DROPPABLE_VERDICT


async def test_a_corrupted_stored_verdict_causes_a_re_judgement_not_a_rewrite() -> None:
    """The end-to-end consequence of the test above: the loop asks the model again and
    the store ends up holding a real verdict, never a fabricated one."""
    audit = InMemoryAuditStore()
    judge, client, _a, _i = make_judge(
        [verdict_turn("existing-plus-delta", covered_by="bp::abc", confidence=0.6)],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        audit=audit,
    )
    # Hand-write a corrupt row at the key this session will use.
    summary = make_summary()
    fingerprint = judgement_fingerprint("pre_extraction", summary.content_hash, "x")
    audit._judgements[pre_extraction_ref(fingerprint)] = _record(
        judgement_ref=pre_extraction_ref(fingerprint)
    )

    await judge.screen_session(summary)
    assert client.calls_made == 1
    assert all(r.assessment.verdict in COVERAGE_VERDICTS for r in audit.judgements)


# --- the span ----------------------------------------------------------------------


async def test_the_span_carries_the_shape_and_never_the_reason(tracer, exporter) -> None:
    """`reason` is free model prose about a real session and can name a department or a
    person. It goes to the access-controlled audit bucket with the evidence quotes — the
    span has no verbose branch at all, deliberately, so there is no obvious place for
    someone to add it later."""
    judge, _client, _audit, _index = make_judge(
        [
            verdict_turn(
                "duplicate",
                covered_by="bp::abc",
                reason="Analytics department earnings, identical grain",
                confidence=0.95,
            )
        ],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        tracer=tracer,
    )
    await judge.screen_session(make_summary())

    spans = [s for s in exporter.get_finished_spans() if s.name == "learning.judge"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["learning.judge.stage"] == "pre_extraction"
    assert attrs["learning.judge.outcome"] == "dropped"
    assert attrs["learning.judge.verdict"] == "duplicate"
    assert attrs["learning.judge.dropped"] is True
    assert attrs["learning.judge.covered_by_tier"] == "mcp"
    assert attrs["learning.judge.confidence"] == pytest.approx(0.95)
    assert attrs["learning.judge.cards_shown"] == 1
    assert attrs["learning.judge.reused"] is False
    assert not any("Analytics" in str(v) for v in attrs.values())
    assert not any("reason" in k for k in attrs)


async def test_the_span_is_the_denominator_the_store_cannot_be(tracer, exporter) -> None:
    """A session the judge declined to judge records NOTHING (a fabricated verdict would
    poison the dataset) — so the only place "we skipped 4000 sessions because the graph
    was down" exists is here."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn()], cards=[card()], index_fails=True, tracer=tracer
    )
    await judge.screen_session(make_summary())

    assert audit.judgements == ()
    span = next(s for s in exporter.get_finished_spans() if s.name == "learning.judge")
    assert dict(span.attributes)["learning.judge.outcome"] == "skipped_unavailable"


async def test_a_reused_verdict_is_marked_as_such_on_the_span(tracer, exporter) -> None:
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", confidence=0.95)],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        tracer=tracer,
    )
    await judge.screen_session(make_summary())
    await judge.screen_session(make_summary())

    spans = [s for s in exporter.get_finished_spans() if s.name == "learning.judge"]
    assert [dict(s.attributes)["learning.judge.reused"] for s in spans] == [False, True]
