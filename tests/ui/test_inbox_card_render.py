"""What the reviewer card ACTUALLY renders, driven in a real DOM.

`tests/ui/test_inbox_markup.py` pins structural facts by reading `inbox.html` as text,
because the page is hand-written with no build step. That is the right tool for "is there an
HTML sink anywhere in this file", and the wrong one for "does a blueprint with no frozen
filters say so" — a `<pre>` dump and a typed card are both just source text to a regex.

So this module runs the page. It needs NO stack and no subprocesses — a loopback
`http.server` serves `inbox.html` and answers the two GETs the page makes on bootstrap. That
is why it sits on the default path rather than under `RUN_E2E` with the Layer-3 suite: the
seam being tested is the renderer, and a browser plus a socket is the whole dependency.

The load-bearing cases, in the order they would bite:

  * the card is a RANKING of the payload, not a replacement — the verbatim dump survives on
    every card, so nothing a typed renderer does not understand can be lost;
  * an EMPTY frozen-filters section still renders, because "this blueprint freezes nothing"
    is the single most reviewable fact about an over-parameterized template and a section
    that disappears when empty cannot state it;
  * a payload key the renderer does not know about reaches the reviewer anyway;
  * an unknown candidate TYPE falls all the way back to the dump rather than rendering a
    confident partial view of itself;
  * a payload string containing markup renders as TEXT. The slot chips are the first place
    this page takes a server string apart and reassembles it into elements, which is exactly
    the shape that historically turns a textContent page into an injection surface.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

_INBOX = pathlib.Path(__file__).resolve().parents[2] / "ui" / "static" / "inbox.html"

# The list the stub server is currently serving. Module-level rather than threaded through
# the handler because `HTTPServer` constructs the handler per request.
_SERVED: dict[str, Any] = {"items": [], "revise": None, "last_post": ""}

# How long to wait for the first card. Short ON PURPOSE: nothing here is slow, and a long
# default turns "the renderer threw" into a minutes-long hang across a dozen cases instead
# of a fast failure that names the test.
_RENDER_TIMEOUT_MS = 5_000


class _StubHandler(BaseHTTPRequestHandler):
    """The two GETs the page makes on bootstrap, plus the page itself.

    A real HTTP origin rather than a stubbed `window.fetch`: `add_init_script` does not apply
    to `set_content` (the page is already on `about:blank`, so no navigation re-runs it), and
    the page then ran its REAL fetch against a relative URL with no origin to resolve it
    against. Serving it is both simpler and closer to production — the page's own fetch code
    is exercised, not bypassed.
    """

    def log_message(self, *args: Any) -> None:  # keep pytest output clean
        return

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        """Answer the one POST the card makes: `revise`.

        The response is whatever the test queued in `_SERVED["revise"]`, so a proposal, an
        empty suggestion and a 422 template-edit refusal are all reachable from here.
        """
        length = int(self.headers.get("Content-Length") or 0)
        _SERVED["last_post"] = self.rfile.read(length).decode() if length else ""
        status, payload = _SERVED.get("revise") or (200, {})
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        if self.path.startswith("/api/inbox/health"):
            body = b'{"write_plane":"full"}'
            ctype = "application/json"
        elif self.path.startswith("/api/inbox"):
            items = _SERVED["items"]
            body = json.dumps({"count": len(items), "items": items}).encode()
            ctype = "application/json"
        else:
            body = _INBOX.read_bytes()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    """A loopback origin serving the page and the two bootstrap endpoints."""
    httpd = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/inbox"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """A chromium instance, or a clean skip where one is not installed.

    Skipping rather than failing is deliberate: a missing browser binary is an environment
    fact, and a suite that goes red on it teaches people to ignore red. The assertions below
    are about the page, and they are worth having wherever a browser exists.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as pw:
            try:
                instance = pw.chromium.launch()
            except Exception as exc:  # pragma: no cover — environment-dependent
                pytest.skip(f"chromium is not launchable here: {exc}")
            try:
                yield instance
            finally:
                instance.close()
    except NotImplementedError as exc:  # pragma: no cover — no browser support at all
        pytest.skip(f"playwright cannot run here: {exc}")


@pytest.fixture
def render(browser: Any, server: str) -> Iterator[Any]:
    """`render(items)` → a loaded page showing exactly those items.

    Fails the test on any uncaught page error rather than letting it surface as a selector
    timeout: a renderer that throws halfway leaves a half-built card, and "element not found"
    is a much worse description of that than the exception itself.
    """
    pages: list[Any] = []

    def _render(items: list[dict[str, Any]]) -> Any:
        _SERVED["items"] = items
        _SERVED.setdefault("revise", None)
        page = browser.new_page()
        pages.append(page)
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(server)
        try:
            # ATTACHED, not visible. The page keeps every fetched item in the DOM and
            # HIDES the ones outside the active type tab, and that tab list is a fixed
            # four — so a candidate of an unrecognised type renders and is then filtered
            # out of sight. Waiting for visibility would make this fixture assert the tab
            # filter's behaviour instead of the renderer's.
            page.wait_for_selector(
                '[data-testid="inbox-item"]',
                state="attached",
                timeout=_RENDER_TIMEOUT_MS,
            )
        except Exception:
            if errors:
                raise AssertionError(f"the page threw while rendering: {errors}") from None
            raise
        assert not errors, f"the page threw while rendering: {errors}"
        return page

    try:
        yield _render
    finally:
        for page in pages:
            page.close()


def _item(**overrides: Any) -> dict[str, Any]:
    """One wire item, defaulted to the shape the service actually sends."""
    base: dict[str, Any] = {
        "candidate_id": "candidate::hash::0",
        "type": "blueprint",
        "status": "in_review",
        "reason": "blueprint_sampled",
        "summary": "total earnings for a department in a given year",
        "payload_view": {},
        "evidence_refs": ["evidence::s::e1"],
        "entity_scan": {"result": "pass", "hits": [], "scanner": "regex"},
        "dedup": None,
        "created_at": "2026-08-28T00:00:00Z",
        "verified": False,
        "route_reason": None,
        "score": {
            "score": 0.0,
            "novelty": 0.0,
            "groundedness": 0.0,
            "session_quality": 0.0,
            "novelty_measured": False,
            "quality_measured": False,
            "groundedness_measured": False,
        },
        "decline": None,
        "template_parts": [],
        "param_judge": None,
        "leakage_attestation": None,
    }
    base.update(overrides)
    return base


