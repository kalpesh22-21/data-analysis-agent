"""Routing contract for `AGENT_SYSTEM_PROMPT` (Release 1 §3, build doc 01).

Deliberately crude. A keyword scan cannot tell a well-ordered routing section from
a badly-ordered one containing the right words — **ordering and emphasis are proven
by 07's live-model suite, not here.** This file guards only against an edit silently
DROPPING an instruction the runtime contract depends on, and against the two things
that are mechanically checkable: route-class vocabulary leaking back into
model-facing text, and the prompt outgrowing its budget (it is re-sent on every
round-trip, so growth is multiplied by round count).

See `docs/decisions/release-1/01a-prompt-draft.md` for the reviewed text and the
rationale for each section.
"""

from __future__ import annotations

from data_agent.runtime.blueprint.models import SLOT_TYPE_GLOSS, SLOT_TYPES
from data_agent.runtime.prompts import AGENT_SYSTEM_PROMPT

# The four tools that lock the `analysisState` late-init boundary (03 §E). The
# prompt must name them concretely — "before substantive execution" is not
# actionable prose, and the model cannot recover from crossing the boundary.
SUBSTANTIVE_TOOLS = ("runQuery", "runBlueprint", "sampleRows", "resolveValues")


def test_no_route_class_vocabulary_in_model_facing_text() -> None:
    """Route classes live in telemetry (§8) and evaluation, never in the prompt.

    The removed "Sizing the request" / "Planning a complicated request" sections
    made the model classify the *sentence* ("SIMPLE" vs "COMPLICATED") and route on
    that — but a linguistically complex question may map to one validated
    blueprint, so semantic complexity is the wrong routing criterion.
    """
    assert "SIMPLE" not in AGENT_SYSTEM_PROMPT
    assert "COMPLICATED" not in AGENT_SYSTEM_PROMPT
    # The removed sections' own headings, in case a future edit reinstates them.
    assert "Sizing the request" not in AGENT_SYSTEM_PROMPT
    assert "Planning a complicated request" not in AGENT_SYSTEM_PROMPT


def test_prompt_names_the_tools_the_routing_contract_depends_on() -> None:
    """Each of these is a runtime contract the prompt is the only carrier of."""
    assert "searchBlueprints" in AGENT_SYSTEM_PROMPT
    assert "updateAnalysisState" in AGENT_SYSTEM_PROMPT
    for tool in SUBSTANTIVE_TOOLS:
        assert tool in AGENT_SYSTEM_PROMPT, f"prompt no longer names {tool}"


def test_search_blueprints_is_per_deliverable_practice_not_a_fallback() -> None:
    """Spec §3 step 3 — the defect this release exists to fix.

    The old text ("If none of the blueprints offered to you fit, call
    searchBlueprints...") made per-intent search conditional on the pre-injected
    top-3 missing, which is exactly why it never happened: the model cannot tell
    that a whole-question embedding under-served one clause of a four-part ask.
    """
    assert "Whether or not an offered card fits" in AGENT_SYSTEM_PROMPT
    assert "one search per deliverable" in AGENT_SYSTEM_PROMPT
    assert "not a fallback" in AGENT_SYSTEM_PROMPT
    # ...and the fallback framing is gone.
    assert "If none of the blueprints offered to you fit" not in AGENT_SYSTEM_PROMPT


def test_blueprint_cards_are_consulted_before_schema_discovery() -> None:
    """P1: blueprint routing must precede schema discovery.

    The cards are already in context before round 1, so leading with discovery
    actively worked against material the model already held.
    """
    assert "BEFORE fetching any schema" in AGENT_SYSTEM_PROMPT
    routing = AGENT_SYSTEM_PROMPT.index("## Routing the request")
    procedure = AGENT_SYSTEM_PROMPT.index("## Operating procedure")
    assert routing < procedure


def test_deliverable_and_intent_are_stated_to_be_the_same_thing() -> None:
    """Spec §3 uses "deliverable"; §5/§7/§9 and the state field use "intent". The
    prompt states the equivalence once so the model does not treat the tracked set
    as a narrower thing than the set it was told to identify (01 §Terminology)."""
    assert '"Deliverable" and "intent" mean the same thing here.' in AGENT_SYSTEM_PROMPT
    assert "DELIVERABLES" in AGENT_SYSTEM_PROMPT


def test_authoritative_blueprint_result_rule_survived_the_rewrite() -> None:
    """01's ⚠: this rule lived INSIDE the section that was rewritten wholesale.

    It is spec §3 step 9 and the entire subject of 07's re-derivation case (§9.1
    case 6) — dropping it while shipping a test for it is the worst outcome
    available, so the rule is asserted here rather than assumed.
    """
    assert "treat that result as the authoritative answer for that intent" in AGENT_SYSTEM_PROMPT
    assert '"authoritative"' in AGENT_SYSTEM_PROMPT
    assert "do NOT run additional runQuerys" in AGENT_SYSTEM_PROMPT
    assert "DISTINCT part of the user's question" in AGENT_SYSTEM_PROMPT


def test_the_other_three_in_section_rules_survived_the_rewrite() -> None:
    """The remaining three of 01's four in-section rules.

    Do-not-re-fetch-a-visible-schema is *why this prompt exists* (see the module
    docstring); batching is the mechanism behind §3 step 5; resolveValues-first is
    the tenant-code wrong-answer class.
    """
    assert "do NOT re-fetch a " in AGENT_SYSTEM_PROMPT
    assert "schema you already have and can still see" in AGENT_SYSTEM_PROMPT
    assert "issue those tool calls together in one turn" in AGENT_SYSTEM_PROMPT
    assert "Use resolveValues to map a user's wording" in AGENT_SYSTEM_PROMPT
    assert "before filtering on it" in AGENT_SYSTEM_PROMPT


