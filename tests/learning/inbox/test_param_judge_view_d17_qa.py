"""D17 on the parameterization judge's verdict, and the listing endpoint's contract with a
damaged store document.

⚠ WHY THIS IS THE SHARP EDGE OF THE §D SLICE. The judge is shown the RAW payload — design §C.2
states the asymmetry deliberately (*"the human sees the redacted view; the model sees what the
extractor saw"*) — and then writes prose ABOUT what it saw into `feedback` and
`findings[].note`. A verdict about a leaked entity therefore quotes it: *"employee = 'E12345'
should be inline"*. That prose is then stamped on the envelope, stored, projected onto
`InboxItem`, and rendered on the card inches from a `payload_view` in which the same value
reads `[redacted]`.

`inbox/models.py::_param_judge_view` is the guard. `tests/learning/inbox/test_template_parts.py`
pins the sibling rule for `template_parts`; this module pins this one, over all three carriers
of the span (the template, the feedback, a finding's note), on both scan states, and at the
WIRE rather than at the projection — because the projection is not the thing a browser
receives.

The second half is the other property a listing endpoint owes: one unreadable row is a boring
row, never a 500 for the whole queue.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.audit.judgement import ParamAssessment, ParamFinding
from data_agent.learning.candidate.decline import DeclineBlock
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.models import InboxItem
from data_agent.learning.inbox.service import create_inbox_app

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}

# The leaked value. Chosen to appear in all THREE carriers at once, because the redaction is
# one pass over one document and a test that used a different string per carrier could pass
# while two of the three were unprotected.
SPAN = "E12345"

# An inline predicate keeps its literal in the template body — that is the whole reason
# `template_parts` had to be derived from the redacted view — so this template is a carrier.
LEAKY_TEMPLATE = (
    "SELECT sum(gross_pay) FROM payroll.payroll_fact "
    f"WHERE employee = '{SPAN}' AND department = {{department}}"
)

LEAKY_VERDICT = ParamAssessment(
    verdict="revise",
    feedback=f"employee = '{SPAN}' is frozen inline but names one person, not a metric",
    confidence=0.82,
    findings=(
        ParamFinding(
            finding_class="C",
            criterion=f"inline_{SPAN}",
            note=f"the predicate employee = '{SPAN}' should have been a slot",
            entry_index=0,
        ),
    ),
)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _settled(result: str = "quarantine") -> dict[str, Any]:
    return LeakageVerdict(
        result=result,
        hits=(EntityHit(field="intent", kind="employee_code", span=SPAN),),
        scanned_fields=("intent", "generalization"),
        scanner="regex+ner+llm",
    ).to_doc()


def _env(
    *,
    scan: dict[str, Any] | None,
    param_judge: ParamAssessment | None = LEAKY_VERDICT,
    status: str = CandidateStatus.IN_REVIEW,
    decline: DeclineBlock | None = None,
) -> CandidateEnvelope:
    """A blueprint whose template, intent and judge verdict all carry the same span."""
    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())["leakage_near_miss"]
    env = CandidateEnvelope.from_doc(base)
    payload = {
        **env.payload,
        "intent": f"gross pay for employee {SPAN}",
        "generalization": {
            **(env.payload.get("generalization") or {}),
            "sql_template": LEAKY_TEMPLATE,
        },
    }
    return replace(
        env,
        status=status,
        payload=payload,
        entity_scan=scan if scan is not None else {"result": "pending", "hits": []},
        param_judge=param_judge,
        decline=decline,
    )


async def _wire(env: CandidateEnvelope, *, status: str) -> tuple[dict[str, Any], str]:
    """The row AS A BROWSER RECEIVES IT, plus the raw response text.

    Asserted at the wire and not at `InboxItem`, because the projection is one hop short of
    the thing the D17 rule is about: a field could be redacted on the dataclass and re-derived
    from the envelope by the serializer, and only the body would show it.
    """
    store = InMemoryCandidateStore()
    await store.put(env)
    client = TestClient(create_inbox_app(inbox=ReviewInbox(store), write_plane="offline"))
    resp = client.get("/inbox", params={"status": status}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    return items[0], resp.text


# --- the settled scan: the span is redacted everywhere it can appear ----------


async def test_the_span_does_not_reach_the_wire_through_any_of_its_three_carriers(
    enabled: None,
) -> None:
    """⚠ THE ONE THAT MATTERS. Three independent routes for the same value, all on one row:

      (a) the `sql_template`, via an inline predicate — `template_parts`;
      (b) the judge's `feedback` — model prose about the unredacted payload;
      (c) a finding's `note` — the same, one level down.

    Asserted on the whole response body, so a carrier nobody thought of fails the test too.
    """
    item, body = await _wire(_env(scan=_settled()), status="in_review")

    assert SPAN not in body, "an entity span crossed to a browser through the reviewer row"

    # (a) the template, in both of its shapes.
    rendered = "".join(part.get("text", "") for part in item["template_parts"])
    assert SPAN not in rendered
    assert "[redacted]" in rendered
    assert SPAN not in item["payload_view"]["generalization"]["sql_template"]

    # (b) and (c) the judge.
    assert SPAN not in item["param_judge"]["feedback"]
    assert "[redacted]" in item["param_judge"]["feedback"]
    assert SPAN not in item["param_judge"]["findings"][0]["note"]
    assert "[redacted]" in item["param_judge"]["findings"][0]["note"]


async def test_the_criterion_is_redacted_too_because_the_model_also_writes_it(
    enabled: None,
) -> None:
    """`criterion` is described to the model as *"a short tag for the kind of problem"*, which
    is a suggestion, not a constraint — the schema types it `str` and nothing rejects a tag
    with a literal in it. It is redacted by the same pass because the pass is over the whole
    document rather than over a list of field names, which is the property worth pinning: a
    field added to `ParamAssessment` later inherits the protection."""
    item, _ = await _wire(_env(scan=_settled()), status="in_review")
    assert SPAN not in item["param_judge"]["findings"][0]["criterion"]


async def test_the_verdict_itself_survives_the_redaction(enabled: None) -> None:
    """The complement, and it is not a formality: a redaction that flattened the verdict, the
    confidence or the finding class would destroy the phase-D-1 measurement while passing
    every leak test above."""
    item, _ = await _wire(_env(scan=_settled()), status="in_review")
    judge = item["param_judge"]
    assert judge["verdict"] == "revise"
    assert judge["confidence"] == 0.82
    assert judge["findings"][0]["class"] == "C"
    assert judge["findings"][0]["entry_index"] == 0


def test_the_source_envelope_is_not_mutated_by_the_projection() -> None:
    """The store keeps the raw verdict: it is access-controlled (D51), it is what the D-1
    measurement is graded against, and the projection is a VIEW. A projection that redacted
    in place would silently destroy the dataset the whole phase exists to produce."""
    env = _env(scan=_settled())
    InboxItem.from_envelope(env)
    assert env.param_judge is not None
    assert SPAN in env.param_judge.feedback
    assert SPAN in env.payload["generalization"]["sql_template"]


async def test_a_clean_scan_leaves_the_verdict_intact(enabled: None) -> None:
    """No spans, no redaction. The judge's whole subject is literal values, so a blanket
    scrub would make the card unreadable for the population it was built for."""
    clean = LeakageVerdict(result="pass", scanned_fields=("intent",), scanner="regex").to_doc()
    verdict = ParamAssessment(
        verdict="revise",
        feedback="record_type = 'EARNING' defines the metric the intent names",
        confidence=0.9,
    )
    item, _ = await _wire(
        _env(scan=clean, param_judge=verdict), status="in_review"
    )
    assert item["param_judge"]["feedback"] == verdict.feedback


# --- the unsettled scan -------------------------------------------------------


async def test_an_unscanned_form_withholds_the_verdict_with_the_payload(
    enabled: None,
) -> None:
    """A fail-to-review row whose scan never settled: `payload_view` is replaced by the
    withheld notice, and the judge's prose — written about that same unredacted payload —
    must go with it. Redaction is keyed off SETTLED spans, so on this row "redacted" would
    mean "unmodified", and shipping the verdict would be an elaborate way of leaking exactly
    what the payload rule protects."""
    env = _env(
        scan=None,
        status=CandidateStatus.NEEDS_PARAMETERIZATION,
        decline=DeclineBlock(reason="totality_violation", detail="d"),
    )
    item, body = await _wire(env, status="needs_parameterization")

    assert item["param_judge"] is None
    assert "withheld" in item["payload_view"]
    assert SPAN not in body
    # The ROW still lists — the shape, the status and the decline are all there. Withholding
    # content is not the same as hiding work.
    assert item["status"] == "needs_parameterization"
    assert item["decline"] is not None


async def test_an_unscanned_row_with_no_decline_carries_the_verdict_as_it_carries_the_payload(
    enabled: None,
) -> None:
    """⚠ CHARACTERIZATION of a reachable combination, recorded so it is a decision rather
    than an accident.

    `_withheld_for_unsettled_scan` fires only on a DECLINE-bearing row. A candidate that
    passed static validation, got stamped by the judge and then met no leakage stage (none
    wired) keeps the S3 `pending` sentinel and can still route to `in_review` — and the judge
    stage runs BEFORE the leakage stage by design (§D.1), so "judged but unscanned" is the
    normal intermediate state, not a corner.

    On that row the verdict crosses unredacted. That is CONSISTENT with `payload_view`, which
    is also unredacted there (nothing settled, nothing to key off), so the judge adds no new
    exposure — the row already ships the same literals in the payload beside it. Pinned
    because the consistency is the whole argument: if `payload_view` ever learns to withhold
    on an unsettled scan regardless of the decline block, this field must learn it in the same
    commit, and this test is what will say so.
    """
    item, body = await _wire(_env(scan=None), status="in_review")

    assert item["param_judge"] is not None
    assert SPAN in item["param_judge"]["feedback"]
    # ...and the payload beside it is equally unredacted, which is the point.
    assert SPAN in item["payload_view"]["intent"]
    assert SPAN in body


# --- a damaged document must not take the queue down --------------------------


@pytest.mark.parametrize(
    "stored",
    [
        "a string a human typed in cbq",
        ["a", "list"],
        7,
        True,
        {"verdict": "APPROVED", "confidence": "high", "findings": {"not": "a list"}},
        {"verdict": "revise", "confidence": 0.5, "findings": [None, 3, "x"]},
        {"findings": [{"class": "Z", "entry_index": "zero"}]},
    ],
)
async def test_a_hand_edited_param_judge_lists_instead_of_500ing(
    enabled: None, stored: Any
) -> None:
    """The store is a KV a human can edit with cbq, and this projection runs inside a LISTING
    endpoint — so a document nobody can parse must cost that ROW its verdict, never the
    queue its readability."""
    env = _env(scan=_settled())
    doc = env.to_doc()
    doc["param_judge"] = stored

    store = InMemoryCandidateStore()
    await store.put(CandidateEnvelope.from_doc(doc))
    client = TestClient(create_inbox_app(inbox=ReviewInbox(store), write_plane="offline"))

    resp = client.get("/inbox", params={"status": "in_review"}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    # Absent (unreadable ⇒ "the judge did not run") or normalized into the closed
    # vocabularies. What it may never be is a Class A finding conjured out of damage — that
    # is the class that would authorize a phase-D-2 discard, and nothing in a broken document
    # is evidence for it.
    judge = item["param_judge"]
    if judge is not None:
        assert judge["verdict"] in ("ok", "revise", "reject")
        assert 0.0 <= judge["confidence"] <= 1.0
        assert all(f["class"] in ("A", "B", "C") for f in judge["findings"])
        assert not any(f["class"] == "A" for f in judge["findings"])


def test_a_hand_written_class_a_finding_cannot_arrive_pre_authorized() -> None:
    """A document is the cheapest way to introduce the class that authorizes a phase-D-2
    discard, so the two conditions must stay independent of each other on rehydration: a
    hand-written Class A finding under a verdict the store could not parse reads as `ok`, and
    `ok` is inert whatever the findings say.

    Pinned here rather than in D-2's own suite because the property has to hold BEFORE the
    branch that reads it is written — that is the whole point of a rehydration guard.
    """
    assessment = ParamAssessment.from_doc(
        {"verdict": "DISCARD IT", "confidence": 1.0, "findings": [{"class": "A", "note": "n"}]}
    )
    assert assessment.verdict == "ok"
    assert assessment.has_class_a is True  # the finding survives — it is only advisory
    # `would_discard` is `verdict != "ok" and has_class_a`; the unparseable verdict is what
    # keeps this row out of that conjunction.
    assert assessment.verdict == "ok"


async def test_a_confidence_too_large_for_a_float_lists_instead_of_500ing(
    enabled: None,
) -> None:
    """⚠ REGRESSION GUARD, at the endpoint. JSON integers are arbitrary-precision and
    Python honours them, so a
    `confidence` of 10**400 is a legal document. `ParamAssessment.from_doc` calls
    `float(confidence)` inside its range test with no guard and raises `OverflowError` —
    inside the listing endpoint, which then 500s for EVERY row in the queue, not just this
    one.

    The parse boundary a package away already guards the identical conversion
    (`paramjudge/schema.py::_confidence` wraps it in `try/except OverflowError`), and
    `from_doc`'s own docstring says it exists to avoid *"raising inside a listing endpoint"*.

    Two rows are stored so the failure mode is visible: the healthy one must still list.
    """
    healthy = _env(scan=_settled(), param_judge=None)
    healthy = replace(healthy, candidate_id=f"{healthy.candidate_id}::healthy")

    damaged_doc = _env(scan=_settled()).to_doc()
    damaged_doc["param_judge"] = {"verdict": "reject", "feedback": "f", "confidence": 10**400}

    store = InMemoryCandidateStore()
    await store.put(healthy)
    await store.put(CandidateEnvelope.from_doc(damaged_doc))
    client = TestClient(create_inbox_app(inbox=ReviewInbox(store), write_plane="offline"))

    resp = client.get("/inbox", params={"status": "in_review"}, headers=AUTH)

    assert resp.status_code == 200, "one damaged document made the whole queue unreadable"
    assert len(resp.json()["items"]) == 2


async def test_a_lone_surrogate_in_a_stored_verdict_lists_instead_of_500ing(
    enabled: None,
) -> None:
    """⚠ REGRESSION GUARD, at the far end of the surrogate defect. Unstripped, a
    lone surrogate in the model's feedback is stamped on the envelope, written to the store,
    and read back here — where the response body's UTF-8 encoder refuses it and the whole
    queue 500s.

    This is the endpoint-level consequence of
    `tests/learning/paramjudge/test_param_judge_adversarial_qa.py`'s two unit failures, and it
    is why they are worth fixing rather than noting: the blast radius is not one verdict, it
    is every row a reviewer would have seen.
    """
    verdict = ParamAssessment(
        verdict="revise", feedback="re-role\ud800 the guard", confidence=0.5
    )
    item, body = await _wire(_env(scan=_settled(), param_judge=verdict), status="in_review")
    assert "\ud800" not in body