_TEMPLATE = (
    "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact "
    "WHERE department = {department} AND record_type = 'EARNING'"
)

_PARTS = [
    {"text": "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact WHERE department = "},
    {"slot": "department"},
    {"text": " AND record_type = 'EARNING'"},
]

_HEALTHY_PAYLOAD: dict[str, Any] = {
    "intent": "total earnings for a department in a given year",
    "kind": "single",
    "resolves": {"earnings": "payroll.payroll_fact.gross_pay"},
    "parameterization": [
        {
            "locator": {"table": "payroll.payroll_fact", "column": "department", "value": "0420"},
            "role": "slot",
            "slot": {
                "name": "department",
                "type": "entity",
                "binds_to": "payroll.payroll_fact.department",
                "required": True,
            },
            "why": None,
        },
        {
            "locator": {
                "table": "payroll.payroll_fact",
                "column": "record_type",
                "value": "EARNING",
            },
            "role": "inline",
            "slot": None,
            "why": "defines the metric 'earnings'",
        },
    ],
    "generalization": {
        "sql_template": _TEMPLATE,
        "uses": ["payroll.payroll_fact.department", "payroll.payroll_fact.gross_pay"],
        "uses_rules": [],
        "static_validation": {
            "explain_ok": True,
            "binds_to_subset_uses": True,
            "dag_ok": True,
            "read_only_select": True,
            "date_literal_ok": True,
            "outcome": "ok",
            "reason": None,
        },
    },
    "result_signature": {"shape": [{"column": "total_earnings", "type": "Float64"}]},
    "notes": "reusable department-earnings report",
}


def test_the_blueprint_card_renders_the_template_with_slot_chips(render: Any) -> None:
    """The template is the artifact under review, and its holes are the thing being judged.
    Rendering it as chips is what lets a reviewer see the shape of the generalization without
    reading JSON — and the chip text comes from `template_parts`, split SERVER-side, so the
    page never re-spells the slot grammar."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    template = page.locator('[data-testid="inbox-bp-template"]')
    assert template.count() == 1
    assert "payroll.payroll_fact" in template.inner_text()
    chips = template.locator("span.slot")
    assert chips.count() == 1
    assert chips.first.inner_text() == "department"


def test_the_slot_table_shows_what_each_hole_binds_to(render: Any) -> None:
    """`binds_to` is the column a filled slot will constrain. It is the difference between a
    blueprint that answers the question and one that answers a neighbouring question, and in
    the dump it sat four levels deep."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    slots = page.locator('[data-testid="inbox-bp-slots"]')
    text = slots.inner_text()
    assert "department" in text
    assert "payroll.payroll_fact.department" in text
    assert "required" in text


def test_a_frozen_filter_is_shown_with_the_reason_it_was_frozen(render: Any) -> None:
    """An inline literal plus its `why` is the pair a reviewer adjudicates. Splitting it out
    of the parameterization array is the whole point of the section."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    inline = page.locator('[data-testid="inbox-bp-inline"]')
    text = inline.inner_text()
    assert "record_type = EARNING" in text
    assert "defines the metric 'earnings'" in text


def test_a_blueprint_that_freezes_nothing_says_so_rather_than_hiding_the_section(
    render: Any,
) -> None:
    """THE case the card exists for.

    A blueprint whose intent names a metric ("total earnings") but whose template freezes
    NOTHING has no definition of that metric left — the literal that made it an earnings
    query became a fillable hole, and filling it differently answers a different question
    under an unchanged intent. An empty section that renders nothing would say exactly as
    much as the dump did: nothing.
    """
    over_slotted = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    over_slotted["parameterization"][1] = {
        "locator": {"table": "payroll.payroll_fact", "column": "record_type", "value": "EARNING"},
        "role": "slot",
        "slot": {
            "name": "record_type",
            "type": "entity",
            "binds_to": "payroll.payroll_fact.record_type",
            "required": True,
        },
        "why": None,
    }
    page = render([_item(payload_view=over_slotted, template_parts=_PARTS)])
    inline = page.locator('[data-testid="inbox-bp-inline"]')
    assert inline.count() == 1
    assert "none" in inline.inner_text().lower()
    # ...and the two slots are both visible, which is the other half of the comparison.
    assert "record_type" in page.locator('[data-testid="inbox-bp-slots"]').inner_text()


def test_an_inline_filter_with_no_stated_reason_is_marked(render: Any) -> None:
    """Not an error — plenty of frozen literals are legitimate metric definitions — but an
    unexplained one is the single thing on the card worth a second look, and it must not read
    as an ordinary row."""
    unexplained = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    unexplained["parameterization"][1]["why"] = None
    page = render([_item(payload_view=unexplained, template_parts=_PARTS)])
    marked = page.locator('[data-testid="inbox-bp-inline"] .why.is-missing')
    assert marked.count() == 1


def test_the_five_static_checks_render_as_pass_fail_pills(render: Any) -> None:
    """The checks decide the routing. A failing one is why the card is in this queue, so the
    failing pill and the reason tag it produced belong on the card rather than in the dump."""
    failed = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    failed["generalization"]["static_validation"].update(
        {"date_literal_ok": False, "outcome": "fail_to_review", "reason": "frozen_date_literal"}
    )
    page = render([_item(payload_view=failed, template_parts=_PARTS)])
    checks = page.locator('[data-testid="inbox-bp-checks"]')
    assert "no frozen date" in checks.inner_text()
    assert "frozen_date_literal" in checks.inner_text()
    assert checks.locator(".check-pill.is-fail").count() >= 1


def test_a_missing_check_reads_as_unknown_not_as_passing(render: Any) -> None:
    """A payload that simply lacks a check must not render it green. The one direction this
    can fail unsafely is the one where absence reads as clearance."""
    partial = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    del partial["generalization"]["static_validation"]["dag_ok"]
    page = render([_item(payload_view=partial, template_parts=_PARTS)])
    checks = page.locator('[data-testid="inbox-bp-checks"]')
    assert "? dag" in checks.inner_text()


def test_a_fail_to_review_blueprint_renders_without_a_template(render: Any) -> None:
    """The `needs_parameterization` queue is the one this rework exists for, and every card on
    it carries the FAIL-TO-REVIEW generalization: no `sql_template`, no `uses`. Every section
    must tolerate that rather than throwing and taking the card down with it."""
    declined = {
        "intent": "ratio of deductions to earnings by department",
        "parameterization": [],
        "generalization": {
            "sql_template": "",
            "uses": [],
            "static_validation": {
                "explain_ok": False,
                "binds_to_subset_uses": True,
                "dag_ok": True,
                "read_only_select": True,
                "date_literal_ok": True,
                "outcome": "fail_to_review",
                "reason": "unrewritable_sql",
            },
        },
    }
    page = render(
        [_item(status="needs_parameterization", payload_view=declined, template_parts=[])],
    )
    assert page.locator('[data-testid="inbox-item"]').count() == 1
    # No template section (there is no template), but the checks still explain the routing.
    assert page.locator('[data-testid="inbox-bp-template"]').count() == 0
    assert "unrewritable_sql" in page.locator('[data-testid="inbox-bp-checks"]').inner_text()


def test_the_raw_dump_survives_on_every_card(render: Any) -> None:
    """The typed card RANKS the payload; it does not replace it. Anything the renderer does
    not understand is still one click away, which is what makes the ranking safe to change."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    raw = page.locator('[data-testid="inbox-payload-view"]')
    assert raw.count() == 1
    # Present but COLLAPSED — the dump is the fallback, not the default reading order.
    # `text_content` rather than `inner_text` for exactly that reason: the latter reads
    # RENDERED text and a closed `<details>` renders none of it.
    assert page.locator('[data-testid="inbox-raw-toggle"]').count() == 1
    assert page.locator("details.card-raw[open]").count() == 0
    assert "reusable department-earnings report" in (raw.text_content() or "")


