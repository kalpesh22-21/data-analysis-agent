"""ADVERSARIAL: what `parse_proposal` lets through, and what happens to it afterwards.

`test_reviser.py` covers the failures a model makes by accident — the wrong tool, no tool, a
non-list `entries`, a truthy `replace`, a runaway length. What is added here is the set the
design document names as the boundary's whole reason to exist, plus the two things nothing
currently asserts:

  * the FORBIDDEN_KEYS check is a top-level test, and the design's own claim for it
    (*"REJECTED rather than IGNORED … ignoring it would let a model believe it had changed
    something it had not"*) applies equally to the nested spelling. What that costs is traced
    to the store here rather than argued;
  * `entries` and `rationale` are the reviser's OUTPUT, and they go straight onto an HTTP
    response body. `rationale` is cleaned; `entries` are deliberately not (`to_candidate` owns
    per-field validation, correctly). Neither path has a test for the one character class that
    a UTF-8 response body cannot carry.

Everything here runs against the real route where the consequence is observable, because a
guard's value is what it prevents, not what it returns.
"""

from __future__ import annotations

import unicodedata
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.verdicts import LeakageVerdict
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.revise import BlueprintReviser, ForbiddenTemplateEditError
from data_agent.learning.revise.schema import (
    FORBIDDEN_KEYS,
    MAX_ENTRIES,
    MAX_RATIONALE_CHARS,
    _clean,
    parse_proposal,
)
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

from .test_reviser import ScriptedClient, declined_blueprint, proposal_turn

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}

LONE_SURROGATE = "\ud800"

_GOOD_ENTRY = {
    "locator": {"table": "payroll.payroll_fact", "column": "total_earnings", "value": "0"},
    "role": "inline",
    "why": "denominator guard",
}


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _turn(**arguments: object) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[
            ToolCallRequest(id="r1", name="propose_parameterization", arguments=arguments)
        ]
    )


async def _route(turn: ModelTurnResult):
    """The real route over the real declined blueprint, with *turn* as the model's answer.

    The scan is settled CLEAN because the reviser refuses a row whose scan has not — a
    proposal necessarily quotes the accepted SQL's literals, which is the same text the
    decline detail withholds on an uncleared row. Everything below is about what happens
    AFTER that gate opens.
    """
    env = replace(
        declined_blueprint(),
        entity_scan=LeakageVerdict(
            result="pass", scanned_fields=("intent",), scanner="regex+ner"
        ).to_doc(),
    )
    store = InMemoryCandidateStore()
    await store.put(env)
    reviser = BlueprintReviser(model_client=ScriptedClient([turn]))
    inbox = ReviewInbox(store, reviser=reviser)
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))
    return client, store, env.candidate_id


# --- the forbidden key, and where the check's edge actually is ----------------


@pytest.mark.parametrize("key", sorted(FORBIDDEN_KEYS))
def test_the_forbidden_key_is_refused_at_the_top_level(key: str) -> None:
    """The covered half, re-pinned so the parametrization below reads as a comparison rather
    than as a bare claim."""
    with pytest.raises(ForbiddenTemplateEditError):
        parse_proposal(_turn(entries=[_GOOD_ENTRY], rationale="r", **{key: "SELECT 1"}))


@pytest.mark.parametrize("key", sorted(FORBIDDEN_KEYS))
def test_the_same_key_one_level_down_is_refused_too(key: str) -> None:
    """⚠ REGRESSION GUARD for the defect this module was written to find.

    The guard began as `FORBIDDEN_KEYS.intersection(arguments)` — a membership test on the
    RESPONSE OBJECT — so `entries[0]["sql_template"]` sailed through it. The design's argument
    for rejecting rather than ignoring applies identically to the nested spelling: *"ignoring
    it would let a model believe it had changed something it had not, and would let a reviewer
    read a rationale about a template edit that never happened."*

    Where it actually bit, since the answer is not obvious: on the COMPLETE success path the
    key is dropped (`to_candidate` normalizes each entry through `ParamPlan.to_doc`, which
    emits exactly five keys), so the provenance chain was never at risk. But on the DECLINE
    path `completion.py::_still_declined` persists the merged RAW entries so the reviewer's
    work is not lost — and a `sql_template` key would land inside a stored parameterization
    entry and render in the card's raw dump, which is precisely the "a template nobody
    authored" confusion the top-level rejection exists to prevent.
    """
    with pytest.raises(ForbiddenTemplateEditError):
        parse_proposal(_turn(entries=[{**_GOOD_ENTRY, key: "SELECT 1"}], rationale="r"))


def test_the_key_is_refused_however_deeply_it_is_buried() -> None:
    """One level down is the placement a model would actually produce; the sweep must not be
    special-cased to it. A `slot` sub-object is two levels, and a list inside an entry is
    three — all of them are still "the model thinks it is editing the SQL"."""
    for entries in (
        [{**_GOOD_ENTRY, "slot": {"name": "d", "sql": "SELECT 1"}}],
        [{**_GOOD_ENTRY, "notes": [{"template": "SELECT 1"}]}],
        [{**_GOOD_ENTRY, "a": {"b": {"c": {"sql_template": "SELECT 1"}}}}],
    ):
        with pytest.raises(ForbiddenTemplateEditError):
            parse_proposal(_turn(entries=entries, rationale="r"))


