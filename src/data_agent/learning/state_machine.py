"""The `learning_status` CAS transition helper (D96 §3).

Adds a `VALID_TRANSITIONS` legality check on top of the store's CAS + from-state assertion.
The two guards are complementary: an illegal edge is a PROGRAMMING error
(`InvalidTransitionError`), a lost CAS is an EXPECTED race (`CASMismatchError`) that callers
treat as "skip this session". Single-writer-per-session is the store's CAS; no locking here.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import RECOVERY_TRANSITIONS, VALID_TRANSITIONS, LearningStatus


class InvalidTransitionError(Exception):
    """A `to` state not reachable from `expected_from` — a programming error, not a race."""


class _TransitionableStore(Protocol):
    async def transition_learning_status(
        self,
        session_id: str,
        expected_from: str,
        to: str,
        cas: Any,
        *,
        content_hash: str | None = None,
        assert_from: bool = True,
    ) -> Any: ...


async def transition(
    store: _TransitionableStore,
    session_id: str,
    expected_from: str,
    to: str,
    cas: Any,
    *,
    content_hash: str | None = None,
    assert_from: bool = True,
) -> Any:
    """Validate the edge against `VALID_TRANSITIONS`, then run the store's CAS-guarded transition.

    *assert_from* False skips the from-state assertion, used only by the `* → dead_letter`
    edge, which a poison job can reach from either `queued` or `processing`. Returns the new
    CAS token; raises `InvalidTransitionError` for an illegal edge and propagates
    `CASMismatchError` on a lost race.
    """
    if assert_from:
        if to not in VALID_TRANSITIONS.get(expected_from, frozenset()):
            raise InvalidTransitionError(
                f"Illegal learning_status transition {expected_from!r} -> {to!r}."
            )
    elif to != LearningStatus.DEAD_LETTER:
        # The only from-unasserted transition is the dead-letter escape hatch.
        raise InvalidTransitionError(
            f"Unasserted transition to {to!r} is not permitted (only -> dead_letter)."
        )
    return await store.transition_learning_status(
        session_id,
        expected_from,
        to,
        cas,
        content_hash=content_hash,
        assert_from=assert_from,
    )


async def recover_to_processing(
    store: _TransitionableStore,
    session_id: str,
    expected_from: str,
    cas: Any,
) -> Any:
    """RE-CLAIM a session the consumer already owns the message for (`RECOVERY_TRANSITIONS`).

    Deliberately a second, narrow entry point rather than a widening of `VALID_TRANSITIONS`,
    which must stay readable as the forward ownership contract (`done` has no successor;
    nothing re-enters `processing` on the ordinary path). CAS semantics are unchanged. Raises
    `InvalidTransitionError` outside the recovery table — notably for `queued → processing`,
    which is the FORWARD edge (use `transition`).
    """
    if LearningStatus.PROCESSING not in RECOVERY_TRANSITIONS.get(
        expected_from, frozenset()
    ):
        raise InvalidTransitionError(
            f"Illegal learning_status RECOVERY {expected_from!r} -> "
            f"{LearningStatus.PROCESSING!r}."
        )
    return await store.transition_learning_status(
        session_id,
        expected_from,
        LearningStatus.PROCESSING,
        cas,
        content_hash=None,
        assert_from=True,
    )