def test_a_payload_key_the_renderer_does_not_know_still_reaches_the_reviewer(
    render: Any,
) -> None:
    """The correctness argument for typed rendering at all.

    A renderer shows the fields it knows about, so a key added upstream would become
    INVISIBLE rather than merely ugly — a strictly worse failure than the dump this replaces.
    Accounting for the remainder means the worst a new key can do is land in a plain block.
    """
    extended = dict(_HEALTHY_PAYLOAD)
    extended["some_future_field"] = {"added": "later"}
    page = render([_item(payload_view=extended, template_parts=_PARTS)])
    other = page.locator('[data-testid="inbox-other-fields"]')
    assert other.count() == 1
    assert "some_future_field" in other.inner_text()


def test_an_unknown_candidate_type_falls_back_to_the_dump(render: Any) -> None:
    """A candidate type added later must render UNPRETTILY, never partially. A typed renderer
    guessing at an unfamiliar shape would show a confident subset of it, which is the one
    outcome worse than the JSON.

    ⚠ Such a card is rendered and then HIDDEN, because the type tabs are a fixed list of four
    and an item matching none of them is filtered out of every view. That is pre-existing and
    NOT this renderer's to fix — it is noted here because it is the reason this test reads the
    DOM rather than the screen, and because it means the fallback leg protects against a
    payload the page cannot currently show. It is still worth having: the leg is what makes
    adding a fifth type a one-line change to `TYPES` rather than a rendering bug.
    """
    page = render(
        [_item(type="some_new_type", payload_view={"a": 1, "b": {"c": 2}}, template_parts=[])],
    )
    assert page.locator('[data-testid="inbox-payload-view"]').count() == 1
    # No typed sections claimed it, and the dump is NOT hidden behind a toggle here.
    assert page.locator('[data-testid="inbox-raw-toggle"]').count() == 0
    assert page.locator('[data-testid="inbox-bp-slots"]').count() == 0


def test_a_knowledge_candidate_gets_one_block_per_field(render: Any) -> None:
    """Knowledge payloads have no shape this page may assume, so they are rendered by VALUE —
    one labelled block per top-level key. It cannot be wrong about a field it never claimed
    to understand, and it is still not one blob."""
    page = render(
        [
            _item(
                type="global_knowledge",
                payload_view={"statement": "pay_period is stored as a date", "scope": "global"},
                template_parts=[],
            )
        ],
    )
    # `text_content`, and lower-cased: the section labels are uppercased by
    # `text-transform`, which `inner_text` faithfully reports and which says nothing
    # about what the renderer produced.
    text = (page.locator('[data-testid="inbox-payload"]').text_content() or "").lower()
    assert "statement" in text
    assert "pay_period is stored as a date" in text
    assert page.locator('[data-testid="inbox-raw-toggle"]').count() == 1


def test_markup_in_a_payload_value_renders_as_text(render: Any) -> None:
    """The page's whole safety story is `textContent`, and the slot chips are the first place
    it takes a server string apart and reassembles it into elements — the shape that turns a
    safe page into an injection surface. A payload value is analyst-derived SQL text; it is
    allowed to contain anything."""
    hostile = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    hostile["notes"] = "<img src=x onerror=window.__pwned=1>"
    hostile["parameterization"][1]["why"] = "<script>window.__pwned=1</script>"
    page = render(
        [
            _item(
                payload_view=hostile,
                template_parts=[{"text": "SELECT '<b>x</b>' FROM t WHERE a = "}, {"slot": "s"}],
            )
        ],
    )
    assert page.evaluate("window.__pwned === undefined") is True
    assert page.locator("#inbox-list img").count() == 0
    assert page.locator("#inbox-list script").count() == 0
    # ...and the text is still SHOWN, not silently dropped — withholding it would hide the
    # literal a reviewer is being asked to judge.
    assert "<b>x</b>" in page.locator('[data-testid="inbox-bp-template"]').inner_text()


# --- the parameterization judge's marker (design §D, phase D-1) -------------


def _judge(verdict="revise", findings=None, feedback="record_type defines the metric"):
    return {
        "verdict": verdict,
        "feedback": feedback,
        "confidence": 0.91,
        "findings": findings
        if findings is not None
        else [
            {
                "class": "A",
                "criterion": "slot_should_be_inline",
                "entry_index": 1,
                "note": "record_type = EARNING is the metric definition",
            }
        ],
    }


