"""answer_judge.py — the finish-time ANSWER JUDGE (09), the model-call half.

THE FIFTH COMPLAINT AT A TERMINAL EXIT, and the first that can READ THE ANSWER. §B asks
"did you do the work?", §J "did you deliver it in the required form?", §K "did you say
anything at all?", §L "does what you said stand on anything?" — all four are regexes and
counters over the loop's own bookkeeping. None of them can tell whether the prose answers
the question, discloses the assumption it rests on, or contradicts the rows behind it.

THIS MODULE HOLDS NO CALL SITES AND NO ALLOWANCE. It builds a brief, asks a model, and
guards what comes back. The loop keeps the placement, the `may_refuse` claim, the draft
clears and the nudge splicing, exactly as it does for the other four gates — see 09 §C/§F.

EVERYTHING FAILS OPEN. A disabled judge, an unreachable provider, a timeout, a malformed
response, an invented violation slug and a rejection the runtime cannot name all return
`APPROVED`, so the turn behaves exactly as a deployment with no judge wired. The asymmetry
is 05 §L.6's and it is sharper here than for any regex: a false negative costs what today
already costs, while a false positive burns a round-trip telling a model that answered
correctly that it did not — and 05 §J.2 measured the response to that, an apology asserting
the turn is over on a turn nothing had refused.

THE BRIEF IS JSON, NOT A PROSE BLOCK, and that is a security decision rather than a
formatting one. It interpolates SQL the model wrote, previews of warehouse rows, and the
model's own draft — all of which the base prompt's Trust boundary treats as content that
may attempt to instruct. A fenced text block can be forged from inside any of those
(`learning/extractor/prior_art.py::_sanitize` exists to collapse the runs of `=` that spell
its fence); a JSON document cannot, because `json.dumps` escapes every string it encodes
and the structure is carried by the encoder rather than by characters in the payload.

WHAT COMES BACK IS UNTRUSTED IN THE OTHER DIRECTION. `feedback` re-enters MODEL context as
an instruction the agent will act on, so it is structurally sanitised (`sanitize_text`)
before it leaves this module — the treatment `finalization.py::_describe_pending` gives
intent descriptions, and for a stronger reason: this text was composed by a model that had
just read tool results.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Literal

from data_agent.runtime.model.client import ModelClient, ModelTurnResult, begin_turn_client
from data_agent.runtime.observability import tracing
from data_agent.runtime.sanitize import sanitize_text

# How much of a refused draft is quoted back. Deliberately the SAME constant the other four
# gates use, imported rather than re-chosen: the model reads these messages in the same
# position, and a judge nudge echoing a different amount would read as a different KIND of
# refusal. `answer_rules.py` imports it from here too, for the same reason.
from .finalization import MAX_NUDGE_DRAFT_CHARS

__all__ = [
    "ANSWER_JUDGE_CALLED_EVENT",
    "ANSWER_JUDGE_EXHAUSTED_EVENT",
    "ANSWER_JUDGE_FAILED_EVENT",
    "ANSWER_JUDGE_REFUSED_EVENT",
    "ANSWER_JUDGE_SKIPPED_EVENT",
    "ANSWER_VIOLATIONS",
    "APPROVED",
    "ASK_USER_JUDGE_EXHAUSTED_EVENT",
    "ASK_USER_JUDGE_REFUSED_EVENT",
    "ASK_USER_VIOLATIONS",
    "JUDGE_TOOL_NAME",
    "AnswerJudge",
    "JudgeBrief",
    "JudgeSite",
    "JudgeVerdict",
    "answer_judge_nudge_text",
    "ask_user_judge_nudge_text",
    "build_judge_tool",
    "parse_verdict",
    "violations_for_site",
]

_logger = logging.getLogger(__name__)

# THE `loop_` PREFIX IS LOAD-BEARING, NOT A CONVENTION: `observability/tracing.py::
# guardrail_observer` drops every event that lacks it, SILENTLY, so a misnamed event fires
# perfectly in every raw-recorder unit test and reaches production telemetry never (the
# `loop_analysis_state_auto_bound` near-miss shipped that way for a review round).
#
# `violation` and `site` are the only attributes any of these carry, and both are
# runtime-authored closed vocabularies — never model text, never the user's question, never
# a figure (09 §K).
ANSWER_JUDGE_REFUSED_EVENT = "loop_answer_judge_refused"
ANSWER_JUDGE_EXHAUSTED_EVENT = "loop_answer_judge_exhausted"
ANSWER_JUDGE_SKIPPED_EVENT = "loop_answer_judge_skipped"
ANSWER_JUDGE_CALLED_EVENT = "loop_answer_judge_called"
ANSWER_JUDGE_FAILED_EVENT = "loop_answer_judge_failed"
ASK_USER_JUDGE_REFUSED_EVENT = "loop_ask_user_judge_refused"
ASK_USER_JUDGE_EXHAUSTED_EVENT = "loop_ask_user_judge_exhausted"

# --- the vocabulary ---------------------------------------------------------

# The three terminal moments a judge can stand at. `exit_prose` and `exit_table` are the
# loop's two terminal exits; `ask_user` is the pause branch, which 05 §L.9 leaves ungated
# and which is the ONLY site where the proposed "non-contextual follow-up" complaint can
# be made at all — an `askUser` is intercepted in the loop and never reaches either exit.
JudgeSite = Literal["exit_prose", "exit_table", "ask_user"]

# WHY THE SLUGS ARE A CLOSED, RUNTIME-AUTHORED SET (09 §E). `violation` is the only field
# of the verdict that may go on a span — `observability/tracing.py::guardrail_observer`
# keeps allowlisted `str|int|float|bool` attributes, and a model-composed string reaching
# one would put the user's question or a warehouse figure into telemetry, which 05 §L's
# span test explicitly forbids. It is also the GROUP BY key of every distribution query
# this feature will be tuned on, and an open string silently becomes its own bucket.
ANSWER_VIOLATIONS: tuple[str, ...] = (
    # A choice shaped the answer and `recordAssumptions` does not carry it. THE CORE ONE:
    # `accum.assumptions` can say what WAS recorded and nothing in the runtime can say
    # what SHOULD have been.
    "unrecorded_assumption",
    # Part of the ask is unanswered and the prose does not say so. The intent ledger
    # already refuses a finish with anything still `pending` (05 §B); this is its
    # residue — the ledger says completed, the answer never mentions it.
    "unexplained_gap",
    # The prose disagrees with the rows the turn produced. Nothing in the runtime can see
    # this, and unlike the two above it is checkable against evidence the judge is holding.
    "contradicts_result",
)

ASK_USER_VIOLATIONS: tuple[str, ...] = (
    # The question is posed in schema terms rather than the user's. `scrub_answer_prose`
    # already runs on the question (agent_loop.py, ISSUES I1), which is the argument FOR
    # this check and not against it: the scrub turns "which AnnualSalary did you mean?"
    # into "which [schema detail withheld] did you mean?", which is 05 §L.3's trap — a
    # half-redacted string that is neither usable nor honest. The judge's job is to get a
    # question that never needed redacting.
    "non_contextual_question",
    # Raw identifiers are not meaningful choices for a business user. Codes may be
    # included only beside a human-readable label (for example, "Jane Doe (E123)").
    "code_only_choices",
    # Choices embedded in the prose do not render as selectable UI controls.
    "options_in_question",
)


def violations_for_site(site: JudgeSite) -> tuple[str, ...]:
    """The closed slug set this *site* may return. An empty tuple is impossible by
        construction, so a caller may treat the result as non-empty."""
    return ASK_USER_VIOLATIONS if site == "ask_user" else ANSWER_VIOLATIONS


# --- the verdict ------------------------------------------------------------

# `feedback` becomes an instruction line in a nudge the model reads. Generous enough to
# name the part that went unanswered or the figure that disagrees, short enough that a
# runaway generation cannot displace the draft echo beside it in the same message.
MAX_FEEDBACK_CHARS = 600


@dataclass(frozen=True)
class JudgeVerdict:
    """One judgement. `violation` is `""` on approval and a member of the site's set
        otherwise; `feedback` is empty on approval and sanitised, non-empty on rejection.

        THE INVARIANT THE PARSER ENFORCES: a rejection always carries BOTH a known slug and
        non-empty feedback. A rejection the runtime cannot name is a rejection it cannot
        report on a span, and a rejection with nothing to say costs the model a round-trip
        and tells it nothing — both degrade to approval.
    """

    approved: bool
    violation: str = ""
    feedback: str = ""


# The single fail-open value. Every failure shape in this module returns THIS object, so a
# caller cannot accidentally distinguish "the judge approved" from "the judge could not
# run" and build behaviour on the difference — 09 §E's contract is that they are the same.
APPROVED = JudgeVerdict(approved=True)


# --- the brief --------------------------------------------------------------


@dataclass(frozen=True)
class JudgeBrief:
    """Everything the judge reads, and nothing else (09 §D.2).

        ALREADY SCOPE-FILTERED ON ARRIVAL. This module has no scope information of its own
        and performs no filtering — the same ordering contract `context/budget.py` states for
        the model-request path, and for the same D44 reason. *results* must be built from a
        `filter_trail`ed trail through `context/budget.py::render_entry`, which is the seam
        that keeps the judge's view identical to the model's (09 §D.3).

        *figure_corroborated* IS ONLY EVER `True` OR `None` in production, and the type is
        `bool | None` rather than `Literal[True] | None` so a test can pin that `False`
        renders honestly if a future producer ever emits one. `None` means "not checked" —
        no figure in the prose, no `result_full_ref`, a failed read, or a scan that simply
        did not find it. That last case is why `False` is not produced: 05 §L.7 works
        through why a non-match is not evidence (derived figures never match literally,
        rounding and formatting diverge), and reporting one as `False` would push the judge
        toward `contradicts_result` on exactly those answers.
    """

    site: JudgeSite
    question: str
    date_anchor: str | None = None
    # `(intent_id, description, status, reason_code)` — the ledger as the model left it.
    intents: tuple[tuple[str, str, str, str | None], ...] = ()
    assumptions: tuple[str, ...] = ()
    sql_executed: tuple[str, ...] = ()
    # Rendered tool entries, `context/budget.py::render_entry` output, verbatim.
    results: tuple[Mapping[str, Any], ...] = ()
    # The answer under judgement: `result.assistant_text` at `exit_prose`, the
    # `answerWithTable` `answer` argument at `exit_table`.
    draft: str = ""
    # `(caption, sql_or_blueprint_id)` per designated table — `exit_table` only.
    designated_tables: tuple[tuple[str | None, str], ...] = ()
    figure_corroborated: bool | None = None
    # The question the model wants to ask — `ask_user` only.
    pending_question: str = ""
    # Structured choices accompanying the question — `ask_user` only.
    pending_options: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        """The brief as a plain JSON-able document, before fitting."""
        doc: dict[str, Any] = {"question": self.question}
        if self.date_anchor:
            doc["today"] = self.date_anchor
        if self.site == "ask_user":
            doc["question_the_agent_wants_to_ask"] = self.pending_question
            doc["structured_options"] = list(self.pending_options)
        else:
            doc["draft_answer"] = self.draft
        if self.intents:
            doc["tracked_parts"] = [
                {
                    "id": intent_id,
                    "part": description,
                    "status": status,
                    "reason": reason,
                }
                for intent_id, description, status, reason in self.intents
            ]
        doc["recorded_assumptions"] = list(self.assumptions)
        if self.sql_executed:
            doc["queries_run"] = list(self.sql_executed)
        if self.designated_tables:
            doc["tables_shown_to_the_user"] = [
                {"caption": caption, "identified_by": ident}
                for caption, ident in self.designated_tables
            ]
        if self.figure_corroborated is not None:
            doc["figures_found_in_results"] = self.figure_corroborated
        if self.results:
            doc["results"] = [dict(entry) for entry in self.results]
        return doc


# Crude token estimate, chars/4 — the same heuristic `context/budget.py::_estimate_tokens`
# uses, restated rather than imported so this module stays free of the context package
# (`context/__init__` eagerly imports `context.assembly`, and the leaf-ness rule
# `sanitize.py` states applies here for the same reason). It is a BUDGET heuristic and
# under-counts JSON- and SQL-dense content, which is exactly what a brief is — see
# `_fit_payload` for the margin that pays for it.
def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _dump(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fit_payload(payload: dict[str, Any], token_budget: int) -> dict[str, Any]:
    """Fit the brief to *token_budget*, trimming RESULTS ONLY.

        THE DROP PRIORITY IS NOT `fit_request_to_budget`'S, and the difference is the whole
        reason this is not that function. There, the current turn's work is what must
        survive. Here, the question, the draft, the assumptions, the ledger and the executed
        SQL are the SUBJECT of the judgement: dropping any one of them does not shrink the
        judgement, it INVERTS it — a brief with the assumptions trimmed away reports
        `unrecorded_assumption` against a model that recorded them.

        So only `results` is droppable, largest first, in two passes: rows out of the fattest
        preview, then whole entries. EVERY DROP IS MARKED. The rendered preview already
        carries `truncated`, and the judge prompt is told what it means; an entry dropped
        whole leaves a stub naming the tool. A judge that SILENTLY loses a result reports
        `unexplained_gap` for a part it could not see — the most dangerous failure in this
        design, invisible in telemetry because the verdict looks like every other rejection
        (09 §D.6).

        Returns a NEW payload; the input is not mutated.
    """
    fitted = dict(payload)
    results = [dict(entry) for entry in fitted.get("results", ())]
    if not results or _estimate_tokens(_dump(fitted)) <= token_budget:
        return fitted

    def _size(entry: Mapping[str, Any]) -> int:
        return _estimate_tokens(_dump(entry))

    # Pass 1: rows out of the fattest preview, one entry at a time, re-measuring after
    # each so a single wide result cannot force every other preview to be emptied.
    for _ in range(len(results)):
        fitted["results"] = results
        if _estimate_tokens(_dump(fitted)) <= token_budget:
            return fitted
        fattest = max(range(len(results)), key=lambda i: _size(results[i]))
        preview = results[fattest].get("result_preview")
        if not isinstance(preview, dict) or not preview.get("preview_rows"):
            break
        trimmed = dict(preview)
        trimmed["preview_rows"] = []
        trimmed["truncated"] = True
        results[fattest] = {**results[fattest], "result_preview": trimmed}

    # Pass 2: whole entries, oldest first — a later result is likelier to be the one the
    # answer was written from. The stub keeps the COUNT honest, so the judge can see that
    # it is not holding everything the turn produced.
    while results:
        fitted["results"] = results
        if _estimate_tokens(_dump(fitted)) <= token_budget:
            return fitted
        dropped = results.pop(0)
        results.insert(
            0,
            {
                "tool_name": dropped.get("tool_name"),
                "status": dropped.get("status"),
                "result_preview": None,
                "omitted_for_size": True,
            },
        )
        if all(entry.get("omitted_for_size") for entry in results):
            break

    fitted["results"] = results
    return fitted


# --- the forced tool --------------------------------------------------------

JUDGE_TOOL_NAME = "record_judgement"


def build_judge_tool(site: JudgeSite) -> dict[str, Any]:
    """The single forced tool for *site*, in the runtime's canonical flat shape.

        The `violation` enum is DERIVED from `violations_for_site`, never re-spelled: a model
        cannot be offered a slug the span allowlist and the tuning queries have no meaning
        for, and adding a complaint is one edit at the vocabulary.
    """
    slugs = violations_for_site(site)
    return {
        "type": "function",
        "name": JUDGE_TOOL_NAME,
        "description": (
            "Record your judgement of this turn. Call this exactly once. Do not emit "
            "free text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "approved": {
                    "type": "boolean",
                    "description": (
                        "True when the answer may be sent to the user as it stands. "
                        "Approve when in doubt: a wrong rejection costs the user a "
                        "correct answer they were about to receive."
                    ),
                },
                "violation": {
                    "type": "string",
                    "enum": ["", *slugs],
                    "description": (
                        "Empty string when approved. Otherwise the single most "
                        "fundamental problem — not every problem you can see."
                    ),
                },
                "feedback": {
                    "type": "string",
                    "description": (
                        "Empty string when approved. Otherwise one or two sentences "
                        "addressed TO THE AGENT, naming exactly what to change. The "
                        "agent will read this and gets one more attempt, so state the "
                        "fix, not the fault. Never quote SQL, a table or a column name."
                    ),
                },
            },
            "required": ["approved", "violation", "feedback"],
        },
    }


# --- the prompts ------------------------------------------------------------

# WHY THE AGENT'S OWN SYSTEM PROMPT IS NOT SENT HERE (09 §D.5): it is ~2,824 tokens per
# call, and including it turns the judge into a conformance checker for every rule in it,
# after which a prompt edit silently changes judge behaviour with nothing testing it. What
# follows restates ONLY the rules these criteria test.
_ANSWER_JUDGE_PROMPT = (
    "You are reviewing the final answer an HR/payroll data-analysis agent is about to "
    "send to a user. You see the user's question, what the agent ran, previews of what "
    "came back, and the answer it drafted.\n"
    "\n"
    "APPROVE UNLESS ONE OF THESE IS TRUE:\n"
    "- unrecorded_assumption: the answer rests on a choice the user cannot see and that "
    "`recorded_assumptions` does not carry — a period the question did not name, a "
    "population narrowed one way rather than another, a definition picked among "
    "several.\n"
    "- unexplained_gap: part of what the user asked for is not in the answer, and the "
    "answer does not say that it is missing or why.\n"
    "- contradicts_result: the answer states something the results contradict — a "
    "direction, a ranking, a figure that is not what the rows show.\n"
    "\n"
    "THINGS THAT ARE NOT VIOLATIONS, and rejecting for them is an error:\n"
    "- An assumption in `recorded_assumptions` that the answer does not repeat. The "
    "agent is instructed NOT to repeat recorded assumptions; the user is shown them "
    "separately. Absence from the answer text is CORRECT.\n"
    "- A choice THE ANSWER ITSELF states in plain words. The test is whether the user "
    "can SEE the choice, not which channel carried it: \"the average is $125,000 across "
    "2 active employees\" has disclosed the population, and demanding the same fact "
    "again in `recorded_assumptions` is rejecting a transparent answer. Read the draft "
    "for the disclosure BEFORE reporting unrecorded_assumption.\n"
    "- Formatting, markdown, tables, SQL or schema names in the answer. Separate checks "
    "own all of these and have already run.\n"
    "- An answer reporting a window that ends at the latest data on record rather than "
    "at today. The agent is instructed to do this for data-anchored analyses. It is a "
    "violation only if the answer never says so — and then it is unrecorded_assumption.\n"
    "- A figure you cannot verify from the previews. You see at most 20 rows of each "
    "result, and a `truncated` preview or an `omitted_for_size` entry means there was "
    "more the agent could see and you cannot. Never fault the agent for what you were "
    "not shown. When `figures_found_in_results` is true, a figure in the answer was "
    "located in the full results — treat it as verified. Its ABSENCE means nothing was "
    "checked, never that a figure is missing.\n"
    "- An empty result. A correct query returning no rows is an answer.\n"
    "\n"
    "You are not checking whether the query measured the right thing, and you are not "
    "grading style. Report the single most fundamental problem or approve."
)

_ASK_USER_JUDGE_PROMPT = (
    "You are reviewing a clarifying question an HR/payroll data-analysis agent wants to "
    "ask a user. The user is a business user: they know their own business, they have "
    "never seen the database, and they cannot answer a question about it.\n"
    "\n"
    "REJECT AS non_contextual_question WHEN the question can only be answered by "
    "someone who knows the schema — it names columns, tables, codes or internal field "
    "names, or asks the user to choose between them. The same choice asked in the "
    "user's own terms is fine: not which of two fields to read, but which THING they "
    "want measured.\n"
    "\n"
    "REJECT AS code_only_choices when the question or structured options present raw "
    "codes/IDs without the human-readable information needed to identify them. A code "
    "may be shown only together with its label. For employees require the employee name "
    "and code, for departments require the department name and code, and apply the same "
    "rule to every other coded entity (location, job, cost center, earning code, etc.). "
    "For example, reject 'E1042' but accept 'Jane Doe (E1042)'; reject 'D07' but accept "
    "'Sales (D07)'.\n"
    "\n"
    "REJECT AS options_in_question when the question prose enumerates choices but "
    "structured_options is empty, or when additional choices are hidden in the question "
    "instead of being represented in structured_options. Tell the agent to put the "
    "choices in the options field. There may be at most five structured options, so it "
    "must consolidate a longer list.\n"
    "\n"
    "APPROVE otherwise, including when the question is merely long, or when you think "
    "the agent could have worked the answer out for itself. You are checking that the "
    "question is ANSWERABLE by this user, not whether it needed asking."
)


def _system_prompt(site: JudgeSite) -> str:
    return _ASK_USER_JUDGE_PROMPT if site == "ask_user" else _ANSWER_JUDGE_PROMPT


# --- the guard on what comes back -------------------------------------------


def parse_verdict(result: ModelTurnResult, site: JudgeSite) -> JudgeVerdict:
    """One judge turn → a guarded `JudgeVerdict`. Every unusable shape returns `APPROVED`.

        THE GUARDS ARE DERIVED FROM WHAT THE CALLER DOES WITH EACH FIELD, not from the field
        names (the discipline `learning/judge/schema.py` states):

          `approved`  | branched on          ⇒ must be a real `bool`; anything else is
          |             unusable, and truthiness would read `"false"` as approval.
          `violation` | span attribute + the tuning GROUP BY key ⇒ must be a MEMBER of the
          |             site's closed set. An unknown slug is a rejection the runtime
          |             cannot name or report, so it degrades to approval rather than to a
          |             nameless refusal.
          `feedback`  | spliced into a model-facing nudge ⇒ structurally sanitised and
          |             capped. EMPTY feedback on a rejection also degrades to approval: a
          |             refusal with nothing to say costs a round-trip and teaches nothing.

        `tool_calls` is checked as a CONTAINER and as MEMBERS — `ModelClient` is a Protocol,
        and a bare string char-explodes into an `AttributeError` on `.name`.
    """
    calls = result.tool_calls
    if not isinstance(calls, list | tuple):
        _logger.warning(
            "answer judge: tool_calls was %s, not a sequence — approving",
            type(calls).__name__,
        )
        return APPROVED
    call = next(
        (
            c
            for c in calls
            if getattr(c, "name", None) == JUDGE_TOOL_NAME and hasattr(c, "arguments")
        ),
        None,
    )
    if call is None:
        _logger.warning(
            "answer judge: model did not call %s (got %r) — approving",
            JUDGE_TOOL_NAME,
            [getattr(c, "name", type(c).__name__) for c in calls],
        )
        return APPROVED
    arguments = call.arguments
    if not isinstance(arguments, dict):
        _logger.warning(
            "answer judge: %s arguments were %s, not an object — approving",
            JUDGE_TOOL_NAME,
            type(arguments).__name__,
        )
        return APPROVED

    approved = arguments.get("approved")
    if not isinstance(approved, bool):
        _logger.warning(
            "answer judge: approved was %r, not a boolean — approving", approved
        )
        return APPROVED
    if approved:
        return APPROVED

    violation = arguments.get("violation")
    if violation not in violations_for_site(site):
        _logger.warning(
            "answer judge: rejected with violation %r, not one of %s — approving",
            violation,
            list(violations_for_site(site)),
        )
        return APPROVED

    raw_feedback = arguments.get("feedback")
    feedback = (
        sanitize_text(raw_feedback, MAX_FEEDBACK_CHARS)
        if isinstance(raw_feedback, str)
        else ""
    )
    if not feedback:
        _logger.warning(
            "answer judge: rejected as %s with no usable feedback — approving", violation
        )
        return APPROVED
    return JudgeVerdict(approved=False, violation=str(violation), feedback=feedback)


# --- the nudges -------------------------------------------------------------


def answer_judge_nudge_text(draft: str | None, feedback: str) -> str:
    """The ephemeral `user`-role message injected when the judge refuses a finish at
        exit #1.

        IT CARRIES THE DRAFT BACK, for `finalization.py::answer_shape_nudge_text`'s reason
        and with its marking: exit #1 persists NOTHING (a persisted draft would surface in
        `/session/history` as something the user said) and D22 discards free text around tool
        calls, so this echo is the model's only surviving copy — and an unmarked truncation
        beside an instruction to re-send would lose the tail silently.

        IT OPENS BY SAYING THE TURN IS NOT OVER. 05 §J.4/§K.5: the measured response to a
        refusal is a belief about turn MECHANICS, not about content — the model apologising
        for being unable to act, on a turn nothing had ended.

        IT DOES NOT NAME THE VIOLATION SLUG. The slug is runtime vocabulary for telemetry;
        what the model needs is the sentence the judge wrote about THIS answer. Naming the
        category as well would invite the model to argue with the taxonomy instead of fixing
        the answer.
    """
    lines: list[str] = []
    if draft and draft.strip():
        stripped = draft.strip()
        echo = stripped[:MAX_NUDGE_DRAFT_CHARS]
        if len(echo) < len(stripped):
            echo += " …[truncated]"
        lines.append(f"You drafted: {echo}")
        lines.append("")
    lines.append("That answer was NOT sent. A review of it against this turn found:")
    lines.append(feedback)
    lines.append(
        "The turn is NOT over and every tool is still available to you. Send your answer "
        "again with that fixed — the rest of it unchanged. If the fix is an assumption "
        "the user cannot see, call recordAssumptions first and state it in one plain "
        "sentence; if part of the question went unanswered, say so in the answer itself."
    )
    return "\n".join(lines)


def ask_user_judge_nudge_text(question: str, feedback: str) -> str:
    """The ephemeral `user`-role message injected when the askUser judge refuses the
        question the model wanted to ask.

        IT ECHOES THE QUESTION BACK, for the reason every other nudge echoes the draft: the
        `askUser` call is INTERCEPTED and never persisted, so nothing on the trail carries the
        question and D22 discards the free text around the call. Without the echo the model is
        asked to rewrite something it can no longer see.

        IT OPENS BY SAYING THE TURN IS NOT OVER. 05 §J.4/§K.5 establish that the measured
        response to a refusal is a belief about turn mechanics rather than about content — the
        model apologising for being unable to act on a turn nothing had ended.

        *question* is the RAW argument, pre-scrub: the judge refused it precisely because the
        scrub would have had to redact it, and echoing the redacted form back would ask the
        model to repair a string it did not write. It is structurally sanitised here for the
        same reason `feedback` is — it re-enters model context as part of an instruction.
    """
    lines: list[str] = []
    asked = sanitize_text(question, MAX_FEEDBACK_CHARS) if question else ""
    if asked:
        lines.append(f"You were about to ask: {asked}")
        lines.append("")
    lines.append(
        "That question was NOT sent. The user has not seen it, and it is not suitable "
        "as written."
    )
    lines.append(feedback)
    lines.append(
        "The turn is NOT over and every tool is still available to you. Ask the same "
        "thing again in the user's own words. Put every choice in the options field "
        "(maximum five), never as a list in the question. Never offer a bare code or ID: "
        "pair it with its human-readable label, such as employee name plus employee code "
        "or department name plus department code. If you can pick a sensible default, "
        "take it and record it with recordAssumptions instead of asking."
    )
    return "\n".join(lines)


# --- the judge --------------------------------------------------------------


@dataclass
class AnswerJudge:
    """One model call, one verdict, no retries (09 §E).

        NO RETRY, DELIBERATELY. The judge's output is a control signal, not a product: a
        retry budget is right for a call whose result IS the work and wrong for one whose
        result only decides whether to spend another round. A second attempt also doubles
        the latency added to the terminal path of a wall-clock-bounded turn.

        DISABLED IS INDISTINGUISHABLE FROM APPROVED, by construction — `review` returns the
        same `APPROVED` object either way. `answer_judge_enabled` defaults False (09 §L):
        the loop is byte-deterministic by design (D45) and this is a non-deterministic gate
        on the terminal path, so a deployment opts in and the scripted-mechanics suite runs
        with it off unless it is driving the judge on purpose.

        THE OBSERVER IS OPTIONAL AND THE EVENTS ARE THE LOOP'S. Only `loop_answer_judge_
        called` and `loop_answer_judge_failed` are emitted here, because nothing observable
        happens between the call and them; refusals, exhaustions and skips are emitted at
        the loop's own sites, interleaved with draft clears and nudge splicing.
    """

    model_client: ModelClient
    token_budget: int
    enabled: bool = True
    timeout_seconds: float = 20.0
    observer: Any = None
    # The turn's `Tracer`. `None` (Layer-1 tests, an unconfigured deploy) means no
    # judge span is opened and the auto-instrumented LLM span parents to whatever is
    # ambient, exactly as before this was wired.
    tracer: Any = None
    _tools: dict[JudgeSite, list[dict[str, Any]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def _tools_for(self, site: JudgeSite) -> list[dict[str, Any]]:
        if site not in self._tools:
            self._tools[site] = [build_judge_tool(site)]
        return self._tools[site]

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            self.observer(event, dict(payload))
        except Exception:  # pragma: no cover - an observer must never break a turn
            _logger.exception("answer judge: observer raised on %s", event)

    def messages_for(self, brief: JudgeBrief) -> list[dict[str, Any]]:
        """The canonical request for *brief* — exposed so a test can assert what the judge
                was shown without reaching into `review`."""
        fitted = _fit_payload(brief.payload(), self.token_budget)
        return [
            {"role": "system", "content": _system_prompt(brief.site)},
            {"role": "user", "content": _dump(fitted)},
        ]

    async def review(self, brief: JudgeBrief) -> JudgeVerdict:
        """Judge one finish. Never raises, never returns `None`, never retries."""
        if not self.enabled:
            return APPROVED
        with self._span(brief.site) as judge_span:
            return await self._review(brief, judge_span)

    def _span(self, site: JudgeSite) -> Any:
        """The judge's own CHAIN span, or a no-op context when no tracer is wired.

        WHY IT MATTERS THAT THIS WRAPS THE CALL rather than annotating it afterwards:
        the OpenAI SDK is auto-instrumented, so the judge's round-trip already emits an
        `LLM` span — one indistinguishable from the agent's own. Opening a span AROUND
        the call makes that LLM span its child through ambient context, so the turn trace
        reads `agent.turn -> answer_judge -> LLM` and judge cost and latency become
        attributable. 09 §I makes that the ONLY place judge spend is observable, since it
        is deliberately outside `max_window_token_spend`.
        """
        if self.tracer is None:
            return nullcontext()
        return tracing.answer_judge_span(self.tracer, site=site)

    async def _review(self, brief: JudgeBrief, judge_span: Any) -> JudgeVerdict:
        """`review`'s body, inside the span. Split so the span wraps EVERY exit — the
        timeout and provider-error paths included, which is where latency questions are
        actually answered."""

        def _mark(**attributes: Any) -> None:
            # SHAPE ONLY (D25): closed runtime vocabularies and counts. `feedback` and the
            # brief never touch a span — a span attribute bypasses the observer allowlist
            # entirely, so the discipline has to hold here by hand.
            if judge_span is None or not hasattr(judge_span, "set_attribute"):
                return
            for key, value in attributes.items():
                if isinstance(value, str | int | float | bool):
                    judge_span.set_attribute(key, value)

        try:
            # INSIDE the try, so `review`'s "never raises" is true by construction rather
            # than by `json.dumps(default=str)` happening to tolerate every brief.
            messages = self.messages_for(brief)
            tools = self._tools_for(brief.site)
            client = begin_turn_client(self.model_client)
            result = await asyncio.wait_for(
                client.send_turn(messages, tools), timeout=self.timeout_seconds
            )
        except TimeoutError:
            _logger.warning(
                "answer judge: timed out after %.1fs at %s — approving",
                self.timeout_seconds,
                brief.site,
            )
            self._emit(ANSWER_JUDGE_FAILED_EVENT, {"reason": "timeout"})
            _mark(outcome="timeout")
            return APPROVED
        except asyncio.CancelledError:
            # NOT swallowed. A cancellation is the turn being torn down, not a judge
            # failure, and converting it into an approval would let a cancelled turn
            # finalize an answer the caller is no longer waiting for.
            raise
        except Exception:
            _logger.exception("answer judge: provider error at %s — approving", brief.site)
            self._emit(ANSWER_JUDGE_FAILED_EVENT, {"reason": "provider_error"})
            _mark(outcome="provider_error")
            return APPROVED

        usage = result.usage if isinstance(result.usage, Mapping) else {}
        total = usage.get("total_tokens")
        # 09 §I: judge spend is deliberately OUTSIDE `max_window_token_spend`, so this
        # event is the only place it is observable at all. Telemetry, never control flow.
        self._emit(
            ANSWER_JUDGE_CALLED_EVENT,
            {
                "site": brief.site,
                "tokens": total if isinstance(total, int) else 0,
            },
        )
        verdict = parse_verdict(result, brief.site)
        _mark(
            approved=verdict.approved,
            tokens=total if isinstance(total, int) else 0,
        )
        if not verdict.approved:
            _mark(violation=verdict.violation)
            return verdict
        # A malformed response has already been logged by `parse_verdict`; the `failed`
        # event is emitted here so the loop's telemetry can tell "the judge approved" from
        # "the judge could not be read", which the returned value deliberately cannot.
        if _looks_malformed(result, brief.site):
            self._emit(ANSWER_JUDGE_FAILED_EVENT, {"reason": "malformed"})
        return verdict


def _looks_malformed(result: ModelTurnResult, site: JudgeSite) -> bool:
    """Whether this response approved because it could not be READ, rather than because the
        judge approved. Kept separate from `parse_verdict` so that function has ONE return
        shape and the caller cannot branch on the difference (09 §E) — this is for the event
        only, and is deliberately cheap and approximate."""
    calls = result.tool_calls
    if not isinstance(calls, list | tuple):
        return True
    call = next((c for c in calls if getattr(c, "name", None) == JUDGE_TOOL_NAME), None)
    if call is None or not isinstance(getattr(call, "arguments", None), dict):
        return True
    approved = call.arguments.get("approved")
    if not isinstance(approved, bool):
        return True
    if approved:
        return False
    # Rejected, yet `parse_verdict` returned an approval: the slug or the feedback was
    # unusable.
    return (
        call.arguments.get("violation") not in violations_for_site(site)
        or not isinstance(call.arguments.get("feedback"), str)
        or not sanitize_text(call.arguments["feedback"], MAX_FEEDBACK_CHARS)
    )
