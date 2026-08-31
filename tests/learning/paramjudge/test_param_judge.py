"""The parameterization judge, phase D-1 (design §D).

THE PHASE'S ONE INVARIANT: it observes. The first test below asserts that over the whole
verdict space rather than over the shadow flag, because design §D.0 says "do not implement a
discard path in D-1" and that instruction is only real if a test can fail when someone does.

Everything else here is fail-open and guard coverage. The judge exists to MEASURE a base rate
nobody has measured — the pipeline's documented failure mode is refusing good work — so a judge
that silently loses observations, or invents them, breaks the phase without breaking anything
visible.
"""

from __future__ import annotations

import asyncio

import pytest

from data_agent.learning.audit.judgement import param_judgement_ref
from data_agent.learning.audit.memory_audit_store import InMemoryAuditStore
from data_agent.learning.paramjudge import ParameterizationJudgeStage
from data_agent.learning.stage import StageContext
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .helpers import (
    BoomModelClient,
    clean_blueprint,
    make_judge,
    verdict_turn,
)


async def _run(judge, env, ctx=None):
    stage = ParameterizationJudgeStage(judge=judge)
    return await stage.process(env, ctx or StageContext(summary=None, verdict=None))


# --- the phase invariant ----------------------------------------------------


@pytest.mark.parametrize(
    "verdict,findings",
    [
        ("ok", []),
        ("revise", [{"class": "C", "criterion": "c", "note": "n"}]),
        ("revise", [{"class": "A", "criterion": "a", "note": "n", "entry_index": 1}]),
        ("reject", [{"class": "A", "criterion": "a", "note": "n"}]),
    ],
)
async def test_no_verdict_can_drop_route_or_change_the_candidate_payload(
    verdict: str, findings: list
) -> None:
    """⚠ THE D-1 GUARANTEE, asserted over the whole verdict space.

    Parameterised across every verdict INCLUDING the two that phase D-2 would act on, because
    "shadow mode is on" is not the property being claimed — "there is no discard path" is. A
    stage that grew one behind a flag would pass a test that only checked the flag.
    """
    judge, _, _ = make_judge([verdict_turn(verdict, confidence=0.99, findings=findings)])
    env = clean_blueprint()
    result = await _run(judge, env)

    assert result.control == "continue"
    # The payload is untouched — the stamp is additive and inert.
    assert result.envelope.payload == env.payload
    assert result.envelope.status == env.status


async def test_the_stamp_is_additive_and_carries_the_verdict() -> None:
    """The stamp is how the reviewer card shows the verdict, which is how the agree/disagree
    half of the measurement gets collected without asking anybody to do extra work."""
    judge, _, _ = make_judge([verdict_turn("revise")])
    result = await _run(judge, clean_blueprint())
    assert result.envelope.param_judge is not None
    assert result.envelope.param_judge.verdict == "revise"
    assert result.envelope.param_judge.has_class_a is True


# --- what it skips ----------------------------------------------------------


async def test_it_never_runs_on_a_candidate_that_already_failed_static_validation() -> None:
    """A `fail_to_review` candidate ALREADY has a deterministic complaint with a reason tag the
    writer routes on. A second, model-authored opinion about a candidate that is already going
    to a human risks the two disagreeing about why it is there — and costs a call to do it."""
    judge, audit, client = make_judge([verdict_turn()])
    result = await _run(judge, clean_blueprint(outcome="fail_to_review"))
    assert result.envelope.param_judge is None
    assert client.calls == []
    assert audit.param_judgements == ()


async def test_it_never_runs_on_a_non_blueprint() -> None:
    from dataclasses import replace

    judge, audit, client = make_judge([verdict_turn()])
    env = replace(clean_blueprint(), type="global_knowledge")
    result = await _run(judge, env)
    assert result.control == "continue"
    assert client.calls == []
    assert audit.param_judgements == ()


async def test_it_never_runs_on_a_blueprint_with_no_entries() -> None:
    judge, audit, client = make_judge([verdict_turn()])
    await _run(judge, clean_blueprint(entries=[]))
    assert client.calls == []
    assert audit.param_judgements == ()


# --- the record IS the product ---------------------------------------------


async def test_the_verdict_lands_as_a_durable_row_with_the_template_it_was_about() -> None:
    """A verdict without the artifact it judged cannot be graded a week later — the candidate
    may have been approved, rejected or mutated by a completion since. The template on the row
    is what makes the measurement possible at all."""
    judge, audit, _ = make_judge([verdict_turn("revise", confidence=0.91)])
    env = clean_blueprint()
    await _run(judge, env)

    assert len(audit.param_judgements) == 1
    row = audit.param_judgements[0]
    assert row.candidate_id == env.candidate_id
    assert row.record_type == "param_judgement"
    assert row.assessment.verdict == "revise"
    assert row.model == "test-model"
    assert "record_type" in row.template
    assert row.shadow is True


