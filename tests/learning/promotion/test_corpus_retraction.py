"""Layer-1 — corpus retraction write-back (S9-activation Slice 3, §8.6).

The demote / reject / user-correction edges write the landed neo4j node's
`status`/`drift_status` back so the recall filter excludes a demoted blueprint; the
write-back FAILS OPEN (a corpus hiccup never blocks the authoritative store demote) and
the clean validated-rescan RE-ASSERTS the stamp (self-heal). Proven with the
`FakeLandingWriter` (records/ scripts the write-backs, no real neo4j) plus a fake driver
for the writer's own idempotent no-op.

Slugs: `S9-demote-writes-back-failopen`, `S9-retract-self-heals`.
"""

from __future__ import annotations

import logging

from data_agent.learning.candidate import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.inbox.inbox import ReviewInbox
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.promotion.landing import CorpusLandingWriter, landing_id
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient

from .helpers import (
    FakeDependencyResolver,
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

KEY = "sha256:single-bp"
_CLOCK = "2026-07-05T00:00:00+00:00"


def _scheduler(store, *, probe, writer):
    return PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=FakeDependencyResolver(),
        policy=promotion_policy(),
        landing_writer=writer,
        require_landing=True,
        clock=lambda: _CLOCK,
    )


# --- the demote edges write the landed node back (node stamped ineligible) ---------


async def test_drift_suspect_demote_writes_node_back() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True, canonical_key=KEY
    )
    await store.put(env)
    # A fan-out (row_count 10 != distinct 5) → grain teeth FAIL → suspect demote.
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=probe, writer=writer)

    await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    # The landed node was stamped ineligible (candidate + suspect) → un-recallable.
    assert [(u[1], u[2]) for u in writer.status_updates] == [("candidate", "suspect")]
    assert writer.status_updates[0][0].candidate_id == env.candidate_id


async def test_user_correction_writes_node_back() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=writer)

    await sched.apply_user_correction(env)

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE
    _env, status, drift_status = writer.status_updates[0]
    assert (status, drift_status) == ("candidate", "suspect")


async def test_reject_writes_node_back() -> None:
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=writer)

    await sched.apply_human_decision(env, "reject")

    assert (await store.get(env.candidate_id)).status == CandidateStatus.REJECTED
    _env, status, _drift = writer.status_updates[0]
    assert status == "rejected"


# --- S9-demote-writes-back-failopen ------------------------------------------------


async def test_demote_fail_open_when_write_back_raises(caplog) -> None:
    """A FAILING corpus write-back must NOT block the store demote (fail-open): the
    store still demotes to candidate + suspect, and the failure is logged loudly."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True, canonical_key=KEY
    )
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)
    writer = FakeLandingWriter(
        update_fail=RuntimeError("neo4j down"), update_fail_times=99
    )
    sched = _scheduler(store, probe=probe, writer=writer)

    with caplog.at_level(logging.WARNING, logger="data_agent.learning.promotion.scheduler"):
        sweep = await sched.run_once()

    # Store demote is source-of-truth — it SUCCEEDED despite the corpus-write failure.
    demoted = await store.get(env.candidate_id)
    assert demoted.status == CandidateStatus.CANDIDATE
    assert demoted.drift.status == "suspect"
    assert sweep.decisions[0].action == "demote"
    assert writer.update_calls == 1  # the write-back was ATTEMPTED
    assert writer.status_updates == []  # ...and it failed (nothing recorded)
    assert any(
        "corpus status write-back FAILED" in rec.message for rec in caplog.records
    )


async def test_user_correction_fail_open_when_write_back_raises() -> None:
    """The user-correction edge is equally fail-open: a corpus-write failure never
    blocks the negative-signal demote."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter(
        update_fail=RuntimeError("neo4j down"), update_fail_times=99
    )
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=writer)

    decision = await sched.apply_user_correction(env)

    assert decision.action == "demote"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


# --- S9-retract-self-heals (the clean validated-rescan re-asserts the stamp) --------


