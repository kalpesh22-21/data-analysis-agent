"""Invalid-transition guard + VALID_TRANSITIONS shape (design §3, task item 8).

`VALID_TRANSITIONS` is the STATIC intent guard: an illegal edge is a PROGRAMMING
error and raises `InvalidTransitionError` BEFORE any store write — distinct from
the runtime `CASMismatchError` race guard.
"""

from __future__ import annotations

import pytest

from data_agent.learning import state_machine
from data_agent.learning.models import VALID_TRANSITIONS, LearningStatus
from data_agent.learning.state_machine import InvalidTransitionError

from .conftest import make_message


class _ExplodingStore:
    """Any store touch is a bug: the legality check must reject BEFORE the store
    is consulted."""

    async def transition_learning_status(self, *a, **kw):  # pragma: no cover
        raise AssertionError("store must not be touched on an illegal transition")


async def test_done_to_processing_is_illegal():
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
