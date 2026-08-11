"""The `learning.judge` span's D25 verbose branch (added 2026-08-10, gated).

**Why this file exists at all.** This span was written to refuse a verbose parameter on
principle: the judge's `reason` is free model prose about a real session, it belongs in
the access-controlled audit bucket, and the docstring said a verbose branch would only
create the obvious place for someone to put it later. That refusal has been reversed by
deliberate operator choice, with a reason of its own — a stage that CANCELS work while
the only readable account of why lives behind separate credentials is a stage nobody
tunes. `outcome=dropped` answers "how often"; it does not answer "was that right".

So the thing under test is not "does the attribute appear". It is that the reversal is
GATED and therefore reversible, and that what the gate reveals is the actual BASIS of the
decision rather than a restatement of the verdict:

  * J-span-verbose-gated    — every entity-bearing attr is absent with the gate shut and
                              present with it open. The opt-out is the whole design.
  * J-span-verbose-basis    — what the model was SHOWN (the prior-art block, verbatim
                              from the same renderer `_ask` feeds it), what it NAMED, and
                              what it SAID. A span that showed only the verdict would
                              answer nothing the shape-only span did not.
  * J-span-verbose-nomodel  — the block appears on the FREE-GATE paths too, where no
                              model ran. `skipped_below_floor` is unreadable without it:
                              "nothing was close" and "three were close and the floor is
                              mistuned" are the same span otherwise.
  * J-span-verbose-onepost  — the span's posture is the composition root's
                              (`LEARNING_TRACE_VERBOSE`), so a session cannot end up with
                              a verbose triage span and a shape-only judge span.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.learning.judge import JudgeConfig

from .helpers import card, make_judge, make_summary, verdict_turn

_QUERY = (
    "what did Analytics earn in total? SELECT sum(AnnualSalary) AS total FROM "
    "dbpcm_warehouse.employee WHERE Department = 'Analytics'"
)
_REASON = "the Analytics headcount roll-up already computes this at the same grain"

# Every attribute the gate governs. Named once so a new verbose attribute added to
# `judge_span` without a home in this set fails the OFF test rather than shipping unseen.
_VERBOSE_ATTRS = {
    "learning.judge.reason",
    "learning.judge.covered_by",
    "learning.judge.prior_art",
}


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


def _judge_span_attrs(exporter) -> dict:
    spans = [s for s in exporter.get_finished_spans() if s.name == "learning.judge"]
    assert len(spans) == 1, f"expected one learning.judge span, got {len(spans)}"
    return dict(spans[0].attributes)


async def test_verbose_off_sets_no_entity_bearing_attr(tracer, exporter) -> None:
    """[J-span-verbose-gated] The opt-out. With the gate shut the keys are ABSENT — not
    present-and-empty — so a Phoenix filter cannot even see that a reason existed."""
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::abc", reason=_REASON, confidence=0.95)],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        tracer=tracer,
        trace_verbose=False,
    )
    await judge.screen_session(make_summary())

    attrs = _judge_span_attrs(exporter)
    leaked = set(attrs) & _VERBOSE_ATTRS
    assert not leaked, f"learning.judge leaked {leaked} with verbose OFF"
    assert not any("Analytics" in str(v) for v in attrs.values())


async def test_verbose_on_carries_the_whole_basis_of_the_verdict(tracer, exporter) -> None:
    """[J-span-verbose-basis] The reveal has to answer "why did it decide that", which
    takes four things and not one: the verdict (shape), the reason in the model's OWN
    words, the artifact it named plus that artifact's tier, and the confidence against
    the bar it was compared to."""
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::abc", reason=_REASON, confidence=0.95)],
        cards=[card()],
        scores={(_QUERY, "bp::abc"): 0.85},
        tracer=tracer,
        trace_verbose=True,
    )
    await judge.screen_session(make_summary())

    attrs = _judge_span_attrs(exporter)
    assert attrs["learning.judge.verdict"] == "duplicate"
    assert attrs["learning.judge.reason"] == _REASON
    assert attrs["learning.judge.covered_by"] == "bp::abc"
    assert attrs["learning.judge.covered_by_tier"] == "mcp"
    assert attrs["learning.judge.confidence"] == pytest.approx(0.95)
    assert attrs["learning.judge.threshold"] == pytest.approx(0.90)
    assert attrs["learning.judge.dropped"] is True


async def test_verbose_on_shows_the_cards_the_model_was_actually_fed(tracer, exporter) -> None:
    """[J-span-verbose-basis] The operator asked to see what went IN, not only what came
    out — a `duplicate` is only auditable against the block that produced it.

    Pinned against `render_prior_art_block` rather than against a hand-written string:
    the span uses the SAME renderer on the SAME lookup object that `_ask` puts in the
    prompt, which is what stops the span drifting from the prompt when either changes."""
    from data_agent.learning.extractor.prior_art import (
        PriorArtLookup,
        render_prior_art_block,
    )

    the_card = card(intent="total earnings for a department")
    judge, _client, _audit, _index = make_judge(
        [verdict_turn("duplicate", covered_by="bp::abc", reason=_REASON, confidence=0.95)],
        cards=[the_card],
        scores={(_QUERY, "bp::abc"): 0.85},
        tracer=tracer,
        trace_verbose=True,
    )
    await judge.screen_session(make_summary())

    block = _judge_span_attrs(exporter)["learning.judge.prior_art"]
    assert "bp::abc" in block
    assert "total earnings for a department" in block
    assert "tier=mcp" in block
    # Byte-identical to the renderer's own output for the same lookup.
    assert block == render_prior_art_block(
        PriorArtLookup(query=_QUERY, cards=(the_card,), available=True)
    )


async def test_the_block_is_on_the_span_even_when_no_model_ran(tracer, exporter) -> None:
    """[J-span-verbose-nomodel] `skipped_below_floor` is the judge declining to spend a
    call because nothing was close enough. Without the block that outcome is a dead end:
    an operator retuning `band_low` needs to see WHAT was below the floor and by how much,
    and there is no model reason to read because there was no model call."""
    judge, client, _audit, _index = make_judge(
        [verdict_turn()],
        cards=[card(similarity=0.20)],
        scores={(_QUERY, "bp::abc"): 0.20},
        config=JudgeConfig(band_low=0.70),
        tracer=tracer,
        trace_verbose=True,
    )
    await judge.screen_session(make_summary())

    assert client.calls_made == 0  # the free gate: no model call was paid for
    attrs = _judge_span_attrs(exporter)
    assert attrs["learning.judge.outcome"] == "skipped_below_floor"
    assert "bp::abc" in attrs["learning.judge.prior_art"]
    assert attrs["learning.judge.best_similarity"] == pytest.approx(0.20)
    assert attrs["learning.judge.threshold"] == pytest.approx(0.90)
    # No model spoke, so there is no prose to carry — the key is simply absent.
    assert "learning.judge.reason" not in attrs


@pytest.mark.parametrize("verbose", [True, False])
def test_the_factory_gives_the_judge_the_same_posture_as_every_other_span(verbose) -> None:
    """[J-span-verbose-onepost] One switch, one posture, read at the composition root.

    If the judge defaulted its own gate instead of reading `LEARNING_TRACE_VERBOSE`, a
    session would emit a verbose triage span and a shape-only judge span from the same
    setting — and an operator would reasonably conclude the judge had not run. Driven
    through the REAL factory rather than by reading its source: the failure this guards
    is a missing argument, which a source match would also catch, but a wrong SETTING
    name would not (`extra="ignore"` means a typo'd field is a silent False)."""
    from data_agent.learning.audit import InMemoryAuditStore
    from data_agent.learning.candidate import InMemoryCandidateStore
    from data_agent.learning.config import LearningSettings
    from data_agent.learning.dedup import InMemoryBlueprintCorpus
    from data_agent.learning.factory import build_learning_consumer
    from data_agent.learning.priorart import InMemoryPriorArtIndex
    from data_agent.learning.user import InMemoryUserKnowledgeStore
    from data_agent.runtime.model.scripted_client import ScriptedModelClient

    consumer = build_learning_consumer(
        LearningSettings(_env_file=None, learning_trace_verbose=verbose),
        session_store=object(),  # type: ignore[arg-type]
        queue=object(),  # type: ignore[arg-type]
        model_client=ScriptedModelClient([]),
        audit_store=InMemoryAuditStore(),
        candidate_store=InMemoryCandidateStore(),
        blueprint_corpus=InMemoryBlueprintCorpus(),
        user_store=InMemoryUserKnowledgeStore(),
        catalog_schema={},
        prior_art=InMemoryPriorArtIndex([]),
    )
    assert consumer._judge is not None
    assert consumer._judge._trace_verbose is verbose


def test_the_shipped_default_is_verbose() -> None:
    """[J-span-verbose-onepost] The operator's posture, asserted where a reader looks for
    it. `LEARNING_TRACE_VERBOSE` defaults TRUE, so the judge span ships entity-bearing and
    the `learning-loop` Phoenix project must be access-controlled like `learning_audit`.
    `_env_file=None` so a developer's local `.env` cannot make this pass or fail."""
    from data_agent.learning.config import LearningSettings

    assert LearningSettings(_env_file=None).learning_trace_verbose is True
