"""Invalid-transition guard + VALID_TRANSITIONS shape (design §3, task item 8).

`VALID_TRANSITIONS` is the STATIC intent guard: an illegal edge is a PROGRAMMING
error and raises `InvalidTransitionError` BEFORE any store write — distinct from
the runtime `CASMismatchError` race guard.
"""

from __future__ import annotations

import pytest

from data_agent.learning import state_machine
from data_agent.learning.models import (
    RECOVERY_TRANSITIONS,
    VALID_TRANSITIONS,
    LearningStatus,
)
from data_agent.learning.state_machine import InvalidTransitionError
from data_agent.runtime.session.store import CASMismatchError

from .conftest import make_message


class _ExplodingStore:
    """Any store touch is a bug: the legality check must reject BEFORE the store
    is consulted."""

    async def transition_learning_status(self, *a, **kw):  # pragma: no cover
        raise AssertionError("store must not be touched on an illegal transition")


async def test_done_to_processing_is_illegal():
    """On the FORWARD helper. The consumer's recovery re-entry reaches
    `done -> processing` through `recover_to_processing` (a separate, narrow entry
    point) precisely so this stays true of the forward lifecycle."""
    with pytest.raises(InvalidTransitionError):
        await state_machine.transition(
            _ExplodingStore(), "s", LearningStatus.DONE, LearningStatus.PROCESSING, cas=0
        )


async def test_terminal_states_have_no_successors():
    assert VALID_TRANSITIONS[LearningStatus.DONE] == frozenset()
    assert VALID_TRANSITIONS[LearningStatus.DEAD_LETTER] == frozenset()


@pytest.mark.parametrize("frm,to", [
    (LearningStatus.ACTIVE, LearningStatus.QUEUED),       # skips the claim
    (LearningStatus.ACTIVE, LearningStatus.DONE),
    (LearningStatus.PENDING, LearningStatus.DONE),
    (LearningStatus.PENDING, LearningStatus.PROCESSING),
    (LearningStatus.QUEUED, LearningStatus.DONE),         # skips processing
    (LearningStatus.PROCESSING, LearningStatus.QUEUED),   # backwards
    (LearningStatus.DEAD_LETTER, LearningStatus.DONE),
])
async def test_illegal_edges_raise(frm, to):
    with pytest.raises(InvalidTransitionError):
        await state_machine.transition(_ExplodingStore(), "s", frm, to, cas=0)


@pytest.mark.parametrize("frm,to", [
    (LearningStatus.ACTIVE, LearningStatus.PENDING),
    (LearningStatus.PENDING, LearningStatus.QUEUED),
    (LearningStatus.QUEUED, LearningStatus.PROCESSING),
    (LearningStatus.QUEUED, LearningStatus.DEAD_LETTER),
    (LearningStatus.PROCESSING, LearningStatus.DONE),
    (LearningStatus.PROCESSING, LearningStatus.DEAD_LETTER),
])
async def test_legal_edges_pass_through_to_store(store, seed_session, frm, to):
    seed_session(store, "s", learning_status=frm, messages=[make_message(0, "user", "hi")])
    _, cas = await store.get_session_with_cas("s")
    await state_machine.transition(store, "s", frm, to, cas)
    assert store._docs["s"].learning_status == to


async def test_unasserted_transition_must_target_dead_letter():
    """`assert_from=False` is the escape hatch reserved for `* -> dead_letter`;
    using it for any other target is a programming error."""
    with pytest.raises(InvalidTransitionError):
        await state_machine.transition(
            _ExplodingStore(), "s", LearningStatus.QUEUED, LearningStatus.DONE,
            cas=0, assert_from=False,
        )


async def test_forward_table_is_unchanged_by_the_recovery_hatch():
    """The recovery edges live in their OWN table so the forward lifecycle keeps
    reading as one: `done` still has no forward successor, and `processing` still
    forwards only to `done`/`dead_letter`."""
    assert VALID_TRANSITIONS[LearningStatus.DONE] == frozenset()
    assert VALID_TRANSITIONS[LearningStatus.PROCESSING] == frozenset(
        {LearningStatus.DONE, LearningStatus.DEAD_LETTER}
    )
    assert RECOVERY_TRANSITIONS == {
        LearningStatus.PROCESSING: frozenset({LearningStatus.PROCESSING}),
        LearningStatus.DONE: frozenset({LearningStatus.PROCESSING}),
    }


@pytest.mark.parametrize("frm", [LearningStatus.PROCESSING, LearningStatus.DONE])
async def test_recovery_reentry_passes_through_to_store(store, seed_session, frm):
    """The two RECOVERY edges: a crashed owner's `processing` session and a `done`
    session whose content changed. Both land on `processing`."""
    seed_session(store, "s", learning_status=frm, messages=[make_message(0, "user", "hi")],
                 learning_content_hash="recorded-earlier")
    _, cas = await store.get_session_with_cas("s")
    await state_machine.recover_to_processing(store, "s", frm, cas)
    assert store._docs["s"].learning_status == LearningStatus.PROCESSING
    # Recovery records NO content hash — the hash is written once, at `done`, from
    # the doc the consumer actually loaded (MEDIUM-3).
    assert store._docs["s"].learning_content_hash == "recorded-earlier"


@pytest.mark.parametrize("frm", [
    LearningStatus.QUEUED,       # the FORWARD edge — must go through `transition`
    LearningStatus.ACTIVE,
    LearningStatus.PENDING,
    LearningStatus.DEAD_LETTER,
    "banana",                    # a status outside the machine entirely
])
async def test_recovery_refuses_every_other_from_state(frm):
    with pytest.raises(InvalidTransitionError):
        await state_machine.recover_to_processing(_ExplodingStore(), "s", frm, cas=0)


async def test_recovery_still_asserts_the_from_state(store, seed_session):
    """The re-entry keeps the store's from-assertion AND its CAS: it is a
    CAS-guarded claim, not a force-write. A session that moved under us raises
    `CASMismatchError` — the consumer's skip path."""
    seed_session(store, "s", learning_status=LearningStatus.PROCESSING,
                 messages=[make_message(0, "user", "hi")])
    _, cas = await store.get_session_with_cas("s")
    # A peer finishes the work between our read and our re-entry.
    await state_machine.transition(
        store, "s", LearningStatus.PROCESSING, LearningStatus.DONE, cas
    )
    with pytest.raises(CASMismatchError):
        await state_machine.recover_to_processing(store, "s", LearningStatus.PROCESSING, cas)


async def test_unasserted_dead_letter_is_allowed_from_any_state(store, seed_session):
    # transition #5 has NO from-assertion: a poison job can die from queued OR
    # processing. `assert_from=False` bypasses VALID_TRANSITIONS entirely.
    seed_session(store, "s", learning_status=LearningStatus.PROCESSING)
    _, cas = await store.get_session_with_cas("s")
    await state_machine.transition(
        store, "s", LearningStatus.PROCESSING, LearningStatus.DEAD_LETTER,
        cas, assert_from=False,
    )
    assert store._docs["s"].learning_status == LearningStatus.DEAD_LETTER
