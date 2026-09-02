"""§C.5 — the reviser may REWRITE the SQL, but only when a reviewer ticks the box.

Design §C.1 said the model edits entries and never emits SQL, and §H.1 rejected the alternative
outright. This slice reverses that for one reason: a reviewer looking at a candidate whose SQL is
WRONG had exactly one action available, and it was `reject`. The provenance argument was real —
the five static checks stop describing a query the warehouse answered — but it is not the only
safety argument available, and the one that replaces it is already load-bearing elsewhere.

THE ARGUMENT THAT REPLACES IT IS `learning/mint`'S. Hand-authored SQL has entered this system for
a while: it goes through the SAME completer, the SAME `to_candidate` totality walk (STRICTER
here, because the query came from outside the entries and the walk cannot be circular), the SAME
write-router stages, and it is stamped `authored=True` so `writer/routing.py::_is_authored`
forces `in_review`. It can never auto-land. A rewritten candidate is that object, so it gets that
treatment — which is what the tests below pin.

WHAT IS PINNED ON THE OTHER SIDE, and matters more: with `allow_sql=False` — every path that has
not opted in — NOTHING CHANGES. The tool is byte-identical, a response carrying `sql` is still a
422, and the system prompt still says the model cannot change the query. A licence that leaked
into the default path would be the whole reversal happening by accident.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    build_declined_envelope,
    mint_review_candidate_id,
)
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.extractor.models import Decline
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.mint.models import MAX_SQL_CHARS
from data_agent.learning.revise import (
    SQL_REWRITE_CAUTION,
    BlueprintReviser,
    ForbiddenTemplateEditError,
    SqlRewriteUnsupportedError,
    build_revise_tool,
)
from data_agent.learning.revise.prompt import (
    DATE_RULE,
    REWRITE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    system_prompt,
)
from data_agent.learning.revise.schema import REVISE_TOOL_NAME, parse_proposal
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from ..extractor.helpers import PAYROLL_SQL, make_summary, make_tool_call
from .test_reviser import ScriptedClient, declined_blueprint, make_reviser, proposal_turn

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = mint_review_candidate_id("hash-rewrite", 0)

# The SQL the fixture candidate's snapshot holds — see `test_reviser.declined_blueprint`.
RATIO_SQL = (
    "SELECT department, sum(deductions) / sum(total_earnings) AS ratio "
    "FROM payroll.payroll_fact WHERE total_earnings != '0' "
    "AND register_type IN ('DDUCT', 'EARN') GROUP BY department ORDER BY ratio DESC"
)
NEW_SQL = (
    "SELECT department, sum(deductions) AS deductions FROM payroll.payroll_fact "
    "WHERE register_type = 'DDUCT' GROUP BY department"
)

_ENTRY = {
    "locator": {"table": "payroll.payroll_fact", "column": "register_type", "value": "DDUCT"},
    "role": "inline",
    "why": "defines the metric deductions",
}


def rewrite_turn(*, sql: str | None = NEW_SQL, **extra: object) -> ModelTurnResult:
    arguments: dict[str, object] = {
        "entries": [_ENTRY],
        "rationale": "the ratio's denominator cannot be classified; asked for deductions only",
        **extra,
    }
    if sql is not None:
        arguments["sql"] = sql
    return ModelTurnResult(
        tool_calls=[ToolCallRequest(id="r1", name=REVISE_TOOL_NAME, arguments=arguments)]
    )


# --- allow_sql=False: the default path is untouched ---------------------------


def test_the_default_tool_has_no_sql_field_and_is_unchanged() -> None:
    """The schema is the primary defence, and the opt-in must not have widened it by default.

    Byte-identical to the tool this package has always built: a reviewer who did not tick the
    box gets the system §C.1 described, down to the description string that tells the model it
    cannot change the query.
    """
    default = build_revise_tool()
    assert set(default["parameters"]["properties"]) == {"entries", "replace", "rationale"}
    assert "You cannot change the SQL" in default["description"]
    assert build_revise_tool(allow_sql=False) == default


@pytest.mark.parametrize("key", ["sql", "sql_template", "template", "canonical_ast_norm"])
async def test_without_the_opt_in_a_response_carrying_sql_is_still_refused(key: str) -> None:
    """⚠ THE REVERSAL MUST NOT LEAK INTO THE DEFAULT. A model that writes SQL on a request that
    never licensed it is working against a contract this system does not have, and the honest
    handling is the one it always had — refuse, and say why."""
    reviser, _ = make_reviser([proposal_turn(arguments={
        "entries": [_ENTRY], "rationale": "r", key: NEW_SQL,
    })])
    with pytest.raises(ForbiddenTemplateEditError) as exc:
        await reviser.propose(declined_blueprint(), feedback="fix it")
    assert key in str(exc.value)


def test_the_default_system_prompt_still_forbids_it() -> None:
    """The prompt and the schema have to agree. Offering no field while saying "you may" (or
    the reverse) is the contradiction the two-prompt split exists to prevent."""
    assert system_prompt(allow_sql=False) is SYSTEM_PROMPT
    assert "YOU CANNOT CHANGE THE SQL" in SYSTEM_PROMPT
    assert "YOU MAY CHANGE THE SQL" not in SYSTEM_PROMPT


# --- allow_sql=True: the tool, the prompt, the proposal -----------------------


def test_the_opt_in_tool_gains_exactly_one_field() -> None:
    """ONE field, at the TOP LEVEL, and nothing else moves: `entries` keeps the shape the
    minter shares with it, so the two callers of `to_candidate` cannot drift."""
    default = build_revise_tool()
    opted = build_revise_tool(allow_sql=True)
    props = opted["parameters"]["properties"]
    assert set(props) == {"entries", "replace", "rationale", "sql"}
    assert props["sql"]["type"] == "string"
    assert props["entries"] == default["parameters"]["properties"]["entries"]
    assert "replacement query" in props["sql"]["description"]
    assert "replace=true" in props["sql"]["description"]
    assert "complete replacement query" in opted["description"]


def test_the_rewrite_prompt_replaces_only_the_one_paragraph() -> None:
    """The three role traps and the two transcription rules were learned from six live
    failures. A second prompt that dropped them would rediscover them."""
    assert system_prompt(allow_sql=True) is REWRITE_SYSTEM_PROMPT
    assert "YOU MAY CHANGE THE SQL, BUT PREFER NOT TO" in REWRITE_SYSTEM_PROMPT
    assert "YOU CANNOT CHANGE THE SQL" not in REWRITE_SYSTEM_PROMPT
    for shared in (
        "THE THREE CASES THAT REPEATEDLY GO WRONG",
        "TWO TRANSCRIPTION RULES",
        "Call the tool exactly once.",
    ):
        assert shared in REWRITE_SYSTEM_PROMPT and shared in SYSTEM_PROMPT


def test_the_rewrite_prompt_carries_the_date_rule() -> None:
    """⚠ THE ONE RULE A REWRITE CAN BREAK THAT RE-ROLING NEVER COULD. A run date frozen into
    the body makes the blueprint answer a different question every day it ages;
    `check_no_frozen_date_literal` refuses it downstream, so a model that was never told
    produces a `fail_to_review` the reviewer has to decode."""
    assert DATE_RULE in REWRITE_SYSTEM_PROMPT
    assert DATE_RULE not in SYSTEM_PROMPT
    assert "today()" in DATE_RULE and "dateDiff" in DATE_RULE
    assert "TWO LITERALS, NOT ONE" in DATE_RULE


async def test_a_rewrite_comes_back_with_the_sql_the_caution_and_forced_replace() -> None:
    """The three travel together. `replace` is FORCED because every existing entry describes
    the OLD query — appending to them produces a parameterization half about a string nobody
    has — and the completer refuses a rewrite without it, so the surface agrees with the guard
    rather than substituting for it."""
    reviser, client = make_reviser([rewrite_turn(replace=False)])
    proposal = await reviser.propose(
        declined_blueprint(), feedback="drop the ratio, just deductions", allow_sql=True
    )
    assert proposal.sql_changed is True
    assert proposal.sql == NEW_SQL
    assert proposal.replace is True  # forced, though the model said False
    assert proposal.caution == SQL_REWRITE_CAUTION
    assert "no longer the query the session ran" in proposal.caution
    assert "cannot auto-land" in proposal.caution
    assert "trial-run" in proposal.caution
    assert proposal.entries
    # ...and the tool it was actually offered had the field.
    _messages, tools = client.calls[0]
    assert "sql" in tools[0]["parameters"]["properties"]


async def test_rewrite_proposal_is_withheld_when_new_sql_has_an_unclassified_predicate() -> None:
    sql = (
        "SELECT department, sum(deductions) AS deductions FROM payroll.payroll_fact "
        "WHERE register_type = 'DDUCT' AND total_earnings != '0' GROUP BY department"
    )
    reviser, _ = make_reviser([rewrite_turn(sql=sql)])

    proposal = await reviser.propose(
        declined_blueprint(), feedback="keep the denominator guard", allow_sql=True
    )

    assert proposal.sql_changed is False
    assert proposal.sql == ""
    assert proposal.entries == ()
    assert "parameterization do not agree" in proposal.reason
    assert "total_earnings" in proposal.reason


async def test_the_wire_projection_carries_the_new_fields() -> None:
    """ADDITIVE and ALWAYS PRESENT. A caution that appeared only sometimes would make its
    absence indistinguishable from an older server that could not produce one."""
    reviser, _ = make_reviser([rewrite_turn()])
    wire = (
        await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)
    ).to_wire()
    assert wire["sql_changed"] is True
    assert wire["sql"] == NEW_SQL
    assert wire["caution"] == SQL_REWRITE_CAUTION
    assert wire["replace"] is True

    plain = (await make_reviser([proposal_turn()])[0].propose(
        declined_blueprint(), feedback="f"
    )).to_wire()
    assert plain["sql_changed"] is False
    assert plain["sql"] == ""
    assert plain["caution"] == ""


async def test_a_rewrite_equal_to_the_accepted_sql_is_not_a_rewrite() -> None:
    """⚠ THE ECHO CASE, and it decides whether an ordinary revision quietly becomes a
    hand-authored candidate. A model shown the accepted SQL frequently returns it verbatim,
    and treating that as a rewrite would cost the candidate its provenance and its ability to
    auto-land in exchange for no change at all."""
    reviser, _ = make_reviser([rewrite_turn(sql=f"  {RATIO_SQL}  ")])
    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)
    assert proposal.sql_changed is False
    assert proposal.sql == ""
    assert proposal.caution == ""
    # ...and `replace` is NOT forced, because nothing invalidated the existing entries.
    assert proposal.replace is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELCT nonsense FROM",
        "DELETE FROM payroll.payroll_fact WHERE department = '0420'",
        "SELECT * FROM payroll.payroll_fact",
        "INSERT INTO payroll.payroll_fact VALUES (1)",
    ],
)
async def test_an_unusable_rewrite_is_a_reason_not_an_exception(sql: str) -> None:
    """DERIVED FROM WHAT DOWNSTREAM READS: the same `check_read_only_select` that
    `decide_outcome` runs over the derived template later. A query that fails here would have
    failed there — the difference is that here it costs a sentence on a 200, instead of a
    `fail_to_review` on a candidate the reviewer already committed to."""
    reviser, _ = make_reviser([rewrite_turn(sql=sql)])
    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)
    assert proposal.sql_changed is False
    assert proposal.sql == ""
    assert proposal.reason
    assert "read-only SELECT" in proposal.reason
    # NOT offered half a proposal: entries written against a query that was discarded would
    # decline naming predicates the reviewer never saw.
    assert proposal.entries == ()


@pytest.mark.parametrize("blank", ["", "   ", None, 7, ["SELECT 1"]])
async def test_an_absent_or_placeholder_sql_means_no_rewrite(blank: object) -> None:
    """⚠ A LIVE MODEL EMITS EVERY DECLARED PROPERTY. Given an optional `sql` it returns one on
    every call, frequently empty — the shape of the question echoed back, not a claim that the
    query should become nothing."""
    reviser, _ = make_reviser([rewrite_turn(sql=blank)])  # type: ignore[arg-type]
    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)
    assert proposal.sql_changed is False
    assert proposal.entries  # the entries are still a usable proposal


async def test_an_over_long_rewrite_is_dropped_rather_than_truncated() -> None:
    """A truncated query is a DIFFERENT query that might still parse, which is the one way this
    could hand a reviewer something to approve that no model ever wrote. Capped at mint's cap,
    imported rather than restated — a rewrite and a minted draft are the same object."""
    reviser, _ = make_reviser([rewrite_turn(sql=NEW_SQL + " -- " + "x" * MAX_SQL_CHARS)])
    proposal = await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)
    assert proposal.sql_changed is False
    assert proposal.sql == ""


@pytest.mark.parametrize("key", ["sql_template", "template", "canonical_ast_norm"])
async def test_a_template_is_still_forbidden_in_rewrite_mode(key: str) -> None:
    """⚠ THE OPT-IN LICENSES ONE CLAIM, NOT TWO. A rewrite authors the ACCEPTED SQL; the
    template is still DERIVED from it by the AST rewrite, and a model-authored one would leave
    all five static checks passing about a subject nobody chose."""
    reviser, _ = make_reviser([rewrite_turn(**{key: "SELECT 1"})])
    with pytest.raises(ForbiddenTemplateEditError) as exc:
        await reviser.propose(declined_blueprint(), feedback="f", allow_sql=True)
    assert key in str(exc.value)


def test_a_nested_sql_is_still_forbidden_in_rewrite_mode() -> None:
    """Top level ONLY. An `entries[i].sql` is not a rewrite by any reading — nothing would ever
    read it — so it is the same "believing it changed something it did not" this sweep exists
    for. The depth rule is what makes the un-forbidding narrow enough to be safe."""
    turn = ModelTurnResult(
        tool_calls=[ToolCallRequest(id="r1", name=REVISE_TOOL_NAME, arguments={
            "entries": [{**_ENTRY, "sql": NEW_SQL}], "rationale": "r",
        })]
    )
    with pytest.raises(ForbiddenTemplateEditError):
        parse_proposal(turn, allow_sql=True)


def test_parse_proposal_returns_the_sql_only_when_licensed() -> None:
    """The parse boundary's whole job on this field: hand back what the model said, stripped,
    and let the engine decide whether it is usable. `allow_sql=False` never sees it — it raises
    at the sweep — so the empty answer below is the one for a mode that has no such field."""
    entries, replace_, rationale, sql = parse_proposal(rewrite_turn(), allow_sql=True)
    assert sql == NEW_SQL
    assert entries and rationale and replace_ is False  # NOT forced here; the engine forces it
    assert parse_proposal(proposal_turn(), allow_sql=True)[3] == ""


# --- composite: refused before the model call ---------------------------------


async def test_a_composite_candidate_refuses_the_rewrite_without_calling_the_model() -> None:
    """A composite blueprint's SQL lives on its NODES — one query per step, wired by a DAG the
    expert declared — so "the SQL" is not a single string a reviser could return. Refused
    BEFORE the turn, because nothing about the answer could change the outcome; `mint/schema.py`
    refuses the identical shape for the identical reason."""
    env = declined_blueprint()
    env = replace(env, payload={**env.payload, "kind": "composite"})
    reviser, client = make_reviser([rewrite_turn()])

    with pytest.raises(SqlRewriteUnsupportedError) as exc:
        await reviser.propose(env, feedback="rewrite it", allow_sql=True)

    assert str(exc.value) == (
        "SQL rewrite is not offered for composite blueprints yet: a composite has one "
        "accepted query per node, so a single replacement query would replace all of them "
        "with one. Untick the option to revise roles only"
    )
    assert client.calls == []  # no model turn was paid for
    # ...and the same candidate still revises ROLES, which is the fallback the message names.
    reviser2, _ = make_reviser([proposal_turn()])
    assert (await reviser2.propose(env, feedback="re-role it")).entries


async def test_both_composite_guards_give_the_same_reason_and_different_advice() -> None:
    """ONE RULE, TWO DOORS. The reviser refuses before the model call and the completer refuses
    at the write boundary; sharing the REASON is what stops the two drifting into describing
    different limitations, which a reviewer who met both would otherwise have to reconcile.

    The ADVICE is deliberately not shared: the reviser is answering a ticked checkbox, and the
    completer may be answering a stale form or a direct POST where there is no checkbox on the
    page. Naming a control that is not on the reader's page is the failure
    `propose_revision`'s withheld-scan message already records.

    Both real refusals are TRIGGERED here rather than inspected, so this cannot pass against a
    constant nothing raises.
    """
    from data_agent.learning.inbox.completion import (
        CompletionInputError,
        ParameterizationCompleter,
    )
    from data_agent.learning.revise.schema import COMPOSITE_REWRITE_REASON

    env = declined_blueprint()
    composite = replace(env, payload={**env.payload, "kind": "composite"})

    reviser, _ = make_reviser([rewrite_turn()])
    with pytest.raises(SqlRewriteUnsupportedError) as proposed:
        await reviser.propose(composite, feedback="rewrite it", allow_sql=True)

    completer = ParameterizationCompleter(store=InMemoryCandidateStore())
    with pytest.raises(CompletionInputError) as written:
        await completer.complete(
            composite, entries=[_ENTRY], replace_all=True, rewritten_sql=NEW_SQL
        )

    assert COMPOSITE_REWRITE_REASON in str(proposed.value)
    assert COMPOSITE_REWRITE_REASON in str(written.value)
    # ...and each names the step that exists on ITS caller's page.
    assert "Untick the option" in str(proposed.value)
    assert "re-mint the blueprint" in str(written.value)
    assert "Untick" not in str(written.value)


async def test_the_composite_refusal_is_mapped_like_a_forbidden_edit() -> None:
    """A SUBCLASS of the error the route already maps, so the 422-with-the-reason handler needs
    no second branch: both are "this request asked for something this contract does not offer",
    and in both cases the reviewer's next step is to read the sentence."""
    assert issubclass(SqlRewriteUnsupportedError, ForbiddenTemplateEditError)


