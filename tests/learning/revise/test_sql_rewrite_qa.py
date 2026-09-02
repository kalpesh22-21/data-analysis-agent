"""§C.5 ADVERSARIAL, ENGINE HALF — what the reviser will and will not hand a reviewer.

`test_sql_rewrite.py` pins the contract: the tool, the prompt, the caution, the composite refusal
and the read-only guard. This file goes after the EDGES of that contract, and it is organised
around one question — WHICH of the rules a rewritten query must obey are enforced HERE, and which
are only asked for in the prompt and enforced later.

That distinction is the whole value of the file. `check_read_only_select` is a GUARD: an unusable
rewrite is discarded before the reviewer ever sees it, and the engine says so in a sentence. The
frozen-run-date rule is not — it is a paragraph in the system prompt, so a model that ignores it
produces a proposal that looks exactly like a good one, and the reviewer finds out only after
applying it. Both are defensible; neither is obvious from reading the code; and a future change
that moved one into the other's category would break nothing today.
"""

from __future__ import annotations

import pytest

from data_agent.learning.mint.models import MAX_SQL_CHARS
from data_agent.learning.revise import SQL_REWRITE_CAUTION
from data_agent.learning.revise.schema import REVISE_TOOL_NAME
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .test_reviser import declined_blueprint, make_reviser
from .test_sql_rewrite import _ENTRY, NEW_SQL, RATIO_SQL


def _turn(*, sql: object = NEW_SQL, entries: object = None, **extra: object) -> ModelTurnResult:
    arguments: dict[str, object] = {
        "entries": [_ENTRY] if entries is None else entries,
        "rationale": "the ratio's denominator cannot be classified",
        **extra,
    }
    if sql is not None:
        arguments["sql"] = sql
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="r1", name=REVISE_TOOL_NAME, arguments=arguments)]
    )


# --- the size cap, at its boundary --------------------------------------------


def _padded(total: int) -> str:
    """A legal SELECT padded with a trailing comment to EXACTLY *total* characters."""
    head = f"{NEW_SQL} -- "
    return head + "x" * (total - len(head))


async def test_a_rewrite_of_exactly_the_cap_is_still_offered() -> None:
    """THE INCLUSIVE SIDE OF THE BOUNDARY, which an off-by-one would move silently: the guard is
    `len(raw) > cap`, so a query of exactly `MAX_SQL_CHARS` is legal. Worth a test because the cap
    is IMPORTED from `learning/mint` rather than declared here — a change to mint's limit must
    move this boundary too, and an inclusive/exclusive slip is the way that goes wrong."""
    sql = _padded(MAX_SQL_CHARS)
    assert len(sql) == MAX_SQL_CHARS
    reviser, _ = make_reviser([_turn(sql=sql)])

    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    assert proposal.sql_changed is True
    assert proposal.sql == sql
    assert proposal.caution == SQL_REWRITE_CAUTION


async def test_one_character_over_the_cap_drops_the_rewrite_but_keeps_the_entries() -> None:
    """⚠ THE EXCLUSIVE SIDE, AND THE HALF-PROPOSAL QUESTION. An oversized rewrite is DROPPED, not
    truncated — a truncated query is a different query that might still parse. What survives is
    the interesting part: the entries still come back, because they are a usable proposal for the
    query the candidate already has, and `replace` is NOT forced, because nothing invalidated the
    existing classifications. A dropped rewrite must leave the request looking exactly like one
    that never asked for a rewrite at all."""
    reviser, _ = make_reviser([_turn(sql=_padded(MAX_SQL_CHARS + 1))])

    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    assert proposal.sql_changed is False
    assert proposal.sql == ""
    assert proposal.caution == ""
    assert proposal.entries  # the entries are still worth applying
    assert proposal.replace is False


# --- an unusable rewrite must not leave half a proposal behind ----------------


