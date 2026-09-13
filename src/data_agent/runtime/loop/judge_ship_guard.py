"""Window-local protection against shipping a refused answer without a real approval."""

from .answer_judge import ANSWER_VIOLATIONS, MAX_FEEDBACK_CHARS

NAVIGATION_CLAIM_VIOLATION = "navigation_claim"
GROUNDING_DECLINE_TEXT = "I don't have enough verified information to answer your question."
TABLE_HEDGE_LEAD = "I wasn't able to fully verify the answer, but here are the results available."
CAPABILITY_HEDGE_LEAD = "I wasn't able to fully answer your question, but this option may help."
CARD_FATAL_VIOLATIONS = frozenset(
    {"capability_coverage_gap", "capability_intent_mismatch", "unsupported_by_evidence"}
)
EVIDENTIAL_TABLE_VIOLATIONS = frozenset(
    {"contradicts_result", "unexplained_gap", "unrecorded_assumption", "unsupported_by_evidence"}
)
_REASONS = {
    "unrecorded_assumption": "I could not settle which interpretation to rely on.",
    "unexplained_gap": "I could not verify every part of what you asked for.",
    "contradicts_result": "I could not verify the explanation against the results.",
    "unsupported_by_evidence": "I could not verify the claims against the available evidence.",
    "capability_coverage_gap": "The option does not cover everything you asked for.",
    "capability_intent_mismatch": "The available options do not directly answer your question.",
}


def is_card_fatal(site: str, violation: str) -> bool:
    return site == "exit_capability" and violation in CARD_FATAL_VIOLATIONS


def ship_decline_text(violation: str) -> str:
    return (GROUNDING_DECLINE_TEXT + " " + _REASONS.get(violation, "")).strip()[:MAX_FEEDBACK_CHARS]


def table_hedge_text(violation: str) -> str:
    return (TABLE_HEDGE_LEAD + " " + _REASONS.get(violation, "")).strip()[:MAX_FEEDBACK_CHARS]


def capability_hedge_text(violation: str) -> str:
    return (CAPABILITY_HEDGE_LEAD + " " + _REASONS.get(violation, "")).strip()[:MAX_FEEDBACK_CHARS]


_CLOSED_DECLINES = frozenset(
    {GROUNDING_DECLINE_TEXT, *(ship_decline_text(v) for v in ANSWER_VIOLATIONS)}
)


def coherent_capability_ship_text(text, cards, outstanding):
    if cards and text in _CLOSED_DECLINES:
        return capability_hedge_text(outstanding[1] if outstanding else "")
    return text


class JudgeShipGuard:
    def __init__(self):
        self._outstanding = None
        self.card_fatal_pending = False
        self.assumptions_before_refusal: tuple[str, ...] = ()
        self.repick_used = False

    def note_refusal(self, site, violation, *, assumptions=(), card_fatal=None):
        if self._outstanding is None:
            self.assumptions_before_refusal = tuple(assumptions or ())
        self._outstanding = (site, violation)
        # Overrides may disarm this refusal, never turn prose into a card-fatal claim.
        self.card_fatal_pending |= is_card_fatal(site, violation) and card_fatal is not False

    def note_approval(self):
        self._outstanding = None
        self.card_fatal_pending = False
        self.assumptions_before_refusal = ()

    def refuse_unreviewed_data_card(self, *, assumptions=()):
        """Data widgets may expose personal identifiers: a hedge cannot approve them."""
        if self._outstanding is None:
            self.note_refusal("exit_capability", "unsupported_by_evidence", assumptions=assumptions)
        self.card_fatal_pending = True

    def arm_persisted_refusal(self):
        self.note_refusal("exit_capability", "capability_intent_mismatch")

    def unsafe_ship(self):
        return self._outstanding

    def disposition(self, site):
        if self._outstanding is None:
            return None
        violation = self._outstanding[1]
        if self.card_fatal_pending:
            return "decline_only"
        if site == "exit_table" and violation in EVIDENTIAL_TABLE_VIOLATIONS:
            return "ship_tables_with_hedge"
        if site == "exit_capability" and violation not in {
            "leaks_sql_or_schema",
            "answers_inappropriate_request",
        }:
            return "ship_cards_with_hedge"
        return "decline_only"
