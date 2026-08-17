"""ADVERSARIAL: the WIRING of the fail-to-review completion plane, and the listing knob
that could hide the queue it feeds.

The composition root grew three new guards for this slice and one deliberate fail-open,
and the happy path of all four is covered only indirectly (by a fixture in
`test_inbox_service_corpus_status_wiring.py` that happens to build a real completer). What
is added here is the failure side of each, because every one of them is silent when it
misfires:

  * a completer holding a DIFFERENT candidate store than the inbox writes completed
    candidates into a store nobody lists — the split-brain the guard exists to refuse;
  * a catalog snapshot that cannot be read must disable completion (503, "not available
    here") rather than re-validate against an EMPTY catalog, which would decline every
    rule-role entry a reviewer wrote and blame them for it;
  * the frozen write-router ORDER is now assembled in one shared function with a
    blueprint-only mode, and "leakage after dedup in one process and before it in
    another" fails nothing until a leak lands;
  * and `review_score_cutoff` — a knob that hides low-scoring review rows — must not
    reach the work list, whose rows were already ruled worth doing.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    build_declined_envelope,
    mint_review_candidate_id,
)
from data_agent.learning.candidate.signals import NoveltyStamp
from data_agent.learning.config import LearningSettings
from data_agent.learning.extractor.models import Decline
from data_agent.learning.factory import (
    LearningWiringError,
    build_promotion_plane,
    build_write_router_stages,
)
from data_agent.learning.inbox import ParameterizationCompleter, ReviewInbox
from data_agent.learning.inbox.inbox import _NoOpProbe, _ZeroHitCounts
from data_agent.learning.inbox.service import _build_completer
from data_agent.learning.promotion.models import PromotionPolicy
from data_agent.learning.promotion.scheduler import PromotionScheduler
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import (
    PAYROLL_SQL,
    blueprint_raw,
    make_summary,
    make_tool_call,
    payroll_parameterization,
)

CID = mint_review_candidate_id("hash-ratio", 0)
_CATALOG = fixture_catalog()


def _settings() -> LearningSettings:
    return LearningSettings(_env_file=None)


def _plane(
    store, *, cutoff: float = 0.0, completer: ParameterizationCompleter | None = None
) -> tuple[PromotionScheduler, ReviewInbox]:
    """A minimally-wired promotion plane. The plane has its own suite; what these tests
    need from it is its POLICY and the inbox it builds over the SAME store."""
    return build_promotion_plane(
        _settings(),
        candidate_store=store,
        probe=_NoOpProbe(),
        hit_counts=_ZeroHitCounts(),
        policy=PromotionPolicy(review_score_cutoff=cutoff),
        completer=completer,
    )


def _declined_envelope(*, novelty: NoveltyStamp | None = None):
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="region = 'NA' has no entry",
            correctable=True,
            corrections_attempted=2,
            correction_history=(),
            raw_payload=blueprint_raw(
                parameterization=payroll_parameterization()[:3], source_refs=("tc1",)
            ),
        ),
        make_summary(
            tool_calls=(make_tool_call(ref="tc1", sql=PAYROLL_SQL),),
            content_hash="hash-ratio",
        ),
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
        novelty=novelty,
    )


# --- the split-store guard ----------------------------------------------------------------


async def test_an_inbox_and_a_completer_on_different_stores_is_refused_at_build() -> None:
    """The completer RE-VALIDATES the envelope this inbox read and writes the result back.
    Two stores means the completed candidate lands where nothing lists it — the same
    stranding the frozen-stage comment warns about, arriving through a different door. It
    must fail at BUILD, because at request time it looks like success."""
    inbox_store = InMemoryCandidateStore()
    other_store = InMemoryCandidateStore()

    with pytest.raises(LearningWiringError, match="ONE candidate store"):
        _plane(inbox_store, completer=ParameterizationCompleter(store=other_store))


async def test_one_store_builds_and_keeps_the_policy_cutoff() -> None:
    """The control for the guard above, plus the knob the surrounding code cares about:
    a correctly-wired completer must not cost the inbox the scheduler's policy."""
    store = InMemoryCandidateStore()

    _scheduler, inbox = _plane(
        store, cutoff=0.42, completer=ParameterizationCompleter(store=store)
    )

    assert inbox._policy.review_score_cutoff == 0.42
    assert inbox._completer is not None


# --- the frozen order, in both modes -------------------------------------------------------


def test_the_blueprint_only_pipeline_is_the_frozen_order_minus_the_target_stages() -> None:
    """`include_target_specific=False` is a STATEMENT ABOUT THE CANDIDATE, not a
    convenience — so the four stages that remain, and their relative order, are what a
    completed candidate is promised to pass. Pinned by id: a reordering here is a
    security change (leakage after dedup means a dedup decision taken over unscanned
    text) that nothing else would fail on."""
    store = InMemoryCandidateStore()
    stages = build_write_router_stages(
        _settings(),
        candidate_store=store,
        blueprint_corpus=object(),  # type: ignore[arg-type]
        catalog_schema={},
        embedder=object(),  # type: ignore[arg-type]
        include_target_specific=False,
    )

    assert [s.stage_id for s in stages] == ["generalize", "leakage", "dedup", "writer"]