def test_no_judge_verdict_means_no_marker(render: Any) -> None:
    """Most cards have none — the judge is off by default and skips everything that failed
    static validation. An empty marker would be noise on every one of them."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    assert page.locator('[data-testid="inbox-judge"]').count() == 0


def test_the_verdict_and_its_findings_are_shown(render: Any) -> None:
    """The marker is how the reviewer's ordinary approve/reject BECOMES the agree/disagree half
    of the phase-D-1 measurement, without asking anybody to do extra work."""
    page = render(
        [_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS, param_judge=_judge())]
    )
    marker = page.locator('[data-testid="inbox-judge"]')
    assert marker.count() == 1
    text = marker.inner_text()
    assert "revise" in text
    assert "0.91" in text
    assert "slot_should_be_inline" in text
    # It says SHADOW, because it is: nothing on this card changed because of the verdict.
    assert "shadow" in text.lower()


def test_the_marker_colours_on_a_class_a_finding_not_on_the_verdict(render: Any) -> None:
    """⚠ The distinction the whole judge is built around. `revise` is also what a slot NAMING
    nit produces; Class A is the only class that means the blueprint is WRONG rather than
    narrower — and it is the only one that could ever authorize a discard in phase D-2."""
    serious = _item(
        payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS, param_judge=_judge()
    )
    page = render([serious])
    assert page.locator('[data-testid="inbox-judge"].is-serious').count() == 1
    assert page.locator('[data-testid="inbox-judge-would-discard"]').count() == 1
    page.close()

    mild = _item(
        payload_view=_HEALTHY_PAYLOAD,
        template_parts=_PARTS,
        param_judge=_judge(findings=[{"class": "C", "criterion": "could_be_slot", "note": "n"}]),
    )
    page = render([mild])
    assert page.locator('[data-testid="inbox-judge"]').count() == 1
    assert page.locator('[data-testid="inbox-judge"].is-serious').count() == 0
    assert page.locator('[data-testid="inbox-judge-would-discard"]').count() == 0


def test_the_marker_describes_what_d2_would_do_not_something_that_happened(
    render: Any,
) -> None:
    """The candidate under this marker survived — the judge cannot drop, route or repair. A
    marker phrased as an action taken would misdescribe the row it sits on."""
    page = render(
        [_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS, param_judge=_judge())]
    )
    text = page.locator('[data-testid="inbox-judge-would-discard"]').inner_text()
    assert "would" in text.lower()
    # ...and the ordinary actions are still there, untouched.
    assert page.locator('[data-testid="inbox-actions"]').count() == 1


def test_judge_prose_renders_as_text(render: Any) -> None:
    """`feedback` and `note` are model prose about a payload; they reach the DOM the same way
    everything else on this page does."""
    hostile = _judge(feedback="<img src=x onerror=window.__pwned=1>")
    page = render(
        [_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS, param_judge=hostile)]
    )
    assert page.evaluate("window.__pwned === undefined") is True
    assert page.locator('[data-testid="inbox-judge"] img').count() == 0


# --- the revise assistant (design §C) ---------------------------------------

_DECLINED_PAYLOAD: dict[str, Any] = {
    "intent": "ratio of deductions to earnings by department",
    "parameterization": [],
    "generalization": {
        "sql_template": "",
        "static_validation": {
            "explain_ok": True,
            "binds_to_subset_uses": True,
            "dag_ok": True,
            "read_only_select": True,
            "date_literal_ok": True,
            "outcome": "fail_to_review",
            "reason": "unrewritable_sql",
        },
    },
}


def _form_item(**kw: Any) -> dict[str, Any]:
    return _item(
        status="needs_parameterization",
        reason="needs_parameterization",
        payload_view=_DECLINED_PAYLOAD,
        template_parts=[],
        decline={
            "reason": "totality_violation",
            "detail": "no entry for 2 literal predicate(s)",
            "detail_withheld": False,
            "corrections_attempted": 2,
            "correction_history": [],
            "judge_verdict": "new",
            "judge_covered_by": "",
        },
        **kw,
    )


def test_the_form_offers_the_assistant_alongside_the_raw_entries(render: Any) -> None:
    """The assistant is an addition to the form, not a replacement for it. A reviewer who
    would rather type the array still can — which is what makes the assistant's absence a lost
    convenience rather than a lost capability."""
    page = render([_form_item()])
    assert page.locator('[data-testid="inbox-revise-block"]').count() == 1
    assert page.locator('[data-testid="inbox-revise-feedback"]').count() == 1
    assert page.locator('[data-testid="inbox-complete-entries"]').count() == 1
    assert page.locator('[data-testid="inbox-complete"]').count() == 1


def test_a_proposal_loads_into_the_form_rather_than_being_applied(render: Any) -> None:
    """⚠ THE TWO-STEP. The assistant FILLS the entries box; the reviewer commits with the same
    button a hand-typed array uses. So `complete` stays the only write path, and an assistant
    that proposed something wrong costs a glance rather than a bad write."""
    _SERVED["revise"] = (
        200,
        {
            "entries": [
                {
                    "locator": {"table": "payroll.payroll_fact", "column": "total_earnings",
                                "value": "0"},
                    "role": "inline",
                    "why": "denominator guard",
                }
            ],
            "replace": True,
            "rationale": "it guards the ratio's own denominator",
            "reason": "",
            "diff": [
                {"kind": "added", "locator": "payroll.payroll_fact.total_earnings",
                 "before": "", "after": "total_earnings = 0 → inline — denominator guard"}
            ],
        },
    )
    page = render([_form_item()])
    page.locator('[data-testid="inbox-revise-feedback"]').fill("inline the denominator guard")
    page.locator('[data-testid="inbox-revise"]').click()
    # ATTACHED, not visible: the diff is collapsed under a toggle now that the preview
    # states the same change in the card's own terms.
    page.wait_for_selector('[data-testid="inbox-revise-diff"]', state="attached")

    # `text_content`, not `inner_text`: the diff is COLLAPSED under a toggle now that the
    # preview states the same change in the card's own terms, and a closed `<details>`
    # renders no text.
    assert "denominator guard" in (
        page.locator('[data-testid="inbox-revise-diff"]').text_content() or ""
    )
    # The entries box now holds the proposal, ready to be reviewed and edited.
    loaded = page.locator('[data-testid="inbox-complete-entries"]').input_value()
    assert "total_earnings" in loaded
    assert json.loads(loaded)[0]["role"] == "inline"
    # `replace` is MIRRORED onto the checkbox: it changes what applying MEANS, and a proposal
    # whose flag silently disagreed with the box would apply the other operation.
    assert page.locator('[data-testid="inbox-complete-replace"]').is_checked() is True
    # ...and the reviewer's own words were what went to the server.
    assert "inline the denominator guard" in _SERVED["last_post"]


def test_no_suggestion_is_reported_without_touching_the_form(render: Any) -> None:
    """"The assistant had no suggestion" must not clear work the reviewer already typed."""
    _SERVED["revise"] = (200, {"entries": [], "replace": False, "rationale": "",
                               "reason": "try rephrasing", "diff": []})
    page = render([_form_item()])
    page.locator('[data-testid="inbox-complete-entries"]').fill('[{"mine": true}]')
    page.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-output"] .card-empty')

    assert "try rephrasing" in page.locator('[data-testid="inbox-revise-output"]').inner_text()
    assert page.locator('[data-testid="inbox-complete-entries"]').input_value() == '[{"mine": true}]'


def test_a_refused_template_edit_surfaces_the_reason_verbatim(render: Any) -> None:
    """The 422 says the model worked against a contract this system does not have. A reviewer
    reading that sentence learns something true, rather than "the assistant failed"."""
    _SERVED["revise"] = (
        422,
        {"detail": "the proposal carried ['sql_template'], but the SQL template is DERIVED"},
    )
    page = render([_form_item()])
    page.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector("#error-banner:not([hidden])")
    assert "sql_template" in page.locator("#error-banner").inner_text()


def test_a_503_says_the_assistant_is_unavailable_and_the_form_survives(render: Any) -> None:
    """No reviser wired is not an error state for this page — the form still works."""
    _SERVED["revise"] = (503, {"detail": "LLM-assisted revision unavailable"})
    page = render([_form_item()])
    page.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector("#error-banner:not([hidden])")
    assert page.locator('[data-testid="inbox-complete"]').count() == 1
    assert page.locator('[data-testid="inbox-complete-entries"]').count() == 1


def test_a_conflicting_proposal_warns_even_though_it_has_entries(render: Any) -> None:
    """⚠ A proposal can carry entries AND a warning: under append, changing an entry that
    already exists is a `conflict` — the merge concatenates, so the apply declines rather than
    updates. The reviewer is one click from that, so the reason must show BESIDE the entries
    rather than only on the empty-proposal path."""
    _SERVED["revise"] = (
        200,
        {
            "entries": [
                {"locator": {"table": "payroll.payroll_fact", "column": "record_type",
                             "value": "EARNING"},
                 "role": "inline", "why": "defines the metric"}
            ],
            "replace": False,
            "rationale": "re-role it to inline",
            "reason": "this proposal changes entries that already exist — tick 'replace'",
            "diff": [
                {"kind": "conflict", "locator": "payroll.payroll_fact.record_type",
                 "before": "record_type = EARNING → slot record_type",
                 "after": "record_type = EARNING → inline — defines the metric"}
            ],
        },
    )
    page = render([_form_item()])
    page.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-warning"]')

    assert "replace" in page.locator('[data-testid="inbox-revise-warning"]').inner_text()
    # The entries still loaded — the proposal is usable, it just needs the other mode.
    assert "record_type" in page.locator('[data-testid="inbox-complete-entries"]').input_value()
    assert "conflict" in (
        page.locator('[data-testid="inbox-revise-diff"]').text_content() or ""
    )


# --- the leakage override (reviewer attestation) ----------------------------

_QUARANTINED = {
    "result": "quarantine",
    "hits": [{"field": "generalization.sql_template", "kind": "person", "span": ""}],
    "scanned_fields": ["generalization.sql_template"],
    "scanner": "regex+ner",
}


def test_the_override_is_offered_only_on_a_flagged_card(render: Any) -> None:
    """A clean pass has nothing to override, and an unsettled scan must not be attested to at
    all — vouching for content nobody scanned is the opposite of the point."""
    page = render([
        _item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS),  # clean pass
        _item(candidate_id="c2", payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS,
              entity_scan={"result": "pending", "hits": []}),
        _item(candidate_id="c3", payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS,
              entity_scan=_QUARANTINED),
    ])
    assert page.locator('[data-testid="inbox-scan-override"]').count() == 1
    card = page.locator('[data-candidate-id="c3"]')
    assert card.locator('[data-testid="inbox-scan-override"]').count() == 1


def test_the_override_button_is_dead_without_a_reason(render: Any) -> None:
    """⚠ This is the only control on the page that steps past a D17 gate, so the reason is
    part of the operation rather than an optional extra — an override with no stated reason is
    not an audit trail. The server 422s on a blank note; the button never sends one."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS,
                         entity_scan=_QUARANTINED)])
    btn = page.locator('[data-testid="inbox-scan-override-apply"]')
    assert btn.is_disabled()
    page.locator('[data-testid="inbox-scan-override-note"]').fill("leave-type enum")
    assert btn.is_enabled()


