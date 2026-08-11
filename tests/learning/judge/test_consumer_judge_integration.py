"""The consumer's KEEP path with a coverage judge wired (plan §3b).

The judge sits between triage and the extractor because that is the only place a drop
saves the extractor's call — and the extractor's call is the expensive one, carrying the
whole session transcript. This suite is about the seam, not the verdict.

Slugs:
  * J-consumer-drop-skips-extraction — a drop means the extractor is never called and
                                       no candidate is persisted.
  * J-consumer-drop-is-visible       — the drop still emits a `learning.extract` span
                                       with a machine-readable decline reason, so the
                                       loop's own telemetry shows a session that
                                       produced nothing AND why.
  * J-consumer-proceed               — a non-drop verdict changes nothing.
  * J-consumer-fail-open             — a judge that raises does not cost the session.
  * J-consumer-no-judge              — the unwired path is byte-identical to pre-slice.
  * J-consumer-stub-path             — no extractor ⇒ no judge call: there is no call to
                                       cancel, so paying to cancel nothing would drop a
                                       session on the one path that never spends money.
"""

from __future__ import annotations

from data_agent.learning.audit import InMemoryAuditStore
from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.extractor.prior_art import prior_art_query_text
from data_agent.learning.judge import CoverageJudge
from data_agent.learning.models import LearningStatus
from data_agent.learning.triage import TriageVerdict

from ..extractor.helpers import blueprint_raw, emit_extractor, make_summary, make_turn
from .helpers import card, make_judge, verdict_turn

# The payroll worked example the extractor fixtures are built around — the candidate
# must actually EXTRACT for "the judge did not stop it" to mean anything.
_SUMMARY = make_summary(turns=(make_turn(),))
_QUERY = prior_art_query_text(_SUMMARY)


class _CountingExtractor:
    """Wraps a real extractor and counts calls — the assertion is about whether the
    expensive call HAPPENED, so counting it is the whole point."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    async def extract(self, summary, verdict):
        self.calls += 1
        return await self._inner.extract(summary, verdict)


class _FakeSessionStore:
    """Minimal `SessionStore` stand-in — the consumer's `_do_work` is driven directly in
    this suite, so nothing here is exercised beyond construction."""


def _consumer(judge, extractor, *, candidates=None):
    from types import SimpleNamespace

    return LearningConsumer(
        _FakeSessionStore(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(learning_trace_verbose=False),  # type: ignore[arg-type]
        audit=InMemoryAuditStore(),
        candidates=candidates if candidates is not None else InMemoryCandidateStore(),
        extractor=extractor,  # type: ignore[arg-type]
        judge=judge,
    )


async def _drive(consumer: LearningConsumer, summary) -> None:
    """Run the consumer's real KEEP path (`_do_work`) over *summary*."""

    class _Doc:
        learning_status = LearningStatus.PROCESSING

    class _Delivered:
        job = None

    async def _loader(doc, store, *, job):
        return summary

    consumer._summary_loader = _loader  # type: ignore[assignment]
    consumer._triage = lambda s: TriageVerdict(  # type: ignore[assignment]
        decision="keep", reason="K1", target_hints=("blueprint",)
    )
    await consumer._do_work(_Doc(), _Delivered())  # type: ignore[arg-type]


def _extractor() -> _CountingExtractor:
    return _CountingExtractor(emit_extractor([blueprint_raw()]))


def _judge(turns, *, score: float = 0.85, audit=None, cards=None) -> CoverageJudge:
    judge, _client, _audit, _index = make_judge(
        turns,
        cards=cards if cards is not None else [card()],
        scores={(_QUERY, "bp::abc"): score},
        audit=audit,
    )
    return judge


async def test_a_judge_drop_skips_extraction_entirely() -> None:
    """Driven through `_do_work` — the real KEEP path — so the assertion covers the
    ordering (triage → judge → extractor) and not just the helper in isolation."""
    audit = InMemoryAuditStore()
    extractor = _extractor()
    candidates = InMemoryCandidateStore()
    consumer = _consumer(
        _judge([verdict_turn("duplicate", confidence=0.95)], audit=audit),
        extractor,
        candidates=candidates,
    )
    summary = _SUMMARY
    await _drive(consumer, summary)

    assert extractor.calls == 0
    assert await candidates.list_by_status("extracted") == []
    # The durable record is the ONLY trace of the work that did not happen.
    assert len(audit.judgements) == 1
    assert audit.judgements[0].dropped is True


async def test_a_non_drop_verdict_lets_extraction_run() -> None:
    extractor = _extractor()
    candidates = InMemoryCandidateStore()
    consumer = _consumer(
        _judge([verdict_turn("existing-plus-delta", confidence=0.99)]),
        extractor,
        candidates=candidates,
    )
    await _drive(consumer, _SUMMARY)
    assert extractor.calls == 1
    assert len(await candidates.list_by_status("extracted")) == 1


async def test_a_judge_that_raises_never_costs_the_session() -> None:
    """The judge is documented fail-open at every internal boundary; this call site is
    what makes that a guarantee rather than an intention. An unforeseen escape must
    degrade to "extract as usual", not dead-letter the session."""

    class _BoomJudge:
        async def screen_session(self, summary):
            raise RuntimeError("judge exploded")

    consumer = _consumer(_BoomJudge(), _extractor())
    assert await consumer._judged_covered(_SUMMARY) is False


async def test_no_judge_wired_is_the_pre_slice_path() -> None:
    consumer = _consumer(None, _extractor())
    assert await consumer._judged_covered(_SUMMARY) is False


async def test_the_would_extract_stub_path_never_calls_the_judge() -> None:
    """With no extractor there is no call to cancel, so paying a judge to cancel nothing
    would be pure cost — and would drop a session on the ONE path that never spends
    money anyway. `_do_work` checks for the extractor first."""
    audit = InMemoryAuditStore()
    judge = _judge([verdict_turn("duplicate", confidence=1.0)], audit=audit)
    consumer = _consumer(judge, None)
    await _drive(consumer, _SUMMARY)
    assert audit.judgements == ()
