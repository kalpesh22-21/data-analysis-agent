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

import re

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

    The phrase is now mid-sentence rather than sentence-initial (the bullet leads
    with the multi-deliverable condition — see the two tests below), so it is
    asserted in lower case. Its force is unchanged and MUST stay that way for the
    multi-part case.
    """
    assert "whether or not an offered card fits" in AGENT_SYSTEM_PROMPT
    assert "one search per deliverable" in AGENT_SYSTEM_PROMPT
    assert "not a fallback" in AGENT_SYSTEM_PROMPT
    # ...and the fallback framing is gone.
    assert "If none of the blueprints offered to you fit" not in AGENT_SYSTEM_PROMPT


def test_a_clearly_fitting_offered_card_can_be_run_without_searching_first() -> None:
    """Live finding 20: 7 of 7 turns that ran a blueprint called `searchBlueprints`
    first; ZERO used a pre-injected card directly.

    The cause was structural, not disobedience. Bullet 1 said an offered card is
    often the answer and bullet 2 said to search WHETHER OR NOT one fits — so there
    was no path from "this card IS the answer" to "run it": bullet 2 intercepted
    unconditionally. Bullet 1 must therefore be able to TERMINATE, and the split is
    mechanical rather than stylistic: the offered cards are recalled from the whole
    question as one string, which is the right query for a SINGLE deliverable and
    the wrong one for several.
    """
    assert "Analytical, ONE deliverable" in AGENT_SYSTEM_PROMPT
    assert "recalled from this question as a whole" in AGENT_SYSTEM_PROMPT
    assert "RUN IT with runBlueprint and do not search first" in AGENT_SYSTEM_PROMPT


def test_per_deliverable_search_is_still_mandated_for_a_multi_part_request() -> None:
    """The other half of finding 20, asserted separately so a future edit cannot
    collapse one path into the other again — which is how 01's original fix
    overcorrected into this finding in the first place.

    01 deleted the direct-use path to stop search being framed as a fallback. The
    direct-use path is now back, so the per-deliverable mandate needs its own
    guard: it applies when the request has SEVERAL deliverables, or when no offered
    card clearly fits.
    """
    assert "Analytical, SEVERAL deliverables, or no offered card clearly fits" in (
        AGENT_SYSTEM_PROMPT
    )
    assert "under-serve every part of a multi-part request" in AGENT_SYSTEM_PROMPT


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
    # The ESCAPE, which is a runtime contract and not just advice: the
    # repeated-idempotent-read guard used to dedup this re-fetch too, so the line
    # promised something the runtime refused. `loop/agent_loop.py`'s trim-aware
    # exemption now honours it, and the prompt states it positively — the two move
    # together, so this assertion is the prompt half of that pair.
    assert "If it is NO LONGER above" in AGENT_SYSTEM_PROMPT
    assert "that re-fetch is honoured and returns the schema" in AGENT_SYSTEM_PROMPT
    # ...and the old wording is gone: nothing is "summarized away" in this phase
    # (`context/assembly.py` bypasses compaction and produces no summary), so the
    # line named a mechanism that does not exist while the real one — budget
    # trimming — went unmentioned.
    assert "summarized away" not in AGENT_SYSTEM_PROMPT
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


def test_the_boundary_teaches_declare_before_tag() -> None:
    """K2/G1 (live-eval L5). The model tagged a `getTableSchema` with `serves_intent`
    before declaring anything; the tag was silently dropped and the turn finished
    untracked. The runtime now says so in the tool result
    (`loop/agent_loop.py::_INTENT_TAG_DROPPED_NOTE`) — the prompt's half is the
    ORDERING, stated imperatively, which is the part no runtime check can enforce
    before the fact."""
    boundary = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Tracking a multi-part request") :
    ].split("\n- ")[0]
    assert "DECLARE THEM FIRST" in boundary
    # The tag is DROPPED, not refused — the prompt must not imply the call fails,
    # or a model reading this will avoid tagging rather than reorder its calls.
    assert "a serves_intent tag sent before any declaration is IGNORED" in boundary
    assert "REFUSED" not in boundary.split("then declare —")[1]


def test_state_contract_lines_are_present() -> None:
    """01 §"Additions the state contract requires" — each of these is the prompt's
    half of a runtime rule the model is otherwise refused by."""
    # Single-deliverable requests create no state (§5.1).
    assert "A single-deliverable request: do NOT call it at all." in AGENT_SYSTEM_PROMPT
    # State call first in the batch (03 §E.2) — the runtime enforces it too, but
    # the prompt must not rely on the safety net.
    assert "emit updateAnalysisState FIRST" in AGENT_SYSTEM_PROMPT
    # Completion evidence is a closed set (§6.1), and the model binds it at CALL
    # TIME. This assertion used to require the citation wording ("cite the
    # tool_call_id of a runQuery, an authoritative runBlueprint, or a
    # getTableSchema"); citation was retired outright in 01a §14, so the tag is now
    # the ONLY binding path the prompt teaches — the closed set is unchanged and
    # still named.
    assert "pass `serves_intent` with the intent's id" in AGENT_SYSTEM_PROMPT
    assert "runQuery, authoritative runBlueprint or getTableSchema" in AGENT_SYSTEM_PROMPT
    assert "nothing else counts as evidence" in AGENT_SYSTEM_PROMPT
    # ...and the ORDERING caveat survived the rewrite. It applies to the tag exactly
    # as it applied to the citation: state calls dispatch first, so the call you are
    # making right now cannot close an intent in this same message.
    assert "has not run yet" in AGENT_SYSTEM_PROMPT
    assert "close the intent in a LATER one" in AGENT_SYSTEM_PROMPT
    # 04 §A's evidence REUSE is still taught — one call answering two deliverables —
    # but as a runtime behaviour rather than as a citation the model has to write.
    # The ALLOWANCE is the load-bearing half: a model that does not know reuse is
    # permitted will not attempt the second close at all, and the auto-bind backstop
    # it now relies on never gets asked.
    assert "One call answering TWO deliverables" in AGENT_SYSTEM_PROMPT
    assert "the runtime binds that same call to both" in AGENT_SYSTEM_PROMPT
    # ...and the retired field is named NOWHERE — the prompt is re-sent every
    # round-trip, so a stale instruction to send a property the schema no longer
    # declares is live on every turn (01a §14).
    assert "evidence_tool_call_id" not in AGENT_SYSTEM_PROMPT
    assert "reason_code" not in AGENT_SYSTEM_PROMPT
    # Resolve tracked intents before finishing (§3 step 10, §7) — scoped so it
    # cannot read as "withhold the terminal call"; see the two tests below.
    assert "Resolve every tracked intent before you finish." in AGENT_SYSTEM_PROMPT
    assert "which part you did not cover and why" in AGENT_SYSTEM_PROMPT


def test_multi_row_answers_are_delivered_with_answer_with_table() -> None:
    """The table rule is UNCONDITIONAL — live finding 19.

    Two live turns (`50d1ae74`, `e2d24be3`) emitted a multi-row markdown table as
    prose instead of calling `answerWithTable`. Both had declared intents they
    could not close, and read the old "Do not finalize while a tracked intent is
    unresolved" line as covering the terminal call itself. Prose is gated by
    nothing, so the user lost the paginated grid and got a truncated table.
    """
    assert "If the answer is MORE THAN ONE ROW" in AGENT_SYSTEM_PROMPT
    assert "Call answerWithTable instead" in AGENT_SYSTEM_PROMPT
    assert "This rule is UNCONDITIONAL." in AGENT_SYSTEM_PROMPT
    assert "answerWithTable is how you finalize in those cases too" in AGENT_SYSTEM_PROMPT


def test_closing_the_last_intent_is_not_the_end_of_the_turn() -> None:
    """01a §13, measured: on multi-intent turns the model never called
    `answerWithTable` — not multi-table, not single-table.

    Three live runs of one three-intent question, on the build that shipped
    multi-table `answerWithTable`: `answer_tables: 0` every time, 13 tool calls, all
    three intents `completed`, answer written as bullet-list prose. The chains show
    the last call was `updateAnalysisState`, after which the model behaved as though
    the turn was over; the single-deliverable control on the same build ended on
    `answerWithTable`. So this is a TRACKING defect, not a table one — closing the
    ledger was being read as finishing.

    The instruction lands in the tracking section (where the model closes its last
    intent), is cross-referenced from the table section (where it decides the
    answer's shape), and is mirrored into BOTH tool descriptions, which are re-sent
    beside the prompt on every round-trip — a prompt/description divergence has
    bitten this release three times.

    Placement is asserted, not just presence: the same wording in one carrier only
    is the failure mode this test exists to catch.
    """
    from data_agent.runtime.mcp.tool_schema import (
        ANSWER_WITH_TABLE_TOOL_SCHEMA,
        UPDATE_ANALYSIS_STATE_TOOL_SCHEMA,
    )

    tracking = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Tracking a multi-part request") : AGENT_SYSTEM_PROMPT.index(
            "## Operating procedure"
        )
    ]
    table = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## Presenting a table") :]

    # The rule, in the section that owns it.
    assert "CLOSING YOUR LAST INTENT IS NOT THE END OF THE TURN" in tracking
    assert "The answer still has to be sent" in tracking
    # Both calls in ONE response — 03 §E.2's partition dispatches state first, so
    # this is a runtime guarantee and not a hope (05 §G).
    assert "send both in the SAME response" in tracking
    assert "state calls run first, so one response does both" in tracking
    # ...and it is scoped to the terminal TOOL, because exit #1 requires
    # `not result.tool_calls` (`agent_loop.py:2880`): a plain final message batched
    # with a state call is not final at all — D22 discards the free text and the
    # turn loops. Telling the model to batch "the answer" unqualified would trade
    # one wasted round for another.
    assert "An ordinary message cannot share a response with a tool call" in tracking
    # The multi-row rule is NOT restated here (duplicating it is how the reverted
    # merge rule was built); the tracking section points at the section that owns it.
    assert "see Presenting a table" in tracking
    assert "MORE THAN ONE ROW" not in tracking

    # The cross-reference, where the answer's shape is decided.
    assert "neither is closing your last intent" in table
    assert "send updateAnalysisState and answerWithTable in the SAME response" in table

    # And both tool descriptions, which the model reads in the same request.
    for schema in (ANSWER_WITH_TABLE_TOOL_SCHEMA, UPDATE_ANALYSIS_STATE_TOOL_SCHEMA):
        description = schema["description"]
        assert "CLOSING YOUR LAST INTENT IS NOT THE END OF THE TURN" in description, (
            f"{schema['name']} does not carry the close-and-finalize rule"
        )
        assert "SAME response" in description


def test_the_prompt_never_instructs_re_deriving_a_blueprint_result_into_a_table() -> None:
    """01's must-survive authoritative-result rule, guarded from the TABLE side.

    A blueprint result is authoritative for its intent (`## Operating procedure`).
    Rewriting two such results into one joined query — to satisfy any "one table
    per answer" style rule — IS re-derivation, and it is the concrete way this
    prompt has already broken twice.

    The same-grain merge rule (01a §§10-11, both REVERTED) was measured on four
    live runs of one three-part question. With no merge rule the turn ran
    `runBlueprint x3` (and, after the getBlueprint gate, `runBlueprint x2 +
    runQuery x1`) and completed 3/3 intents. With the rule — unqualified, then
    narrowed — it ran `runQuery x3` and then `runQuery x2` with ZERO
    `runBlueprint`, hand-writing a merged `SELECT` over figures the blueprints had
    already returned, and timed out with no answer both times.

    So this asserts the ABSENCE of any such instruction, in the section where the
    table is decided. It is not a merge-rule test — it guards the re-derivation
    ban, which matters whether or not a merge rule ever returns, and which is
    exactly what the regression violated. If a future multi-table
    `answerWithTable` makes merging legitimate, it must do so WITHOUT telling the
    model to re-query a blueprint result, and this test is the line.

    THE LINE NOW COVERS BOTH CARRIERS. This assertion read `AGENT_SYSTEM_PROMPT`
    only and never `ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]`, which carries
    the same instructions to the same model on EVERY round-trip. That divergence
    has bitten this release twice — README finding 24 had to fix the same text by
    hand in two places, and during 08's verification pass the two were momentarily
    out of step (the prompt reverted, the tool description not yet) with no test
    able to see it. Both are checked here, so a merge rule cannot return through
    the schema while the prompt stays clean.
    """
    from data_agent.runtime.mcp.tool_schema import ANSWER_WITH_TABLE_TOOL_SCHEMA

    section = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## Presenting a table") :]
    description = ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]
    for carrier, text in (("the table section", section), ("the tool description", description)):
        for phrase in (
            "SAME GRAIN MEANS ONE QUERY",
            "ONE TABLE PER ANSWER",
            "MERGE ONLY PARTS",
            "share a grain",
            "same-grain",
            "returning them side by side",
            "headcount by department and average salary by department",
        ):
            assert phrase not in text, (
                f"a same-grain merge instruction is back in {carrier}: {phrase!r} — "
                "see 01a §§10-11, reverted after four live runs"
            )


def test_the_multi_table_rule_is_stated_in_both_carriers() -> None:
    """08 §G: the `tables` instruction lands in the prompt AND in the tool
    description, and the two say the same thing.

    Ordering matters and is asserted implicitly by both being present: a prompt
    that says `tables:` against a schema with no such property produces a rejected
    argument on every multi-intent turn, so the wording may never lead the payload.
    """
    from data_agent.runtime.mcp.tool_schema import ANSWER_WITH_TABLE_TOOL_SCHEMA

    section = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## Presenting a table") :]
    description = ANSWER_WITH_TABLE_TOOL_SCHEMA["description"]
    # The payload really has the property the text names.
    assert "tables" in ANSWER_WITH_TABLE_TOOL_SCHEMA["parameters"]["properties"]
    # The rule, in both carriers.
    assert "One table per part." in section
    assert "ONE TABLE PER PART." in description
    for text in (section, description):
        # Send what you ALREADY produced — every verb is `send`/`goes in as`, never
        # `write`. Both reverted rules were imperative about writing a query.
        assert "the result you ALREADY produced" in text
        # The merge clause is DESCRIPTIVE of work already done, in the past tense,
        # so it cannot route the model back to the SQL editor.
        assert "If one query you ran already covered two parts, that is one table" in text
        # 01a §10's scalar clause, carried forward — the one part of that section
        # with no failure attached.
        assert "single number still belongs in your prose" in text
    # It lands INSIDE the authoritative-result rule rather than beside it (README
    # finding 24's stated general lesson). Only the prompt has a section to cite.
    assert "unchanged (see Operating procedure)" in section
    # NO WORKED EXAMPLE. §10's example was the sharpest part of the defect: both
    # halves were blueprint-covered in the seeded corpus, so the illustration taught
    # the error. The payload sketch is the only illustration and it names no domain.
    for text in (section, description):
        assert "dbpcm_warehouse" not in text
        assert "department" not in text.replace("breakdown by group", "")


def test_the_authoritative_result_rule_is_present_and_uncontradicted() -> None:
    """The other half of the guard above. `test_authoritative_blueprint_result_
    rule_survived_the_rewrite` asserts the rule exists; this asserts nothing later
    in the prompt overrides it.

    Presence alone is not sufficient evidence, and that is the lesson worth
    keeping: on the regressed runs the authoritative-result rule was present,
    verbatim, in the same prompt the model was reading. It was outranked by a
    newer, more specific instruction in a LATER section. So ORDER is asserted too
    — the rule is read before the table section — alongside the absence of any
    phrasing that re-opens re-derivation for the sake of one grid.
    """
    assert "do NOT run additional runQuerys" in AGENT_SYSTEM_PROMPT
    assert "to re-derive, double-check, re-verify, or reformat the same figure" in (
        AGENT_SYSTEM_PROMPT
    )
    procedure = AGENT_SYSTEM_PROMPT.index("## Operating procedure")
    table = AGENT_SYSTEM_PROMPT.index("## Presenting a table")
    assert procedure < table, "the table section is read before the rule it defers to"
    # No phrasing that re-opens re-derivation for the sake of one grid.
    for phrase in (
        "re-run the blueprint's",
        "rewrite the blueprint",
        "re-derive it as one query",
        "merge the blueprint results into one query",
    ):
        assert phrase not in AGENT_SYSTEM_PROMPT, f"re-derivation re-authorized: {phrase!r}"


def test_no_unqualified_instruction_to_withhold_the_final_call() -> None:
    """The other half of finding 19: nothing in the prompt may read as "hold back
    the terminal call while an intent is open".

    An unresolved intent is REPORTED in the final answer; it never suppresses it.
    Keyword-level per 01 §Tests — this guards against the old phrasing (or a
    paraphrase of it) coming back, not against every possible wording.
    """
    for phrase in ("Do not finalize", "Do NOT finalize", "not finalize while", "before finalizing"):
        assert phrase not in AGENT_SYSTEM_PROMPT, f"prompt withholds finalization: {phrase!r}"
    assert "never a reason to withhold one" in AGENT_SYSTEM_PROMPT


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


def test_candidates_are_chosen_from_the_card_without_expanding_each_one() -> None:
    """The half of 02 §Changes item 6 that SURVIVES the getBlueprint-before-run rule.

    The enriched search card carries `slots`, `resolves` and `result_grain`, so
    choosing among `k` search hits still costs no `getBlueprint` per candidate.
    That saving is unaffected by the run gate, and the prompt must keep saying so —
    it leads the message list and is re-sent every round-trip, so a stale
    instruction here would cost a round-trip per candidate on every search.
    """
    assert "read the `slots` on the blueprint's own card" in AGENT_SYSTEM_PROMPT
    assert "you do not need to expand every candidate to pick one" in AGENT_SYSTEM_PROMPT
    assert "read the blueprint's `slots` via getBlueprint" not in AGENT_SYSTEM_PROMPT


def test_the_blueprint_you_run_must_be_expanded_first() -> None:
    """The user's rule, and the prompt is its primary carrier.

    A card carries `intent`, `slots`, `resolves`, `result_grain` — and NO SQL. So a
    model routing on the card alone is trusting AUTHORED PROSE to describe the query
    it is about to execute, and the D56 grain gate cannot catch a mismatch (it
    verifies the result SHAPE against the declared `result_grain`, never that the
    blueprint answers the question).

    The old wording — "Call getBlueprint only for the full step DAG, a composition
    summary, or a card that says its slot list was truncated" — said the OPPOSITE of
    the rule the runtime now enforces, so it is asserted absent: a contradiction
    between the prompt and the gate is live on every round-trip.
    """
    assert "BEFORE YOU RUN THE ONE YOU PICKED, EXPAND IT" in AGENT_SYSTEM_PROMPT
    assert "runBlueprint is refused for an id you have not expanded in this turn" in (
        AGENT_SYSTEM_PROMPT
    )
    # Turn-scoped, and the model is told so rather than discovering it by refusal.
    assert "a getBlueprint from an earlier turn does not count" in AGENT_SYSTEM_PROMPT
    # The routing section, where the model decides, points at the rule too.
    assert "call getBlueprint on it and read what it actually does BEFORE you run it" in (
        AGENT_SYSTEM_PROMPT
    )
    # ...and the superseded instruction is gone.
    assert "Call getBlueprint only" not in AGENT_SYSTEM_PROMPT


def test_the_expand_then_run_batch_shape_is_taught() -> None:
    """Without this the rule reads as 2 round-trips PER deliverable. The batched
    shape is what keeps an N-deliverable request at 2 round-trips, and
    `tests/runtime/loop/test_blueprint_definition_gate.py` measures that it does."""
    assert "call getBlueprint for every blueprint you mean to run in ONE response" in (
        AGENT_SYSTEM_PROMPT
    )
    assert "three deliverables cost two round-trips, not six" in AGENT_SYSTEM_PROMPT


def test_the_run_gate_is_tied_to_the_semantic_correctness_line() -> None:
    """The gate is not a new idea — it is the concrete practice the existing
    "Success is not proof of correctness" line implies for the blueprint route. The
    two must stay adjacent in meaning, or the rule reads as bureaucracy."""
    assert "Success is not proof of correctness" in AGENT_SYSTEM_PROMPT
    assert "On the blueprint route the same check is on the BLUEPRINT'S OWN definition" in (
        AGENT_SYSTEM_PROMPT
    )
    assert "can still be measuring the wrong thing" in AGENT_SYSTEM_PROMPT


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


def _gloss_line() -> str:
    """The prompt's one-line slot-type vocabulary (inside "Understanding blueprints")."""
    return next(
        line for line in AGENT_SYSTEM_PROMPT.splitlines() if line.startswith("Slot types:")
    )


def test_the_prompt_names_no_slot_type_the_runtime_does_not_have() -> None:
    """B5, the other direction. The test above proves every real slot type is NAMED in
    the prompt; nothing proved the reverse, so a type RENAMED or DELETED in
    `SLOT_TYPES` left its old name in the prompt — the model would keep being taught a
    vocabulary word `SlotSpec.parse` now rejects, and the addition half of the pin
    would stay green throughout.

    Derived by scanning the line for backticked tokens rather than by listing them, so
    this keeps working across renames.
    """
    named = set(re.findall(r"`([a-z_]+)`", _gloss_line()))
    assert named == set(SLOT_TYPES), (
        f"the prompt's slot-type line and SLOT_TYPES disagree: "
        f"prompt-only={sorted(named - set(SLOT_TYPES))}, "
        f"runtime-only={sorted(set(SLOT_TYPES) - named)}"
    )


# The slot types whose `SLOT_TYPE_GLOSS` text the prompt reproduces VERBATIM (modulo
# the terminal `.` vs `;` the list format needs). Pinned as a PREMISE, in the style of
# `tests/eval/test_harness_field_drift.py::test_shared_field_set_is_non_trivial`: it is
# not the enumeration under test (the test computes that), it is the claim that the
# comparison below still has something to compare. These three are the corrections the
# gloss exists to make — pass the bare integer, not "6 months"; a period is a warehouse
# key, not a calendar date; a range is `{start, end}` — and they are the ones a model
# gets wrong when the two copies disagree.
_VERBATIM_IN_PROMPT = frozenset({"period", "period_range", "relative_window"})


def test_shared_slot_type_glosses_are_still_shared() -> None:
    """B5 — the CONTENT half of the prompt/`SLOT_TYPE_GLOSS` duplication.

    HONEST LIMITATION, stated because it bounds what this pin can promise: the prompt
    does NOT render the glosses mechanically. Five of the eight are deliberately
    reworded for the prompt's list format (`string`/`entity`/`enum`/`list` are
    shortened; `as_of_date` is folded into `period`'s clause), and for those only the
    NAME is pinned — by the two tests above. There is no test that can tell a
    legitimate rewording from a contradiction, and pretending otherwise with a
    keyword-overlap heuristic would produce a pin that fails on prose edits and passes
    on semantic ones.

    What IS mechanically checkable is the three glosses the prompt copies verbatim.
    That set is COMPUTED here and compared against the pinned premise, so the test
    fails in either direction: reword the gloss in `blueprint/models.py` (or its copy
    in `prompts.py`) and the set shrinks; make a fourth one verbatim and it grows —
    either way somebody has to look at both files, which is the entire point.
    """
    line = _gloss_line()
    shared = {t for t, gloss in SLOT_TYPE_GLOSS.items() if gloss.rstrip(".") in line}
    assert shared == set(_VERBATIM_IN_PROMPT), (
        "the prompt's slot-type glosses and blueprint/models.py::SLOT_TYPE_GLOSS have "
        f"drifted: no longer verbatim={sorted(_VERBATIM_IN_PROMPT - shared)}, "
        f"newly verbatim={sorted(shared - _VERBATIM_IN_PROMPT)}. `getBlueprint` serves "
        "the models.py gloss and the prompt teaches its own copy, so a divergence "
        "teaches the model two different slot vocabularies. Re-align the copies, or "
        "update _VERBATIM_IN_PROMPT if the rewording is deliberate."
    )


def test_metadata_questions_are_routed_to_the_tools_not_to_sql() -> None:
    """01a §12 — measured: one live metadata question spent 3 of its 11 round-trips
    on `SHOW TABLES`, a `system.tables` SELECT, and a hand-built literal table list,
    then hit the 60s wall clock with no answer.

    The model was already calling `listTables`/`getTableSchema` on that same turn,
    so this is not a missing-tool problem — it reached for SQL metadata AS WELL,
    and nothing in the prompt said that path was shut. The positive route is
    therefore asserted first, exactly as the section states it.
    """
    section = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## What runQuery accepts") :]
    # The positive route, stated before the closed paths.
    assert "Metadata comes from the tools, never from SQL" in section
    assert "which tables exist -> listTables" in section
    assert "what one MEANS -> getTableSchema" in section
    # ...and the routing bullet where the decision is actually made agrees.
    assert "listTables/getTableSchema, never with SQL" in AGENT_SYSTEM_PROMPT
    # The closed paths, named literally rather than by error code (§12).
    assert "There is no SQL route to it" in section
    assert "SHOW TABLES" in section
    assert "DESCRIBE/DESC" in section
    assert "`system.` table" in section
    # The third measured attempt: synthesizing a table list from literals.
    assert "do not hand-build a table list out of literals" in section


def test_the_prompt_never_instructs_sql_based_metadata_discovery() -> None:
    """The other half: no section may send the model back to SQL for metadata.

    Keyword-level per 01 §Tests — this guards against such an instruction being
    (re)introduced, not against every possible wording. `SHOW TABLES` and
    `DESCRIBE` appear ONLY inside `## What runQuery accepts`, where they are named
    as rejected.
    """
    section_start = AGENT_SYSTEM_PROMPT.index("## What runQuery accepts")
    section_end = AGENT_SYSTEM_PROMPT.index("## Tracking a multi-part request")
    outside = (
        AGENT_SYSTEM_PROMPT[:section_start] + AGENT_SYSTEM_PROMPT[section_end:]
    )
    for phrase in ("SHOW TABLES", "DESCRIBE", "system.tables", "system.columns"):
        assert phrase not in outside, (
            f"{phrase!r} appears outside the section that closes it — the prompt "
            "must never route metadata through SQL (01a §12)"
        )


def test_metadata_answers_are_business_language_never_physical_identifiers() -> None:
    """The no-schema-disclosure contract, in the bullet where the model decides.

    Metadata questions stay a legitimate deliverable and `listTables`/
    `getTableSchema` stay their grounding route — what changed is the ANSWER
    contract: those tools ground the MODEL, and the user-facing text describes the
    data in business terms.

    THE PROMPT IS NO LONGER THE ONLY CARRIER — and it is still the FIRST one, which
    is why this stays asserted. Since ISSUES I1 the runtime scrubs answer prose of
    identifier-shaped tokens on the way out (`runtime/answer_scrub.py`, applied at
    every exit in `_finish`), so a model that names a table is redacted rather than
    obeyed. That is a backstop, not a substitute: it can only replace an identifier
    with a marker, while this bullet is what gets the model to write the business
    sentence the user actually wanted in the first place.
    """
    routing = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Routing the request") : AGENT_SYSTEM_PROMPT.index(
            "## What runQuery accepts"
        )
    ]
    # The grounding route is UNCHANGED — this rule must not read as "stop looking".
    assert "listTables/getTableSchema, never with SQL" in routing
    assert "those tools ground YOU, they are not the answer" in routing
    # ...and the answer contract.
    assert "ANSWER IT IN BUSINESS TERMS" in routing
    assert "Never put a database, table or column name, DDL, or a schema dump in the answer" in (
        routing
    )
    # The explicit-ask case, which is where a model with only the general rule
    # tends to comply with the user instead.
    assert "Asked outright for the physical schema or the table list" in routing
    assert "do not expose internal database structure" in routing