def test_an_attested_card_says_so_instead_of_offering_it_again(render: Any) -> None:
    """The server only sends an attestation that still BINDS to the current finding, so this
    can never read as "signed off" for a verdict that has since changed."""
    page = render([_item(
        payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS, entity_scan=_QUARANTINED,
        leakage_attestation={"scan_fingerprint": "abc", "attested_at": "2026-08-28T00:00:00Z",
                             "note": "leave-type enum, not a person", "hit_count": 1,
                             "attested_by": "reviewer-token"},
    )])
    assert page.locator('[data-testid="inbox-scan-override"]').count() == 0
    said = page.locator('[data-testid="inbox-scan-attested"]').inner_text()
    assert "false positive" in said and "leave-type enum" in said


# --- the pager (one card at a time) -----------------------------------------


def _many(n: int) -> list:
    return [
        _item(candidate_id=f"c{i}", summary=f"blueprint number {i}",
              payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)
        for i in range(n)
    ]


def test_only_one_card_is_shown_at_a_time(render: Any) -> None:
    """A review queue is a WORKLIST, not a feed: the reviewer adjudicates one candidate and
    moves on. The rest stay ATTACHED — paging must not refetch, and the type filter already
    depended on the whole set being in the DOM."""
    page = render(_many(4))
    assert page.locator('[data-testid="inbox-item"]').count() == 4
    assert page.locator('[data-testid="inbox-item"]:visible').count() == 1
    assert page.locator('[data-testid="inbox-pager-position"]').inner_text() == "1 of 4"


