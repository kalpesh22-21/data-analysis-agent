"""Adversarial QA on the landing hold TAXONOMY, driven by the payloads actually stuck.

`test_land_then_status.py` proves the split works for the payload shape someone LOOKED at
(`{definition, fact_type, intent, scope}`), scripted exceptions for the rest. The stuck
rows are not all that shape: the live queue also holds `{intent, knowledge}` and
`{intent, knowledge_update}`, and neither was ever put through the real mapper here. A
taxonomy that classified one stuck shape and mis-classified the next would be the same
defect it was built to fix, one payload later.

So every scheduler test below drives the REAL `CorpusLandingWriter` over the REAL
`knowledge_seed_from_candidate` — no scripted `ValueError` — and the inbox tests drive the
REAL scheduler through the HTTP surface. The scripted-exception tests upstairs pin the
`except` clauses; these pin that the exceptions the shipped code actually raises land in
them.

Also pinned here, because they are the two ways the split can rot:

  * RETRY STABILITY. `landing_invalid` claims "a retry cannot help". A second approve that
    answered something else would make the claim false and the 409 misleading.
  * THE LEAK REASON IS NOT SWALLOWED. `landing_entity_leak` gets no inbox branch by
    design (it falls to the 409-verbatim default so the reviewer reads the tripwire's
    name). That is only correct while the reason SURVIVES to the response body.

Slug: PA-landing-taxonomy-adversarial.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.promotion.landing import CorpusLandingWriter
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

from .helpers import (
    FakeHitCountReader,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

KEY = "sha256:single-bp"
TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
REVIEWER_TRIAL_TOKEN = "reviewer-pasted-token"


class _NoSessionDriver:
    """A neo4j driver double that RAISES if `session()` is opened.

    Load-bearing rather than decorative: it proves the deterministic refusal happens at the
    MAP, before any embedding call or neo4j write is spent on a payload that can never
    land. A test that only checked the reason would still pass if the writer burned an
    embed on every retry forever.
    """

    def session(self, **_: object) -> object:
        raise AssertionError("neo4j must not be opened for a payload that cannot be mapped")


# --- the payload shapes sitting in the live `in_review` queue ----------------------
#
# Named, not inlined, because each is a distinct historical shape and a test failure
# should say WHICH one regressed.

STUCK_DEFINITION = {
    "definition": "an active employee is one with no termination date",
    "fact_type": "business_rule",
    "intent": "define active employee",
    "scope": "employee",
}
STUCK_KNOWLEDGE = {
    "intent": "define active employee",
    "knowledge": "an active employee is one with no termination date",
}
STUCK_KNOWLEDGE_UPDATE = {
    "intent": "revise the active-employee definition",
    "knowledge_update": "an active employee is one with no termination date",
}

_ALL_STUCK = [
    pytest.param(STUCK_DEFINITION, id="definition-fact_type-intent-scope"),
    pytest.param(STUCK_KNOWLEDGE, id="intent-knowledge"),
    pytest.param(STUCK_KNOWLEDGE_UPDATE, id="intent-knowledge_update"),
]


def _knowledge_env(payload: dict) -> CandidateEnvelope:
    """An `in_review` `global_knowledge` candidate carrying *payload* verbatim — the way
    a pre-validation-era row sits in the store today."""
    return replace(
        with_type(
            make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY),
            "global_knowledge",
        ),
        payload=dict(payload),
    )


def _real_writer(embedder: FakeEmbeddingClient) -> CorpusLandingWriter:
    return CorpusLandingWriter(_NoSessionDriver(), embedder, model_id="all-mpnet-base-v2")


def _scheduler(store, *, writer) -> PromotionScheduler:
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader({KEY: 5}),
        policy=promotion_policy(),
        landing_writer=writer,
        require_landing=True,
        clock=lambda: "2026-09-01T00:00:00+00:00",
    )


# --- the scheduler, over the real mapper -------------------------------------------


@pytest.mark.parametrize("payload", _ALL_STUCK)
async def test_every_stuck_payload_shape_holds_landing_invalid(payload) -> None:
    """The generalization of the builder's single-shape test. What these three have in
    common is the only thing the taxonomy may depend on: NO `statement`. The reason must
    not be a function of which off-contract keys happen to be present, because the next
    stuck row will carry a fourth set of names."""
    store = InMemoryCandidateStore()
    env = _knowledge_env(payload)
    await store.put(env)
    embedder = FakeEmbeddingClient()
    sched = _scheduler(store, writer=_real_writer(embedder))

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.action == "hold"
    assert decision.reason == "landing_invalid"
    # Not landed ⇒ not validated, and no embed spent on the way to finding out.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW
    assert embedder.calls == []


@pytest.mark.parametrize("payload", _ALL_STUCK)
async def test_the_hold_is_stable_across_retries(payload) -> None:
    """`landing_invalid` asserts "a retry re-reads the same stored payload and fails
    identically, forever" — and the 409 tells a reviewer to stop retrying on the strength
    of it. If the second approve answered anything else, the advice would be wrong."""
    store = InMemoryCandidateStore()
    env = _knowledge_env(payload)
    await store.put(env)
    sched = _scheduler(store, writer=_real_writer(FakeEmbeddingClient()))

    first = await sched.apply_human_decision(env, "approve")
    # Re-read from the store, the way the second request would.
    again = await sched.apply_human_decision(await store.get(env.candidate_id), "approve")

    assert first.reason == again.reason == "landing_invalid"
    assert first.to_status == again.to_status == CandidateStatus.IN_REVIEW


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty"),
        pytest.param({"statement": ""}, id="blank-statement"),
        pytest.param({"statement": "   "}, id="whitespace-statement"),
        pytest.param({"statement": 42}, id="numeric-statement"),
        pytest.param({"statement": ["a fact"]}, id="list-statement"),
        pytest.param({"statement": None, "definition": "a fact"}, id="null-statement"),
    ],
)
async def test_a_present_but_unusable_statement_also_holds_landing_invalid(payload) -> None:
    """Not just the MISSING key. The mapper's guard is `isinstance(str) and .strip()`, so
    every one of these raises the same `ValueError` — and a taxonomy that only recognised
    the absent case would send half of them back to the 503 that cannot terminate."""
    store = InMemoryCandidateStore()
    env = _knowledge_env(payload)
    await store.put(env)
    sched = _scheduler(store, writer=_real_writer(FakeEmbeddingClient()))

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.reason == "landing_invalid"


async def test_a_repaired_definition_payload_gets_past_the_map() -> None:
    """The repair script's output, end to end through the edge that was jammed.

    `scripts/fix_global_knowledge_payload_keys.py` exists to make these rows APPROVABLE,
    and the only proof of that is the approve. `_NoSessionDriver` means this cannot go
    all the way to a write — so what is asserted is exactly the boundary that moved: the
    run no longer stops at `landing_invalid`, it reaches the embed (a stage the malformed
    payload never got to) and then fails on the absent neo4j, which is honest infra.
    """
    store = InMemoryCandidateStore()
    repaired = {
        "statement": "an active employee is one with no termination date",
        "knowledge_type": "business_rule",
        "scope": "employee",
    }
    env = _knowledge_env(repaired)
    await store.put(env)
    embedder = FakeEmbeddingClient()
    sched = _scheduler(store, writer=_real_writer(embedder))

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.reason == "landing_failed"  # infra, i.e. the honest remaining gap
    assert embedder.calls  # the map SUCCEEDED — this is what the repair bought
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_a_repaired_knowledge_keyed_payload_gets_past_the_map() -> None:
    """FINDING F1, now FIXED, stated at the edge that hurts.

    `knowledge`/`knowledge_update` are statement sources of `rewrite_payload`, so the
    `{intent, knowledge}` row the live store actually holds comes out of the script
    REPAIRED — and its approve then gets past the map: the run reaches the embed (a
    stage the stuck payload never got to) and fails only on the absent neo4j, which is
    honest infra, exactly like the `definition` shape above.

    Pinned HERE, not only in the script's unit tests, because this is where an operator
    finds out: they run the dry-run, read the report, apply, and the queue must move.
    """
    import importlib.util
    import sys
    from pathlib import Path

    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "fix_global_knowledge_payload_keys.py"
    )
    spec = importlib.util.spec_from_file_location("_repair_at_the_edge", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_repair_at_the_edge"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("_repair_at_the_edge", None)

    repaired = module.rewrite_payload(dict(STUCK_KNOWLEDGE))
    assert repaired == {"statement": STUCK_KNOWLEDGE["knowledge"]}

    store = InMemoryCandidateStore()
    env = _knowledge_env(repaired)  # i.e. what the script leaves behind
    await store.put(env)
    embedder = FakeEmbeddingClient()
    sched = _scheduler(store, writer=_real_writer(embedder))

    decision = await sched.apply_human_decision(env, "approve")

    assert decision.reason == "landing_failed"  # infra, not the map — the repair worked
    assert embedder.calls  # the map SUCCEEDED
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


# --- the inbox, over the real scheduler + the real mapper --------------------------


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _client_over(payload: dict) -> tuple[TestClient, CandidateEnvelope, InMemoryCandidateStore]:
    store = InMemoryCandidateStore()
    env = _knowledge_env(payload)

    async def _put() -> None:
        await store.put(env)

    asyncio.run(_put())
    sched = _scheduler(store, writer=_real_writer(FakeEmbeddingClient()))
    app = create_inbox_app(inbox=ReviewInbox(store, scheduler=sched), write_plane="full")
    return TestClient(app), env, store


@pytest.mark.parametrize("payload", _ALL_STUCK)
def test_approving_a_stuck_candidate_answers_409_through_the_real_stack(
    enabled: None, payload
) -> None:
    """The builder's inbox test scripts the writer's failure. This one does not: the
    request goes through the real scheduler, the real `CorpusLandingWriter` and the real
    `knowledge_seed_from_candidate`, which is the sequence that answered 503 in
    production. Every stuck shape must now answer 409 with an actionable instruction."""
    client, env, store = _client_over(payload)

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve", json={"token": REVIEWER_TRIAL_TOKEN}, headers=AUTH
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "candidate payload cannot be landed (malformed for its type); repair it "
        "in the store or reject it — approving again will not help"
    )

    async def _status() -> str:
        return (await store.get(env.candidate_id)).status

    assert asyncio.run(_status()) == CandidateStatus.IN_REVIEW


@pytest.mark.parametrize("payload", _ALL_STUCK)
def test_the_409_body_never_echoes_the_unscanned_payload_text(enabled: None, payload) -> None:
    """The off-contract keys are precisely the text the S5 gate never read. The error
    body is the one surface that could publish it verbatim, so the mapping's fixed string
    is a safety property, not just tidiness."""
    client, env, _store = _client_over(payload)

    body = client.post(
        f"/inbox/{env.candidate_id}/approve", json={"token": REVIEWER_TRIAL_TOKEN}, headers=AUTH
    ).text

    for value in payload.values():
        assert value not in body


@pytest.mark.parametrize("payload", _ALL_STUCK)
def test_a_second_approve_answers_409_again_not_503(enabled: None, payload) -> None:
    """The behaviour some reviewer will produce anyway, whatever the detail says: they
    click again. The answer must stay the same one — an oscillation between 409 and 503
    is how the original defect stayed invisible for so long."""
    client, env, _store = _client_over(payload)

    codes = [
        client.post(
            f"/inbox/{env.candidate_id}/approve",
            json={"token": REVIEWER_TRIAL_TOKEN},
            headers=AUTH,
        ).status_code
        for _ in range(3)
    ]

    assert codes == [409, 409, 409]


def test_a_genuinely_unavailable_landing_plane_still_answers_503(enabled: None) -> None:
    """The branch the new one sits in front of, re-asserted from the OTHER side: the 409
    must not have captured the transient case. A well-formed payload whose landing fails
    on infra is still "landing plane unavailable", which is the one situation where
    "retry" is correct advice."""
    store = InMemoryCandidateStore()
    env = _knowledge_env({"statement": "an active employee has no termination date"})

    async def _put() -> None:
        await store.put(env)

    asyncio.run(_put())

    from .helpers import FakeLandingWriter

    sched = _scheduler(
        store, writer=FakeLandingWriter(fail=RuntimeError("neo4j down"), fail_times=99)
    )
    client = TestClient(
        create_inbox_app(inbox=ReviewInbox(store, scheduler=sched), write_plane="full")
    )

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve", json={"token": REVIEWER_TRIAL_TOKEN}, headers=AUTH
    )

    assert resp.status_code == 503


def test_an_entity_leak_answers_409_and_names_the_tripwire(enabled: None) -> None:
    """`landing_entity_leak` deliberately gets no branch of its own: it falls through to
    the 409-verbatim default so the reason reaches the reviewer and the log by name.
    That is only true while the reason SURVIVES into the body — and it must not be
    swallowed by the `landing_invalid` branch that now sits above it, nor mistaken for
    the payload-malformed message, because the two ask for opposite actions (edit the
    payload vs. do not touch it, the strip regressed)."""
    store = InMemoryCandidateStore()
    env = _knowledge_env({"statement": "an active employee has no termination date"})

    async def _put() -> None:
        await store.put(env)

    asyncio.run(_put())

    from data_agent.learning.promotion.landing import LandingEntityError

    from .helpers import FakeLandingWriter

    sched = _scheduler(
        store,
        writer=FakeLandingWriter(fail=LandingEntityError("entity leaked"), fail_times=99),
    )
    client = TestClient(
        create_inbox_app(inbox=ReviewInbox(store, scheduler=sched), write_plane="full")
    )

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve", json={"token": REVIEWER_TRIAL_TOKEN}, headers=AUTH
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "landing_entity_leak" in detail
    # NOT the payload-malformed instruction: telling a reviewer to repair their way out
    # of a strip regression sends them after a defect that is not in the candidate.
    assert "malformed for its type" not in detail