def test_the_sweep_is_depth_bounded_rather_than_unbounded() -> None:
    """A recursive sweep over untrusted JSON is a stack overflow waiting to happen inside a
    request handler, and the cure must not be worse than the disease. Past the bound the sweep
    STOPS rather than raising — nothing that deep is a parameterization entry, so it fails the
    totality walk anyway — which is the trade the code makes and the one worth pinning."""
    deep: object = {"sql_template": "SELECT 1"}
    for _ in range(2000):
        deep = {"nest": deep}
    parsed = parse_proposal(_turn(entries=[{"locator": deep, "role": "inline"}], rationale="r"))
    assert parsed is not None  # no RecursionError, no 500


async def test_a_nested_forbidden_key_is_a_422_naming_the_reason(enabled: None) -> None:
    """The route half: the reviewer is told the model worked against a contract this system
    does not have, VERBATIM, rather than being told "the assistant failed" — which teaches
    them something true about why the template is not the model's to write."""
    turn = _turn(
        entries=[{**_GOOD_ENTRY, "sql_template": "SELECT * FROM anything"}],
        rationale="I rewrote the template to add a region filter",
    )
    client, store, cid = await _route(turn)
    before = (await store.get(cid)).to_doc()

    resp = client.post(f"/inbox/{cid}/revise", json={"feedback": "fix it"}, headers=AUTH)

    assert resp.status_code == 422
    assert "sql_template" in resp.json()["detail"]
    assert "AST rewrite" in resp.json()["detail"]
    # ...and nothing was written, which is true of every outcome on this route.
    assert (await store.get(cid)).to_doc() == before


# --- adversarial entry shapes -------------------------------------------------


def test_an_entry_whose_locator_is_not_a_dict_is_carried_not_dropped() -> None:
    """Per-entry validation is `to_candidate`'s, on purpose — a second vocabulary for the
    same mistake is what `ui/server.py::_proxy_inbox` warns against. What the boundary owes is
    that a junk locator cannot make the boundary itself throw, and that the DIFF beside it
    still renders (the diff keys on the locator, so this is where a `str.get` would explode).
    """
    for locator in ("a string", ["t", "c", "v"], 7, None, {"table": {"nested": 1}}):
        parsed = parse_proposal(
            _turn(entries=[{"locator": locator, "role": "inline"}], rationale="r")
        )
        assert parsed is not None, locator
        assert parsed[0][0]["locator"] == locator