def test_trust_boundary_forbids_surfacing_internal_database_structure() -> None:
    """The general rule behind the metadata bullet, in the section that owns
    disclosure limits (which previously covered PII only).

    Scoped to ANSWER TEXT on purpose: `answerWithTable`'s `tables[].sql` is the D56
    transparency channel and is explicitly exempted, so this rule cannot be read as
    withholding the SQL the interface shows beside the grid.
    """
    boundary = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Trust boundary") : AGENT_SYSTEM_PROMPT.index(
            "## Scope and sensitive data"
        )
    ]
    assert "Internal database structure is implementation detail, not an answer" in boundary
    assert "never belong in your answer text" in boundary
    assert "translate them into the user's own business language" in boundary
    # The carve-out for the structured transparency channel.
    assert "The structured fields of a tool call are unaffected" in boundary
    # ...and the table contract it defers to is still intact (D56, out of scope).
    table = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## Presenting a table") :]
    assert "Give it `sql`" in table


def test_the_coverage_caveat_names_what_was_blocked_in_business_terms() -> None:
    """The seam between the two rules, found on review.

    `## Scope and sensitive data` requires the model to name what it could NOT
    access — and the runtime hands it exactly the material the no-structure rule
    forbids: a `COLUMN_SCOPE_VIOLATION` rides `denial_detail`, whose text is an
    author-controlled string that NAMES the out-of-scope column(s)
    (`dispatch/tool_dispatcher.py`). Without this amendment the prompt instructed
    the model to print that column name in its answer.

    The caveat itself is unchanged — an honest coverage statement is the point;
    only its vocabulary is constrained.
    """
    section = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Scope and sensitive data") : AGENT_SYSTEM_PROMPT.index(
            "## Asking vs. assuming"
        )
    ]
    # The caveat survives.
    assert "state that the result covers only what you could access" in section
    assert "name what you could not" in section
    assert "never imply coverage you do not have" in section.lower()
    # ...in business vocabulary, with the denial-text source named so the rule is
    # actionable at the moment the model is holding one.
    assert "IN BUSINESS TERMS" in section
    assert "a denial message can quote the internal field it blocked" in section
    assert "not the column name" in section