async def test_clean_rescan_re_asserts_node_stamp() -> None:
    """The self-heal: a still-validated blueprint's CLEAN rescan re-asserts the node
    stamp (`validated`/`clean`). A demote whose write-back transiently failed leaves a
    stale node; the recall filter keeps it out meanwhile, and this periodic re-assert
    repairs the stamp once the blueprint is validated + clean again."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True, canonical_key=KEY
    )
    await store.put(env)
    # row_count == distinct ⇒ grain teeth pass ⇒ clean, stays validated.
    probe = FakeWarehouseProbe(row_count=5, distinct_grain_count=5)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=probe, writer=writer)

    sweep = await sched.run_once()

    assert sweep.decisions[0].action == "drift_clean"
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    _env, status, drift_status = writer.status_updates[0]
    assert (status, drift_status) == ("validated", "clean")


# --- CorpusLandingWriter.update_status — idempotent no-op on a never-landed node ----


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, object]]:
        return self._rows


class _FakeSession:
    def __init__(self, driver: _RecordingDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run(self, query: str, **params: object) -> _FakeResult:
        self._driver.queries.append((query, params))
        return _FakeResult(self._driver.rows)


class _RecordingDriver:
    """A neo4j driver double whose session records the query + params and returns a
    scripted row set (`rows`) — `[]` models a MATCH miss (never-landed node)."""

    def __init__(self, *, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, dict[str, object]]] = []

    def session(self, **_: object) -> _FakeSession:
        return _FakeSession(self)


async def test_update_status_no_op_on_never_landed_node() -> None:
    """MATCH-by-id matches nothing (never landed) ⇒ no rows ⇒ returns False, no raise —
    retracting a never-landed node is a safe idempotent no-op."""
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    driver = _RecordingDriver(rows=[])  # MATCH miss
    writer = CorpusLandingWriter(driver, FakeEmbeddingClient(), model_id="m")

    stamped = await writer.update_status(env, status="candidate", drift_status="suspect")

    assert stamped is False
    query, params = driver.queries[0]
    assert params == {"id": landing_id(env), "status": "candidate", "drift_status": "suspect"}


async def test_update_status_idempotent_double_retract() -> None:
    """Retracting an already-retracted node is a safe repeat (same id, same SET) — the
    node is present both times, so both calls stamp True with no error."""
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    driver = _RecordingDriver(rows=[{"id": landing_id(env)}])  # node present
    writer = CorpusLandingWriter(driver, FakeEmbeddingClient(), model_id="m")

    first = await writer.update_status(env, status="candidate", drift_status="suspect")
    second = await writer.update_status(env, status="candidate", drift_status="suspect")

    assert first is True and second is True
    assert len(driver.queries) == 2


async def test_retraction_no_op_without_landing_writer() -> None:
    """The dormant Slice-1 state (`landing_writer is None`): nothing ever landed, so a
    demote has nothing to retract — `_retract_corpus` is a clean no-op (no crash)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True, canonical_key=KEY
    )
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)
    sched = PromotionScheduler(
        store,
        probe=probe,
        hit_counts=FakeHitCountReader({KEY: 5}),
        dependency_resolver=FakeDependencyResolver(),
        clock=lambda: _CLOCK,
    )

    await sched.run_once()

    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


# --- BLOCKER 1: a FAILED demote write-back converges on the NEXT cycle --------------


