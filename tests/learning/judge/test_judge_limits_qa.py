"""What the judge's unit suite does NOT establish — a deliberate tripwire (plan §3b).

Read this before citing a green run of `tests/learning/judge` as evidence that the
coverage judge works.

**The unit suite proves PLUMBING. It cannot prove JUDGEMENT.** Every verdict in every
other file here is scripted: a `ScriptedModelClient` returns a `record_coverage` tool
call that a test author wrote. So the suite establishes that a verdict is parsed,
guarded, gated, recorded, honoured and made idempotent — and it establishes NOTHING
about whether a real model, shown a real session and five real prior-art cards, would
call the right one a duplicate. The one question the judge exists to answer is the one
question no test in this directory asks.

This repo has already paid for the analogous lesson twice, and both are on the record in
`docs/decisions/learning-prior-art-and-promotion-plan.md`:

  * Golden replay reported `passed=True` on templates ClickHouse rejects, because the
    fake probe never executes SQL. "Anything whose contract is 'the far system tolerates
    this' needs a live probe or an explicit tripwire admitting the gap."
  * The `period_range` slot type extracted cleanly and dead-ended at landing, and the
    unit suite was green the whole way.

The judge is the same shape of thing: its contract is "a language model's opinion about
similarity is good enough to discard an analyst's session", and no scripted double can
speak to that.

**What follows from it, concretely.** The blast radius of a wrong verdict is asymmetric
and the design leans on that, so the parts that CAN be tested hermetically are the fences
rather than the judgement:

  * a wrong KEEP costs one extraction and lands in a queue a human reads;
  * a wrong DROP is invisible — nothing downstream ever sees the work that did not
    happen. The `learning_audit` record is the only thing that makes it recoverable
    after the fact, which is why the record is a precondition of the drop and why THAT
    is heavily tested here.

**What would actually establish the judge's accuracy**, none of which is in this suite:

  1. A labelled set of real sessions with a human's own duplicate/delta/new call,
     scored against the judge. The verdict record was designed to be queryable partly so
     this set can be assembled from production rather than invented.
  2. A live run against the real corpus and a real model
     (`tests/integration/test_extractor_prefetch_live.py` is the pattern for the
     prior-art half of it).
  3. A period in SHADOW MODE (`LEARNING_JUDGE_SHADOW_MODE=true`): the judge runs, is
     asked, and records every verdict, and the drop is forced to False. Query
     `WHERE record_type='judge_verdict' AND would_drop = true` to see what it would have
     discarded, before it discards anything. Built, and deliberately NOT the default —
     the user's decision was to drop, taken with the risk stated.

     **An earlier revision of this docstring claimed a high confidence bar was the way
     to do this, and that was false.** `LEARNING_JUDGE_PRE_DROP_CONFIDENCE=1.01` is
     rejected by pydantic and by `JudgeConfig.__post_init__`; at exactly 1.0 the
     comparison is inclusive, so a model asserting perfect certainty still drops; and
     `LEARNING_JUDGE_ENABLED=false` records nothing at all. So the one safe rollout path
     was not expressible, while two documents asserted that it was. It is a real flag
     now, and `test_shadow_mode_is_a_real_flag_and_not_a_high_bar` below is what stops
     the claim drifting back into a docstring.

The tests below assert the CURRENT state of that gap so it cannot be quietly closed in a
docstring. They are the house convention for a named limitation (a plain passing
assertion about today's behaviour, not an xfail) and must not be deleted or weakened to
make a metric look better.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_agent.learning.config import LearningSettings
from data_agent.learning.judge import JudgeConfig, JudgeConfigError

from .helpers import card, make_judge, make_summary, verdict_turn

_JUDGE_SUITE = Path(__file__).parent
_INTEGRATION = Path(__file__).parents[2] / "integration"
_QUERY = "what did Analytics earn in total? SELECT sum(AnnualSalary) AS total FROM dbpcm_warehouse.employee WHERE Department = 'Analytics'"


def test_no_test_in_the_judge_suite_calls_a_real_model_is_a_known_limitation() -> None:
    """Every judge test drives a scripted double. If this ever fails because a live
    client appeared here, the module docstring above is out of date — update it rather
    than deleting the test."""
    sources = [
        path.read_text()
        for path in _JUDGE_SUITE.glob("*.py")
        if path.name != Path(__file__).name
    ]
    assert sources, "the judge suite should not be empty"
    for text in sources:
        assert "build_openai_model_client" not in text
        assert "OpenAIModelClient" not in text


def test_there_is_no_live_judge_probe_yet_is_a_known_limitation() -> None:
    """The counterpart of the tripwire above: nothing under `tests/integration` exercises
    the judge against a real model and the real corpus either, so the gap is total rather
    than merely local to this directory. When such a probe is written, this assertion
    fails and the docstring's "none of which is in this suite" must be revised."""
    live = [p.name for p in _INTEGRATION.glob("*judge*")]
    assert live == []


def test_shadow_mode_is_a_real_flag_and_not_a_high_bar() -> None:
    """Pins the correction described in the module docstring, in both directions.

    A confidence bar CANNOT express "record everything, drop nothing":
      * above 1.0 is refused at construction (a bar outside the range either drops
        unconditionally or never drops, so it is a config error, not a posture);
      * at exactly 1.0 the comparison is inclusive, so a model asserting perfect
        certainty still drops.
    Shadow mode is therefore its own flag, off by default.
    """
    with pytest.raises(JudgeConfigError):
        JudgeConfig(pre_drop_confidence=1.01)
    assert JudgeConfig(pre_drop_confidence=1.0).pre_drop_confidence == 1.0
    assert JudgeConfig().shadow is False
    assert JudgeConfig(shadow=True).shadow is True
    assert LearningSettings().learning_judge_shadow_mode is False


async def test_shadow_mode_records_the_verdict_and_discards_nothing() -> None:
    """The rollout path, end to end: the model IS asked, the row IS written, and the
    work survives. `would_drop` is the column the rollout reads."""
    judge, client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::abc", confidence=0.99)],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        config=JudgeConfig(shadow=True),
    )
    outcome = await judge.screen_session(make_summary())

    assert outcome.drop is False
    assert client.calls_made == 1
    (row,) = audit.judgements
    assert row.would_drop is True      # what it WOULD have thrown away
    assert row.dropped is False        # and what it actually did
    assert row.shadow is True          # the row is self-describing
    assert row.assessment.verdict == "duplicate"


async def test_outside_shadow_mode_would_drop_and_dropped_agree() -> None:
    """Otherwise the rollout query would silently mean two different things depending on
    when the row was written."""
    judge, _client, audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::abc", confidence=0.99)],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
    )
    await judge.screen_session(make_summary())
    (row,) = audit.judgements
    assert row.would_drop is True and row.dropped is True and row.shadow is False