@pytest.mark.parametrize(
    "entries", [[], "not-a-list", [7], None], ids=["empty", "stringified", "scalars", "omitted"]
)
async def test_a_rewrite_with_no_usable_entries_takes_the_rewrite_down_with_it(
    entries: object,
) -> None:
    """⚠ A REWRITE WITH NOTHING TO CLASSIFY IS THE WORST SHAPE TO OFFER, because it looks
    plausible: a reviewer shown a new query and an empty entries box would apply it and get a
    `totality_violation` naming predicates of a query they had no chance to classify — and would
    have paid for it with the candidate's provenance, since applying it makes the row authored.

    `parse_proposal` refuses the WHOLE response when no entry survives coercion, so the `sql`
    goes with the entries rather than being offered on its own. The four shapes are the ones a
    live model actually produces — an omitted array, a stringified one, scalars where objects
    belong, and the field left out entirely — and all four must land on the same "no suggestion"
    outcome rather than on four different ones.
    """
    reviser, _ = make_reviser([_turn(entries=entries if entries is not None else [])])

    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    assert proposal.entries == ()
    assert proposal.sql == ""
    assert proposal.sql_changed is False
    assert proposal.reason


async def test_a_structurally_incomplete_rewrite_entry_is_withheld_before_apply() -> None:
    """Proposal-time validation reuses the extractor's vocabulary and offers no broken draft."""
    reviser, _ = make_reviser([_turn(entries=[{"role": "inline"}])])

    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    assert proposal.entries == ()
    assert proposal.sql_changed is False
    assert "parameterization do not agree" in proposal.reason


async def test_a_candidate_with_no_snapshot_is_refused_before_any_model_call() -> None:
    """The rewrite opt-in cannot manufacture a subject. With no `ValidationSnapshot` there is no
    accepted SQL to replace, and the honest handling is the one the ordinary path already has: a
    reason, not an exception — and NO MODEL TURN, because nothing about the answer could change
    the outcome, and a turn on a candidate with nothing to show would brief the model on an empty
    query."""
    reviser, client = make_reviser([_turn()])

    proposal = await reviser.propose(
        declined_blueprint(with_snapshot=False), feedback="rewrite it", allow_sql=True
    )

    assert proposal.sql_changed is False
    assert proposal.entries == ()
    assert "no re-validation snapshot" in proposal.reason
    assert client.calls == []


# --- the echo test, at its edges ----------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "expect_rewrite"),
    [
        ("  \n" + RATIO_SQL + "\t ", False),
        (RATIO_SQL.replace("SELECT", "select"), True),
        (RATIO_SQL.replace("FROM", "\nFROM"), True),
        (RATIO_SQL + " -- unchanged, just annotated", True),
    ],
    ids=["surrounding_whitespace", "recased", "reflowed", "commented"],
)
async def test_only_a_byte_identical_echo_escapes_being_called_a_rewrite(
    mutation: str, expect_rewrite: bool
) -> None:
    """⚠ CURRENT BEHAVIOUR, AND THE COST OF IT STATED PLAINLY.

    The echo guard is a STRING comparison against `last_sql(...).strip()`. Surrounding whitespace
    is forgiven — a model shown a query in a brief routinely pads it — but nothing else is. A
    model that returns the same query re-cased, reflowed onto new lines, or with a comment
    appended has proposed a REWRITE as far as this system is concerned: the candidate becomes
    hand-authored, loses its provenance chain and loses the ability to auto-land, in exchange for
    a query that is semantically identical.

    That is the SAFE direction (an unnoticed rewrite would be far worse than a noticed non-change)
    and a normalized comparison would need an AST, which is a real decision rather than a tidy-up.
    Pinned so the trade is visible, and so a future canonicalizing comparison has to argue with a
    test that spells out what it is buying.
    """
    reviser, _ = make_reviser([_turn(sql=mutation)])

    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    offered = expect_rewrite and mutation == NEW_SQL
    # Semantically identical rewrites with an incomplete replacement entry list are now
    # withheld; applying them would discard the old entries and fail totality.
    if expect_rewrite:
        assert proposal.sql_changed is False
        assert "parameterization do not agree" in proposal.reason
    else:
        assert proposal.sql_changed is False
    assert bool(proposal.caution) is offered
    # `replace` is forced exactly when a rewrite happened, and never otherwise.
    assert proposal.replace is offered


# --- which rules are GUARDS here, and which are only prompt -------------------


DATED_REWRITE = (
    "SELECT department, sum(deductions) AS deductions FROM payroll.payroll_fact "
    "WHERE register_type = 'DDUCT' "
    "AND dateDiff('day', pay_date, toDateTime64('2026-09-01 00:00:00', 3)) < 30 "
    "GROUP BY department"
)


