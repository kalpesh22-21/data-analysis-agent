# inbox — Slice 7, the review inbox (Track B, Contract D §4).
#
# NOT a second store: the inbox is a PROJECTION over `learning_candidates`
# (D101) queried by `status == "in_review"`, plus the three human transitions
# (approve → validated, reject → rejected [a NEGATIVE signal, not a delete],
# retract → retired). Entity-bearing `hits.span` is stripped before any promotion
# into a global store (D17).
from .inbox import InboxTransitionError, ReviewInbox
from .models import InboxItem

__all__ = [
    "InboxItem",
    "InboxTransitionError",
    "ReviewInbox",
]
