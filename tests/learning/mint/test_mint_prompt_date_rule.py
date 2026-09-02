"""Every minting prompt that lets a model WRITE SQL carries the frozen-run-date rule.

THE GAP THIS CLOSES was found by auditing §C.5 against what already existed: the extractor's
system prompt has date guidance (rule 5 — a trailing window is a relative window, an explicit
start/end is TWO bounds), and `check_no_frozen_date_literal` refuses a run date in the template
downstream. The minting prompts, which are the other place a model writes SQL, had NONE. So a
drafted blueprint could freeze the day it was minted into its body, get stamped
`fail_to_review/date_literal`, and hand the expert a validator complaint about a rule they were
never shown.

ONE SENTENCE, IMPORTED. The rule lives in `learning/prompts/sql_rules.py` — not in either package
that uses it, because minting importing it from revising would read as one peer depending on the
other when what they share is a CHECK. Both read it from there, so the prompts cannot drift apart
from each other or from `check_no_frozen_date_literal`.

⚠ NOT on `CLASSIFY_SYSTEM_PROMPT`, and that asymmetry is the point of the test: in `exact` mode
the expert's query IS the accepted SQL and the model has no field in which to write any. Telling
it how to write dates would be advice about an action it cannot take, on the one prompt whose
entire job is "you cannot change this query".
"""

from __future__ import annotations

import pytest

from data_agent.learning.mint.prompt import (
    CLASSIFY_SYSTEM_PROMPT,
    COMPOSITE_SYSTEM_PROMPT,
    DRAFT_SYSTEM_PROMPT,
)
from data_agent.learning.prompts.sql_rules import DATE_RULE


@pytest.mark.parametrize(
    ("name", "prompt"),
    [("draft", DRAFT_SYSTEM_PROMPT), ("composite", COMPOSITE_SYSTEM_PROMPT)],
)
def test_a_prompt_that_writes_sql_states_the_date_rule(name: str, prompt: str) -> None:
    assert DATE_RULE in prompt, name
    assert "NEVER WRITE THE RUN DATE INTO THE QUERY" in prompt
    # The three substantive halves, so a future edit cannot leave the heading and drop the rule.
    assert "today()" in prompt and "dateDiff" in prompt
    assert "TWO LITERALS, NOT ONE" in prompt
    assert "SENTINEL FLOOR" in prompt


def test_the_classify_prompt_does_not_state_it() -> None:
    """It has no field in which to write SQL — see the module docstring."""
    assert DATE_RULE not in CLASSIFY_SYSTEM_PROMPT
    assert "YOU CANNOT CHANGE THE SQL" in CLASSIFY_SYSTEM_PROMPT


def test_the_rule_is_shared_rather_than_copied() -> None:
    """A second copy would keep only one of them current with the checker that enforces it.

    IDENTITY, not equality: two equal strings today are two strings to edit tomorrow, and the
    one that gets missed is the one nobody is looking at.
    """
    from data_agent.learning import mint
    from data_agent.learning.revise import prompt as revise_prompt

    assert mint.prompt.DATE_RULE is DATE_RULE
    assert revise_prompt.DATE_RULE is DATE_RULE