def test_the_cost_of_a_rejected_query_is_stated() -> None:
    """The motivation, without which the rule reads as trivia (01a §12).

    A guess is not free: the rejection comes back a full round-trip later and the
    turn is bounded by `max_wall_clock_seconds` (180 by default since 2026-08-12,
    60 when this was measured — `runtime/config.py`), which is what turned three
    rejected queries into a turn with no answer. The prompt names no number, so the
    raise does not touch the text.
    """
    assert "A rejected query costs a full round-trip" in AGENT_SYSTEM_PROMPT
    assert "bounded by a wall clock" in AGENT_SYSTEM_PROMPT
    assert "end a turn with no answer at all" in AGENT_SYSTEM_PROMPT


def test_the_accepted_statement_shape_is_stated_without_over_deterring() -> None:
    """01a §12: the shape `runQuery` actually admits, plus the two clauses that
    stop a list of prohibitions from suppressing the SQL that works.

    Telling a model what is blocked reliably makes it stop using adjacent things
    that are not — the mechanism behind the reverted §§10-11. Joins/subqueries/
    CTEs/UNION and the auto-injected LIMIT were verified against
    `clickhouse-api/app/sqlparse/provenance.py` and `app/security.py:397-402`.
    """
    section = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## What runQuery accepts") :]
    assert "ONE read-only statement" in section
    assert "a SELECT, or a WITH ... SELECT" in section
    # Not over-deterred.
    assert "Joins, subqueries, CTEs and UNION are fine" in section
    assert "the server adds a LIMIT if you omit one" in section
    # The denylist entries worth naming (`app/security.py:157-268`).
    assert "SET/SETTINGS/FORMAT" in section
    assert "external table functions (url, file, s3, remote, merge, view)" in section