async def test_would_discard_is_true_only_for_a_serious_finding() -> None:
    """`would_discard` IS the measurement. Two conditions, both from §D.4: a non-ok verdict AND
    a Class A finding — the class that means the blueprint is WRONG rather than narrower. A
    `revise` carrying only naming nits is not a candidate anybody should destroy."""
    judge, audit, _ = make_judge(
        [
            verdict_turn("revise", findings=[{"class": "A", "criterion": "x", "note": "n"}]),
            verdict_turn("revise", findings=[{"class": "C", "criterion": "x", "note": "n"}]),
            verdict_turn("ok", feedback="", findings=[]),
        ]
    )
    from dataclasses import replace

    env = clean_blueprint()
    for i in range(3):
        await _run(judge, replace(env, candidate_id=f"candidate::h::{i}"))
    assert [r.would_discard for r in audit.param_judgements] == [True, False, False]


async def test_a_redelivery_reuses_the_recorded_verdict_instead_of_re_asking() -> None:
    """Two rows with different verdicts about the same content would corrupt the dataset the
    rollout decision is computed over — and the second call is money spent to do it."""
    judge, audit, client = make_judge([verdict_turn("revise")])
    env = clean_blueprint()
    await _run(judge, env)
    second = await _run(judge, env)

    assert len(client.calls) == 1
    assert len(audit.param_judgements) == 1
    assert second.envelope.param_judge is not None
    assert second.envelope.param_judge.verdict == "revise"
    assert audit.param_judgements[0].judgement_ref == param_judgement_ref(
        env.content_hash, env.candidate_id
    )


async def test_a_failed_record_write_does_not_take_the_candidate_down_with_it() -> None:
    """In D-1 the write is the phase's only product but NOT a precondition of anything, so a
    failure costs an observation and never a candidate. It is logged loudly because a silently
    lost row is the one way this phase can fail while everything looks healthy."""
    audit = InMemoryAuditStore(fail_param_judgements=True)
    judge, _, _ = make_judge([verdict_turn("revise")], store=audit)
    result = await _run(judge, clean_blueprint())
    assert result.control == "continue"
    assert result.envelope.param_judge is not None


async def test_a_store_that_cannot_record_at_all_still_lets_the_candidate_through() -> None:
    """An audit store predating this record family duck-types to "no writer". Degrading to a
    loud log rather than an AttributeError in a queue worker is the whole reason the call site
    uses `getattr`."""

    class OldStore:
        async def read(self, ref):  # pragma: no cover — never reached
            return None

    judge, _, _ = make_judge([verdict_turn()], store=OldStore())  # type: ignore[arg-type]
    result = await _run(judge, clean_blueprint())
    assert result.control == "continue"


# --- fail open --------------------------------------------------------------


@pytest.mark.parametrize(
    "turn",
    [
        ModelTurnResult(assistant_text="I think it's fine", tool_calls=[]),
        ModelTurnResult(tool_calls=[ToolCallRequest(id="x", name="other", arguments={})]),
        ModelTurnResult(
            tool_calls=[ToolCallRequest(id="x", name="judge_parameterization", arguments="{}")]
        ),
        verdict_turn(arguments={"verdict": "definitely-bad", "feedback": "f", "confidence": 0.9}),
        verdict_turn(arguments={"verdict": "revise", "feedback": "f", "confidence": 5.0}),
        verdict_turn(arguments={"verdict": "revise", "feedback": "f", "confidence": True}),
        verdict_turn(arguments={"verdict": "revise", "feedback": "f", "confidence": float("nan")}),
        verdict_turn(arguments={"verdict": "revise", "feedback": "f"}),
    ],
    ids=[
        "free-text",
        "wrong-tool",
        "arguments-not-an-object",
        "invented-verdict",
        "confidence-out-of-range",
        "confidence-is-a-bool",
        "confidence-is-nan",
        "confidence-missing",
    ],
)
async def test_every_malformed_response_fails_open_and_records_nothing(turn) -> None:
    """A fabricated verdict is indistinguishable in the store from one the model gave, and
    these rows ARE the dataset the ship/don't-ship decision reads. So an unusable response
    records NOTHING rather than a defaulted `ok`."""
    judge, audit, _ = make_judge([turn])
    result = await _run(judge, clean_blueprint())
    assert result.control == "continue"
    assert result.envelope.param_judge is None
    assert audit.param_judgements == ()


async def test_a_provider_explosion_fails_open() -> None:
    judge, audit, _ = make_judge([], client=BoomModelClient())
    result = await _run(judge, clean_blueprint())
    assert result.control == "continue"
    assert audit.param_judgements == ()


async def test_a_bare_base_exception_from_the_provider_does_not_escape() -> None:
    """WIDER than `Exception`, for the reason `judge/judge.py` records from QA: a provider SDK
    raising a bare `BaseException` escaped every narrower handler, killed a batch, and left
    sessions stuck at `processing` — a state only a dead-letter clears. An observer must not be
    able to do that."""

    class Weird(BaseException):
        pass

    judge, _, _ = make_judge([], client=BoomModelClient(Weird("bare")))
    result = await _run(judge, clean_blueprint())
    assert result.control == "continue"


