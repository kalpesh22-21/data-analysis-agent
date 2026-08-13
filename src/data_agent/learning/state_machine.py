"""The `learning_status` CAS transition helper (D96 §3).

Thin wrapper over `SessionStore.transition_learning_status` that adds the
`VALID_TRANSITIONS` legality check ON TOP of the store's CAS + from-state
assertion. The two guards are complementary:

  - `VALID_TRANSITIONS` is a STATIC assertion about the caller's intent (the
    sweeper/consumer never asks for an illegal edge like `active → done`); a
    violation is a PROGRAMMING error and raises `InvalidTransitionError`.
  - the store's CAS + from-state assertion is a RUNTIME race guard (a peer won,
    or the session was resumed); a violation is EXPECTED under concurrency and
    raises `CASMismatchError`, which callers treat as "skip this session".

The single-writer-per-session guarantee (D96) is the store's CAS; this helper
does not add locking.
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import RECOVERY_TRANSITIONS, VALID_TRANSITIONS, LearningStatus


class InvalidTransitionError(Exception):
    """Raised when a caller requests a `to` state not reachable from
    `expected_from` per `VALID_TRANSITIONS` (a programming error, not a race)."""


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
    """Validate the edge against `VALID_TRANSITIONS`, then perform the store's
    CAS-guarded transition (asserting the `from` state unless *assert_from* is
    False — the `* → dead_letter` transition #5 has no `from` assertion because a
    poison job can die from either `queued` or `processing`).

    Returns the new CAS token. Raises `InvalidTransitionError` for an illegal
    edge; propagates `CASMismatchError` from the store on a lost race.
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
    """RE-CLAIM a session the consumer already owns the message for, per
    `RECOVERY_TRANSITIONS` (`processing → processing`, `done → processing`).

    A SECOND, deliberately narrow entry point rather than a widening of
    `VALID_TRANSITIONS`, for the same reason the `* → dead_letter` hatch is a
    parameter and not a table row: the forward table is the design's ownership
    contract and it must stay readable as one (`done` has no forward successor;
    nothing may re-enter `processing` on the ordinary path). Recovery is a
    different question — "this delivery is mine to run even though the session is
    not `queued`" — and it is asked from exactly one call site.

    KEEPS the CAS semantics unchanged: the store still asserts the `from` state AND
    still replaces under the caller's *cas*, so a peer that wrote since the caller's
    read wins and this raises `CASMismatchError` (the caller's skip path). Raises
    `InvalidTransitionError` for any edge not in `RECOVERY_TRANSITIONS` — notably
    `queued → processing` (that is the FORWARD edge; use `transition`) and anything
    out of `active`/`pending`/`dead_letter`, which recovery must never manufacture.
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