def test_the_current_date_reaches_sql_as_a_function_never_as_the_anchor_literal() -> None:
    """ONE instruction, TWO carriers, so both are asserted together.

    Live turns pasted the rendered date anchor into the SQL itself
    (`toDateTime64('2026-08-28 00:00:00', 6)` as a `dateDiff` bound). That is not a
    query the guard can reject — it parses, runs and verifies — and the learning
    plane then froze it into blueprint templates that are wrong the next morning.
    The prompt carries the positive rule (use `dateDiff` against `today()`/`now()`);
    the anchor message carries the prohibition, because "never as this literal"
    only means anything beside the literal. Neither half alone is the instruction.
    """
    from data_agent.runtime.context.assembly import DATE_ANCHOR_SQL_NOTE

    section = AGENT_SYSTEM_PROMPT[AGENT_SYSTEM_PROMPT.index("## What runQuery accepts") :]
    section = section[: section.index("## Tracking a multi-part request")]
    assert "prefer dateDiff against `today()`/`now()`" in section
    assert "subtracting dates by hand or pasting in a fixed date" in section
    # The anchor's half: the frame is for reading the question, not for pasting.
    assert "in SQL express the current date or time as `today()`/`now()`" in (
        DATE_ANCHOR_SQL_NOTE
    )
    assert "never as this literal" in DATE_ANCHOR_SQL_NOTE