def test_next_and_prev_walk_the_queue(render: Any) -> None:
    page = render(_many(3))
    def shown() -> str | None:
        return page.locator('[data-testid="inbox-item"]:visible').get_attribute(
            "data-candidate-id"
        )
    assert shown() == "c0"
    page.click('[data-testid="inbox-pager-next"]')
    assert shown() == "c1"
    assert page.locator('[data-testid="inbox-pager-position"]').inner_text() == "2 of 3"
    page.click('[data-testid="inbox-pager-prev"]')
    assert shown() == "c0"


def test_the_ends_of_the_queue_disable_their_button(render: Any) -> None:
    """Rather than wrapping around. A worklist has an end, and silently looping would make
    "have I seen them all?" unanswerable."""
    page = render(_many(2))
    assert page.locator('[data-testid="inbox-pager-prev"]').is_disabled()
    assert page.locator('[data-testid="inbox-pager-next"]').is_enabled()
    page.click('[data-testid="inbox-pager-next"]')
    assert page.locator('[data-testid="inbox-pager-next"]').is_disabled()


def test_a_single_card_hides_the_pager_entirely(render: Any) -> None:
    """"1 of 1" between two dead buttons is furniture, not navigation."""
    page = render(_many(1))
    assert page.locator('[data-testid="inbox-pager"]:visible').count() == 0


def test_arrow_keys_page_but_never_while_typing(render: Any) -> None:
    """⚠ The fail-to-review form and the assistant's feedback box both take free text. A
    global Left/Right handler that did not check the focus target would make them unusable —
    every attempt to move the caret would change the card underneath."""
    page = render(_many(3))
    page.keyboard.press("ArrowRight")
    assert page.locator('[data-testid="inbox-pager-position"]').inner_text() == "2 of 3"

    page.locator('[data-testid="inbox-revise-feedback"]:visible').first.click()
    page.keyboard.press("ArrowLeft")
    assert page.locator('[data-testid="inbox-pager-position"]').inner_text() == "2 of 3"


def test_switching_tab_resets_the_position(render: Any) -> None:
    """Position 7 of the blueprints means nothing among the knowledge candidates."""
    items = _many(3) + [
        _item(candidate_id="k1", type="global_knowledge",
              payload_view={"statement": "a rule"}, template_parts=[])
    ]
    page = render(items)
    page.click('[data-testid="inbox-pager-next"]')
    assert page.locator('[data-testid="inbox-pager-position"]').inner_text() == "2 of 3"
    page.click('button[data-type="global_knowledge"]')
    page.click('button[data-type="blueprint"]')
    assert page.locator('[data-testid="inbox-pager-position"]').inner_text() == "1 of 3"


# --- the on-card preview ----------------------------------------------------


def _proposal(**over) -> tuple:
    body = {
        "entries": [{
            "locator": {"table": "payroll.payroll_fact", "column": "record_type",
                        "value": "EARNING"},
            "role": "inline", "why": "defines the metric earnings",
        }],
        "replace": True,
        "rationale": "record_type defines the metric",
        "reason": "",
        "diff": [{"kind": "role_changed", "locator": "payroll.payroll_fact.record_type",
                  "before": "… → slot record_type", "after": "… → inline — defines the metric"}],
    }
    body.update(over)
    return (200, body)


def _over_slotted() -> dict:
    payload = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    payload["parameterization"][1] = {
        "locator": {"table": "payroll.payroll_fact", "column": "record_type",
                    "value": "EARNING"},
        "role": "slot",
        "slot": {"name": "record_type", "type": "entity",
                 "binds_to": "payroll.payroll_fact.record_type", "required": True},
        "why": None,
    }
    return payload


def test_the_proposal_is_previewed_in_the_cards_own_terms(render: Any) -> None:
    """⚠ THE POINT OF PREVIEWING ON THE CARD. A diff row says
    `role_changed | payroll.payroll_fact.record_type`. The card says which SLOTS the template
    takes and which filters are FROZEN — the terms the reviewer is judging in. So the preview
    re-renders those sections with the proposal applied: record_type leaves the slot table and
    arrives under frozen filters."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_over_slotted(), template_parts=_PARTS)])
    card = page.locator('[data-testid="inbox-item"]:visible')
    card.locator('[data-testid="inbox-revise-feedback"]').fill("freeze the metric definition")
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-preview"]')

    preview = card.locator('[data-testid="inbox-preview"]')
    # Lower-cased: the section labels are uppercased by `text-transform`, which `inner_text`
    # faithfully reports and which says nothing about what the renderer produced.
    text = preview.inner_text().lower()
    assert "not yet applied" in text
    # record_type is now a FROZEN FILTER in the preview, with its reason...
    assert "record_type = earning" in text
    assert "defines the metric earnings" in text
    # The preview's sections are addressable under their OWN prefix — rendering the card's
    # sections twice would otherwise make `inbox-bp-slots` ambiguous for every selector on
    # the page, not just for this test.
    assert card.locator('[data-testid="preview-inbox-bp-inline"]').count() == 1
    # ...and the LIVE card still shows record_type as a slot, so the two panes read as
    # before/after rather than replacing one another.
    assert "record_type" in card.locator('[data-testid="inbox-bp-slots"]').inner_text()


def test_the_preview_does_not_invent_a_sql_template(render: Any) -> None:
    """⚠ The template is DERIVED by the S4 AST rewrite from the accepted SQL. This page cannot
    reproduce that, and a spliced approximation would be the "model writes the template"
    failure the whole design refuses, wearing a UI costume. The preview says so instead."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_over_slotted(), template_parts=_PARTS)])
    card = page.locator('[data-testid="inbox-item"]:visible')
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-preview"]')

    preview = card.locator('[data-testid="inbox-preview"]')
    assert preview.locator('[data-testid="preview-inbox-bp-template"]').count() == 0
    assert preview.locator('[data-testid="inbox-bp-template"]').count() == 0
    assert "regenerated by the server" in preview.inner_text().lower()