async def test_failed_demote_write_back_converges_next_cycle() -> None:
    """The demote write-back is fail-open, so a TRANSIENT neo4j failure at demote leaves
    the node un-stamped (still `validated`/`clean` ⇒ recallable — the coalesce-default
    recall filter does NOT catch it). The convergence re-assert in the CANDIDATE scan
    re-stamps the demoted blueprint ineligible the next cycle (once neo4j recovers), so
    a broken blueprint is never recallable indefinitely."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True, canonical_key=KEY
    )
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)  # grain fail → demote
    # The write-back fails EXACTLY ONCE (the demote cycle), then neo4j recovers.
    writer = FakeLandingWriter(update_fail=RuntimeError("neo4j down"), update_fail_times=1)
    sched = _scheduler(store, probe=probe, writer=writer)

    # Cycle 1 — demote; the write-back FAILS (fail-open) → store demotes, node UN-stamped.
    await sched.run_once()
    demoted = await store.get(env.candidate_id)
    assert demoted.status == CandidateStatus.CANDIDATE
    assert demoted.drift.status == "suspect"
    assert writer.update_calls == 1  # attempted...
    assert writer.status_updates == []  # ...and failed → the node is NOT yet stamped

    # Cycle 2 — the demoted blueprint is now in the candidate scan; the convergence
    # re-assert re-stamps it ineligible (candidate/suspect) → recall now excludes it.
    await sched.run_once()
    assert writer.update_calls == 2  # retried
    assert [(u[1], u[2]) for u in writer.status_updates] == [("candidate", "suspect")]


async def test_knowledge_user_correction_converges_via_candidate_scan() -> None:
    """UI Slice 2 knowledge convergence gap: global_knowledge now LANDS, so a
    user-correction demote of a validated chunk whose fail-open write-back TRANSIENTLY
    fails must still converge — otherwise the chunk stays recallable forever (the
    per-cycle re-assert was blueprint-only). Cycle 1's write-back fails; the CANDIDATE
    scan's demote-direction re-assert (now covering landed non-blueprint types) re-stamps
    the demoted `:KnowledgeChunk` ineligible the next cycle."""
    store = InMemoryCandidateStore()
    env = with_type(
        make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY),
        "global_knowledge",
    )
    await store.put(env)
    # The write-back fails EXACTLY ONCE (the demote), then neo4j recovers.
    writer = FakeLandingWriter(update_fail=RuntimeError("neo4j down"), update_fail_times=1)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=writer)

    # Demote via user correction; the write-back FAILS (fail-open) → store demotes,
    # the knowledge node is left UN-stamped (still validated ⇒ recallable).
    await sched.apply_user_correction(env)
    demoted = await store.get(env.candidate_id)
    assert demoted.status == CandidateStatus.CANDIDATE
    assert demoted.drift.status == "suspect"
    assert writer.update_calls == 1  # attempted...
    assert writer.status_updates == []  # ...and failed → the node is NOT yet stamped

    # Next cycle — the demoted knowledge chunk is now in the candidate scan; the
    # convergence re-assert re-stamps it ineligible (candidate) → recall excludes it.
    await sched.run_once()
    assert writer.update_calls == 2  # retried
    assert [(u[1], u[2]) for u in writer.status_updates] == [("candidate", "suspect")]
    # It never auto-promotes (human-gated) — only the corpus re-stamp fires.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.CANDIDATE


# --- BLOCKER 2: the inbox retract (leak PULL) stamps the node retired ---------------


async def test_inbox_retract_stamps_node_retired() -> None:
    """The highest-stakes human edge — pulling a LEAKED blueprint from recall — must
    stamp the landed node so recall excludes it. `ReviewInbox.retract` delegates to the
    scheduler's `apply_retract`, which stamps `status='retired'` (fail-open) before the
    store retire."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=writer)
    inbox = ReviewInbox(store, scheduler=sched)

    retired = await inbox.retract(env.candidate_id)

    assert retired.status == CandidateStatus.RETIRED
    _env, status, _drift = writer.status_updates[0]
    assert status == "retired"  # the leaked node is stamped → recall excludes it


async def test_apply_retract_non_validated_is_no_op() -> None:
    """A mis-routed retract of a non-validated env never mutates + never touches the
    corpus (idempotent no-op hold)."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.CANDIDATE, canonical_key=KEY)
    await store.put(env)
    writer = FakeLandingWriter()
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=writer)

    decision = await sched.apply_retract(env)

    assert decision.action == "hold"
    assert decision.reason == "not_validated"
    assert writer.status_updates == []  # no corpus write for a non-validated env


# --- Suggestion 3: retract-before-store ordering is load-bearing --------------------


class _OrderRecordingStore(InMemoryCandidateStore):
    """An in-memory store that appends `"store.put"` to a shared event list on every
    put — pairs with `_OrderRecordingWriter` to lock the write-back-before-store order."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def put(self, envelope: CandidateEnvelope) -> None:
        self._events.append("store.put")
        await super().put(envelope)


class _OrderRecordingWriter(FakeLandingWriter):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def update_status(
        self, env: CandidateEnvelope, *, status: str, drift_status: str
    ) -> bool:
        self._events.append("update_status")
        return await super().update_status(env, status=status, drift_status=drift_status)


async def test_drift_demote_writes_back_before_store_put() -> None:
    events: list[str] = []
    store = _OrderRecordingStore(events)
    env = make_blueprint_candidate(
        status=CandidateStatus.VALIDATED, grain_verifiable=True, canonical_key=KEY
    )
    await store.put(env)
    probe = FakeWarehouseProbe(row_count=10, distinct_grain_count=5)  # grain fail → demote
    sched = _scheduler(store, probe=probe, writer=_OrderRecordingWriter(events))

    events.clear()  # drop the setup put
    await sched.run_once()

    assert events == ["update_status", "store.put"]  # corpus write-back FIRST


async def test_user_correction_writes_back_before_store_put() -> None:
    events: list[str] = []
    store = _OrderRecordingStore(events)
    env = make_blueprint_candidate(status=CandidateStatus.VALIDATED, canonical_key=KEY)
    await store.put(env)
    sched = _scheduler(store, probe=FakeWarehouseProbe(), writer=_OrderRecordingWriter(events))

    events.clear()  # drop the setup put
    await sched.apply_user_correction(env)

    assert events == ["update_status", "store.put"]  # corpus write-back FIRST