def test_the_sql_guard_section_names_no_error_codes() -> None:
    """The model receives codes through `denial_detail` at the moment of failure
    (`dispatch/denial_mapping.py`); the prompt teaches behaviour. Asserted for the
    whole prompt, which is the existing convention every other section follows."""
    for code in (
        "PARSE_FAILED_CLOSED",
        "DISALLOWED_KEYWORD",
        "DISALLOWED_STATEMENT_TYPE",
        "CARTESIAN_JOIN_FORBIDDEN",
        "COLUMN_SCOPE_VIOLATION",
        "SCRATCH_SESSION_VIOLATION",
    ):
        assert code not in AGENT_SYSTEM_PROMPT, f"error code {code!r} leaked into the prompt"


def test_a_successful_query_is_never_re_run() -> None:
    """01a §15, measured: one live turn re-ran the SAME two successful `runQuery`s
    14+ times across 3 budget windows — 34 `runQuery` trail entries,
    `BUDGET_EXHAUSTED`, no answer.

    Nothing stopped it, and that is the point this test guards. The
    authoritative-result rule above is scoped to BLUEPRINT results; the
    repeated-idempotent-read guard deliberately excludes `runQuery` (a re-query
    after a corrected filter is legitimate); and the re-fetch bullet is about
    schemas and leads with an ESCAPE. So there was no sentence anywhere in the
    model-facing text prohibiting the behaviour that consumed the turn.

    The stopping condition is asserted alongside the prohibition, deliberately:
    the observed loop was a turn that HELD the answer and did not recognise it was
    finished, which is finding 19/§13's defect from the other side. "Do not repeat"
    without "therefore send the answer" leaves that half unaddressed.
    """
    procedure = AGENT_SYSTEM_PROMPT[
        AGENT_SYSTEM_PROMPT.index("## Operating procedure") : AGENT_SYSTEM_PROMPT.index(
            "## Understanding blueprints"
        )
    ]
    assert "THE SAME QUERY RETURNS THE SAME ROWS" in procedure
    assert "never repeat a runQuery you already ran this turn" in procedure
    assert "read it there instead of running it again" in procedure
    # The exit, not just the prohibition.
    assert "STOP QUERYING" in procedure
    assert "close your tracked intents and send the answer" in procedure
    # It sits with the rule it generalises, not in a section of its own — a result
    # you already have is a result you already have, blueprint or not.
    assert procedure.index("treat that result as the authoritative answer") < procedure.index(
        "THE SAME QUERY RETURNS THE SAME ROWS"
    )