def test_the_changed_rows_are_marked_on_both_sides(render: Any) -> None:
    """So the eye can pair them without hunting for the matching line."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_over_slotted(), template_parts=_PARTS)])
    card = page.locator('[data-testid="inbox-item"]:visible')
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-preview"]')
    assert card.locator(".is-preview-changed").count() >= 2


# --- the SQL rewrite opt-in, driven ---------------------------------------------------------

_REWRITTEN_SQL = (
    "SELECT sum(gross_pay) AS total_earnings FROM payroll.payroll_fact "
    "WHERE department = {department} AND record_type = 'EARNING' AND gross_pay > 0"
)

_CAUTION = (
    "This replaces the query the session ran. It will be re-checked from scratch and "
    "cannot auto-land."
)


def _rewrite_proposal(**over: Any) -> tuple:
    status, body = _proposal()
    body.update(
        {
            "replace": True,
            "sql_changed": True,
            "sql": _REWRITTEN_SQL,
            "caution": _CAUTION,
        }
    )
    body.update(over)
    return (status, body)


def _queue_card(page: Any) -> Any:
    return page.locator('[data-testid="inbox-item"]:visible')


def _next_post(timeout: float = 5.0) -> dict:
    """The next POST body the stub receives, decoded.

    Polled rather than inferred from a DOM change: a write POST is fired AFTER the modal
    closes, and both asks in a row leave the same selector attached — so keying on the page
    would make these tests pass or fail on timing rather than on the body that was sent.
    Callers clear `last_post` first.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _SERVED["last_post"]
        if body:
            return json.loads(body)
        time.sleep(0.02)
    raise AssertionError("no POST arrived within the timeout")


def test_the_rewrite_option_is_off_until_a_reviewer_asks_for_it(render: Any) -> None:
    """⚠ DEFAULT-OFF, with the cost written beside the box. Every other thing the assistant
    proposes is a re-reading of a query that really ran; this one REPLACES it, which is the
    only action on this surface that cuts the provenance chain the static checks stand on."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    box = card.locator('[data-testid="inbox-revise-allow-sql"]')
    assert box.count() == 1
    assert box.is_checked() is False
    caution = card.locator('[data-testid="inbox-revise-allow-sql-caution"]').inner_text()
    assert "no longer the one the session ran" in caution
    assert "trial-run before approval" in caution


def test_the_ask_tells_the_server_whether_a_rewrite_was_allowed(render: Any) -> None:
    """`allow_sql` rides every ask, explicitly false when untouched — the BFF forwards this
    body verbatim, so an omitted field would leave the reviser to guess, and the guess that
    costs something is the permissive one."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)

    _SERVED["last_post"] = ""
    card.locator('[data-testid="inbox-revise"]').click()
    assert _next_post()["allow_sql"] is False

    _SERVED["last_post"] = ""
    card.locator('[data-testid="inbox-revise-allow-sql"]').check()
    card.locator('[data-testid="inbox-revise"]').click()
    assert _next_post()["allow_sql"] is True


def test_a_composite_candidate_is_told_why_it_cannot_have_one(render: Any) -> None:
    """A composite composes other blueprints and has no single query to replace — the server
    422s the attempt. The box is disabled and says so, which teaches the reason instead of
    handing back an error the reviewer has to interpret."""
    composite = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    composite["kind"] = "composite"
    page = render([_item(payload_view=composite, template_parts=_PARTS)])
    card = _queue_card(page)
    assert card.locator('[data-testid="inbox-revise-allow-sql"]').is_disabled() is True
    assert "composite" in card.locator(
        '[data-testid="inbox-revise-allow-sql-unavailable"]'
    ).inner_text()


def test_the_server_refusal_for_a_composite_reaches_the_reviewer(render: Any) -> None:
    """The 422 says something true about this system's design. It goes to the banner verbatim
    rather than becoming "the assistant failed"."""
    _SERVED["revise"] = (
        422,
        {"detail": "SQL rewrite is not offered for composite blueprints (they compose)."},
    )
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _queue_card(page).locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector("#error-banner:not([hidden])")
    assert "not offered for composite blueprints" in page.locator("#error-banner").inner_text()


def test_a_rewritten_proposal_shows_both_queries_under_a_warning(render: Any) -> None:
    """⚠ The reviewer's question is not "which characters moved" but "is this still the query
    I asked about" — which is read by looking at both queries whole. The server's own caution
    leads, because it knows why THIS rewrite is being cautioned about."""
    _SERVED["revise"] = _rewrite_proposal()
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise-allow-sql"]').check()
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-sql-warning"]')

    warning = card.locator('[data-testid="inbox-revise-sql-warning"]')
    assert _CAUTION in warning.inner_text()
    assert warning.get_attribute("role") == "alert"
    # The current template is the card's own, from the SAME accessor the card renders with.
    assert "gross_pay > 0" not in (
        card.locator('[data-testid="inbox-revise-sql-before"]').text_content() or ""
    )
    assert "total_earnings" in (
        card.locator('[data-testid="inbox-revise-sql-before"]').text_content() or ""
    )
    assert "gross_pay > 0" in (
        card.locator('[data-testid="inbox-revise-sql-after"]').text_content() or ""
    )


def test_a_proposal_that_left_the_sql_alone_raises_no_alarm(render: Any) -> None:
    """The alarm has to mean something. A proposal that only re-roles entries looks exactly as
    it did before the rewrite existed — an alarm on every proposal is an alarm on none."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-diff"]', state="attached")
    assert card.locator('[data-testid="inbox-revise-sql-warning"]').count() == 0
    assert card.locator('[data-testid="inbox-revise-sql-before"]').count() == 0
    assert card.locator('[data-testid="inbox-revise-sql-after"]').count() == 0


def test_the_modal_shows_the_proposed_sql_and_sends_it_verbatim(render: Any) -> None:
    """⚠ The `<pre>` in the modal is the query being committed to, so it must be the NEW one —
    the old template above entries written against a new one is an approval of something never
    read. What is posted is the server's own string, unedited: the page never composes SQL."""
    _SERVED["revise"] = _rewrite_proposal()
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise-allow-sql"]').check()
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-apply"]:not([hidden])')
    card.locator('[data-testid="inbox-revise-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-modal"]')

    modal = page.locator('[data-testid="inbox-revise-modal"]')
    assert modal.locator('[data-testid="inbox-revise-modal-sql-rewritten"]').count() == 1
    assert _CAUTION in modal.locator('[data-testid="inbox-revise-modal-sql-caution"]').inner_text()
    assert "gross_pay > 0" in (modal.locator(".modal-sql").text_content() or "")
    # The button names what it is applying — "Apply to the blueprint" would understate it.
    apply_label = modal.locator('[data-testid="inbox-revise-modal-apply"]').inner_text()
    assert "rewritten SQL" in apply_label
    # There is still no way to EDIT the query by hand: a box here would invalidate the checks.
    assert modal.locator("textarea").count() == 0

    _SERVED["last_post"] = ""
    modal.locator('[data-testid="inbox-revise-modal-apply"]').click()
    assert _next_post()["sql"] == _REWRITTEN_SQL


