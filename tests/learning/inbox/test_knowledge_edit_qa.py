"""QA gaps around K1 — editing a knowledge candidate (knowledge-edit design §B, §C).

`test_knowledge_edit.py` and `test_knowledge_reviser.py` cover the paths the design describes.
This file covers the ones it does not mention, which are the ones a browser and a second
reviewer produce:

  * the form OMITS an empty optional field, so "clear the scope" and "leave the scope alone"
    are the SAME request on the wire — this asserts which one the server does;
  * `related_terms` arriving as a bare string, which is the shape a reviewer's paste produces
    and the shape the intake reader's docstring says lands as ONE term;
  * `structured` nested past the reviser's sweep bound, where the off-contract-key guard stops
    walking — what happens after the bound is reached is the whole question;
  * `apply_knowledge` racing a `reject`, the other direction of the window
    `test_a_row_that_moved_is_a_race` opens;
  * a dirty draft flagged somewhere OTHER than `statement`, where the reason is built from a
    dotted field path rather than a top-level name.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.completion import CompletionRaceError
from data_agent.learning.inbox.knowledge_edit import (
    KnowledgeEditInputError,
    KnowledgeEditor,
    stage_scanner,
)
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage.gate import LeakageGateStage
from data_agent.learning.revise import KnowledgeReviser
from data_agent.learning.revise.knowledge_schema import (
    KNOWLEDGE_TOOL_NAME,
    MAX_LABEL_CHARS,
    MAX_STATEMENT_CHARS,
    MAX_TERM_CHARS,
    MAX_TERMS,
    forbidden_knowledge_keys,
)
from data_agent.runtime.model.client import ModelTurnResult, ToolCallRequest

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = "candidate::kn-qa"

DIRTY = "employee E10842 accrues 1.5 days of leave per month"
CLEAN = "leave accrues at 1.5 days per month for full-time staff"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def knowledge_candidate(**over: Any) -> CandidateEnvelope:
    fields: dict[str, Any] = {
        "candidate_id": CID,
        "type": "global_knowledge",
        "status": CandidateStatus.IN_REVIEW,
        "payload": {
            "statement": DIRTY,
            "knowledge_type": "business_rule",
            "scope": "leave accrual",
            "related_terms": ["leave", "accrual"],
            "structured": {"rate": "1.5 days per month"},
        },
        "source_session": "sess-1",
        "source_trace": "trace-1",
        "evidence_refs": ("audit::1",),
        "extractor_rationale": "stated by the user",
        "entity_scan": LeakageVerdict(
            result="reject",
            hits=(EntityHit(field="statement", kind="employee_code", span="E10842"),),
            scanned_fields=("statement",),
            scanner="regex+ner",
        ).to_doc(),
        "confidence": 0.8,
        "proposed_action": "add",
        "depends_on": (),
        "content_hash": "hash-kn-qa",
    }
    fields.update(over)
    return CandidateEnvelope(**fields)


async def wired(*, stages: bool = True, env: CandidateEnvelope | None = None):
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else knowledge_candidate())
    gate = (LeakageGateStage(candidate_store=store),) if stages else ()
    return ReviewInbox(store, knowledge_editor=KnowledgeEditor(store=store, stages=gate)), store


# --- what the FORM's omission actually does (§C.3) --------------------------


async def test_omitting_an_optional_field_clears_it_rather_than_preserving_it() -> None:
    """⚠ THE FORM'S ONLY WAY TO SAY "DELETE THIS", asserted server-side.

    `readKnowledgeForm` in `inbox.html` OMITS every optional field the reviewer left blank, so
    an emptied `scope` box and a `scope` the reviewer never touched produce the IDENTICAL body.
    The behaviour that makes emptying the box meaningful is that `admit` replaces the payload
    WHOLESALE — an absent key is a deletion, not a "leave it as it was".

    If this ever became a merge instead, the form would silently lose the ability to remove a
    field, and a reviewer clearing a wrong `scope` would see it come straight back — with no
    error, on a payload whose closed key set means there is no other way to reach it.
    """
    inbox, store = await wired()
    before = await store.get(CID)
    assert before.payload["scope"] and before.payload["related_terms"]

    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})

    after = await store.get(CID)
    assert after.payload == {"statement": CLEAN}
    for dropped in ("scope", "related_terms", "structured", "knowledge_type"):
        assert dropped not in after.payload


async def test_the_wholesale_replace_is_visible_through_the_route(enabled: None) -> None:
    """The same fact through the wire, because this is a claim about what the BROWSER's request
    does and the browser only ever speaks HTTP."""
    inbox, store = await wired()
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))

    resp = client.post(
        f"/inbox/{CID}/apply_knowledge",
        json={"payload": {"statement": CLEAN, "knowledge_type": "business_rule"}},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert set((await store.get(CID)).payload) == {"statement", "knowledge_type"}


# --- shapes a paste produces ------------------------------------------------


async def test_related_terms_as_a_bare_string_is_refused_rather_than_landed_as_one_term() -> None:
    """⚠ THE FAILURE THE INTAKE READER'S DOCSTRING NAMES: `related_terms: "active, headcount"`
    lands as ONE term rather than two, because `_collect_text` walks a string without complaint.

    A reviewer pasting a comma-separated list into a one-per-line box is the obvious way to
    produce it, and the failure is SILENT at every later layer — the chunk lands, the scan
    passes, and the fact is simply never recalled by either word. So the refusal has to happen
    at intake, and it has to name the field.
    """
    inbox, store = await wired()

    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(
            CID, payload={"statement": CLEAN, "related_terms": "leave, accrual"}
        )

    assert "related_terms" in str(exc.value)
    assert (await store.get(CID)).payload["statement"] == DIRTY  # nothing was written


@pytest.mark.parametrize(
    "payload",
    [
        {"statement": CLEAN, "related_terms": ["leave", ""]},
        {"statement": CLEAN, "related_terms": [None]},
        {"statement": CLEAN, "related_terms": [{"term": "leave"}]},
        {"statement": CLEAN, "structured": ["rate", "1.5"]},
        {"statement": CLEAN, "structured": "rate=1.5"},
        {"statement": CLEAN, "knowledge_type": ["business_rule"]},
        {"statement": CLEAN, "scope": True},
    ],
)
async def test_a_surface_of_the_wrong_shape_is_refused_and_nothing_is_written(
    payload: dict[str, Any],
) -> None:
    """The declared contract, checked per surface. Each of these lands QUIETLY wrong rather than
    raising downstream — an empty term recalls nothing, a list-shaped `structured` lands leaves
    keyed by index — which is why the reader checks the type rather than waiting for a crash."""
    inbox, store = await wired()

    with pytest.raises(KnowledgeEditInputError):
        await inbox.apply_knowledge_edit(CID, payload=payload)

    assert (await store.get(CID)).payload["statement"] == DIRTY


async def test_a_structured_value_that_is_not_a_string_is_accepted_but_still_scanned() -> None:
    """⚠ THE ASYMMETRY BETWEEN THE TWO DOORS, pinned rather than assumed.

    The reviser's tool declares `structured` as string→string and REFUSES anything else; the
    intake reader checks only that `structured` is an OBJECT and does not type its values. So
    the human door accepts a shape the assistant door cannot produce.

    That is tolerable ONLY because the gate flattens nested dicts and lists to any depth, so the
    text is still scanned and still attributed. This asserts the second half — an entity nested
    two levels inside `structured` is FOUND, with a dotted field path, and the row is flagged.
    Without it, the closed key set would be enforced on keys while values quietly carried
    unscanned text into the global index.
    """
    inbox, store = await wired()

    result = await inbox.apply_knowledge_edit(
        CID,
        payload={"statement": CLEAN, "structured": {"detail": {"who": "employee E10842"}}},
    )

    assert result.entity_scan["result"] != "pass"
    fields = {hit["field"] for hit in result.entity_scan["hits"]}
    assert "structured.detail.who" in fields


# --- the sweep bound in the reviser (§C.1) ---------------------------------


def _nested(depth: int, leaf: Any) -> Any:
    """A chain of *depth* single-key dicts wrapping *leaf*."""
    for index in range(depth):
        leaf = {f"level{index}": leaf}
    return leaf


def test_the_off_contract_sweep_names_keys_up_to_its_bound() -> None:
    """The bound exists so untrusted input cannot blow the stack inside a request handler; what
    it costs is visibility past the bound. Both halves asserted together, because a reader who
    sees only one will misjudge the other.

    `_nested` wraps outermost-LAST, so in a 12-deep chain `level11` is the outermost key and
    `evil` sits at depth 13 — past `_MAX_SWEEP_DEPTH`.
    """
    shallow = {"statement": "s", "rationale": "r", "scope": _nested(3, {"evil": "x"})}
    assert "evil" in forbidden_knowledge_keys(shallow)

    deep = {"statement": "s", "rationale": "r", "scope": _nested(12, {"evil": "x"})}
    found = forbidden_knowledge_keys(deep)
    assert "evil" not in found  # past the bound the sweep STOPS rather than raising
    # ...but the trespass is still named at every level ABOVE the bound, so the refusal happens
    # anyway and the model is told about a real key rather than silently ignored.
    assert "level11" in found
    assert found


class _ScriptedClient:
    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self._turns = list(turns)

    async def send_turn(self, messages: list[dict], tools: list[dict]) -> ModelTurnResult:
        return self._turns.pop(0) if self._turns else ModelTurnResult(tool_calls=[])


def _reviser(arguments: Any, **kw: Any) -> KnowledgeReviser:
    options: dict[str, Any] = {
        "scanner": stage_scanner((LeakageGateStage(candidate_store=InMemoryCandidateStore()),)),
        "timeout_seconds": 5.0,
    }
    options.update(kw)
    return KnowledgeReviser(
        model_client=_ScriptedClient(
            [
                ModelTurnResult(
                    tool_calls=[
                        ToolCallRequest(id="k", name=KNOWLEDGE_TOOL_NAME, arguments=arguments)
                    ]
                )
            ]
        ),
        **options,
    )


async def test_a_structure_past_the_sweep_bound_is_still_refused_by_its_upper_levels() -> None:
    """⚠ WHAT THE BOUND COSTS, AND WHY IT COSTS LESS THAN IT LOOKS.

    Past `_MAX_SWEEP_DEPTH` the key sweep stops looking, so the DEEPEST off-contract key is
    never named. That is not a hole, because a key that deep is necessarily wrapped in keys that
    are NOT that deep, and those are named — so the proposal is refused all the same, and the
    reviewer is told about real keys rather than being handed a draft that quietly lost a
    subtree.

    The assertion that matters is the last one: whatever else the refusal says, the flagged SPAN
    buried in that structure is not in it. A refusal message is rendered on the card, and this
    is the one error path that carries model-authored text about an unredacted payload.
    """
    from data_agent.learning.revise import ForbiddenKnowledgeEditError

    reviser = _reviser(
        {
            "statement": CLEAN,
            "rationale": "r",
            "structured": {"detail": _nested(12, {"evil": "employee E10842"})},
        }
    )

    with pytest.raises(ForbiddenKnowledgeEditError) as exc:
        await reviser.propose(knowledge_candidate(), feedback="clean it")

    assert "level11" in str(exc.value)
    assert "E10842" not in str(exc.value)


async def test_a_draft_flagged_outside_statement_is_withheld_and_named_by_its_surface() -> None:
    """The withholding rule for a hit whose field is a DOTTED PATH.

    `_flagged_summary` renders each hit's `field` verbatim, and a hit inside `structured`
    arrives as `structured.<key>`. So this asserts the two things a reviewer needs from the
    refusal — which surface to look at, and NOT the value — for the case the shipped tests do
    not reach, where the flagged surface is not the statement.
    """
    reviser = _reviser(
        {
            "statement": CLEAN,
            "rationale": "r",
            "structured": {"owner": "employee E10842"},
        }
    )

    proposal = await reviser.propose(knowledge_candidate(), feedback="clean it")

    assert proposal.payload == {}
    assert "structured.owner" in proposal.reason
    assert "E10842" not in str(proposal.to_wire())
    assert proposal.to_wire()["diff"] == []


async def test_a_draft_flagged_only_in_related_terms_is_withheld_too() -> None:
    """`related_terms` is a list, so its hits arrive as `related_terms.<index>`. Same rule; the
    point of asserting it separately is that a list surface is the one where an off-by-one in
    the field-path handling would silently produce an EMPTY reason and a withheld draft with no
    explanation — refusing correctly for a reason nobody could act on."""
    reviser = _reviser(
        {
            "statement": CLEAN,
            "rationale": "r",
            "related_terms": ["leave", "employee E10842"],
        }
    )

    proposal = await reviser.propose(knowledge_candidate(), feedback="clean it")

    assert proposal.payload == {}
    assert "related_terms.1" in proposal.reason
    assert "E10842" not in proposal.reason


# --- the other direction of the race window --------------------------------


async def test_apply_racing_a_reject_is_refused_and_the_reject_stands() -> None:
    """⚠ THE HUMAN "NO" MUST WIN.

    `test_a_row_that_moved_is_a_race` moves the row to `validated`. The dangerous direction is
    `rejected`: a reviewer removed this fact deliberately (D29 keeps the row as negative
    memory), and an edit that landed afterwards would resurrect it at `in_review` — with a
    payload nobody rejected, in a queue the reject was supposed to clear.

    The window is real: `apply_knowledge_edit` reads the row, then validates and SCANS (a model
    call's worth of time on a wired deployment) before writing.
    """
    inbox, store = await wired()
    editor = KnowledgeEditor(
        store=store, stages=(LeakageGateStage(candidate_store=store),)
    )
    env = await store.get(CID)
    # The reject lands while the edit is between its read and its write.
    await store.put(replace(env, status=CandidateStatus.REJECTED))

    with pytest.raises(CompletionRaceError):
        await editor.admit(env, payload={"statement": CLEAN}, route_reason="knowledge_edited")

    after = await store.get(CID)
    assert after.status == CandidateStatus.REJECTED
    assert after.payload["statement"] == DIRTY
    assert after.knowledge_edit is None


async def test_the_race_is_a_409_through_the_route(enabled: None) -> None:
    """The same collision as the reviewer sees it. 409 rather than 200, because a 200 here would
    tell them their edit was saved onto a row that no longer exists in that state."""
    inbox, store = await wired()
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))
    env = await store.get(CID)
    await store.put(replace(env, status=CandidateStatus.REJECTED))

    resp = client.post(
        f"/inbox/{CID}/apply_knowledge", json={"payload": {"statement": CLEAN}}, headers=AUTH
    )

    # The status guard catches it first (the row is no longer `in_review`), which is the same
    # 409 by a shorter route — what matters is that it is never a 200.
    assert resp.status_code == 409
    assert (await store.get(CID)).payload["statement"] == DIRTY


# --- the digest, on the path that most needs it -----------------------------


async def test_the_previous_statement_digest_is_of_the_text_being_replaced() -> None:
    """The badge exists so a chain of edits leaves a chain of digests. Asserted against a
    computed sha256 rather than "is not empty", because a digest of the WRONG text (the new
    statement, or the empty string) is the failure that looks identical at a glance and makes
    the audit trail useless — it would say every edit replaced nothing."""
    import hashlib

    inbox, store = await wired()

    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})

    stamp = (await store.get(CID)).knowledge_edit
    assert stamp.previous_statement_sha256 == hashlib.sha256(DIRTY.encode()).hexdigest()
    assert stamp.previous_statement_sha256 != hashlib.sha256(CLEAN.encode()).hexdigest()
    # ...and the previous TEXT is nowhere on the row.
    assert DIRTY not in str((await store.get(CID)).to_doc())


async def test_a_numeric_surface_is_stored_as_a_number_and_is_therefore_not_scanned() -> None:
    """⚠ OBSERVED BEHAVIOUR, PINNED — the closed key set is closed over KEYS, not over VALUES.

    `as_text` deliberately COERCES a number ("str(2025) is exactly as usable as '2025'"), so
    `scope: 7` passes intake. But `validate_payload` only ANSWERS; it does not return a
    normalized payload, and `admit` stores `dict(payload)` verbatim — so the row keeps the int.
    `gate._collect_text` walks strings, dicts and lists and ignores every other leaf, so that
    surface is not among `scanned_fields` at all.

    Harmless as it stands: the entity kinds this gate looks for (employee codes, names, emails)
    cannot be spelled as a bare number, and the mapper reads the statement. It is recorded here
    because the argument for the closed key set — "every key intake permits is a surface this
    gate scans" — is exactly one step weaker than it reads, and the next person to widen a
    surface's accepted types should know that before they do it.
    """
    from data_agent.learning.leakage.gate import _scanned_fields

    inbox, store = await wired()

    result = await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN, "scope": 7})

    stored = (await store.get(CID)).payload
    assert stored["scope"] == 7
    assert isinstance(stored["scope"], int)  # NOT coerced to "7" on the way in
    assert "scope" not in _scanned_fields("global_knowledge", stored)
    assert result.entity_scan["result"] == "pass"


# --- size caps on the HUMAN write path (review fix, design §F.1) ------------


async def test_a_statement_past_the_reviser_cap_is_refused_rather_than_stored() -> None:
    """⚠ THE HUMAN PATH HAD NO SIZE BOUND AT ALL, and it is the one path with no model in it.

    `revise/knowledge_schema.py` truncates every field the assistant proposes, so a runaway
    generation cannot be persisted. A reviewer-token caller posting straight to
    `apply_knowledge` faced nothing: the payload it stores is walked by the regex+NER scanner,
    rendered onto a card and eventually landed, so a multi-megabyte statement was a stored,
    re-scanned, re-rendered object with no upper bound anywhere.
    """
    inbox, store = await wired()
    huge = "a" * (MAX_STATEMENT_CHARS + 1)

    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(CID, payload={"statement": huge})

    assert "statement" in str(exc.value)
    assert str(MAX_STATEMENT_CHARS) in str(exc.value)
    # NOTHING WAS WRITTEN — the cap runs before the scan, before the guarded put.
    assert (await store.get(CID)).payload["statement"] == DIRTY


async def test_a_statement_exactly_at_the_cap_is_accepted() -> None:
    """The boundary, in the direction that matters: the cap must not be off by one against the
    reviewer, or a fact the assistant is allowed to WRITE would be one the reviewer cannot
    SAVE."""
    inbox, store = await wired()
    at_cap = "leave accrues " + "x" * (MAX_STATEMENT_CHARS - len("leave accrues "))

    await inbox.apply_knowledge_edit(CID, payload={"statement": at_cap})

    assert (await store.get(CID)).payload["statement"] == at_cap


@pytest.mark.parametrize("field", ["knowledge_type", "scope"])
async def test_an_oversized_label_is_refused(field: str) -> None:
    """`knowledge_type` and `scope` are LABELS — and `scope` becomes the landed node's title,
    so an unbounded one is an unbounded title in the shared corpus."""
    inbox, _store = await wired()
    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(
            CID, payload={"statement": CLEAN, field: "l" * (MAX_LABEL_CHARS + 1)}
        )
    assert field in str(exc.value)


async def test_too_many_related_terms_are_refused_rather_than_truncated() -> None:
    """⚠ REFUSED, NOT TRUNCATED, and the difference is who is talking. The reviser TRUNCATES a
    model's proposal because a model cannot be told; a reviewer can, and silently dropping the
    terms they typed would leave them believing the fact recalls on words it does not."""
    inbox, _store = await wired()
    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(
            CID,
            payload={"statement": CLEAN, "related_terms": [f"t{i}" for i in range(MAX_TERMS + 1)]},
        )
    assert "related_terms" in str(exc.value)


async def test_an_oversized_structured_value_is_refused_at_any_depth() -> None:
    """⚠ THE NESTING INTAKE DELIBERATELY PERMITS (§F.1.b) IS ALSO THE WAY ROUND A TOP-LEVEL
    CAP. Intake accepts a nested `structured` and the gate scans its leaves at any depth, so a
    cap that only looked at the top level would leave the payload unbounded one key down."""
    inbox, _store = await wired()
    with pytest.raises(KnowledgeEditInputError) as exc:
        await inbox.apply_knowledge_edit(
            CID,
            payload={
                "statement": CLEAN,
                "structured": {"detail": {"note": "v" * (MAX_TERM_CHARS + 1)}},
            },
        )
    assert "structured.detail.note" in str(exc.value)


async def test_a_nested_structured_within_the_caps_still_applies() -> None:
    """The companion, so the cap cannot be read as "nesting is now refused": §F.1.b keeps a
    nested `structured` legal, and it must still store and still be scanned."""
    inbox, store = await wired()
    await inbox.apply_knowledge_edit(
        CID, payload={"statement": CLEAN, "structured": {"detail": {"note": "monthly"}}}
    )
    assert (await store.get(CID)).payload["structured"] == {"detail": {"note": "monthly"}}


async def test_the_caps_are_not_added_to_the_mined_intake_reader() -> None:
    """⚠ THE FIX THAT WAS DELIBERATELY NOT MADE. `validate_payload` also governs the EXTRACTED
    path, and a length refusal there would start declining candidates the loop accepts today —
    a behaviour change to the mined pipeline, smuggled in under a hardening of the human one.
    The cap therefore lives at the editor, and this asserts the reader is untouched."""
    from data_agent.learning.extractor.validation import validate_payload

    assert validate_payload(
        "global_knowledge", {"statement": "a" * (MAX_STATEMENT_CHARS + 1)}
    ) is None


async def test_the_route_answers_422_for_an_oversized_statement(enabled: None) -> None:
    """Same status as every other refusal this payload can earn: the reviewer sent something
    the system will not store, and the sentence says which field and what the bound is."""
    inbox, _store = await wired()
    client = TestClient(create_inbox_app(inbox=inbox, write_plane="offline"))
    resp = client.post(
        f"/inbox/{CID}/apply_knowledge",
        json={"payload": {"statement": "a" * (MAX_STATEMENT_CHARS + 1)}},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "statement" in resp.json()["detail"]


# --- the edit badge counts EDITS (review fix) ------------------------------


async def test_the_badge_counts_from_one_on_the_first_real_edit() -> None:
    """`edits` is a count of times a human changed this fact, and it has to start at one for
    the first change — see `test_a_promotion_carries_no_edit_badge` for the row shape that
    used to make it start at two."""
    inbox, store = await wired()
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN})
    first = (await store.get(CID)).knowledge_edit
    await inbox.apply_knowledge_edit(CID, payload={"statement": CLEAN + " (revised)"})
    second = (await store.get(CID)).knowledge_edit

    assert first.edits == 1
    assert second.edits == 2