async def test_a_junk_locator_still_produces_a_renderable_diff(enabled: None) -> None:
    """The diff is computed server-side from these entries and shipped as rows a card renders
    with no further interpretation. A non-dict locator must therefore produce a ROW, not an
    exception in a request handler."""
    client, _, cid = await _route(
        _turn(entries=[{"locator": "not a dict", "role": "inline"}], rationale="r")
    )

    resp = client.post(f"/inbox/{cid}/revise", json={"feedback": "f"}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    rows = resp.json()["diff"]
    added = [row for row in rows if row["kind"] == "added"]
    assert added == [
        {
            "kind": "added",
            "locator": "(no locator)",
            "before": "",
            "after": "(no locator) =  → inline — no reason given",
        }
    ]
    # ...and the entry the candidate already had is still reported, so the reviewer can see
    # that applying this would not disturb it.
    assert [row["kind"] for row in rows] == ["added", "unchanged"]


def test_deeply_nested_junk_in_an_entry_does_not_recurse_the_boundary() -> None:
    """`entries` is only shape-checked (`list` of `dict`) and length-capped, so an entry can
    be arbitrarily deep. The boundary must not walk it — the depth is `to_candidate`'s
    problem, and a parse boundary that recursed here would be a stack overflow away from a
    500 on every proposal."""
    deep: object = "leaf"
    for _ in range(500):
        deep = {"nest": deep}
    parsed = parse_proposal(_turn(entries=[{"locator": deep, "role": "inline"}], rationale="r"))
    assert parsed is not None
    assert len(parsed[0]) == 1


def test_a_list_of_ten_thousand_entries_is_sliced_before_it_is_filtered() -> None:
    """The cap must bound the WORK, not just the output: `entries[:MAX_ENTRIES]` before the
    `isinstance` filter, so a runaway generation cannot make the parse cost proportional to
    the generation."""
    parsed = parse_proposal(
        _turn(entries=[dict(_GOOD_ENTRY) for _ in range(10_000)], rationale="r")
    )
    assert parsed is not None
    assert len(parsed[0]) == MAX_ENTRIES


@pytest.mark.parametrize(
    "replace_value",
    [
        {"truthy": "object"},
        ["non", "empty"],
        "true",
        "false",
        1,
        1.0,
        object(),
    ],
)
def test_only_a_literal_true_selects_the_destructive_branch(replace_value: object) -> None:
    """`replace=true` DISCARDS every classification the extractor already got right. A truthy
    object selecting it would mean a model's stray value silently destroying validated work,
    so the test is identity against `True`, not truthiness — and `1` is in the list because
    `1 == True` in Python while `1 is True` is False."""
    parsed = parse_proposal(_turn(entries=[_GOOD_ENTRY], rationale="r", replace=replace_value))
    assert parsed is not None
    assert parsed[1] is False


def test_replace_true_is_still_honoured() -> None:
    """The complement: the strictness must not have removed the feature."""
    parsed = parse_proposal(_turn(entries=[_GOOD_ENTRY], rationale="r", replace=True))
    assert parsed is not None
    assert parsed[1] is True


# --- the response body's character set ---------------------------------------


def test_a_lone_surrogate_in_the_rationale_is_flattened() -> None:
    """⚠ REGRESSION GUARD — the same defect as `paramjudge/schema.py`'s, in the module
    beside it.

    `revise/schema.py::_clean` strips `Cc`, `Cf`, `Zl`, `Zp` and NOT `Cs`, while both house
    implementations it was modelled on (`judge/schema.py:220`, `extractor/prior_art.py:364`)
    strip all five, the second with a comment saying why: *"'the serializer saves us' is not a
    property to depend on."*

    Here it is not a property that holds at all. The rationale is returned on an HTTP response
    body, and a lone surrogate cannot be UTF-8 encoded — see the route test below.
    """
    assert unicodedata.category(LONE_SURROGATE) == "Cs"
    cleaned = _clean(f"before{LONE_SURROGATE}after", limit=MAX_RATIONALE_CHARS)
    assert LONE_SURROGATE not in cleaned, (
        "a lone surrogate survived the reviser's flattening; it is unencodable on the "
        "response body this string is returned on"
    )


async def test_a_lone_surrogate_in_the_rationale_does_not_break_the_route(
    enabled: None,
) -> None:
    """⚠ THE CONSEQUENCE, stated where a reviewer would meet it.

    `json.loads('"\\ud800"')` accepts the escape without complaint — which is exactly how
    `openai_client._safe_json_loads` builds tool arguments — and the value then rides the
    parse boundary unchanged into `ReviseProposal.to_wire`. FastAPI serializes that with a
    UTF-8 encoder, which refuses.

    The reviewer's experience is a 500 with no body on a route whose entire failure design is
    "no suggestion is a 200 with a reason". Nothing about the request was wrong.
    """
    client, _, cid = await _route(
        _turn(entries=[_GOOD_ENTRY], rationale=f"re-role{LONE_SURROGATE} the guard")
    )

    resp = client.post(f"/inbox/{cid}/revise", json={"feedback": "f"}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    assert LONE_SURROGATE not in resp.text


async def test_a_lone_surrogate_in_a_proposed_literal_does_not_break_the_route(
    enabled: None,
) -> None:
    """⚠ OPEN. The same character on the OTHER field, which is deliberately not cleaned.

    Not cleaning entries is the RIGHT call for content — `to_candidate` owns per-field
    validation and a second vocabulary for it is the mistake `revise/schema.py`'s docstring
    names. But `ReviseProposal.to_wire` puts those entries on a response body *before*
    `to_candidate` ever sees them, so the one guard that is about the TRANSPORT rather than
    about the content has no owner on this path: `rationale` is swept for surrogates and
    `entries` are not, while both leave through the same `JSONResponse`.

    A `locator.value` is copied by the model out of the accepted SQL, so it is exactly the
    field most likely to carry whatever the warehouse had in it. The reviewer's experience is
    a 500 with no body on a route whose entire failure design is "no suggestion is a 200 with
    a reason", and nothing about their request was wrong.

    The narrow fix is a transport sweep over the wire projection — the same
    `0xD800 <= ord(ch) <= 0xDFFF` test `_clean` already carries — applied in `to_wire` rather
    than to the entry contents, so the "one validator" rule is not forked to fix an encoding
    bug.
    """
    entry = {
        "locator": {
            "table": "payroll.payroll_fact",
            "column": "register_type",
            "value": f"DD{LONE_SURROGATE}UCT",
        },
        "role": "inline",
        "why": "spans two rules",
    }
    client, _, cid = await _route(_turn(entries=[entry], rationale="r"))

    resp = client.post(f"/inbox/{cid}/revise", json={"feedback": "f"}, headers=AUTH)

    assert resp.status_code == 200, resp.text


async def test_a_well_formed_proposal_is_unaffected_by_all_of_the_above(
    enabled: None,
) -> None:
    """The control. Every hardening above must leave the ordinary case exactly as it was."""
    client, store, cid = await _route(proposal_turn())
    before = (await store.get(cid)).to_doc()

    resp = client.post(f"/inbox/{cid}/revise", json={"feedback": "inline both"}, headers=AUTH)

    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 2
    assert (await store.get(cid)).to_doc() == before