def test_late_init_boundary_is_named_as_the_four_substantive_tools() -> None:
    """03 §E — the boundary is mechanical and unrecoverable once crossed, so the
    prompt names the locking set rather than saying "before substantive work"."""
    boundary = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Tracking a multi-part request") :
    ].split("\n- ")[0]
    for tool in SUBSTANTIVE_TOOLS:
        assert tool in boundary, f"late-init boundary sentence no longer names {tool}"
    assert "before any substantive tool call" in boundary
    # Discovery is explicitly NOT locking — an over-deterred model would decompose
    # blind rather than look first (03 §E).
    assert "discovery does not close that door" in boundary


def test_state_contract_lines_are_present() -> None:
    """01 §"Additions the state contract requires" — each of these is the prompt's
    half of a runtime rule the model is otherwise refused by."""
    # Single-deliverable requests create no state (§5.1).
    assert "A single-deliverable request: do NOT call it at all." in AGENT_SYSTEM_PROMPT
    # State call first in the batch (03 §E.2) — the runtime enforces it too, but
    # the prompt must not rely on the safety net.
    assert "emit updateAnalysisState FIRST" in AGENT_SYSTEM_PROMPT
    # Completion evidence is a closed set (§6.1) and the model chooses the citation.
    assert "cite the tool_call_id of a runQuery, an" in AGENT_SYSTEM_PROMPT
    assert "authoritative runBlueprint, or a getTableSchema" in AGENT_SYSTEM_PROMPT
    # Do not finalize with an unresolved intent (§3 step 10, §7).
    assert "Do not finalize while a tracked intent is unresolved" in AGENT_SYSTEM_PROMPT


def test_empty_result_set_is_completed_never_blocked() -> None:
    """04 §B.4's second hole, mitigated prompt-side.

    Zero rows is simultaneously valid *completion* evidence and valid
    `REQUIRED_DATA_UNAVAILABLE` *block* evidence, and blocking is cheaper (no
    prose, no table, no answerWithTable). Left to the cheaper exit, coverage
    under-reports on exactly the "nobody did" questions users already distrust.
    """
    assert "AN EMPTY RESULT SET IS AN ANSWER, NOT AN ABSENCE." in AGENT_SYSTEM_PROMPT
    assert "none were found" in AGENT_SYSTEM_PROMPT
    assert "never mark it blocked" in AGENT_SYSTEM_PROMPT


def test_semantic_correctness_reminder_is_present() -> None:
    """An ADDITION, not a retention — no such line existed before (01 §"The
    semantic-correctness reminder does not exist"). Recorded as a decision in
    01a §2.1: added rather than deliberately omitted."""
    assert "Success is not proof of correctness" in AGENT_SYSTEM_PROMPT
    assert "not that it measured what was asked" in AGENT_SYSTEM_PROMPT


def test_slots_are_read_from_the_card_not_via_get_blueprint() -> None:
    """Coordinated with 02 §Changes item 6.

    The enriched search card carries `slots`, `resolves` and `result_grain`, so
    `getBlueprint` is needed only for the full DAG, a composition summary, or a
    card whose slot list was capped (02's `slots_omitted`). 02 amends the tool
    schema; the prompt is the half that matters, because it leads the message list
    and is re-sent every round-trip — a stale instruction here would defeat 02's
    entire round-trip saving.
    """
    assert "read the `slots` on the blueprint's own card" in AGENT_SYSTEM_PROMPT
    assert "Call getBlueprint only" in AGENT_SYSTEM_PROMPT
    assert "for the full step DAG, a composition summary, or a card that says its slot" in (
        AGENT_SYSTEM_PROMPT
    )
    assert "read the blueprint's `slots` via getBlueprint" not in AGENT_SYSTEM_PROMPT


def test_slot_type_gloss_parity_with_the_prompt() -> None:
    """01 §Edge cases: the "Understanding blueprints" section duplicates
    `SLOT_TYPE_GLOSS` (`blueprint/models.py:32`) with no parity test, and the
    rewrite touched that section. A new slot type must not ship glossed for
    `getBlueprint` but absent from the prompt's vocabulary line."""
    assert set(SLOT_TYPE_GLOSS) == set(SLOT_TYPES)  # the models.py invariant
    gloss_line = next(
        line for line in AGENT_SYSTEM_PROMPT.splitlines() if line.startswith("Slot types:")
    )
    for slot_type in SLOT_TYPES:
        assert f"`{slot_type}`" in gloss_line, f"slot type {slot_type!r} missing from the prompt"


def test_prompt_stays_within_its_token_budget() -> None:
    """Hard ceiling, not "net neutral to slightly smaller" (01 §Token budget).

    11,297 is the pre-rewrite size. The prompt is re-sent on every round-trip and
    the loop's per-window ceiling is `model_context_window`, so any growth is
    multiplied by round count.
    """
    assert len(AGENT_SYSTEM_PROMPT) <= 11_297


def test_prompt_is_byte_stable() -> None:
    """D45: every per-round-trip rebuild and every resume must re-derive
    byte-identical messages, so the constant may not depend on the clock, the
    environment, or anything else re-evaluated at import time."""
    import importlib

    from data_agent.runtime import prompts

    first = prompts.AGENT_SYSTEM_PROMPT
    importlib.reload(prompts)
    assert prompts.AGENT_SYSTEM_PROMPT == first