async def test_a_frozen_run_date_is_refused_before_the_reviewer_ever_sees_it() -> None:
    """⚠ FIXED — the asymmetry this test recorded is gone, and this was the requirement.

    Two rules a rewritten query must obey. `read_only_select` was always a GUARD in this engine:
    an offending query is discarded and the reviewer is told, in a sentence, before they commit
    to anything. The frozen-run-date rule was only `DATE_RULE`, a paragraph in the rewrite system
    prompt, and nothing re-read the answer for it — so a model that pasted today's date into a
    `dateDiff` produced a proposal INDISTINGUISHABLE from a good one, and the reviewer learned
    about the frozen date only after applying it, from a `fail_to_review` they had to decode.

    Now both are guards, adjudicated by the SAME walk that will read the derived template later
    (`frozen_date_literals`, which `check_no_frozen_date_literal` is the boolean of). The argument
    for putting it here is `engine.py`'s own: it "costs a sentence on a 200 instead of a
    `fail_to_review` on a candidate the reviewer already committed to".

    THE REASON NAMES THE LITERAL. "Your query froze the run date" sends someone diffing two
    queries by eye; the string itself is something they can search for.
    """
    reviser, _ = make_reviser([_turn(sql=DATED_REWRITE)])

    proposal = await reviser.propose(
        declined_blueprint(), feedback="last 30 days only", allow_sql=True
    )

    assert proposal.sql_changed is False
    assert proposal.sql == ""
    assert proposal.entries == ()
    assert "2026-09-01 00:00:00" in proposal.reason
    assert "today()" in proposal.reason
    # ...and the caution is NOT the vehicle for this: it is the generic provenance warning, shown
    # only when a rewrite is actually being offered.
    assert proposal.caution == ""
    # The rule the engine already guarded still names itself the same way.
    unusable, _ = make_reviser([_turn(sql="DROP TABLE payroll.payroll_fact")])
    refused = await unusable.propose(declined_blueprint(), feedback="f", allow_sql=True)
    assert refused.sql_changed is False
    assert "read-only SELECT" in refused.reason


async def test_a_date_literal_an_entry_adjudicates_is_not_treated_as_frozen() -> None:
    """⚠ THE FALSE-POSITIVE SIDE, which is what makes running the check on the RAW query rather
    than on the derived template safe.

    A date-shaped literal sitting in a comparison the S3 enumerator recognizes is an ordinary
    literal predicate — a reviewer will classify it, and in the template it becomes a `{slot}` and
    disappears. Being adjudicated is exactly conditions (a)+(b) of the check, so it passes on the
    raw query too: the raw form cannot flag something the template would not. If that stopped
    being true, this test fails before anyone loses a good rewrite to a guard.
    """
    dated_but_adjudicated = (
        "SELECT department, sum(deductions) AS deductions FROM payroll.payroll_fact "
        "WHERE register_type = 'DDUCT' AND pay_date >= '2026-01-01' GROUP BY department"
    )
    date_entry = {
        "locator": {
            "table": "payroll.payroll_fact",
            "column": "pay_date",
            "value": "2026-01-01",
        },
        "role": "slot",
        "slot": {
            "name": "start_date",
            "type": "as_of_date",
            "binds_to": "payroll.payroll_fact.pay_date",
            "required": True,
        },
    }
    reviser, _ = make_reviser(
        [_turn(sql=dated_but_adjudicated, entries=[_ENTRY, date_entry])]
    )

    proposal = await reviser.propose(
        declined_blueprint(), feedback="from January", allow_sql=True
    )

    assert proposal.sql_changed is True
    assert proposal.sql == dated_but_adjudicated


# --- the withheld-scan refusal ------------------------------------------------