def test_an_entries_only_apply_sends_no_sql_at_all(render: Any) -> None:
    """Absence means "keep the query the session ran". Sending `""` on every apply would ask
    the server to tell no-rewrite from rewrite-to-nothing, and one of those readings deletes
    the query."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_over_slotted(), template_parts=_PARTS)])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-apply"]:not([hidden])')
    card.locator('[data-testid="inbox-revise-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-modal"]')
    _SERVED["last_post"] = ""
    page.locator('[data-testid="inbox-revise-modal-apply"]').click()
    assert "sql" not in _next_post()


def test_a_rewritten_card_carries_the_fact_permanently(render: Any) -> None:
    """⚠ THE BADGE OUTLIVES THE REVIEW THAT MADE IT. The modal's caution is seen once, by the
    reviewer who chose the rewrite; everyone after them would otherwise read the card as a
    faithful record of a query that ran. It says so in both places a decision is taken — the
    header, and beside Approve — and it blocks neither."""
    rewritten = json.loads(json.dumps(_HEALTHY_PAYLOAD))
    rewritten["sql_rewrite"] = {
        "by": "assistant",
        "applied_at": "2026-08-30T14:05:00Z",
        "previous_sql_sha256": "9f2b" + "0" * 60,
    }
    page = render([_item(payload_view=rewritten, template_parts=_PARTS)])
    card = _queue_card(page)
    badge = card.locator('[data-testid="inbox-card-sql-rewritten"]').inner_text()
    assert "SQL rewritten by the assistant" in badge
    assert "2026-08-30 14:05 UTC" in badge
    assert "Trial-run before approving" in badge

    assert card.locator('[data-testid="inbox-approve-sql-rewritten"]').count() == 1
    # A CAUTION, not a gate: the reviewer who has trial-run the rewrite is exactly the person
    # who should be able to approve it.
    assert card.locator('[data-testid="inbox-approve"]').is_disabled() is False


def test_an_untouched_card_carries_no_badge(render: Any) -> None:
    """The badge means something only if a card without a rewrite never shows it."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    assert card.locator('[data-testid="inbox-card-sql-rewritten"]').count() == 0
    assert card.locator('[data-testid="inbox-approve-sql-rewritten"]').count() == 0


def test_a_rewrite_with_no_entries_can_still_be_applied(render: Any) -> None:
    """⚠ THE ZERO-ENTRIES REWRITE. A rewritten query with no literal predicates at all — a
    plain aggregate with no WHERE — passes the server's totality walk with an EMPTY entries
    array. That proposal must be applicable: the alternative is a warning the reviewer can
    read and cannot act on, which is the worst of both.

    What is posted from the QUEUE is the empty array and the proposed SQL — `apply_revision`
    is replace-only, so the verb carries what `replace` says on the form (asserted below)."""
    _SERVED["revise"] = _rewrite_proposal(entries=[], diff=[], rationale="drop the guard entirely")
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise-allow-sql"]').check()
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-sql-warning"]')

    # The panes are there even though no entry moved...
    assert "gross_pay > 0" in (
        card.locator('[data-testid="inbox-revise-sql-after"]').text_content() or ""
    )
    # ...and so is the button, which "no suggestion" would have hidden.
    apply_btn = card.locator('[data-testid="inbox-revise-apply"]')
    assert apply_btn.is_visible() is True
    apply_btn.click()
    page.wait_for_selector('[data-testid="inbox-revise-modal"]')

    modal = page.locator('[data-testid="inbox-revise-modal"]')
    assert modal.locator('[data-testid="inbox-revise-modal-sql-rewritten"]').count() == 1
    # The empty parameterization is SAID, not left as a blank body: applying clears the array.
    assert "empty array" in modal.locator('[data-testid="inbox-revise-modal-no-entries"]').inner_text()
    assert "rewritten SQL" in modal.locator('[data-testid="inbox-revise-modal-apply"]').inner_text()

    _SERVED["last_post"] = ""
    modal.locator('[data-testid="inbox-revise-modal-apply"]').click()
    posted = _next_post()
    assert posted["entries"] == []
    assert posted["sql"] == _REWRITTEN_SQL


def test_the_form_applies_a_zero_entries_rewrite_as_a_replace(render: Any) -> None:
    """The same proposal on the FORM, where `replace` is an explicit field rather than the
    verb's own meaning. It must be TRUE: appending an empty array onto entries written for the
    old query would leave the blueprint parameterized against predicates the rewritten SQL no
    longer has."""
    _SERVED["revise"] = _rewrite_proposal(entries=[], diff=[], rationale="no predicates left")
    page = render([_form_item()])
    page.locator('[data-testid="inbox-revise-allow-sql"]').check()
    page.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-sql-warning"]')
    page.locator('[data-testid="inbox-revise-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-modal"]')

    _SERVED["last_post"] = ""
    page.locator('[data-testid="inbox-revise-modal-apply"]').click()
    posted = _next_post()
    assert posted["entries"] == []
    assert posted["replace"] is True
    assert posted["sql"] == _REWRITTEN_SQL
    # The textarea is left holding exactly what was sent — the modal never becomes a hidden
    # second source of truth for a body the reviewer cannot see.
    assert json.loads(page.locator('[data-testid="inbox-complete-entries"]').input_value()) == []


def test_an_entries_only_proposal_with_nothing_in_it_still_offers_nothing(render: Any) -> None:
    """The other half of the same rule. No rewrite and no entries is genuinely empty — no
    button, no modal, and the reviewer's own typing left alone."""
    _SERVED["revise"] = (200, {"entries": [], "replace": False, "rationale": "",
                               "reason": "try rephrasing", "diff": [],
                               "sql_changed": False, "sql": "", "caution": ""})
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-output"] .card-empty')
    assert card.locator('[data-testid="inbox-revise-apply"]').is_visible() is False
    assert card.locator('[data-testid="inbox-revise-sql-warning"]').count() == 0