# --- the route ----------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _declined_envelope(*, kind: str = "single"):
    summary = make_summary(tool_calls=[make_tool_call("tc1", PAYROLL_SQL)])
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="no entry for 1 literal predicate(s)",
            correctable=True,
            corrections_attempted=1,
            # NESTED under `payload`, because `build_declined_envelope` reads the RAW
            # CANDIDATE ENVELOPE the model emitted, not its payload — the one shape that
            # carries `kind`, which is what the composite guard reads.
            raw_payload={
                "payload": {
                    "intent": "deductions by department",
                    "kind": kind,
                    "parameterization": [],
                    "source_tool_call_refs": ["tc1"],
                }
            },
        ),
        summary,
        candidate_id=CID,
    )
    return replace(
        env,
        entity_scan=LeakageVerdict(
            result="pass", scanned_fields=("intent",), scanner="regex"
        ).to_doc(),
    )


async def _client(*, turns, kind: str = "single"):
    store = InMemoryCandidateStore()
    await store.put(_declined_envelope(kind=kind))
    reviser = BlueprintReviser(model_client=ScriptedClient(turns))
    inbox = ReviewInbox(store, reviser=reviser)
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


async def test_the_route_defaults_to_no_rewrite(enabled: None) -> None:
    """⚠ THE OPT-IN IS NEVER INFERRED. A body with no `allow_sql` must behave exactly as it did
    before this slice — the licence costs the candidate its provenance and its ability to
    auto-land, so it has to be something a reviewer TICKED."""
    client, store = await _client(turns=[rewrite_turn()])
    before = (await store.get(CID)).to_doc()

    resp = client.post(f"/inbox/{CID}/revise", json={"feedback": "rewrite it"}, headers=AUTH)

    assert resp.status_code == 422
    assert "sql" in resp.json()["detail"]
    assert (await store.get(CID)).to_doc() == before