def test_prompt_stays_within_its_token_budget() -> None:
    """Hard ceiling, not "net neutral to slightly smaller" (01 §Token budget).

    The prompt is re-sent on every round-trip and the loop's per-window ceiling is
    `model_context_window`, so any growth is multiplied by round count. That
    reasoning is why the ceiling exists at all: growth must be deliberate and
    pinned, and an unbounded prompt is the thing to avoid.

    History of the number, so a re-baseline is never mistaken for drift: 11,297
    (the pre-rewrite size, which left 18 chars of headroom after the Release-1
    rewrite) -> 12,000 for the finding-19 fix (the unconditional `answerWithTable`
    rule and the rescoped no-finalize line) -> 12,200 for two live-measured fixes
    that landed together, call-time intent tagging (+171) and the direct-use
    routing path (+110). Each of those steps paid for an instruction the runtime
    cannot enforce, so the prompt is its only carrier.

    The 12,200 was set by the agent that overshot the 12,000 it had been given, and
    was explicitly flagged for ratification rather than treated as settled. **The
    user has since ratified a ceiling of 15,000** — chosen so the routing and
    intent-tracking instructions Release 1 is still adding have room to land, and
    so the next author is not pushed into deleting working instructions to fit,
    which is what a ceiling should never buy.

    15,000 was therefore a deliberate budget with real headroom, not a number
    trailing the current size. It took the prompt to 14,750 chars — 250 spare —
    after the getBlueprint-before-runBlueprint rule (01a §8, +1,224), the re-fetch
    escape rewording (01a §9, +75), the `## What runQuery accepts` section (01a §12,
    +844) and the multi-table `tables` bullet (08 §G, +523).

    **The ceiling is now 16,000, raised by the user** with the close-and-finalize
    instruction (01a §13, +667) that this file asserts in
    `test_closing_the_last_intent_is_not_the_end_of_the_turn`. 250 chars of headroom
    could not hold a 667-char instruction, and the only other way to fit it was to
    delete working instructions — precisely what the 15,000 ratification existed to
    prevent, so the raise follows that ruling rather than overriding it. The prompt
    is 15,417 chars today (583 spare); if that figure and this one drift far apart,
    the ceiling has stopped meaning anything and should be re-argued, not silently
    re-fitted.

    It briefly reached 14,587 for the same-grain merge rule (01a §10, +1,056) and
    its narrowing (01a §11, +148). Both were REVERTED after live measurement, so
    the headroom the ceiling was ratified to provide is back — and the lesson is
    that a ceiling with room to spare is what let a bad rule be tried and then
    withdrawn on its merits, instead of being argued about on size.

    **2026-08-12, two changes, ceiling UNCHANGED at 16,000.** 15,417 -> 15,439
    (01a §14, +22) trimming `updateAnalysisState` to three properties — close to
    size-neutral in the prompt, and its real saving is in the tool DESCRIPTION,
    which is re-sent beside this constant every round-trip and is not counted
    here. Then 15,439 -> **15,736** (01a §15, +297) for the no-re-run rule, whose
    live evidence is a turn that re-ran two successful queries 14+ times and never
    answered. The Lead waived the size concern for that one; it did not need the
    waiver, but **264 chars of headroom is thin**, and per the paragraph above the
    next addition should re-argue the ceiling rather than shave working
    instructions to fit under it.

    **2026-08-13, ceiling UNCHANGED at 16,000.** 15,736 -> **15,875** (+139, 08 §O,
    the tables-only rewording): `tables` became the only designation carrier and the
    two "Presenting a table" bullets were re-phrased from *the* table to *each
    entry*. Not a new instruction — the same rules against a narrower payload — and
    the redundancy in the first draft (+166) was trimmed to +139 rather than
    argued for. **125 chars spare.** That is thinner than the 264 already called
    thin above, so the paragraph above now applies with force: THE NEXT ADDITION
    MUST RE-ARGUE THE CEILING. It must not be paid for by shaving working
    instructions to fit, which is the one way this constant gets worse without
    anyone deciding that it should.

    **2026-08-13, ceiling RE-ARGUED and raised to 17,000 — RATIFIED by the user.**
    15,875 -> **16,631**
    (+756) for the no-schema-disclosure contract: the metadata routing bullet now
    says what the ANSWER may contain (business terms, never a database/table/column
    name, DDL, or a schema dump, with the explicit-request case named), and
    `## Trust boundary` gains the general rule that internal database structure is
    implementation detail to be translated, not surfaced. The paragraph above
    demanded the ceiling be re-argued rather than the addition shaved, so it is:
    125 chars of headroom could not hold a 756-char contract, the prompt is the ONLY
    carrier of it (no runtime filter inspects answer prose), and the alternative —
    deleting working instructions — is what the 15,000 ratification forbade. 17,000
    restores ~370 chars of headroom, deliberately modest so the next addition faces
    the same argument rather than a blank cheque.

    16,631 -> **16,778** (+147) on review: `## Scope and sensitive data` told the
    model to "name what you could not" access, and a `COLUMN_SCOPE_VIOLATION`
    denial quotes the blocked column VERBATIM (`dispatch/tool_dispatcher.py`'s
    `denial_detail` carve-out) — so the coverage caveat was a standing instruction
    to print a physical column name, contradicting the rule added above it. The
    amendment keeps the caveat and constrains its vocabulary. **222 chars spare**;
    the paragraph above still governs the next addition.

    **2026-08-17, ceiling UNCHANGED at 17,000.** 16,778 -> **16,929** (+151, J7, the
    data-anchored-window line). Its live evidence: `bp-hires-per-month` anchors its
    trailing window on `max(hire_date)`, the model now has a grounded "today", and it
    judged the resulting 2021 window unresponsive and re-derived the whole answer with
    its own calendar-anchored SQL — completing the intent on unverified evidence.

    The first draft of the line ran 201 chars and left 21 spare, which is no headroom at
    all. It was TRIMMED to 151 rather than paid for with a raise, because most of the
    work here is done by surfaces the runtime controls and can therefore say at length:
    `getBlueprint` renders the blueprint's own `window_anchor` declaration, and a
    data-anchored run carries a note on its tool result. The prompt carries only the
    standing rule those two are instances of, which is the part no runtime check can
    enforce.

    CEILING RAISED 17,000 -> 17,400 (2026-08-18, C5 slice): the two-tier schema
    teaching (+206 chars: a name/type-only column has unseen documentation — never
    infer semantics from the name) crossed the old ceiling with no trimmable
    neighbour. The ceiling is a growth TRIPWIRE, not a resource limit — the prompt
    costs ~4.3k tokens of an 89.6k request budget — so it moved deliberately, once,
    with this note. **265 chars spare** at the raise; the re-argue paragraph applies
    with full force to whatever comes next.

    **2026-08-18, ceiling UNCHANGED at 17,400.** 17,135 -> **17,246** (+111, C5b):
    the two-tier sentence ended at "ask instead", which was the truth only while
    `getTableSchema` had no narrowing argument. The MCP now takes an optional
    `columns` list, so the clause was rewritten from a dead end into the recovery —
    fetch that table again naming just the columns you need. PAID FOR OUT OF THE
    EXISTING HEADROOM, not with a raise: it edits a sentence the prompt already
    carries, and turning "interrupt the user" into "make one cheap call" is the
    single highest-value change available in that sentence. **154 chars spare.**
    The same clause rides the schema payload's own `_truncated` marker
    (`dispatch/schema_preview.py::_marker_text`), which is unbudgeted here and
    carries the detail; the prompt states only the standing rule.

    **2026-08-19, ceiling UNCHANGED at 17,400.** 17,246 -> **17,314** (+68, K2/G1):
    "declare them all with" became "DECLARE THEM FIRST with" (+3, it stated a
    requirement without stating an ORDER, and live-eval L5 failed purely on order),
    plus "— and a serves_intent tag sent before any declaration is IGNORED" (+65).
    The model tagged a `getTableSchema` with `serves_intent` before declaring any
    intents, the tag was silently dropped, and the turn finished untracked. The
    runtime half is a note on that call's tool result
    (`loop/agent_loop.py::_INTENT_TAG_DROPPED_NOTE`), which is unbudgeted here; the
    prompt carries only the ordering, which no runtime check can enforce ahead of
    time. PAID OUT OF THE EXISTING 154, not with a raise.

    THE SECOND HALF OF THAT SLICE WAS DROPPED FOR SIZE, deliberately and on this
    ceiling's own terms. G2 would add the conjunctive-decomposition example ("the
    employee columns, and the average salary by department" is TWO deliverables,
    +150 as drafted) because the decomposition trigger under-fires on "X and Y"
    phrasing. 154 - 68 = 86 spare cannot hold 150. Tightening it to ~78 by deleting
    the example fits arithmetically and leaves 8 spare, which is BELOW the 21 this
    ledger already called "no headroom at all" two entries up — and the example is
    the instruction, since the abstract rule ("a conjunctive ask can be multi-part")
    is what the section's opening line already implies and the model already
    ignores. So it is not shipped shaved: it waits for a ceiling argument of its
    own. **86 chars spare.**

    **2026-08-28, ceiling RE-ARGUED and raised 17,400 -> 17,550.** 17,314 ->
    **17,474** (+160): one line in `## What runQuery accepts` telling the model to
    do date arithmetic with `dateDiff` against `today()`/`now()` instead of
    subtracting dates by hand or pasting a fixed one. Live turns took the per-turn
    date anchor and pasted the rendered day INTO the SQL
    (`toDateTime64('2026-08-28 00:00:00', 6)` as a `dateDiff` bound), which the
    learning plane then froze into blueprint templates that go stale the next
    morning — a wrong answer that still parses, still runs and still verifies.
    RAISED RATHER THAN SHAVED, per this ledger's own rule: 86 spare cannot hold
    any honest wording of it, and the two clauses are one instruction (naming the
    function without forbidding the hand-rolled alternative leaves the observed
    behaviour permitted). THE OTHER HALF OF THE FIX IS UNBUDGETED: "never as this
    literal" needs the literal beside it, so it rides the anchor message itself
    (`context/assembly.py::DATE_ANCHOR_SQL_NOTE`), which is a per-turn `user`
    message and not part of this constant. `today()`/`now()` were probed against
    the live MCP first — both pass the SQL validator, scope enforcement and
    `explainQuery`. **76 chars spare.**
    """
    assert len(AGENT_SYSTEM_PROMPT) <= 17_550