class _RecordingTracer:
    """Captures the spans the reviser emits; shaped like the tracer `revise_span` expects."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []

    def start_as_current_span(self, name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        attributes = dict(kwargs.get("attributes") or {})
        self.spans.append((name, attributes))

        class _Span:
            def __enter__(self_inner):  # noqa: N805
                return self_inner

            def __exit__(self_inner, *exc):  # noqa: N805, ANN002
                return False

            def set_attribute(self_inner, key, value):  # noqa: N805, ANN001
                attributes[key] = value

            def set_status(self_inner, *a, **k):  # noqa: N805, ANN002, ANN003
                return None

            def record_exception(self_inner, *a, **k):  # noqa: N805, ANN002, ANN003
                return None

        return _Span()


async def test_the_withheld_refusal_is_a_proposal_with_a_reason_and_its_own_span() -> None:
    """⚠ A REFUSAL THAT LEFT NO SPAN WOULD BE INDISTINGUISHABLE FROM NOBODY ASKING.

    This is the one outcome on the plane that means a GUARD WORKED — the leakage verdict is not
    clean, so the assistant is locked out of a row whose literals are being withheld from the card
    — and it is also the one a reviewer is most likely to report as "the assistant does nothing on
    this candidate". Without a distinct `withheld_scan` outcome an operator has nothing to
    substantiate it with, because this path writes nothing and calls no model.
    """
    tracer = _RecordingTracer()
    reviser, client = make_reviser([_turn()], tracer=tracer)

    proposal = reviser.refuse_withheld(declined_blueprint(), reason="scan settled quarantine")

    assert proposal.entries == ()
    assert proposal.sql == ""
    assert proposal.sql_changed is False
    assert proposal.reason == "scan settled quarantine"
    assert client.calls == []
    outcomes = [attrs.get("learning.revise.outcome") for _n, attrs in tracer.spans]
    assert "withheld_scan" in outcomes


async def test_the_rewrite_outcome_is_traced_apart_from_an_ordinary_proposal() -> None:
    """Two outcomes, not one flag on one outcome: `proposed_sql_rewrite` is the event an operator
    has to be able to COUNT. It is the only reviser outcome that costs a candidate its provenance,
    so "how often is the assistant rewriting queries" must be answerable without joining spans to
    candidate rows after the fact."""
    tracer = _RecordingTracer()
    reviser, _ = make_reviser([_turn()], tracer=tracer)

    await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    outcomes = [attrs.get("learning.revise.outcome") for _n, attrs in tracer.spans]
    assert "proposed_sql_rewrite" in outcomes
    assert "proposed" not in outcomes


async def test_an_unusable_rewrite_traces_its_own_outcome_too() -> None:
    """The discard is a MEASUREMENT, not just a message: a model repeatedly returning
    unparseable SQL is a prompt problem, and it is invisible if it shares an outcome with "the
    assistant had no suggestion"."""
    tracer = _RecordingTracer()
    reviser, _ = make_reviser([_turn(sql="DELETE FROM payroll.payroll_fact")], tracer=tracer)

    await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)

    outcomes = [attrs.get("learning.revise.outcome") for _n, attrs in tracer.spans]
    assert "sql_rewrite_unusable" in outcomes


# --- the brief the model is given ---------------------------------------------


async def test_the_rewrite_brief_shows_the_accepted_sql_and_the_closed_column_list() -> None:
    """A rewrite is only as safe as what the model was GROUNDED on. The brief has to carry the
    query being replaced (or the model rewrites blind) and the catalog columns it may read (or it
    invents column names that then fail `explain_ok` — a decline the reviewer has to decode). The
    system prompt must be the REWRITE one, not the default: offering a `sql` field while telling
    the model it cannot change the query is the contradiction the two-prompt split exists to
    prevent."""
    reviser, client = make_reviser([_turn()])

    await reviser.propose(declined_blueprint(), feedback="drop the ratio", allow_sql=True)

    messages, tools = client.calls[0]
    system, brief = messages[0]["content"], messages[1]["content"]
    assert "YOU MAY CHANGE THE SQL" in system
    assert RATIO_SQL in brief
    assert "drop the ratio" in brief
    assert "payroll.payroll_fact.register_type" in brief
    assert "sql" in tools[0]["parameters"]["properties"]


async def test_an_oversized_reviewer_feedback_cannot_push_the_brief_out() -> None:
    """⚠ THE ONE UNBOUNDED STRING ON THIS REQUEST. The reviewer's feedback is free text pasted
    into a browser, and it sits in the same message as the validator's complaint and the accepted
    SQL — the parts the model actually has to read. Capped so a paste cannot crowd them out (or
    the context window), and capped in the BRIEF rather than at the route, so every caller of the
    engine inherits it."""
    reviser, client = make_reviser([_turn()])
    paste = "z" * 50_000

    await reviser.propose(declined_blueprint(), feedback=paste, allow_sql=True)

    brief = client.calls[0][0][1]["content"]
    assert "z" * 2_000 in brief
    assert "z" * 2_001 not in brief
    assert RATIO_SQL in brief  # ...and the part that matters survived