async def test_the_route_passes_the_opt_in_through_and_writes_nothing(enabled: None) -> None:
    """The §C.3 two-step survives the reversal intact: a rewrite is still a PROPOSAL, and
    `complete`/`apply_revision` remain the only write path into a candidate's payload."""
    client, store = await _client(turns=[rewrite_turn()])
    before = (await store.get(CID)).to_doc()

    resp = client.post(
        f"/inbox/{CID}/revise",
        json={"feedback": "the ratio is wrong; just deductions", "allow_sql": True},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sql_changed"] is True
    assert body["sql"] == NEW_SQL
    assert body["caution"] == SQL_REWRITE_CAUTION
    assert body["replace"] is True
    assert (await store.get(CID)).to_doc() == before
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_a_composite_rewrite_is_a_422_naming_the_fallback(enabled: None) -> None:
    """The message names the control the reviewer should use instead, because "unsupported" on
    its own sends them looking for a page that does not exist."""
    client, _ = await _client(turns=[rewrite_turn()], kind="composite")
    resp = client.post(
        f"/inbox/{CID}/revise", json={"feedback": "rewrite", "allow_sql": True}, headers=AUTH
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "SQL rewrite is not offered for composite blueprints yet: a composite has one "
        "accepted query per node, so a single replacement query would replace all of them "
        "with one. Untick the option to revise roles only"
    )


async def test_an_unusable_rewrite_is_a_200_with_a_reason(enabled: None) -> None:
    """Same call every other "the assistant had no suggestion" makes: the reviewer did nothing
    wrong, and the useful next step belongs on the page rather than in an error banner."""
    client, _ = await _client(turns=[rewrite_turn(sql="DROP TABLE payroll.payroll_fact")])
    resp = client.post(
        f"/inbox/{CID}/revise", json={"feedback": "f", "allow_sql": True}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sql_changed"] is False
    assert resp.json()["sql"] == ""
    assert resp.json()["reason"]