def test_the_full_pipeline_keeps_the_two_target_specific_stages_in_place() -> None:
    store = InMemoryCandidateStore()

    stages = build_write_router_stages(
        _settings(),
        candidate_store=store,
        blueprint_corpus=object(),  # type: ignore[arg-type]
        catalog_schema={},
        embedder=object(),  # type: ignore[arg-type]
        user_store=object(),  # type: ignore[arg-type]
    )

    assert [s.stage_id for s in stages] == [
        "generalize",
        "leakage",
        "dedup",
        "schema_edit_writer",
        "user_knowledge_writer",
        "writer",
    ]


def test_the_full_pipeline_refuses_to_build_without_a_user_store() -> None:
    """Fail at build, loudly, with the alternative named. The S8 auto-commit stage
    commits per-user facts through that store; a pipeline assembled without one would
    drop them silently."""
    with pytest.raises(LearningWiringError, match="user-knowledge store"):
        build_write_router_stages(
            _settings(),
            candidate_store=InMemoryCandidateStore(),
            blueprint_corpus=object(),  # type: ignore[arg-type]
            catalog_schema={},
            embedder=object(),  # type: ignore[arg-type]
        )


# --- the catalog fail-open at the composition root ---------------------------------------------


class _Runtime:
    def __init__(self, path) -> None:
        self._path = path

    def catalog_fixture_file(self):
        return self._path


@pytest.mark.parametrize(
    "content",
    [
        None,  # the file does not exist
        "not json at all",
        "[]",  # valid JSON, wrong shape
        '{"no_catalog_key": 1}',
        "null",
    ],
)
def test_an_unreadable_catalog_snapshot_disables_completion_rather_than_emptying_it(
    tmp_path, content: str | None
) -> None:
    """FAIL-OPEN, deliberately, and the alternative is what makes it right: a completer
    built on an EMPTY catalog would decline every `rule`-role entry a reviewer wrote —
    telling them their rule ids do not exist when it is the deployment that cannot see
    the catalog. `None` here becomes a 503 at the route: "not available in this
    deployment", which is true."""
    path = tmp_path / "catalog_export.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    completer = _build_completer(
        _settings(),
        _Runtime(path),
        candidate_store=InMemoryCandidateStore(),
        corpus=object(),
        embedding_client=object(),
    )

    assert completer is None


def test_a_readable_catalog_grounds_the_completer_in_the_same_rules_as_the_extractor(
    tmp_path,
) -> None:
    """The control, and the property that matters most about this function: the
    completer's `known_rules` must be the SAME grounding the extraction was judged
    against, or the review queue and the loop disagree about which rules the deployment
    has. Built from the same frozen export the extractor reads."""
    from data_agent.learning.extractor.grounding import known_rule_ids_from_catalog

    path = tmp_path / "catalog_export.json"
    path.write_text(json.dumps({"catalog": _CATALOG}), encoding="utf-8")
    store = InMemoryCandidateStore()

    completer = _build_completer(
        _settings(),
        _Runtime(path),
        candidate_store=store,
        corpus=object(),
        embedding_client=object(),
    )

    assert completer is not None
    assert completer.known_rules == known_rule_ids_from_catalog(_CATALOG)
    assert completer.known_rules  # the fixture really does declare rules
    assert completer.store is store
    assert [s.stage_id for s in completer.stages] == [
        "generalize",
        "leakage",
        "dedup",
        "writer",
    ]


# --- the cutoff must not reach the work list ------------------------------------------------------


async def test_the_review_score_cutoff_can_never_hide_a_form() -> None:
    """`needs_parameterization` is a WORK list, not a judgement queue: its rows were
    already ruled worth extracting, so a knob that hides low-scoring REVIEW rows must not
    touch it. The row below carries a MEASURED, near-zero novelty — precisely the shape
    the cutoff removes from `in_review` — and it must still be listed."""
    store = InMemoryCandidateStore()
    await store.put(
        _declined_envelope(
            novelty=NoveltyStamp(novelty=0.0, measured=True, compared_against=12)
        )
    )
    inbox = ReviewInbox(
        store,
        scheduler=_plane(store, cutoff=0.9)[0],
    )

    forms = await inbox.list(status=CandidateStatus.NEEDS_PARAMETERIZATION)

    assert [item.candidate_id for item in forms] == [CID]
    assert forms[0].reason == "needs_parameterization"


async def test_the_same_cutoff_still_trims_the_review_queue() -> None:
    """The control that keeps the test above honest: with the same knob and the same
    stamp, an `in_review` row IS hidden. If this ever stops being true, the test above
    proves nothing."""
    store = InMemoryCandidateStore()
    await store.put(
        replace(
            _declined_envelope(
                novelty=NoveltyStamp(novelty=0.0, measured=True, compared_against=12)
            ),
            status=CandidateStatus.IN_REVIEW,
            decline=None,
        )
    )
    inbox = ReviewInbox(
        store,
        scheduler=_plane(store, cutoff=0.9)[0],
    )

    assert await inbox.list(status=CandidateStatus.IN_REVIEW) == []