async def test_cancellation_still_propagates() -> None:
    """Swallowing `CancelledError` would make the judge a task that cannot be cancelled, which
    is how a graceful shutdown hangs."""
    judge, _, _ = make_judge([], client=BoomModelClient(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await _run(judge, clean_blueprint())


async def test_a_timeout_fails_open() -> None:
    class SlowClient:
        async def send_turn(self, messages, tools):
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    judge, audit, _ = make_judge([], client=SlowClient())
    judge = judge.__class__(
        model_client=SlowClient(),
        audit_store=audit,
        config=judge.config.__class__(model="m", timeout_seconds=0.01),
    )
    result = await _run(judge, clean_blueprint())
    assert result.control == "continue"
    assert audit.param_judgements == ()


# --- the class down-cast ----------------------------------------------------


async def test_an_unrecognised_finding_class_reads_as_the_weakest_never_as_serious() -> None:
    """⚠ `class` is the field that would authorize destroying a candidate in phase D-2.
    Letting a model reach `A` by emitting an unrecognised value is the schema equivalent of
    asking it for permission, so every malformed severity fails toward advisory."""
    judge, audit, _ = make_judge(
        [
            verdict_turn(
                "revise",
                findings=[
                    {"class": "CRITICAL", "criterion": "x", "note": "n"},
                    {"class": "", "criterion": "y", "note": "n"},
                ],
            )
        ]
    )
    result = await _run(judge, clean_blueprint())
    assessment = result.envelope.param_judge
    assert assessment is not None
    assert [f.finding_class for f in assessment.findings] == ["C", "C"]
    assert assessment.has_class_a is False
    assert audit.param_judgements[0].would_discard is False


async def test_a_finding_pointing_past_the_entry_list_is_dropped_not_the_verdict() -> None:
    """A model miscounting a list position says nothing about whether its objection is real,
    but an index that points at nothing cannot be highlighted on a card."""
    judge, _, _ = make_judge(
        [
            verdict_turn(
                "revise",
                findings=[
                    {"class": "A", "criterion": "x", "note": "n", "entry_index": 99},
                    {"class": "A", "criterion": "y", "note": "n", "entry_index": 0},
                ],
            )
        ]
    )
    result = await _run(judge, clean_blueprint())
    assessment = result.envelope.param_judge
    assert assessment is not None
    assert [f.entry_index for f in assessment.findings] == [0]


async def test_a_revise_with_no_feedback_reads_as_ok() -> None:
    """A complaint nobody can act on is not a complaint: in phase D-2 this text IS the
    reviser's prompt, and in D-1 it would be a row saying only "something"."""
    judge, audit, _ = make_judge([verdict_turn("revise", feedback="   ")])
    result = await _run(judge, clean_blueprint())
    assert result.envelope.param_judge is not None
    assert result.envelope.param_judge.verdict == "ok"
    assert audit.param_judgements[0].would_discard is False


# --- what the model is shown ------------------------------------------------


async def test_the_brief_carries_the_accepted_sql_when_the_summary_has_one() -> None:
    """The one ceiling the judge cannot check without it: whether a literal it wants re-roled
    is even IN the original query. A slot for a predicate that was never there would require
    generating SQL, which the whole design forbids."""
    from data_agent.learning.summary.models import AnswerSql

    judge, _, client = make_judge([verdict_turn("ok", feedback="", findings=[])])
    env = clean_blueprint()

    class _Summary:
        """The two attributes `sql_by_ref` reads. Duck-typed rather than a real
        `SessionSummary`: the stage's contract with the summary IS those two, and a full
        object would hide which of its fields this path actually depends on."""

        tool_calls = ()
        answer_sqls = (
            AnswerSql(
                tool_call_ref="tc1",
                sql="SELECT sum(gross_pay) FROM payroll.payroll_fact WHERE dept = '0420'",
                blueprint_id=None,
            ),
        )

    ctx = StageContext(summary=_Summary(), verdict=None)  # type: ignore[arg-type]
    await _run(judge, env, ctx)
    assert client.calls, "the judge should still have been asked"
    brief = client.calls[0][0][-1]["content"]
    assert "TEMPLATE" in brief
    assert "PARAMETERIZATION" in brief
    # Numbered, because `entry_index` points back into this list.
    assert "[0]" in brief and "[1]" in brief
    # The accepted SQL is what makes the "is this literal even in the query?" ceiling
    # checkable at all.
    assert "THE QUERY THIS WAS GENERALIZED FROM" in brief
    assert "'0420'" in brief


async def test_a_missing_summary_weakens_the_brief_but_never_skips_the_judgement() -> None:
    """A re-queued candidate can legitimately arrive with no in-process summary. Skipping would
    bias the D-1 dataset toward the sessions that happened to still have one."""
    judge, audit, client = make_judge([verdict_turn("ok", feedback="", findings=[])])
    await _run(judge, clean_blueprint(), StageContext(summary=None, verdict=None))
    assert len(client.calls) == 1
    assert len(audit.param_judgements) == 1
