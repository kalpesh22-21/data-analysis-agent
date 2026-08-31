"""`InboxItem.template_parts` — the card's pre-split template, and the D17 rule on it.

The reviewer card renders `{slot}` tokens as chips. The split is done HERE rather than in the
browser for two reasons, and the tests below pin one each:

  * the token grammar is `runtime/blueprint/template.SLOT_TOKEN`, and a second spelling of it
    in hand-written JS would fork a definition the runtime and the compiler already share;
  * ⚠ the template is NOT slots-only. Every `role="inline"` predicate keeps its LITERAL VALUE
    in the template body, so a template is exactly as entity-bearing as the payload it came
    from — and this field must therefore be tokenized from the REDACTED `payload_view`, never
    from `env.payload`.

That second one is the reason this module exists. The field was added to make the review
surface safer (no regex in the page, no string concatenated into markup), and deriving it from
the wrong source would have used that field to walk entity literals straight past the
redaction the `<pre>` beside it respects. The general shape: A DERIVED FIELD INHERITS THE
TRUST LEVEL OF ITS SOURCE, NOT OF ITS SIBLING.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import EntityHit, LeakageVerdict
from data_agent.learning.inbox.models import InboxItem, _template_parts
from data_agent.runtime.blueprint.template import SLOT_TOKEN

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _rebuild(parts: tuple[dict[str, str], ...]) -> str:
    """The parts joined back into a template string, slots re-wrapped in their braces.

    This is what the card does with them, minus the DOM: append each part in order. Written
    once here because a reassembly expression inline in three tests is three chances to write
    a different one.
    """
    out: list[str] = []
    for part in parts:
        if "slot" in part:
            out.append("{" + part["slot"] + "}")
        else:
            out.append(part.get("text", ""))
    return "".join(out)


def _blueprint_with_template(template: str, **overrides: object) -> CandidateEnvelope:
    """An `in_review` blueprint whose generalization carries *template*."""
    base_doc = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "leakage_near_miss"
    ]
    env = CandidateEnvelope.from_doc(base_doc)
    payload = {
        **env.payload,
        "generalization": {
            **(env.payload.get("generalization") or {}),
            "sql_template": template,
        },
    }
    return replace(
        env, status=CandidateStatus.IN_REVIEW, payload=payload, **overrides
    )


def test_the_split_is_the_runtime_token_grammar_not_a_second_spelling_of_it() -> None:
    """The slot names recovered from the parts are exactly the ones `SLOT_TOKEN` finds.

    Pinned as an EQUALITY against the runtime regex rather than against a hand-written list,
    so a change to the token grammar cannot leave this projection quietly matching the old
    one — which is the whole reason the split is server-side.
    """
    template = (
        "SELECT sum(gross_pay) FROM payroll.payroll_fact "
        "WHERE department = {department} AND toYear(pay_period) = {year} "
        "AND record_type = 'EARNING' AND region = {region}"
    )
    parts = _template_parts({"generalization": {"sql_template": template}})
    from_parts = [p["slot"] for p in parts if "slot" in p]
    assert from_parts == SLOT_TOKEN.findall(template)
    assert from_parts == ["department", "year", "region"]


def test_the_parts_reassemble_into_the_template_they_came_from() -> None:
    """Losslessness is the property the card depends on: it appends these parts in order and
    calls the result the template. A split that dropped a span would show the reviewer a query
    that is not the one under review."""
    template = "SELECT a FROM t WHERE x = {one} AND y = {two} AND z = 'lit'"
    parts = _template_parts({"generalization": {"sql_template": template}})
    assert _rebuild(parts) == template


def test_adjacent_slots_do_not_produce_an_empty_text_part() -> None:
    """Two tokens with nothing between them must not emit a zero-length text node — harmless
    to render, but it makes the parts list stop being a faithful description of the string."""
    parts = _template_parts({"generalization": {"sql_template": "{a}{b}"}})
    assert parts == ({"slot": "a"}, {"slot": "b"})


def test_a_template_with_no_slots_is_one_text_part() -> None:
    """A fixed report generalizes to a template with no holes. It still renders."""
    parts = _template_parts({"generalization": {"sql_template": "SELECT 1"}})
    assert parts == ({"text": "SELECT 1"},)


def test_everything_without_a_template_is_empty_rather_than_absent() -> None:
    """Three legitimate shapes reach this projection with no template: a knowledge candidate,
    a payload withheld for an unsettled scan, and a FAIL-TO-REVIEW blueprint — whose
    generalization carries no `sql_template` at all, and which is the population the review
    queue is mostly made of. None is an error, and the card renders the section only when
    this is non-empty."""
    assert _template_parts({"statement": "a business rule"}) == ()
    assert _template_parts({"withheld": "…"}) == ()
    assert _template_parts({"generalization": {"uses": []}}) == ()
    assert _template_parts({"generalization": {"sql_template": ""}}) == ()
    # A rehydrated store doc can carry anything; a non-dict generalization or a non-string
    # template is damage, and damage renders as "no template" rather than raising in a
    # projection whose caller is a listing endpoint.
    assert _template_parts({"generalization": "not a dict"}) == ()
    assert _template_parts({"generalization": {"sql_template": ["not", "a", "string"]}}) == ()


def test_the_parts_are_tokenized_from_the_redacted_view_not_the_raw_payload() -> None:
    """⚠ THE ONE THAT MATTERS.

    An `inline` predicate keeps its literal in the template body, so a template can carry the
    exact entity value the S5 verdict flagged. `payload_view` is redacted; `env.payload` is
    not. Deriving `template_parts` from the payload would ship that literal to the browser
    through the field added to make the page safer — past the redaction applied to the dump
    rendered inches away from it.

    Asserted on the ENVELOPE projection, not on the helper, because the helper cannot make
    this mistake (it takes the view) and `from_envelope` is where the wrong argument would be
    passed.
    """
    template = "SELECT gross_pay FROM payroll.payroll_fact WHERE employee = 'E12345'"
    settled = LeakageVerdict(
        result="quarantine",
        hits=(EntityHit(field="intent", kind="employee_code", span="E12345"),),
        scanned_fields=("intent", "generalization"),
        scanner="regex+ner+llm",
    )
    env = _blueprint_with_template(template, entity_scan=settled.to_doc())

    item = InboxItem.from_envelope(env)
    rendered = "".join(part.get("text", "") for part in item.template_parts)

    assert "E12345" not in rendered, (
        "template_parts leaked an entity literal — it must be tokenized from the REDACTED "
        "payload_view, never from env.payload"
    )
    assert "[redacted]" in rendered
    # The redaction is the SAME one the payload dump gets: the two surfaces cannot disagree
    # about what a reviewer is allowed to see.
    view_template = item.payload_view["generalization"]["sql_template"]
    assert rendered == view_template
    # ...and the source envelope was not mutated on the way.
    assert env.payload["generalization"]["sql_template"] == template


def test_a_clean_blueprint_keeps_its_inline_literals() -> None:
    """The redaction is keyed on SETTLED entity spans, so a clean blueprint's inline literals
    survive intact — and they must, because `record_type = 'EARNING'` is the metric definition
    the reviewer is there to judge. Withholding it would hide the evidence."""
    template = "SELECT sum(gross_pay) FROM t WHERE record_type = 'EARNING' AND d = {dept}"
    passed = LeakageVerdict(result="pass", scanned_fields=("intent",), scanner="regex+ner+llm")
    env = _blueprint_with_template(template, entity_scan=passed.to_doc())

    item = InboxItem.from_envelope(env)
    rendered = _rebuild(item.template_parts)
    assert rendered == template
    assert "EARNING" in rendered