def test_the_prompt_draft_doc_matches_the_shipped_constant_byte_for_byte() -> None:
    """`docs/decisions/release-1/01a-prompt-draft.md` §3 says "Exactly what
    `AGENT_SYSTEM_PROMPT` renders to", and 01 is the only Release-1 deliverable
    whose artifact is prose — the doc IS the review surface. A doc that has silently
    drifted from the constant is worse than no doc: every later review reads text
    the model never sees. Checked mechanically rather than by eye."""
    import re
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "decisions"
        / "release-1"
        / "01a-prompt-draft.md"
    ).read_text()
    match = re.search(r"\n```text\n(.*?)\n```\n", doc, re.S)
    assert match is not None, "01a §3 no longer contains a ```text fenced block"
    assert match.group(1) == AGENT_SYSTEM_PROMPT


def test_the_three_blueprint_tool_descriptions_agree_with_the_prompt() -> None:
    """All four texts are re-sent on EVERY round-trip, so a contradiction between
    them is live on every turn — this release has already shipped one such
    contradiction once.

    `SEARCH_BLUEPRINTS_TOOL_SCHEMA` used to say `getBlueprint` was needed ONLY for
    the full DAG / a composition summary / `slots_omitted`. That is now false: it is
    required before every run, and the runtime refuses the run without it.
    """
    from data_agent.runtime.mcp.tool_schema import (
        GET_BLUEPRINT_TOOL_SCHEMA,
        RUN_BLUEPRINT_TOOL_SCHEMA,
        SEARCH_BLUEPRINTS_TOOL_SCHEMA,
    )

    search = SEARCH_BLUEPRINTS_TOOL_SCHEMA["description"]
    get = GET_BLUEPRINT_TOOL_SCHEMA["description"]
    run = RUN_BLUEPRINT_TOOL_SCHEMA["description"]

    # All three state the rule, in their own voice.
    assert "you MUST call getBlueprint(id)" in search
    assert "CALL THIS BEFORE EVERY runBlueprint" in get
    assert "CALL getBlueprint ON THAT ID FIRST" in run
    # All three state the enforcement, so none of them reads as advice.
    for description in (search, get, run):
        assert "you have not expanded" in description
    # All three state the turn scoping (the gate's most surprising edge).
    for description in (get, run):
        assert "earlier turn does not count" in description
    # The stale "only when you need the full DAG" carve-out is gone.
    assert "only when you need the full" not in search
    # ...and the per-candidate saving 02 bought is still stated.
    assert "do NOT need a getBlueprint on every candidate" in search
    # The batched shape, so the rule is not read as 2 round-trips per deliverable.
    assert "getBlueprint for several blueprints in one response" in get
    assert "call getBlueprint for every blueprint you mean to run in ONE response" in (
        AGENT_SYSTEM_PROMPT
    )


def test_prompt_is_byte_stable() -> None:
    """D45: every per-round-trip rebuild and every resume must re-derive
    byte-identical messages, so the constant may not depend on the clock, the
    environment, or anything else re-evaluated at import time."""
    import importlib

    from data_agent.runtime import prompts

    first = prompts.AGENT_SYSTEM_PROMPT
    importlib.reload(prompts)
    assert prompts.AGENT_SYSTEM_PROMPT == first
