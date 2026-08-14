# inbox — Slice 7, the review inbox (Track B, Contract D §4).
#
# NOT a second store: the inbox is a PROJECTION over `learning_candidates`
# (D101) queried by `status == "in_review"`, plus the three human transitions
# (approve → validated, reject → rejected [a NEGATIVE signal, not a delete],
# retract → retired). Entity-bearing `hits.span` is stripped before any promotion
# into a global store (D17).
from .completion import (
    CompletionInputError,
    CompletionRaceError,
    CompletionResult,
    CompletionUnavailableError,
    ParameterizationCompleter,
)
from .inbox import InboxTransitionError, ReviewInbox
from .models import InboxItem
from .ranking import RankedScore, groundedness, novelty, rank_key, review_score, session_quality

__all__ = [
    "CompletionInputError",
    "CompletionRaceError",
    "CompletionResult",
    "CompletionUnavailableError",
    "InboxItem",
    "InboxTransitionError",
    "ParameterizationCompleter",
    "RankedScore",
    "ReviewInbox",
    "groundedness",
    "novelty",
    "rank_key",
    "review_score",
    "session_quality",
]
