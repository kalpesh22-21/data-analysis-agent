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
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

_INBOX = pathlib.Path(__file__).resolve().parents[2] / "ui" / "static" / "inbox.html"

# The list the stub server is currently serving. Module-level rather than threaded through
# the handler because `HTTPServer` constructs the handler per request.
_SERVED: dict[str, Any] = {
    "items": [],
    "revise": None,
    "last_post": "",
    # The knowledge-edit + user-promotion surfaces (knowledge-edit design §C.3/§D.3). Keyed by
    # PATH rather than folded into `revise`, because a single card now talks to four different
    # endpoints and a stub that answered them all with one queued body could not tell an
    # assistant proposal apart from an apply result.
    "revise_knowledge": None,
    "apply_knowledge": None,
    "uk_records": [],
    "uk_promote": None,
    # Every POST, in order, as {path, body} — the only way to assert that "Use this draft"
    # wrote NOTHING.
    "posts": [],
    "last_get": "",
}

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
        _SERVED["posts"].append({"path": self.path, "body": _SERVED["last_post"]})
        if self.path.endswith("/revise_knowledge"):
            queued = _SERVED.get("revise_knowledge")
        elif self.path.endswith("/apply_knowledge"):
            queued = _SERVED.get("apply_knowledge")
        elif self.path.endswith("/user_knowledge/promote"):
            queued = _SERVED.get("uk_promote")
        else:
            queued = _SERVED.get("revise")
        status, payload = queued or (200, {})
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        _SERVED["last_get"] = self.path
        if self.path.startswith("/api/inbox/health"):
            body = b'{"write_plane":"full"}'
            ctype = "application/json"
        elif self.path.startswith("/api/inbox/user_knowledge"):
            # BEFORE the generic `/api/inbox` branch, for the same reason the BFF declares its
            # route before the catch-all: the prefix matches both, and the first arm wins.
            body = json.dumps({"records": _SERVED["uk_records"]}).encode()
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
        _SERVED["posts"] = []
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


# --- The composite plan, as a DAG ------------------------------------------------
#
# A composite is a GRAPH: `consumes` says whose output lands in which name, and rendering it
# as a vertical list of SQL blocks asked the reviewer to rebuild the wiring from `$0.col`
# strings while judging the SQL. What is pinned here is that the graph exists, that it is
# TYPED (a table hop is the one a trial run cannot make), that the ports and the drawing
# agree, and — the part with no visual tell — that a HALF-PRESENT or hostile plan renders
# instead of throwing, because a renderer that throws takes the whole card with it.


def _step(order: int, **kw: Any) -> dict[str, Any]:
    """One `composes` entry, defaulted to a plain source query."""
    base: dict[str, Any] = {
        "order": order,
        "node_kind": "query",
        "step_intent": f"step {order} does something",
        "feeds_from": [],
        "consumes": {},
        "output": {},
    }
    base.update(kw)
    return base


_FAN_IN_STEPS = [
    _step(0, step_intent="pull the earnings detail for department A",
          output={"detail_a": "table"}),
    _step(1, step_intent="pull the earnings detail for department B",
          output={"detail_b": "table"}),
    _step(2, step_intent="difference the two details and report the delta",
          feeds_from=[0, 1],
          consumes={"detail_a": "$0", "detail_b": "$1"},
          output={"delta": "scalar"}),
]

_FAN_IN_SQL = [
    {"order": 0, "sql_template": "SELECT employee_id, gross_pay FROM payroll.payroll_fact"},
    {"order": 1, "sql_template": "SELECT employee_id, gross_pay FROM payroll.payroll_fact"},
    {"order": 2, "sql_template": "SELECT sum(a.gross_pay) - sum(b.gross_pay) AS delta"},
]

_SCALAR_STEPS = [
    _step(0, step_intent="compute the company-wide average pay",
          output={"company_avg": "scalar"}),
    _step(1, step_intent="list everyone paid above it",
          node_kind="approval",
          feeds_from=[0],
          consumes={"company_avg": "$0.company_avg"},
          output={"listing": "table"},
          when="row_estimate > 500"),
]


def _composite(steps: Any = None, templates: Any = None, **kw: Any) -> dict[str, Any]:
    """A composite payload_view with either half — or neither — present."""
    view: dict[str, Any] = {
        "intent": "a plan with more than one query in it",
        "kind": "composite",
        "generalization": {"uses": [], "uses_rules": []},
    }
    if steps is not None:
        view["composes"] = steps
    if templates is not None:
        view["generalization"]["node_templates"] = templates
    view.update(kw)
    return view


def _dag_nodes(card: Any) -> Any:
    return card.locator('[data-testid="inbox-dag-node"]')


def test_the_composite_plan_is_drawn_as_a_graph(render: Any) -> None:
    """⚠ THE FAN-IN IS THE CASE. Two sources feeding one step is invisible in a vertical
    stack — the reviewer sees three queries in a row and has to derive from `$0`/`$1` that the
    third one joins the other two. The overview draws it, labels each edge with the VALUE that
    crosses it, and puts the fan-in a layer to the right of both of its inputs."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)

    svg = card.locator('[data-testid="inbox-dag"]')
    assert svg.count() == 1
    # Announced with its size in it: a screen reader gets "how big is this plan" without
    # walking three nodes to find out.
    assert svg.get_attribute("aria-label") == "plan graph: 3 steps, 2 edges"
    # ⚠ `group`, NOT `img`, and the nodes below are the reason. This pinned `img` until the
    # per-node `tabindex`/`aria-label` went in: the children of a `role="img"` are
    # PRESENTATIONAL by definition, so the names on the focus stops sat inside a subtree
    # assistive technology may flatten to the single image name — a graph a reviewer can tab
    # through but not hear. `group` keeps the graph's own name and leaves its children exposed.
    assert svg.get_attribute("role") == "group"

    assert _dag_nodes(card).count() == 3
    assert card.locator('[data-testid="inbox-dag-edge"]').count() == 2

    # LONGEST PATH, not shortest: the consumer sits after both producers.
    layers = {
        _dag_nodes(card).nth(i).get_attribute("data-order"):
            _dag_nodes(card).nth(i).get_attribute("data-layer")
        for i in range(3)
    }
    assert layers == {"0": "0", "1": "0", "2": "1"}

    # `text_content` rather than `inner_text`: these are SVG elements, not HTMLElements.
    labels = " ".join(
        card.locator('[data-testid="inbox-dag-edge"]').nth(i).text_content() or ""
        for i in range(2)
    )
    assert "detail_a" in labels
    assert "detail_b" in labels

    # Each node is reachable and named on its own, not just as part of the picture.
    first = _dag_nodes(card).nth(0)
    assert first.get_attribute("tabindex") == "0"
    assert "pull the earnings detail for department A" in (first.get_attribute("aria-label") or "")


def test_the_section_header_says_how_big_the_plan_is(render: Any) -> None:
    """"template" on a three-step plan invites reading the first node as the whole blueprint.
    The count is the cheapest correction to that, and it is the first thing on the section."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    # The label is upper-cased by CSS, so the assertion is on the text the page was GIVEN.
    labels = _queue_card(page).locator(".item-field-label")
    assert "plan — 3 steps, 2 edges" in [
        (labels.nth(i).text_content() or "") for i in range(labels.count())
    ]


def test_a_step_names_its_inputs_and_types_them(render: Any) -> None:
    """The ports are where `$0` becomes readable: which value, from which step, of which type.
    TABLE is the load-bearing half of that — it is exactly what the trial run comes back
    `table_intermediate_unsupported` on, so it has to be visible before the run."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)
    fan_in = card.locator('.card-node[data-order="2"]')

    inputs = fan_in.locator('[data-testid="inbox-node-input"]')
    assert inputs.count() == 2
    texts = inputs.all_inner_texts()
    assert any("detail_a ← step 0" in t for t in texts)
    assert any("detail_b ← step 1" in t for t in texts)
    assert [inputs.nth(i).get_attribute("data-type") for i in range(2)] == ["table", "table"]

    outputs = fan_in.locator('[data-testid="inbox-node-output"]')
    assert outputs.count() == 1
    assert outputs.first.get_attribute("data-name") == "delta"
    assert outputs.first.get_attribute("data-type") == "scalar"


def test_a_source_step_has_outputs_and_no_inputs(render: Any) -> None:
    """The other half of the same claim: a step that takes nothing shows no inputs row at all,
    rather than an empty one that reads as "inputs: none known"."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    source = _queue_card(page).locator('.card-node[data-order="0"]')
    assert source.locator('[data-testid="inbox-node-inputs"]').count() == 0
    outputs = source.locator('[data-testid="inbox-node-output"]')
    assert outputs.count() == 1
    assert outputs.first.get_attribute("data-name") == "detail_a"
    assert outputs.first.get_attribute("data-type") == "table"


def test_a_scalar_reference_is_typed_as_a_scalar_and_the_condition_is_marked(
    render: Any,
) -> None:
    """`$0.company_avg` is ONE COLUMN, not the table — the same hop that would be refused as a
    table intermediate is fine as a scalar, so the two cannot render alike. A `when` is marked
    on the node because a conditional step may not run at all."""
    page = render([_item(payload_view=_composite(_SCALAR_STEPS))])
    card = _queue_card(page)
    pill = card.locator('.card-node[data-order="1"] [data-testid="inbox-node-input"]')
    assert pill.get_attribute("data-type") == "scalar"
    assert "company_avg ← step 0.company_avg" in (pill.inner_text() or "")

    node = card.locator('[data-testid="inbox-dag-node"][data-order="1"]')
    assert "when" in (node.text_content() or "")
    assert node.get_attribute("data-kind") == "approval"
    # The ordering-only chip is gone where a consumes edge already says it, more precisely.
    assert "runs after" not in (card.locator('.card-node[data-order="1"]').inner_text() or "")


def test_one_step_is_one_colour_in_the_graph_the_card_and_the_pill(render: Any) -> None:
    """⚠ THE HUE IS A JOIN, AND A JOIN THAT ONLY ALMOST HOLDS IS WORSE THAN NO COLOUR. Three
    surfaces claim to be showing the same step: the node in the drawing, the step card below
    it, and the input pill on whatever consumes it. A reviewer follows the colour instead of
    re-reading `$0`, so if the three ever diverged the card would be grouping things that are
    not related — and nothing on screen would say so.

    Resolved, not compared as source text: `--dag-step-hue` and `--dag-port-hue` are set inline
    to `var(--dag-hue-N)`, and two properties can carry the identical string and still paint
    differently if the palette is scoped somewhere one of them cannot see it. What is pinned is
    the COLOUR that comes out the far end of the cascade.
    """
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    _queue_card(page)

    seen = page.evaluate(
        """() => {
            const card = document.querySelector('[data-testid="inbox-item"]');
            const q = (s) => card.querySelector(s);
            const step = q('.card-node[data-order="0"]');
            const port = q(
                '.card-node[data-order="2"] [data-testid="inbox-node-input"][data-from="0"]'
            );
            return {
                palette: getComputedStyle(q(".card-plan"))
                    .getPropertyValue("--dag-hue-0").trim(),
                step_prop: getComputedStyle(step)
                    .getPropertyValue("--dag-step-hue").trim(),
                port_prop: getComputedStyle(port)
                    .getPropertyValue("--dag-port-hue").trim(),
                node_paint: getComputedStyle(
                    q('[data-testid="inbox-dag-node"][data-order="0"] .dag-node-box')
                ).stroke,
                step_paint: getComputedStyle(step).borderLeftColor,
                port_paint: getComputedStyle(port).borderLeftColor,
                consumer_paint: getComputedStyle(q('.card-node[data-order="2"]'))
                    .borderLeftColor,
            };
        }"""
    )

    # The palette entry actually resolved to a colour — an unknown var() would come back "".
    assert seen["palette"], seen
    assert seen["step_prop"] == seen["palette"], seen
    assert seen["port_prop"] == seen["palette"], seen
    # ...and all three surfaces PAINT it, not merely name it.
    assert seen["node_paint"] == seen["step_paint"] == seen["port_paint"], seen
    assert seen["node_paint"].startswith("rgb"), seen
    # The pill carries the PRODUCER's colour, so the step it consumes must be a different one
    # or the join says nothing.
    assert seen["consumer_paint"] != seen["port_paint"], seen


def test_hovering_a_node_lights_the_step_it_stands_for(render: Any) -> None:
    """The graph and the cards are one surface or they are two. Hovering a node marks its card
    (and its edges), which is what makes "which of these three blocks is the fan-in" a glance
    rather than a scroll."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)
    step_card = card.locator('.card-node[data-order="2"]')
    assert "is-linked" not in (step_card.get_attribute("class") or "")

    card.locator('[data-testid="inbox-dag-node"][data-order="2"]').hover()
    page.wait_for_selector('.card-node[data-order="2"].is-linked')
    assert "is-linked" in (step_card.get_attribute("class") or "")
    # Both edges into the fan-in light with it.
    assert card.locator('[data-testid="inbox-dag-edge"].is-linked').count() == 2


def test_the_layout_is_a_pure_function_of_the_steps(render: Any) -> None:
    """The layering rule has no markup, and asserting it through rendered coordinates would
    test the drawing instead. A DIAMOND is the shape that separates longest-path from
    shortest-path layering: node 3 belongs after BOTH middles, not beside them."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    diamond = [
        _step(0, output={"a": "table"}),
        _step(1, feeds_from=[0], consumes={"a": "$0"}, output={"b": "table"}),
        _step(2, feeds_from=[0], consumes={"a": "$0"}, output={"c": "table"}),
        _step(3, feeds_from=[1, 2], consumes={"b": "$1", "c": "$2"}),
    ]
    layout = page.evaluate("steps => window.__inboxDag.layout(steps)", diamond)
    assert [n["layer"] for n in layout["nodes"]] == [0, 1, 1, 2]
    assert len(layout["edges"]) == 4
    # The two middles are drawn apart rather than on top of each other.
    assert sorted(n["row"] for n in layout["nodes"] if n["layer"] == 1) == [0, 1]

    # A reference to a step that is not in the plan, and one to the step itself, are dropped —
    # a payload_view is untrusted input and an edge to nowhere is not drawable.
    hostile = page.evaluate(
        "steps => window.__inboxDag.layout(steps)",
        [
            _step(0, consumes={"self": "$0", "nobody": "$9"}, feeds_from=[0, 42]),
            _step(1, consumes={"a": "not-a-ref"}, feeds_from=[0]),
        ],
    )
    assert [e["flow"] for e in hostile["edges"]] == ["order"]
    assert hostile["edges"][0]["from"] == 0 and hostile["edges"][0]["to"] == 1

    empty = page.evaluate("() => window.__inboxDag.layout([])")
    assert empty["nodes"] == [] and empty["edges"] == []
    assert page.evaluate("() => window.__inboxDag.layout(null).nodes") == []


def test_a_plan_with_wiring_and_no_sql_still_renders(render: Any) -> None:
    """⚠ HALF A COMPOSITE IS STILL A COMPOSITE. A hand-authored candidate arrives with a plan
    and no generalization; keying the card off `node_templates` alone showed it as a blueprint
    with no steps at all. The missing SQL is STATED per step rather than making the step
    disappear."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS))])
    card = _queue_card(page)
    assert _dag_nodes(card).count() == 3
    assert card.locator('[data-testid="inbox-bp-node"]').count() == 3
    assert card.inner_text().count("no SQL on this step") == 3


def test_sql_with_no_wiring_still_renders_its_steps(render: Any) -> None:
    """The mirror case — node templates whose plan was withheld. There is nothing to draw
    edges from, so there are none; the steps are still readable."""
    page = render([_item(payload_view=_composite(templates=_FAN_IN_SQL))])
    card = _queue_card(page)
    assert card.locator('[data-testid="inbox-bp-node"]').count() == 3
    assert card.locator('[data-testid="inbox-dag-edge"]').count() == 0
    assert _dag_nodes(card).count() == 3


def test_a_malformed_plan_renders_instead_of_throwing(render: Any) -> None:
    """Every shape a hostile or half-migrated payload can take, on one card: a non-object
    entry, a missing order, a DUPLICATE order, a string order, non-object `consumes`. The
    render fixture fails the test on any page error, so this asserting anything at all is the
    claim."""
    page = render([
        _item(payload_view=_composite(
            [
                "not an object",
                _step(0, output={"a": "table"}),
                {"order": 0, "step_intent": "a duplicate of step 0"},
                {"step_intent": "no order at all"},
                {"order": "1", "consumes": "not a map", "feeds_from": "not a list"},
            ],
            [{"order": 0, "sql_template": "SELECT 1"}, "not an object"],
        ))
    ])
    card = _queue_card(page)
    assert card.locator('[data-testid="inbox-bp-node"]').count() >= 2
    # First occurrence wins for a duplicated order, so the graph has one node per number.
    assert sorted(
        _dag_nodes(card).nth(i).get_attribute("data-order")
        for i in range(_dag_nodes(card).count())
    ) == ["0", "1"]


def test_the_plan_is_not_repeated_as_a_chip_list(render: Any) -> None:
    """`composes` used to render a second time as bare chips under the sections — which for
    the object form it actually takes drew nothing, and for any other drew the plan twice. The
    plan section owns it now; the raw dump still has it verbatim."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)
    chips = card.locator(".card-chip")
    for i in range(chips.count()):
        text = (chips.nth(i).inner_text() or "").strip()
        assert not text.startswith("{"), text
        assert not text.startswith("[object"), text
    # ...and it did not fall through to the "other fields" JSON dump either.
    assert card.locator('[data-testid="inbox-other-fields"]').count() == 0
    assert "detail_a" in (card.locator('[data-testid="inbox-payload-view"]').text_content() or "")


def test_an_ordering_only_dependency_reads_as_runs_after(render: Any) -> None:
    """A `feeds_from` with no `consumes` behind it is an ORDERING constraint and nothing more.
    It is drawn as its own kind of edge and said in the step's own words — "runs after" rather
    than an input port, because no value crosses it."""
    steps = [
        _step(0, output={"staged": "table"}),
        _step(1, step_intent="runs only once the first one has", feeds_from=[0]),
    ]
    page = render([_item(payload_view=_composite(steps))])
    card = _queue_card(page)
    edge = card.locator('[data-testid="inbox-dag-edge"]')
    assert edge.count() == 1
    assert edge.get_attribute("data-flow") == "order"
    # Unlabelled: there is no value to name.
    assert (edge.text_content() or "").strip() == ""
    assert "runs after" in (card.locator('.card-node[data-order="1"]').inner_text() or "").lower()
    assert card.locator('.card-node[data-order="1"] [data-testid="inbox-node-input"]').count() == 0


# --- The plan under attack ---------------------------------------------------------
#
# `payload_view` is UNTRUSTED INPUT. It is model-authored on the fail-to-review path and
# hand-authored on the borrowed-authority path, and the DAG is the first thing on this page
# that does arithmetic on it — parses references, resolves producers, walks predecessors.
# Every one of those is a place a malformed plan can either throw (taking the whole card with
# it, including the verbatim dump that is the reviewer's last resort) or draw something that
# is not what the payload says.
#
# What follows drives the hostile and the half-present shapes a real payload has taken or
# could take — a `composes` that is not a list, an order that is not a number, a reference
# with no producer, a cycle, a step that consumes itself. Each pins the CORRECTED behaviour:
# the card survives, and the drawing claims no wiring the payload did not state.


def _dag_edges(card: Any) -> Any:
    return card.locator('[data-testid="inbox-dag-edge"]')


def _orders(locator: Any) -> list:
    return sorted(
        locator.nth(i).get_attribute("data-order") or "" for i in range(locator.count())
    )


def _labels(card: Any) -> list[str]:
    labels = card.locator(".item-field-label")
    return [(labels.nth(i).text_content() or "") for i in range(labels.count())]


def test_a_composes_that_is_not_a_list_leaves_the_card_standing(render: Any) -> None:
    """⚠ `composes` HAS ARRIVED AS AN OBJECT. The chip list this section replaced rendered
    nothing at all for that shape, which is how it went unnoticed — so the non-array forms are
    the ones most likely to be in the corpus right now.

    Each of these is a composite by its declared kind with no readable plan. The claim is
    narrow and total: no page error, the card still renders, no graph is drawn from a shape
    that has no steps in it, and the unreadable value is still SHOWN rather than swallowed by
    the section that could not use it."""
    for shape in ("not a list", {"0": {"order": 0}}, None, 7):
        card = _queue_card(render([_item(payload_view=_composite(shape))]))
        assert card.locator('[data-testid="inbox-dag"]').count() == 0, shape
        assert card.locator('[data-testid="inbox-bp-node"]').count() == 0, shape
        # The dump is the floor under every card and is what makes a renderer that declines
        # to draw acceptable in the first place.
        assert card.locator('[data-testid="inbox-payload-view"]').count() == 1, shape
    # `null` is genuinely empty and says nothing; the others are the payload saying something
    # this page cannot draw, and the reviewer has to see that it said it rather than have the
    # section that could not use it swallow the field.
    for shape, shown in (("not a list", "not a list"), (7, "7")):
        card = _queue_card(render([_item(payload_view=_composite(shape))]))
        assert shown in (card.inner_text() or ""), shape


def test_a_null_step_is_skipped_without_taking_its_neighbours_with_it(render: Any) -> None:
    """A `null` hole in the list — the shape a partial redaction leaves behind. The steps
    around it must still draw, and the hole must not become a node."""
    page = render([
        _item(payload_view=_composite([_step(0, output={"a": "table"}), None,
                                       _step(1, consumes={"a": "$0"})]))
    ])
    card = _queue_card(page)
    assert _orders(_dag_nodes(card)) == ["0", "1"]
    assert _dag_edges(card).count() == 1


def test_the_order_reading_decides_which_steps_are_the_same_step(render: Any) -> None:
    """The whole set, through the pure layout so the awkward numbers are expressible.

    A STRING "2" reads as 2 (the payload's own spelling, and the service has emitted it). A
    negative order is a number like any other. `NaN` and a non-numeric string read as ABSENT
    — the alternative, reading them as 0, silently merges every unnumbered step into one node.

    A FRACTIONAL order reads as absent too, and that is the sharp one: truncating `1.5` to 1
    makes it the JOIN KEY of step 1, which absorbs the 1.5 step into it — see
    `test_a_fractional_order_is_unplaced_rather_than_merged` for what that cost on a card.
    There is no reading of a plan under which step 1.5 is step 1."""
    page = render([_item(payload_view=_composite([_step(0)]))])
    layout = page.evaluate(
        """() => window.__inboxDag.layout([
            {order: "2", step_intent: "a string order"},
            {order: -1, step_intent: "before the beginning"},
            {order: NaN, step_intent: "not a number"},
            {order: "later", step_intent: "not a number either"},
            {order: 1e9, step_intent: "a very late step"},
            {order: 1.5, step_intent: "between two steps"},
            {order: null, step_intent: "no order at all"}
        ])"""
    )
    assert [n["order"] for n in layout["nodes"]] == [-1, 2, 1000000000]
    # Sorted ascending, so the graph reads left-to-right in execution order whatever the
    # document's own ordering was.
    assert [n["layer"] for n in layout["nodes"]] == [0, 0, 0]

    # A DUPLICATED order is a malformed payload and the first occurrence wins — the
    # alternative to picking one is throwing on the reviewer's card.
    dup = page.evaluate(
        """() => window.__inboxDag.layout([
            {order: 0, step_intent: "the real one"},
            {order: 0, step_intent: "the impostor"},
            {order: "0", step_intent: "the impostor, spelled differently"}
        ])"""
    )
    assert [n["order"] for n in dup["nodes"]] == [0]

    # ...and the hue is stable and in range for every one of them, including the negative and
    # the enormous: the hue is the only thing tying a node to its card, and a `NaN` var() name
    # would silently drop the join for exactly the steps hardest to read.
    hues = page.evaluate(
        "() => [-1, 0, 1.5, 7, 8, 1e9].map(n => window.__inboxDag.hue(n))"
    )
    assert hues == [
        "var(--dag-hue-7)", "var(--dag-hue-0)", "var(--dag-hue-1)",
        "var(--dag-hue-7)", "var(--dag-hue-0)", "var(--dag-hue-0)",
    ]


def test_a_fractional_order_is_unplaced_rather_than_merged(render: Any) -> None:
    """⚠ A STEP MUST NOT DISAPPEAR FROM THE PLAN. `order: 1.5` used to truncate to 1 — the JOIN
    KEY — so the 1.5 step was absorbed into step 1: only the fields step 1 had left undefined
    survived, and the 1.5 step's own intent and SQL went on the floor, leaving a two-step card
    for a three-step payload with nothing anywhere saying a step had been discarded.

    An order this page cannot place is ABSENT (as `NaN` and `"later"` already were), so the
    step renders as an UNPLACED card — its SQL readable, its number not claimed — and the
    header counts it separately from the steps the graph actually drew.

    Its two halves still join: the wiring and the SQL both written under `1.5` are one step,
    because they are joined on the payload's own spelling when the number is unusable."""
    page = render([
        _item(payload_view=_composite(
            [
                _step(0, step_intent="the first step"),
                _step(1, step_intent="the second step"),
                {"order": 1.5, "step_intent": "the step that vanishes",
                 "consumes": {"x": "$0"}},
            ],
            [
                {"order": 1, "sql_template": "SELECT 1"},
                {"order": 1.5, "sql_template": "SELECT 'the SQL that vanishes'"},
            ],
        ))
    ])
    card = _queue_card(page)
    text = card.inner_text()
    # The graph draws the two steps it can place...
    assert _orders(_dag_nodes(card)) == ["0", "1"]
    # ...and the third is on the card in full, once, as one step rather than as two halves.
    assert card.locator('[data-testid="inbox-bp-node"]').count() == 3
    assert "the step that vanishes" in text
    assert "the SQL that vanishes" in text
    unplaced = card.locator(".card-node.is-unplaced")
    assert unplaced.count() == 1
    assert (unplaced.locator(".card-node-step").text_content() or "") == "step ?"
    # It takes no number that belongs to another step, so the hover join cannot reach it.
    assert (unplaced.get_attribute("data-order") or "").startswith("u")
    # The header says what was drawn and what was not, rather than one number for both.
    assert any("plan — 2 steps (+1 unplaced), 0 edges" in label for label in _labels(card))


def test_an_unnumbered_step_takes_no_other_steps_number(render: Any) -> None:
    """⚠ TWO STEP CARDS MUST NOT CLAIM THE SAME NUMBER. A step with no `order` used to fall
    back to `index + 1` for its heading and its `data-order`, which COLLIDED with the real
    step 1: two blocks titled "step 1", one of them with no node in the graph above. The
    collision was not cosmetic — `linkPlan` finds a node's card by `data-order`, so hovering
    the one real node lit both cards and the graph pointed at a step it does not contain.

    An unplaced step is now labelled as unplaced and addressed by a key no node can hold."""
    page = render([
        _item(payload_view=_composite([
            {"step_intent": "the step with no number", "sql_template": "SELECT 'unplaced'"},
            _step(1, step_intent="the step that is actually step 1"),
        ]))
    ])
    card = _queue_card(page)
    # One node in the graph — the unplaced step is correctly not drawn...
    assert _orders(_dag_nodes(card)) == ["1"]
    # ...and exactly one card answers to its number.
    assert card.locator('.card-node[data-order="1"]').count() == 1
    headings = card.locator(".card-node .card-node-step")
    assert headings.all_text_contents() == ["step ?", "step 1"]
    # Its SQL is still readable — unplaced is a fact about the number, not a reason to hide
    # the query.
    assert "SELECT 'unplaced'" in (card.locator(".card-node.is-unplaced").inner_text() or "")
    # The header separates what was drawn from what could not be.
    assert any("plan — 1 step (+1 unplaced), 0 edges" in label for label in _labels(card))

    # And the highlight reaches the one step the node stands for.
    card.locator('[data-testid="inbox-dag-node"][data-order="1"]').hover()
    page.wait_for_selector('.card-node[data-order="1"].is-linked')
    assert card.locator(".card-node.is-linked").count() == 1


def test_the_reference_grammar_accepts_only_what_it_can_resolve(render: Any) -> None:
    """`$0` is the whole table output of step 0; `$0.company_avg` is one scalar column of it.
    Everything else is the payload saying something this page does not understand, and the
    only safe reading of that is NONE — a guessed reference draws an edge that is not in the
    plan, which is worse than drawing no edge.

    The near-misses are the point: `$1.` and `$1.2` and `$-1` are all one character away from
    valid, and a looser regex would accept them as step 1, step 1 and step 1."""
    page = render([_item(payload_view=_composite([_step(0)]))])
    parsed = page.evaluate(
        """() => ["$0", "$0.company_avg", " $2 ", "$99", "$", "$1.", "$1.2", "$-1",
                 "$1.col.x", "$01", "$1 ", "", "0", "step 0", 123, null, {}, ["$0"]]
                .map(r => window.__inboxDag.parseRef(r))"""
    )
    assert parsed[:4] == [
        {"order": 0, "column": None},
        {"order": 0, "column": "company_avg"},
        {"order": 2, "column": None},          # trimmed
        {"order": 99, "column": None},         # parseable; resolvable is a separate question
    ]
    assert parsed[4:10] == [None, None, None, None, None, {"order": 1, "column": None}]
    assert parsed[10:] == [{"order": 1, "column": None}, None, None, None, None, None,
                           None, None]


def test_an_unreadable_reference_is_shown_verbatim_and_draws_nothing(render: Any) -> None:
    """A reference this page cannot parse is rendered AS THE PAYLOAD WROTE IT and given no
    type — the reviewer is being told "the plan says this and I do not know what it means",
    which is a fact about the candidate. What it must not do is become an edge."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"a": "table"}),
            _step(1, consumes={
                "bare": "$", "trailing": "$1.", "numeric_col": "$1.2",
                "negative": "$-1", "nested": "$1.col.x", "a_number": 123,
            }),
        ]))
    ])
    card = _queue_card(page)
    assert _dag_edges(card).count() == 0

    pills = card.locator('.card-node[data-order="1"] [data-testid="inbox-node-input"]')
    assert pills.count() == 6
    texts = " | ".join(pills.all_inner_texts())
    for spelling in ("bare ← $", "trailing ← $1.", "numeric_col ← $1.2",
                     "negative ← $-1", "nested ← $1.col.x", "a_number ← 123"):
        assert spelling in texts, spelling
    # No type, because there is no producer to ask — a typed pill here would be an invention.
    assert [pills.nth(i).get_attribute("data-type") for i in range(6)] == [None] * 6


def test_a_reference_to_a_step_that_is_not_in_the_plan(render: Any) -> None:
    """⚠ THE PORT AND THE GRAPH MUST NOT DISAGREE. `$99` parses, and the port used to render
    it as a fully-resolved typed input ("x ← step 99", type table) in step 99's hue while the
    layout — correctly — refused to draw an edge to a node that does not exist. The card
    showed a step taking a table from a step the plan does not contain.

    A reference with no producer is UNRESOLVED: it says so in words, carries no type and no
    hue, and is not addressable as a dependency. Both halves resolve through the same map."""
    page = render([
        _item(payload_view=_composite([_step(0), _step(1, consumes={"x": "$99"})]))
    ])
    card = _queue_card(page)
    assert _dag_edges(card).count() == 0
    assert _orders(_dag_nodes(card)) == ["0", "1"]

    pill = card.locator('.card-node[data-order="1"] [data-testid="inbox-node-input"]')
    assert pill.count() == 1
    assert "x ← step 99 (not in plan)" in (pill.inner_text() or "")
    assert pill.get_attribute("data-from") is None
    assert pill.get_attribute("data-type") == "unresolved"
    assert "is-unresolved" in (pill.get_attribute("class") or "")


def test_a_step_that_says_it_runs_after_itself(render: Any) -> None:
    """⚠ A SELF-DEPENDENCY IS NOT A DEPENDENCY. The layout drops a self edge (it is not
    drawable and would make the step its own predecessor), but the card's "runs after" chips
    were built by a SECOND, looser walk of `feeds_from` — so step 1 read "runs after step 1",
    and an entry that is not a step number at all read "runs after step later".

    The chips are derived from the edges the graph drew, so the card cannot claim a dependency
    the drawing does not have."""
    page = render([
        _item(payload_view=_composite([
            _step(0),
            _step(1, feeds_from=[1, "later", 0]),
        ]))
    ])
    card = _queue_card(page)
    # The graph has the one real edge and nothing else.
    assert _dag_edges(card).count() == 1
    assert _dag_edges(card).get_attribute("data-from") == "0"

    chips = card.locator('.card-node[data-order="1"] .card-section .card-chip')
    assert sorted(chips.all_inner_texts()) == ["step 0"]


def test_a_feeds_from_that_is_not_a_list_and_wiring_maps_that_are_not_maps(
    render: Any,
) -> None:
    """The non-object forms of the three wiring fields, on one step. None of them can be read,
    and none of them may throw or leave a half-built card: the step still renders with its
    SQL, and simply carries no ports and no edges."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"a": "table"}),
            {"order": 1, "step_intent": "everything about this step is the wrong shape",
             "sql_template": "SELECT 1", "feeds_from": {"0": True},
             "consumes": ["$0"], "output": "table", "when": {"expr": "rows > 10"}},
        ]))
    ])
    card = _queue_card(page)
    step = card.locator('.card-node[data-order="1"]')
    assert step.count() == 1
    assert "SELECT 1" in (step.inner_text() or "")
    assert step.locator('[data-testid="inbox-node-inputs"]').count() == 0
    assert step.locator('[data-testid="inbox-node-outputs"]').count() == 0
    assert _dag_edges(card).count() == 0
    # ⚠ REVERSED, DELIBERATELY. This used to pin "a non-string `when` raises no mark on the
    # node", on the reasoning that a shape nobody can read is not a condition. But the CARD
    # renders that same object under a "when" heading — so the old rule bought a tidy node at
    # the price of an overview saying UNCONDITIONAL about a step the card called conditional,
    # and the overview is what a reviewer skims before deciding whether to read the card.
    # Both surfaces now ask the identical question (present and non-empty), and the answer
    # here is yes: it is unreadable, but it is there, and the reviewer is told so twice.
    node = card.locator('[data-testid="inbox-dag-node"][data-order="1"]')
    assert "when" in (node.text_content() or "")
    assert "when" in (step.inner_text() or "").lower()


def test_an_output_type_the_page_does_not_know_is_carried_not_guessed(render: Any) -> None:
    """`scalar` and `table` are the two types this page reasons about — `table` is the hop a
    trial run refuses. A third word is the payload's, and it is shown as given rather than
    coerced into one of the two.

    A non-scalar output VALUE (an object) is shown as its JSON, collapsed to one line — it
    used to read `[object Object]`, which told the reviewer nothing about a payload that told
    them something."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"framed": "frame", "structured": {"kind": "table"}}),
            _step(1, consumes={"framed": "$0"}),
        ]))
    ])
    card = _queue_card(page)
    outs = card.locator('.card-node[data-order="0"] [data-testid="inbox-node-output"]')
    assert outs.count() == 2
    assert [outs.nth(i).get_attribute("data-type") for i in range(2)] == [
        "frame", '{ "kind": "table" }',
    ]
    # Nothing declared `table`, so the node carries no table mark...
    node = card.locator('[data-testid="inbox-dag-node"][data-order="0"]')
    assert "table" not in (node.text_content() or "")
    # ...and an unknown type falls back to `table` on the edge, which is the CAUTIOUS reading:
    # a hop assumed materializable that is not would send the reviewer to a trial run that
    # comes back unsupported with no warning on the card.
    assert _dag_edges(card).get_attribute("data-flow") == "table"


def test_a_node_kind_the_page_does_not_know_is_still_drawn(render: Any) -> None:
    """⚠ THE GRAPH IS THE VIEW A REVIEWER SKIMS. Only `approval` changes the SHAPE — that is
    the one gate this page knows how to draw — but a `guard`, or whatever the next gate is
    called, is carried into the drawing verbatim: as a mark on the node and in its accessible
    name. Drawing it as a plain query said "there is no gate here" about a step that is one.

    The shape falling back to the ordinary one is deliberate: inventing a gate shape for an
    unknown word would claim more than the payload said."""
    page = render([_item(payload_view=_composite([_step(0, node_kind="guard")]))])
    card = _queue_card(page)
    node = card.locator('[data-testid="inbox-dag-node"][data-order="0"]')
    assert node.get_attribute("data-kind") == "guard"
    assert "guard" in (node.text_content() or "")
    assert node.get_attribute("aria-label") == "step 0, guard: step 0 does something"
    # ...and the word is still on the card below, as it was.
    assert "guard" in card.locator('.card-node[data-order="0"] .card-chip').all_inner_texts()


def test_a_step_intent_carrying_markup_arrives_as_text(render: Any) -> None:
    """⚠ CONTRACT §4 AT THE ONE PLACE THIS PAGE DRAWS. SVG has its own sinks and its own
    parsing rules, and `step_intent` reaches four of them at once: the node's visible label,
    its `<title>`, its `aria-label`, and the step card's summary. All four go in through
    `createTextNode`/`setAttribute`, so a model-authored sentence with a tag in it is a
    sentence with a tag in it.

    The `&` and the `"` are in the fixture on purpose: an attribute assembled by string
    concatenation breaks on the quote, and an entity-decoding sink turns `&lt;` back into
    `<`."""
    hostile = '<script>window.__pwned = 1</script> & "quoted" <img src=x onerror=alert(1)>'
    page = render([_item(payload_view=_composite([_step(0, step_intent=hostile)]))])
    card = _queue_card(page)

    assert page.evaluate("() => window.__pwned") is None
    assert card.locator("script").count() == 0
    assert card.locator("img").count() == 0

    node = card.locator('[data-testid="inbox-dag-node"][data-order="0"]')
    # The full sentence survives on the pointer and in the accessible name, unmangled.
    assert hostile in (node.text_content() or "")
    assert node.get_attribute("aria-label") == "step 0: " + hostile
    # ...and the drawn label is the same characters, truncated rather than escaped away.
    label = card.locator(".dag-node-intent")
    assert (label.text_content() or "").startswith("<script>window.__pwned = 1")


def test_a_very_long_step_intent_is_truncated_in_the_drawing_and_kept_everywhere_else(
    render: Any,
) -> None:
    """A node box is 148px wide. An intent that overflows it would print across the node
    beside it, so the drawn label is cut — and a truncation with no way back to the original
    is a worse summary than none, which is why the full sentence stays on the title, the
    accessible name and the step card."""
    long_intent = "reconcile the quarterly payroll ledger against the general ledger " * 8
    assert len(long_intent) > 500
    page = render([_item(payload_view=_composite([_step(0, step_intent=long_intent)]))])
    card = _queue_card(page)

    drawn = card.locator(".dag-node-intent").text_content() or ""
    assert len(drawn) <= 28
    assert drawn.endswith("…")
    node = card.locator('[data-testid="inbox-dag-node"][data-order="0"]')
    assert long_intent in (node.get_attribute("aria-label") or "")
    assert long_intent.strip() in (card.locator('.card-node[data-order="0"]').inner_text() or "")


def test_a_step_with_no_intent_is_still_named(render: Any) -> None:
    """The node has to be announceable with nothing but its number — an unnamed node whose
    accessible name is the empty string is a graph a screen reader cannot walk."""
    page = render([_item(payload_view=_composite([{"order": 0, "sql_template": "SELECT 1"}]))])
    node = _queue_card(page).locator('[data-testid="inbox-dag-node"][data-order="0"]')
    assert node.get_attribute("aria-label") == "step 0"
    assert (node.text_content() or "").strip() == "step 0step 0"  # <title> + the drawn label


def test_a_cycle_lays_out_instead_of_recursing_forever(render: Any) -> None:
    """⚠ THE ONE SHAPE THAT CAN HANG THE TAB. Layer is the longest path from a source, which is
    a recursive walk of predecessors — and a payload that says step 0 consumes step 1's output
    while step 1 consumes step 0's makes that walk unbounded unless the visit stack is
    carried. Forward edges are refused upstream, but "the server would not send it" is not a
    property a browser can rely on when the alternative is a frozen review queue.

    Bounded by the render fixture's own timeout: if the walk did not terminate this test would
    fail on the missing card rather than run forever."""
    two = _queue_card(render([_item(payload_view=_composite([
        _step(0, consumes={"back": "$1"}),
        _step(1, consumes={"fwd": "$0"}),
    ]))]))
    assert _orders(_dag_nodes(two)) == ["0", "1"]
    assert _dag_edges(two).count() == 2

    page = render([_item(payload_view=_composite([
        _step(0, consumes={"c": "$2"}),
        _step(1, consumes={"a": "$0"}),
        _step(2, consumes={"b": "$1"}),
    ]))])
    three = _queue_card(page)
    assert _orders(_dag_nodes(three)) == ["0", "1", "2"]
    assert _dag_edges(three).count() == 3

    # Every node lands somewhere finite: a cycle is broken by treating the back edge's target
    # as a source, so the layout is arbitrary but drawable.
    layers = page.evaluate(
        """() => window.__inboxDag.layout([
            {order: 0, consumes: {back: "$1"}},
            {order: 1, consumes: {fwd: "$0"}}
        ]).nodes.map(n => n.layer)"""
    )
    assert all(isinstance(n, int) and 0 <= n < 100 for n in layers), layers


def _chain(n: int) -> list[dict[str, Any]]:
    steps = [_step(0, step_intent="step 0", output={"out0": "table"})]
    steps += [
        _step(i, step_intent=f"step {i}", consumes={f"out{i - 1}": f"${i - 1}"},
              output={f"out{i}": "table"})
        for i in range(1, n)
    ]
    return steps


def _viewport_metrics(page: Any) -> dict:
    return page.evaluate(
        """() => {
            const svg = document.querySelector('[data-testid="inbox-dag"]');
            const wrap = svg.parentElement;
            return {
                wrapScroll: wrap.scrollWidth,
                wrapClient: wrap.clientWidth,
                docScroll: document.documentElement.scrollWidth,
                docClient: document.documentElement.clientWidth,
            };
        }"""
    )


def test_a_forty_step_chain_scrolls_in_its_own_box(render: Any) -> None:
    """⚠ A WIDE PLAN MUST NOT WIDEN THE PAGE. The card is 720px and a forty-layer plan is
    nine thousand — a graph allowed to size the document would push Approve and Reject off the
    right edge of every card in the queue, including the ones that have no plan at all. The
    graph scrolls inside its own box, and the page does not scroll sideways.

    Forty is not a hypothetical ceiling picked for the test: it is the point at which the SVG
    is an order of magnitude wider than its container, which is where a missing `overflow`
    stops being subtle."""
    page = render([_item(payload_view=_composite(_chain(40)))])
    card = _queue_card(page)
    assert _dag_nodes(card).count() == 40
    assert _dag_edges(card).count() == 39

    metrics = _viewport_metrics(page)
    assert metrics["wrapScroll"] > metrics["wrapClient"], metrics
    assert metrics["docScroll"] <= metrics["docClient"], metrics


def test_forty_steps_feeding_one_sink_draw_every_one_of_them(render: Any) -> None:
    """The other extreme of the same plan size: forty sources and one consumer. All forty are
    at layer 0 and the sink is at layer 1 — the longest-path rule puts a fan-in AFTER all of
    its inputs, which is the whole reason it is not shortest-path.

    Every input is named on the sink's card too: forty ports is a lot to read, and a graph that
    silently dropped the fortieth would be a plan the reviewer approved without seeing."""
    sources = [_step(i, output={f"out{i}": "table"}) for i in range(40)]
    sink = _step(40, step_intent="join all forty",
                 consumes={f"out{i}": f"${i}" for i in range(40)})
    page = render([_item(payload_view=_composite([*sources, sink]))])
    card = _queue_card(page)

    assert _dag_nodes(card).count() == 41
    assert _dag_edges(card).count() == 40
    assert card.locator(
        '[data-testid="inbox-dag-node"][data-order="40"]'
    ).get_attribute("data-layer") == "1"
    assert card.locator(
        '.card-node[data-order="40"] [data-testid="inbox-node-input"]'
    ).count() == 40

    metrics = _viewport_metrics(page)
    assert metrics["docScroll"] <= metrics["docClient"], metrics


def test_two_plans_on_one_page_keep_their_defs_and_their_highlights_apart(
    render: Any,
) -> None:
    """⚠ THE PAGER HIDES CARDS, IT DOES NOT UNMOUNT THEM, so several plans are in the DOM at
    once. Two things are shared across them and must not be: the document's `id` space (the
    arrowhead markers are referenced by `url(#…)`, and a duplicate id makes the second graph
    point at the first one's colours), and the highlight, which is found by `data-order` —
    a number every plan uses."""
    page = render([
        _item(candidate_id="candidate::plan::a",
              payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL)),
        _item(candidate_id="candidate::plan::b",
              payload_view=_composite(_SCALAR_STEPS)),
    ])
    # ⚠ ATTACHED, NOT VISIBLE: the pager shows one card at a time and hides the rest, which
    # is precisely why the id space is shared and why this test exists at all.
    cards = page.locator('[data-testid="inbox-item"]')
    assert cards.count() == 2
    assert cards.nth(0).locator('[data-testid="inbox-dag"]').count() == 1
    assert cards.nth(1).locator('[data-testid="inbox-dag"]').count() == 1

    ids = page.evaluate(
        "() => Array.prototype.map.call(document.querySelectorAll('[id]'), n => n.id)"
    )
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)
    # The markers really are per-graph rather than one shared set.
    assert len([i for i in ids if "-arrow-" in i]) >= 2

    cards.nth(0).locator('[data-testid="inbox-dag-node"][data-order="1"]').hover()
    page.wait_for_selector('.card-node[data-order="1"].is-linked')
    assert cards.nth(0).locator(".card-node.is-linked").count() == 1
    assert cards.nth(1).locator(".card-node.is-linked").count() == 0
    assert cards.nth(1).locator('[data-testid="inbox-dag-edge"].is-linked').count() == 0


def test_the_highlight_is_cleared_when_the_pointer_and_the_focus_leave(render: Any) -> None:
    """A highlight is a READING AID and reads as one only while it means "this one". Left
    behind after the pointer moves on, it becomes a selection the reviewer did not make and
    cannot clear — and with several nodes hovered in turn, a plan where everything is lit."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)

    card.locator('[data-testid="inbox-dag-node"][data-order="2"]').hover()
    page.wait_for_selector('.card-node[data-order="2"].is-linked')
    # Off the graph entirely.
    page.mouse.move(0, 0)
    page.wait_for_selector(".card-node.is-linked", state="detached")
    assert card.locator(".card-node.is-linked").count() == 0
    assert card.locator('[data-testid="inbox-dag-edge"].is-linked').count() == 0

    # The same claim through the keyboard: focus lights, blur clears.
    node = card.locator('[data-testid="inbox-dag-node"][data-order="0"]')
    node.focus()
    page.wait_for_selector('.card-node[data-order="0"].is-linked')
    node.evaluate("n => n.blur()")
    page.wait_for_selector(".card-node.is-linked", state="detached")
    assert card.locator(".card-node.is-linked").count() == 0


def test_the_keyboard_reaches_the_step_a_node_stands_for(render: Any) -> None:
    """The node is focusable, so it has to be OPERABLE — a click-only jump on a `tabindex=0`
    element is a control a keyboard user can reach, land on, and not use. Enter and Space both
    scroll the step card into view, and Space is prevented from also scrolling the page, which
    would move the card the reviewer was just sent to."""
    page = render([_item(payload_view=_composite(_chain(20)))])
    card = _queue_card(page)
    page.evaluate(
        """() => {
            window.__scrolls = [];
            const original = Element.prototype.scrollIntoView;
            Element.prototype.scrollIntoView = function (options) {
                window.__scrolls.push(this.getAttribute("data-order"));
                return original.call(this, options);
            };
        }"""
    )
    node = card.locator('[data-testid="inbox-dag-node"][data-order="15"]')
    node.focus()
    node.press("Enter")
    node.press(" ")
    assert page.evaluate("() => window.__scrolls") == ["15", "15"]

    # ...and the card really is on screen afterwards, not merely asked to be.
    box = card.locator('.card-node[data-order="15"]').bounding_box()
    height = page.evaluate("() => window.innerHeight")
    assert box is not None
    assert -1 <= box["y"] <= height, (box, height)


def test_an_edge_says_what_kind_of_value_crosses_it(render: Any) -> None:
    """⚠ THE TABLE HOP IS THE ONE THAT FAILS. `table_intermediate_unsupported` is what a trial
    run comes back with, and the edge is where a reviewer can see which hop it will be before
    running anything. So the typing rule has to hold in all three of its cases:

      * a producer that declares a single `scalar` output types the whole-table reference to
        it as a scalar — there is only one value it could mean;
      * a producer that declares nothing is a TABLE, because assuming otherwise hides exactly
        the hop that will fail;
      * a column reference is a scalar whatever the producer declared, because a column is."""
    plans = {
        "scalar": [_step(0, output={"company_avg": "scalar"}),
                   _step(1, consumes={"threshold": "$0"})],
        "table": [_step(0), _step(1, consumes={"whatever": "$0"})],
        "column": [_step(0, output={"detail": "table"}),
                   _step(1, consumes={"one_value": "$0.detail"})],
    }
    expected = {"scalar": "scalar", "table": "table", "column": "scalar"}
    for name, steps in plans.items():
        card = _queue_card(render([_item(payload_view=_composite(steps))]))
        assert _dag_edges(card).get_attribute("data-flow") == expected[name], name
        # The port beside the SQL agrees with the edge above it — two renderings of one fact.
        assert card.locator(
            '.card-node[data-order="1"] [data-testid="inbox-node-input"]'
        ).get_attribute("data-type") == expected[name], name


def test_two_values_from_one_producer_are_two_edges(render: Any) -> None:
    """Deduplication is by (producer, consumer, NAME): two different values taken from the
    same step are two dependencies and are labelled separately, because which one is the table
    is the question. Collapsing them to one edge would hide a table hop behind a scalar one."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"avg": "scalar", "detail": "table"}),
            _step(1, consumes={"avg": "$0.avg", "detail": "$0"}),
        ]))
    ])
    card = _queue_card(page)
    edges = _dag_edges(card)
    assert edges.count() == 2
    assert sorted(edges.nth(i).get_attribute("data-flow") for i in range(2)) == [
        "scalar", "table",
    ]
    # The drawn label, not the whole group: each edge also carries a `<title>` with the full
    # name for the pointer, so `text_content` on the group would read it twice.
    labels = card.locator(".dag-edge-label-text")
    assert sorted(labels.all_text_contents()) == ["avg", "detail"]


# --- the drawing, measured -------------------------------------------------------
#
# Everything below is about GEOMETRY rather than about which elements exist, and it is here
# because the elements all existed while the picture was still wrong: two arrows painted on one
# line, a label behind the node it pointed past, a cycle drawn as a straight line through the
# row. A DOM assertion cannot see any of those. These read positions out of the rendered SVG.


def _edge_paths(card: Any) -> Any:
    return card.locator('[data-testid="inbox-dag-edge"] path')


def test_two_edges_between_one_pair_are_two_separate_arcs(render: Any) -> None:
    """⚠ THE REPRO: TWO DEPENDENCIES THAT READ AS ONE ARROW. Both edges between step 0 and
    step 1 came off the same four control points, so their `d` attributes were literally
    identical — the 3px table stroke painted over the 1.75px scalar one and the reviewer saw a
    single line with two names hanging off it. Spreading the LABELS apart made that worse, not
    better: two names, one arrow, no way to tell which stroke was the table hop.

    So the CURVES are spread. Pinned as a measurement rather than as a `d` string: what has to
    hold is that a reviewer can see two arrows, which is a distance, not a path expression."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"avg": "scalar", "detail": "table"}),
            _step(1, consumes={"avg": "$0.avg", "detail": "$0"}),
        ]))
    ])
    card = _queue_card(page)
    paths = _edge_paths(card)
    assert paths.count() == 2

    drawn = [paths.nth(i).get_attribute("d") for i in range(2)]
    assert drawn[0] != drawn[1], drawn

    # Measured ON the curves, so this is about ink and not about arithmetic.
    mids = paths.evaluate_all(
        """ps => ps.map(p => {
            const at = p.getPointAtLength(p.getTotalLength() / 2);
            return [at.x, at.y];
        })"""
    )
    apart = abs(mids[0][1] - mids[1][1])
    assert apart > 12, f"the two arcs are still on top of each other: {mids}"

    # ...and each one arrives at its own point on the target's left edge, so the arrowheads
    # are two heads rather than one drawn twice.
    ends = paths.evaluate_all(
        """ps => ps.map(p => {
            const at = p.getPointAtLength(p.getTotalLength());
            return [at.x, at.y];
        })"""
    )
    assert abs(ends[0][1] - ends[1][1]) > 4, ends


def test_the_condition_box_hugs_its_expression(render: Any) -> None:
    """`display: inline-block` did NOT survive the cascade. The box is a direct child of a
    `flex-direction: column` section, which blockifies its items and then stretches them, so a
    twelve-character expression rendered as a 671px strip that read like a code block rather
    than like the one-line predicate it is."""
    page = render([_item(payload_view=_composite(_SCALAR_STEPS))])
    card = _queue_card(page)
    measured = card.locator(".card-when").evaluate(
        """e => [
            e.getBoundingClientRect().width,
            e.closest('.card-node').getBoundingClientRect().width,
        ]"""
    )
    when_width, card_width = measured
    assert when_width < card_width / 2, measured


def test_the_node_tag_and_the_card_agree_about_what_is_conditional(render: Any) -> None:
    """⚠ ONE PREDICATE, TWO RENDERINGS — the same rule the "runs after" chips already keep.

    The node tag fired only for a STRING `when`; the card rendered any non-empty one. So a
    `when` that is an object, or the number 0, produced a card section saying "this step is
    conditional" under a node saying nothing — and the node is what a reviewer skims before
    deciding whether to read the card at all. An overview that says "unconditional" about a
    conditional step is worse than one that says nothing.

    Both shapes are exercised because a string is the only one the old predicate matched."""
    page = render([_item(payload_view=_composite([
        _step(1, when="row_estimate > 500"),
        _step(2, when={"expr": "row_estimate > 500"}),
        _step(3, when=0),
    ]))])
    card = _queue_card(page)

    for order in ("1", "2", "3"):
        node = card.locator(f'[data-testid="inbox-dag-node"][data-order="{order}"]')
        block = card.locator(f'.card-node[data-order="{order}"]')
        assert "when" in (node.text_content() or ""), f"step {order}: no tag on the node"
        assert "when" in (block.inner_text() or "").lower(), f"step {order}: no section on the card"

    # The other direction of the same rule: a step with no condition is marked on neither.
    plain = _queue_card(render([_item(payload_view=_composite([_step(0)]))]))
    assert "when" not in (
        plain.locator('[data-testid="inbox-dag-node"][data-order="0"]').text_content() or ""
    )
    assert "when" not in (plain.locator('.card-node[data-order="0"]').inner_text() or "").lower()


def test_an_unplaced_steps_output_pills_claim_no_step_colour(render: Any) -> None:
    """⚠ THE HUE IS A JOIN, AND AN UNPLACED STEP IS JOINED TO NOTHING. `order: 1.5` is a number
    this page refuses to place — no node, no card hue, and the card border says so with a
    dashed warn edge. Its output PILLS were still painted, because `dagHue` maps anything that
    is not a number onto hue 0: the pills of a step with no place in the plan wore step 0's
    colour, which is the one thing the colour is not allowed to mean.

    Resolved through the cascade rather than compared as source text — an unset custom property
    and one set to the wrong value can only be told apart by what they paint."""
    page = render([_item(payload_view=_composite([
        _step(0, output={"a": "table"}),
        _step(1.5, output={"b": "table"}),
    ]))])
    card = _queue_card(page)

    # The neutral is read off the pill's OWN other borders — the shorthand paints those from
    # `--color-border`, so "fell back" and "was set to something that happens to look the same"
    # are distinguishable without hard-coding a hex value the stylesheet is free to change.
    painted = card.locator('[data-testid="inbox-node-output"]').evaluate_all(
        """ps => ps.map(p => {
            const css = getComputedStyle(p);
            return {
                name: p.dataset.name,
                left: css.borderLeftColor,
                neutral: css.borderTopColor,
                inline: p.style.getPropertyValue('--dag-port-hue'),
            };
        })"""
    )
    by_name = {row["name"]: row for row in painted}
    assert set(by_name) == {"a", "b"}, painted
    # Step 0 is placed, so its pill carries step 0's hue and stands out from its own border...
    assert by_name["a"]["left"] != by_name["a"]["neutral"], painted
    # ...and the unplaced step's pill claims nothing: no property set, and the neutral painted.
    assert by_name["b"]["inline"] == "", painted
    assert by_name["b"]["left"] == by_name["b"]["neutral"], painted
    assert by_name["b"]["left"] != by_name["a"]["left"], painted


def test_a_wide_glyph_intent_is_clipped_to_the_node_box(render: Any) -> None:
    """⚠ AN ESTIMATE IS NOT A CLIP. The compaction gate is `chars * 5.6 > 152`, tuned on
    lowercase — so a 27-character ALL-CAPS intent measured 168px, slipped under the threshold
    at 151.2, and printed out over the edge labels to its right inside a 176px box.

    The fix is structural: the label lives in a nested `<svg>`, which is its own viewport and
    paints nothing outside it. So the claim is about INK, tested by hit-testing across the row:
    the glyphs are genuinely wider than the box (`getBBox`, which clipping does not change) and
    not one of them is painted past its right edge.

    ⚠ `getBoundingClientRect` IS NOT THE MEASUREMENT HERE, and that is not a style preference:
    Chrome reports the text's UNCLIPPED bounds for both the `<text>` and its clipping `<svg>`
    parent, so an assertion on either would have passed against the broken renderer too."""
    intent = "GROSS PAY WITHHOLDING TOTAL"
    assert len(intent) == 27 and len(intent) * 5.6 < 152, "the estimate must NOT catch this one"
    page = render([_item(payload_view=_composite([
        _step(0, step_intent=intent),
        _step(1, consumes={"z": "$0"}),
    ]))])
    card = _queue_card(page)

    measured = card.locator('[data-testid="inbox-dag-node"][data-order="0"]').evaluate(
        """n => {
            const text = n.querySelector('.dag-node-intent');
            const box = n.querySelector('.dag-node-box').getBoundingClientRect();
            const y = box.y + box.height / 2;
            let rightmost = null;
            for (let x = box.x; x < box.right + 80; x += 2) {
                const hit = document.elementFromPoint(x, y);
                if (hit === text) rightmost = x;
            }
            return {
                glyphs: text.getBBox().width,
                right: box.right,
                rightmostInk: rightmost,
                truncated: text.textContent,
            };
        }"""
    )
    # The string is NOT shortened — it fits the 28-character budget the truncation uses.
    assert measured["truncated"] == intent
    # ...and it genuinely overflows, so there is something for the clip to do.
    assert measured["glyphs"] > 152, measured
    # ...and none of it is painted outside the node.
    assert measured["rightmostInk"] is not None, measured
    assert measured["rightmostInk"] <= measured["right"], measured


def test_a_skip_edges_label_is_not_hidden_behind_the_node_it_passes(render: Any) -> None:
    """⚠ AN EDGE THAT CONTRIBUTED AN ARROWHEAD AND NOTHING ELSE. With `0→1→2` and `0→2`, the
    skip edge is twice as long as the direct one, so hanging its label at 40% of the curve put
    the plate INSIDE node 1 — which paints over the edge layer. `elementFromPoint` at the
    label's centre returned `dag-node-box`: the value crossing that edge was unreadable, on the
    one edge whose meaning is least obvious from the layout.

    It is bisected back into the FIRST inter-layer gap instead, where the same spread that
    separates parallel edges keeps it off the direct edge's label rather than swapping one
    collision for another."""
    page = render([_item(payload_view=_composite([
        _step(0, output={"a": "table"}),
        _step(1, consumes={"a": "$0"}, output={"b": "table"}),
        _step(2, consumes={"b": "$1", "a": "$0"}),
    ]))])
    card = _queue_card(page)

    skip = card.locator('[data-testid="inbox-dag-edge"].is-skip')
    assert skip.count() == 1, "the 0→2 edge crosses a layer and is marked as doing so"
    assert skip.get_attribute("data-from") == "0" and skip.get_attribute("data-to") == "2"

    seen = card.locator('[data-testid="inbox-dag-edge"] .dag-edge-label-text').evaluate_all(
        """ts => ts.map(t => {
            const r = t.getBoundingClientRect();
            const x = r.x + r.width / 2;
            const y = r.y + r.height / 2;
            const hit = document.elementFromPoint(x, y);
            return {
                skip: t.closest('[data-testid="inbox-dag-edge"]').classList.contains('is-skip'),
                centre: [x, y],
                hit: hit ? (hit.getAttribute('class') || hit.tagName) : null,
            };
        })"""
    )
    assert len(seen) == 3, seen
    for label in seen:
        assert "dag-node-box" not in (label["hit"] or ""), label
    # ...and it does not simply land on the direct edge's label either.
    gap = [label["centre"] for label in seen if abs(label["centre"][0] - seen[0]["centre"][0]) < 6]
    assert len(gap) == 2, f"the skip label should share the first gap with the direct one: {seen}"
    assert abs(gap[0][1] - gap[1][1]) > 8, gap


def test_a_back_edge_dips_below_the_row_instead_of_crossing_it(render: Any) -> None:
    """⚠ A CYCLE IS A DAMAGED DOCUMENT AND HAS TO LOOK LIKE ONE. The layout already refuses to
    recurse on one; the DRAWING did not. A 3-cycle's closing edge runs right-to-left, so its
    control points landed off the left of the canvas and it painted as a STRAIGHT HORIZONTAL
    LINE straight through the row of nodes — visually identical to a forward edge apart from an
    arrowhead arriving out of the left margin.

    Dropped below the row, warn-coloured and dashed. Cheap on purpose: this shape is refused
    upstream, so it only has to be legible, not pretty."""
    page = render([_item(payload_view=_composite([
        _step(0, consumes={"c": "$2"}),
        _step(1, consumes={"a": "$0"}),
        _step(2, consumes={"b": "$1"}),
    ]))])
    card = _queue_card(page)

    back = card.locator('[data-testid="inbox-dag-edge"].is-back')
    assert back.count() == 1, "one closing edge, marked"
    assert back.get_attribute("data-from") == "2" and back.get_attribute("data-to") == "0"

    # ⚠ USER UNITS ON BOTH SIDES. `getPointAtLength` answers in the SVG's own coordinates and
    # `getBoundingClientRect` in CSS pixels; the graph is scaled to its box, so mixing the two
    # compares a point against a number from a different space. The row's foot is read off the
    # node rectangle's own attributes.
    drawn = card.locator('[data-testid="inbox-dag-edge"]').evaluate_all(
        """gs => gs.map(g => {
            const p = g.querySelector('path');
            const mid = p.getPointAtLength(p.getTotalLength() / 2);
            const row = g.closest('svg').querySelector('.dag-node-box');
            return {
                back: g.classList.contains('is-back'),
                midY: mid.y,
                rowBottom: Number(row.getAttribute('y')) + Number(row.getAttribute('height')),
                dash: p.getAttribute('stroke-dasharray'),
                stroke: p.style.stroke,
            };
        })"""
    )
    loop = [e for e in drawn if e["back"]]
    assert len(loop) == 1, drawn
    assert loop[0]["midY"] > loop[0]["rowBottom"], loop
    assert loop[0]["dash"], "a cycle-closing edge is dashed"
    assert "warn" in loop[0]["stroke"], loop[0]["stroke"]
    # The forward edges are untouched — they still run through the row.
    for forward in [e for e in drawn if not e["back"]]:
        assert forward["midY"] < forward["rowBottom"], forward


def test_the_plain_text_reading_never_invents_a_step_number(render: Any) -> None:
    """⚠ THE SAME DEFECT THE STEP CARDS WERE FIXED FOR, one accessor further down. The revise
    modal shows the current query as PLAIN TEXT, and for a composite that is the node templates
    with a `-- step N` marker between them. `N` fell back to `index + 1` for an unnumbered
    step, which is how a step this page cannot place ended up labelled `-- step 2` in a document
    that already has a step 2 — two blocks under one marker, in the one pane whose whole job is
    to say which query is about to be replaced.

    A step with no readable order says so."""
    _SERVED["revise"] = _proposal()
    page = render([_item(payload_view=_composite(
        [_step(0), _step(2)],
        [
            {"order": 0, "sql_template": "SELECT 0"},
            {"sql_template": "SELECT nothing_says_which_step_i_am"},
            {"order": 2, "sql_template": "SELECT 2"},
        ],
    ))])
    card = _queue_card(page)
    card.locator('[data-testid="inbox-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-apply"]:not([hidden])')
    card.locator('[data-testid="inbox-revise-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-revise-modal"]')

    shown = page.locator('[data-testid="inbox-revise-modal"] .modal-sql').text_content() or ""
    assert "-- step ?" in shown, shown
    assert shown.count("-- step 2") == 1, shown
    assert "SELECT nothing_says_which_step_i_am" in shown


def test_a_single_blueprint_is_left_exactly_as_it_was(render: Any) -> None:
    """⚠ THE REGRESSION THIS CHANGE COULD CAUSE. Most candidates in the queue are single
    blueprints, and the plan section reaches into `renderTemplate` — the one every card uses.
    A single blueprint must come out of it with no graph, no ports, no step cards and no
    per-step chrome: one `<pre>`, slot chips inside it, and the section still called
    "template" rather than a plan of one step."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    card = _queue_card(page)

    assert card.locator('[data-testid="inbox-dag"]').count() == 0
    assert card.locator('[data-testid="inbox-dag-node"]').count() == 0
    assert card.locator('[data-testid="inbox-node-inputs"]').count() == 0
    assert card.locator('[data-testid="inbox-node-outputs"]').count() == 0
    assert card.locator('[data-testid="inbox-bp-node"]').count() == 0
    assert card.locator(".card-node[data-order]").count() == 0

    template = card.locator('[data-testid="inbox-bp-template"]')
    assert template.count() == 1
    assert template.locator(".slot").inner_text() == "department"
    assert "template" in _labels(card)
    assert not [label for label in _labels(card) if label.startswith("plan —")]


def test_a_composite_with_neither_half_falls_back_without_claiming_a_plan(
    render: Any,
) -> None:
    """A payload that says `kind: "composite"` and carries no wiring and no node templates. It
    is still a composite and there is still nothing to draw, so: no graph, no step cards, no
    "plan — 0 steps" (a header that counts nothing is worse than the plain word), and the
    card's other sections unaffected.

    The stray `sql_template` case below is what `isCompositeItem` exists for: a composite's
    generalization can still carry one query, and labelling THAT "template" tells the reviewer
    they are looking at the whole blueprint when they are looking at one step of it. It is
    labelled as a plan with no readable steps instead — which is what the payload says."""
    bare = _queue_card(render([_item(payload_view=_composite([], []))]))
    assert bare.locator('[data-testid="inbox-dag"]').count() == 0
    assert bare.locator('[data-testid="inbox-bp-node"]').count() == 0
    assert not [label for label in _labels(bare) if label.startswith("plan")]
    # The card is still a card: the dump and the verbs are there.
    assert bare.locator('[data-testid="inbox-payload-view"]').count() == 1
    assert bare.locator('[data-testid="inbox-approve"]').count() == 1
    # ...and no trial-run block, because there is genuinely nothing to run.
    assert bare.locator('[data-testid="inbox-trial"]').count() == 0

    # The stray single query on a declared composite, labelled as if it were the whole thing.
    with_sql = _queue_card(render([_item(payload_view=_composite([], [], generalization={
        "uses": [], "uses_rules": [], "sql_template": "SELECT 1 -- one of many"}))]))
    assert "SELECT 1 -- one of many" in (
        with_sql.locator('[data-testid="inbox-bp-template"]').text_content() or ""
    )
    assert "plan — no steps" in _labels(with_sql)
    assert "template" not in _labels(with_sql)


def test_a_composite_still_warns_before_the_trial_run(render: Any) -> None:
    """The graph now says which hops are tables; the trial block still has to say what that
    MEANS for the run the reviewer is about to start. Gated on there being steps at all, so
    this is the assertion that the plan rework did not take the note with it."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)
    note = card.locator('[data-testid="inbox-trial-composite-note"]')
    assert note.count() == 1
    assert "TABLE" in (note.inner_text() or "")
    # And a single blueprint is not told any of it.
    single = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    assert _queue_card(single).locator(
        '[data-testid="inbox-trial-composite-note"]'
    ).count() == 0


# --- What the review found, fixed ---------------------------------------------------
#
# The cases below were found by review and QA on the first cut of this card. They are here as
# their own block because each one is a claim about a DIFFERENT failure than the ones above:
# not "does the plan draw" but "does the drawing say the same thing twice, and only when the
# payload does".


def test_the_hue_is_one_join_across_the_graph_the_card_and_the_port(render: Any) -> None:
    """⚠ THE COLOUR IS THE JOIN. It is the only mark tying a node above to the SQL below and to
    the pill that consumes it — a reviewer follows it instead of re-reading `$0`. So the three
    places have to be the SAME hue for the same step, from one function, or the colour is
    worse than no colour: it would group things that are not related."""
    page = render([_item(payload_view=_composite(_FAN_IN_STEPS, _FAN_IN_SQL))])
    card = _queue_card(page)

    def hue_in(locator: Any) -> str:
        style = locator.get_attribute("style") or ""
        found = re.findall(r"var\(--dag-hue-\d\)", style)
        assert found, style
        return found[0]

    producer = hue_in(
        card.locator('[data-testid="inbox-dag-node"][data-order="0"] .dag-node-box')
    )
    assert producer == hue_in(card.locator('.card-node[data-order="0"]'))
    # ...and the pill on the CONSUMER carries the producer's hue, not its own.
    assert producer == hue_in(
        card.locator('.card-node[data-order="2"] [data-testid="inbox-node-input"][data-from="0"]')
    )
    consumer = hue_in(card.locator('.card-node[data-order="2"]'))
    assert consumer != producer


def test_the_condition_on_a_conditional_step_is_readable(render: Any) -> None:
    """The `when` mark on the node says a step MAY NOT RUN. That is half a fact: a reviewer
    cannot judge a conditional step without reading the condition, and it was nowhere on the
    card — only in the raw dump at the bottom."""
    page = render([_item(payload_view=_composite(_SCALAR_STEPS))])
    step = _queue_card(page).locator('.card-node[data-order="1"]')
    assert "row_estimate > 500" in (step.locator(".card-when").text_content() or "")
    # A step with no condition says nothing about one.
    assert _queue_card(page).locator(
        '.card-node[data-order="0"] .card-when'
    ).count() == 0


def test_a_step_that_consumes_itself_is_not_dressed_as_an_input(render: Any) -> None:
    """The other half of the unresolved rule. A self-reference parses and points at a step
    that exists — but it is not a dependency, the graph refuses to draw it, and a typed pill
    would be the card claiming a hop the runtime cannot make."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"a": "table"}),
            _step(1, consumes={"loop": "$1", "real": "$0"}),
        ]))
    ])
    card = _queue_card(page)
    assert _dag_edges(card).count() == 1

    loop = card.locator('[data-testid="inbox-node-input"][data-name="loop"]')
    assert "loop ← step 1 (itself)" in (loop.inner_text() or "")
    assert loop.get_attribute("data-type") == "unresolved"
    assert loop.get_attribute("data-from") is None
    # The real one beside it is untouched.
    real = card.locator('[data-testid="inbox-node-input"][data-name="real"]')
    assert real.get_attribute("data-type") == "table"
    assert real.get_attribute("data-from") == "0"


def test_a_duplicated_step_number_is_dropped_and_said(render: Any) -> None:
    """⚠ A DROPPED STEP HAS TO BE ANNOUNCED. Two entries with the same `order` in one half is a
    malformed plan; first occurrence wins, because the alternative is merging two different
    steps field-by-field into one that the payload never described. What must not happen is
    the loser vanishing in silence — the reviewer is approving a plan, and "there was another
    step 0 here" is a fact about it.

    A step written in BOTH halves under the same order is the normal case and is not a
    duplicate: that is the join this card is built on."""
    page = render([
        _item(payload_view=_composite(
            [
                _step(0, step_intent="the first step 0"),
                _step(0, step_intent="the second step 0"),
                _step(1, step_intent="an ordinary step"),
            ],
            [{"order": 0, "sql_template": "SELECT 'joined onto step 0'"},
             {"order": 1, "sql_template": "SELECT 1"}],
        ))
    ])
    card = _queue_card(page)
    notes = card.locator('[data-testid="inbox-plan-note"]')
    assert notes.count() == 1
    assert "duplicate step 0 in composes" in (notes.inner_text() or "")

    assert card.locator('[data-testid="inbox-bp-node"]').count() == 2
    step0 = card.locator('.card-node[data-order="0"]')
    assert "the first step 0" in (step0.inner_text() or "")
    assert "the second step 0" not in (step0.inner_text() or "")
    # The cross-half join still happened: the winner kept its SQL.
    assert "SELECT 'joined onto step 0'" in (step0.inner_text() or "")


def test_a_short_intent_is_not_stretched_across_the_node(render: Any) -> None:
    """⚠ `textLength` IS A FIT, NOT A FONT SIZE. Applied to every label it stretched a
    one-word intent across the whole box — "count" rendered as `c o u n t`, which reads as a
    rendering fault rather than as a short sentence. It is applied only where the estimate
    says the text would otherwise overflow its node."""
    page = render([
        _item(payload_view=_composite([
            _step(0, step_intent="count"),
            _step(1, step_intent="reconcile the quarterly payroll ledger against everything"),
        ]))
    ])
    card = _queue_card(page)
    short = card.locator('[data-testid="inbox-dag-node"][data-order="0"] .dag-node-intent')
    long_one = card.locator('[data-testid="inbox-dag-node"][data-order="1"] .dag-node-intent')
    assert short.get_attribute("textLength") is None
    assert long_one.get_attribute("textLength") is not None
    # The short one is drawn at its natural width, well inside the box.
    box = short.bounding_box()
    assert box is not None and box["width"] < 60, box


def test_two_labels_between_the_same_pair_do_not_sit_on_one_another(render: Any) -> None:
    """Two values taken from the same producer are two edges between the same two boxes, drawn
    on top of each other — so their labels land on the same point unless they are spread. One
    plate over another is one name the reviewer cannot read, and which one is the TABLE is the
    question the labels exist to answer."""
    page = render([
        _item(payload_view=_composite([
            _step(0, output={"avg": "scalar", "detail": "table"}),
            _step(1, consumes={"avg": "$0.avg", "detail": "$0"}),
        ]))
    ])
    card = _queue_card(page)
    labels = card.locator(".dag-edge-label-text")
    assert labels.count() == 2
    boxes = [labels.nth(i).bounding_box() for i in range(2)]
    assert all(b is not None for b in boxes)
    assert abs(boxes[0]["y"] - boxes[1]["y"]) >= 8, boxes
    # ...and neither of them reaches the node they point at, where the arrowheads crowd.
    target = card.locator(
        '[data-testid="inbox-dag-node"][data-order="1"] .dag-node-box'
    ).bounding_box()
    assert target is not None
    for b in boxes:
        assert b["x"] + b["width"] < target["x"], (b, target)


def test_a_tall_plan_scrolls_inside_its_own_box_too(render: Any) -> None:
    """⚠ THE OTHER AXIS. Forty sources fanning into one sink is nearly three thousand pixels
    tall — a graph allowed to be that tall pushes the SQL it summarizes an entire screen-height
    below itself, and the reviewer scrolls past the plan to reach the thing being judged. The
    box is bounded in BOTH directions and scrolls."""
    sources = [_step(i, output={f"out{i}": "table"}) for i in range(40)]
    sink = _step(40, step_intent="join all forty",
                 consumes={f"out{i}": f"${i}" for i in range(40)})
    page = render([_item(payload_view=_composite([*sources, sink]))])
    card = _queue_card(page)
    assert _dag_nodes(card).count() == 41

    metrics = page.evaluate(
        """() => {
            const svg = document.querySelector('[data-testid="inbox-dag"]');
            const wrap = svg.parentElement;
            return {
                scrollHeight: wrap.scrollHeight,
                clientHeight: wrap.clientHeight,
                svgHeight: svg.getBoundingClientRect().height,
            };
        }"""
    )
    assert metrics["scrollHeight"] > metrics["clientHeight"], metrics
    assert metrics["clientHeight"] <= 500, metrics
    assert metrics["svgHeight"] > 1000, metrics
    # Every step is still on the card below, in full — bounding the drawing hides nothing.
    assert card.locator('[data-testid="inbox-bp-node"]').count() == 41


# --- editing a knowledge candidate (knowledge-edit design §C.3) ----------------
#
# A `global_knowledge` candidate used to have ONE action for a wrong statement — reject —
# so these cases are about the affordance existing where it is legal and nowhere else, and
# about the two-step holding: the assistant fills the form, the reviewer writes.


def _knowledge(**over: Any) -> dict[str, Any]:
    item = _item(
        candidate_id="candidate::kn::0",
        type="global_knowledge",
        status="in_review",
        reason="knowledge_sampled",
        summary="pay_period is stored as a date",
        payload_view={
            "statement": "pay_period is stored as a date",
            "knowledge_type": "business_rule",
            "related_terms": ["pay period", "payroll calendar"],
            "structured": {"column": "payroll.payroll_fact.pay_period"},
            "scope": "payroll",
        },
        template_parts=[],
    )
    item.update(over)
    return item


def _kn_card(page: Any, candidate_id: str = "candidate::kn::0") -> Any:
    return page.locator(f'[data-candidate-id="{candidate_id}"]')


def test_the_knowledge_form_is_offered_only_on_an_in_review_knowledge_candidate(
    render: Any,
) -> None:
    """Three cards, one form. `validated` knowledge is already a neo4j node — editing it is a
    re-landing, which this slice does not do — and a blueprint has its own assistant, so a
    knowledge form on either would be a button whose only outcome is a refusal."""
    page = render(
        [
            _knowledge(),
            _knowledge(candidate_id="candidate::kn::landed", status="validated"),
            _item(candidate_id="candidate::bp::0", payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS),
        ],
    )
    assert _kn_card(page).locator('[data-testid="inbox-kn-form"]').count() == 1
    assert (
        _kn_card(page, "candidate::kn::landed")
        .locator('[data-testid="inbox-kn-form"]')
        .count()
        == 0
    )
    assert (
        _kn_card(page, "candidate::bp::0").locator('[data-testid="inbox-kn-form"]').count() == 0
    )
    # The payload sections are UNCHANGED by the form's arrival — the card is still the
    # verbatim ranking it was, with the editor under it.
    text = (_kn_card(page).locator('[data-testid="inbox-payload"]').text_content() or "").lower()
    assert "pay_period is stored as a date" in text


def test_the_form_prefills_from_the_payload_view(render: Any) -> None:
    """The reviewer edits what they are looking at. A form that started blank would make the
    smallest possible fix — one word in one sentence — a retyping exercise, which is how a
    field nobody meant to change quietly loses its value."""
    page = render([_knowledge()])
    card = _kn_card(page)
    assert (
        card.locator('[data-testid="inbox-kn-field-statement"]').input_value()
        == "pay_period is stored as a date"
    )
    assert card.locator('[data-testid="inbox-kn-field-scope"]').input_value() == "payroll"
    assert (
        card.locator('[data-testid="inbox-kn-field-knowledge_type"]').input_value()
        == "business_rule"
    )
    # `related_terms` is one per line, not a JSON array: the reviewer types terms, not syntax.
    assert (
        card.locator('[data-testid="inbox-kn-field-related_terms"]').input_value()
        == "pay period\npayroll calendar"
    )
    # `structured` is key/value rows for the same reason.
    rows = card.locator('[data-testid="inbox-kn-structured-row"]')
    assert rows.count() == 1
    assert rows.locator('[data-testid="inbox-kn-structured-key"]').input_value() == "column"
    assert card.locator('[data-testid="inbox-kn-withheld"]').count() == 0


def test_a_withheld_field_prefills_empty_and_says_why(render: Any) -> None:
    """⚠ THE REDACTION MUST NOT ROUND-TRIP. `payload_view` leaves the literal token
    `[redacted]` where the entity scan removed a span, and a form that prefilled that string
    would let a reviewer press Apply and land a sentence with a hole in it — through a clean
    intake check, because "[redacted]" is a perfectly valid string. So the box starts EMPTY,
    and the card says which fields did and why, since an empty box otherwise reads as a field
    the candidate never had."""
    page = render(
        [
            _knowledge(
                payload_view={
                    "statement": "the transit allowance for [redacted] is paid monthly",
                    "knowledge_type": "business_rule",
                    "scope": "payroll",
                }
            )
        ],
    )
    card = _kn_card(page)
    assert card.locator('[data-testid="inbox-kn-field-statement"]').input_value() == ""
    # ...and the fields that were NOT flagged keep their values: withholding everything
    # because one field leaked would make the edit harder than the reject it replaces.
    assert card.locator('[data-testid="inbox-kn-field-scope"]').input_value() == "payroll"
    note = card.locator('[data-testid="inbox-kn-withheld"]').text_content() or ""
    assert "statement" in note
    assert "do not retype what you cannot see" in note


def test_the_assistants_draft_fills_the_form_and_writes_nothing(render: Any) -> None:
    """⚠ THE TWO-STEP, again. `apply_knowledge` is the only write path into a knowledge
    payload, so the assistant's output must face the same Apply button a hand-typed one does —
    otherwise a model turn becomes a write, and the re-adjudication behind Apply is something
    the reviewer opted out of by taking the suggestion."""
    _SERVED["revise_knowledge"] = (
        200,
        {
            "payload": {
                "statement": "a transit allowance is paid monthly to employees on a transit route",
                "knowledge_type": "business_rule",
                "related_terms": ["transit allowance"],
                "structured": {"column": "payroll.payroll_fact.allowance"},
                "scope": "payroll",
            },
            "rationale": "stated for the class rather than the person",
            "reason": "",
            "diff": [
                {"field": "statement", "kind": "changed", "before": "[withheld]",
                 "after": "a transit allowance is paid monthly"},
                {"field": "scope", "kind": "unchanged", "before": "payroll", "after": "payroll"},
            ],
        },
    )
    page = render([_knowledge()])
    card = _kn_card(page)
    card.locator('[data-testid="inbox-kn-feedback"]').fill("say it for the class, not the person")
    card.locator('[data-testid="inbox-kn-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-kn-diff"]')

    diff = card.locator('[data-testid="inbox-kn-diff"]').text_content() or ""
    assert "statement" in diff
    assert "[withheld]" in diff  # the flagged BEFORE is redacted, never the raw span
    assert "stated for the class" in (
        card.locator('[data-testid="inbox-kn-proposal"]').text_content() or ""
    )
    # The form is UNTOUCHED until the reviewer asks for the draft.
    assert (
        card.locator('[data-testid="inbox-kn-field-statement"]').input_value()
        == "pay_period is stored as a date"
    )

    card.locator('[data-testid="inbox-kn-use-draft"]').click()
    assert "transit route" in card.locator('[data-testid="inbox-kn-field-statement"]').input_value()
    assert (
        card.locator('[data-testid="inbox-kn-field-related_terms"]').input_value()
        == "transit allowance"
    )
    # ...and nothing was written: the reviewer's own sentence went out, and no apply followed.
    paths = [post["path"] for post in _SERVED["posts"]]
    assert paths == ["/api/inbox/candidate%3A%3Akn%3A%3A0/revise_knowledge"]
    assert "say it for the class" in _SERVED["posts"][0]["body"]


def test_no_knowledge_suggestion_is_reported_without_touching_the_form(render: Any) -> None:
    """An empty payload with a reason is a 200, not a failure — and it must not clear the
    fields the reviewer has already edited."""
    _SERVED["revise_knowledge"] = (
        200,
        {"payload": {}, "rationale": "", "reason": "statement (person_name) — the draft still "
         "named an entity, so it is withheld", "diff": []},
    )
    page = render([_knowledge()])
    card = _kn_card(page)
    card.locator('[data-testid="inbox-kn-field-statement"]').fill("my own edit")
    card.locator('[data-testid="inbox-kn-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-kn-proposal-reason"]')

    reason = card.locator('[data-testid="inbox-kn-proposal"]').text_content() or ""
    assert "person_name" in reason
    assert card.locator('[data-testid="inbox-kn-use-draft"]').count() == 0
    assert card.locator('[data-testid="inbox-kn-field-statement"]').input_value() == "my own edit"


def test_a_503_leaves_the_knowledge_form_working_and_says_why(render: Any) -> None:
    """No reviser wired is not an error state for this page: the form is the capability, the
    assistant is the convenience. A page-level error banner would say the opposite."""
    _SERVED["revise_knowledge"] = (503, {"detail": "LLM-assisted revision unavailable"})
    page = render([_knowledge()])
    card = _kn_card(page)
    card.locator('[data-testid="inbox-kn-revise"]').click()
    page.wait_for_selector('[data-testid="inbox-kn-proposal"] .card-empty')

    assert "unavailable in this deployment" in (
        card.locator('[data-testid="inbox-kn-proposal"]').inner_text()
    )
    assert page.locator("#error-banner").is_visible() is False
    assert card.locator('[data-testid="inbox-kn-apply"]').count() == 1


def test_an_intake_refusal_is_shown_beside_the_field_not_in_the_banner(render: Any) -> None:
    """The 422 is the intake reader's OWN sentence — the same one that would have declined this
    payload at extraction — and it names the key or the requirement. That is a sentence the
    reviewer fixes by editing the box under it, so it renders there."""
    _SERVED["apply_knowledge"] = (
        422,
        {"detail": "candidate.payload.statement must be a non-empty, ENTITY-FREE sentence"},
    )
    page = render([_knowledge()])
    card = _kn_card(page)
    card.locator('[data-testid="inbox-kn-field-statement"]').fill("")
    card.locator('[data-testid="inbox-kn-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-kn-reason"]:not([hidden])')

    assert "ENTITY-FREE" in (card.locator('[data-testid="inbox-kn-reason"]').text_content() or "")
    assert page.locator("#error-banner").is_visible() is False
    # The whole payload went as ONE object under `payload`, with the empty statement included —
    # a page that dropped the empty field would have asked the server a different question.
    posted = json.loads(_SERVED["posts"][-1]["body"])
    assert posted["payload"]["statement"] == ""
    assert posted["payload"]["scope"] == "payroll"


def test_an_applied_edit_shows_the_settled_scan_and_survives_the_refresh(render: Any) -> None:
    """⚠ THE RESULT IS THE POINT OF THE ROUND TRIP. The apply re-runs the entity scan and
    STAMPS it, and that verdict is what decides whether approve is now possible. The apply also
    refreshes the list — so the outcome has to outlive the re-render, or the reviewer would
    have to apply a second time to learn what the first one decided."""
    _SERVED["apply_knowledge"] = (
        200,
        {
            "candidate_id": "candidate::kn::0",
            "status": "in_review",
            "outcome": "edited",
            "entity_scan": {
                "result": "reject",
                "hits": [{"field": "statement", "kind": "person_name"}],
            },
        },
    )
    page = render([_knowledge()])
    card = _kn_card(page)
    card.locator('[data-testid="inbox-kn-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-kn-reason"]:not([hidden])')

    reason = card.locator('[data-testid="inbox-kn-reason"]')
    assert "reject" in (reason.text_content() or "")
    assert "statement (person_name)" in (reason.text_content() or "")
    # The refresh rebuilds the card; the answer is still on it.
    page.wait_for_timeout(200)
    assert "statement (person_name)" in (
        _kn_card(page).locator('[data-testid="inbox-kn-reason"]').text_content() or ""
    )


# --- the User Knowledge tab (knowledge-edit design §D.3) ----------------------


def _record(**over: Any) -> dict[str, Any]:
    record = {
        "record_id": "userknow::u-1042::c1",
        "user_id": "u-1042",
        "statement": "Dana's cost centre is 0420",
        "fact_type": "user_fact",
        "scope": "user",
        "structured": {"cost_centre": "0420"},
        "committed_at": "2026-08-30T10:00:00Z",
        "provenance": {"source_session": "sess-9", "source_trace": "trace-9"},
        "promotion": None,
    }
    record.update(over)
    return record


def _open_uk_tab(page: Any) -> None:
    page.locator('[data-testid="inbox-tab"][data-type="user_knowledge"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-panel"]:not([hidden])')


def test_the_tab_is_its_own_view_not_a_filter_over_the_candidate_list(render: Any) -> None:
    """It listed nothing for as long as it existed: a `user_knowledge` candidate commits to the
    per-user store and is DROPPED from the candidate store, so the filter could never match.
    The tab now reads the store the facts are actually in — and the candidate list, its pager
    and the status toggle all belong to a queue this view is not showing."""
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _open_uk_tab(page)

    assert page.locator('[data-testid="inbox-item"]').count() == 0
    assert page.locator('[data-testid="inbox-pager"]').is_visible() is False
    assert page.locator('[data-testid="inbox-empty"]').is_visible() is False
    for handle in page.locator('[data-testid="inbox-status-toggle"] .status-btn').all():
        assert handle.is_disabled() is True
    # The badge does NOT claim a candidate count it does not have.
    badge = page.locator(
        '[data-testid="inbox-tab"][data-type="user_knowledge"] [data-testid="inbox-tab-count"]'
    )
    assert badge.text_content() == "—"


def test_a_users_facts_are_listed_only_once_the_user_is_named(render: Any) -> None:
    """The store's only read is per-user and the reviewer supplies the id — an empty box is not
    "everyone", it is a request with no meaning, and the page says so instead of fetching."""
    _SERVED["uk_records"] = [_record()]
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _open_uk_tab(page)

    page.locator('[data-testid="inbox-uk-load"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-status"]:not([hidden])')
    assert "never all" in (page.locator('[data-testid="inbox-uk-status"]').text_content() or "")
    assert page.locator('[data-testid="inbox-uk-card"]').count() == 0

    page.locator('[data-testid="inbox-uk-user-id"]').fill("u-1042")
    page.locator('[data-testid="inbox-uk-load"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-card"]')

    assert "user_id=u-1042" in _SERVED["last_get"]
    card = page.locator('[data-testid="inbox-uk-card"]')
    assert card.count() == 1
    text = card.inner_text()
    assert "Dana's cost centre is 0420" in text
    assert "user_fact" in text
    assert "sess-9" in text
    assert "0420" in (card.locator('[data-testid="inbox-uk-structured"]').inner_text())
    badge = page.locator(
        '[data-testid="inbox-tab"][data-type="user_knowledge"] [data-testid="inbox-tab-count"]'
    )
    assert badge.text_content() == "1"


def test_a_record_that_was_already_promoted_loads_with_a_dead_button(render: Any) -> None:
    """The candidate id is deterministic, so a second press is a no-op upstream — but a live
    button would still invite one, and a reviewer pressing it learns nothing. The card states
    where the fact already is instead."""
    _SERVED["uk_records"] = [
        _record(promotion={"candidate_id": "candidate::userpromote::abc", "status": "in_review"})
    ]
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _open_uk_tab(page)
    page.locator('[data-testid="inbox-uk-user-id"]').fill("u-1042")
    page.locator('[data-testid="inbox-uk-load"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-card"]')

    promoted = page.locator('[data-testid="inbox-uk-promoted"]')
    assert promoted.is_visible() is True
    # A LIVE promotion says only where the fact is. It claims no outcome, because there is
    # not one yet.
    assert (promoted.text_content() or "") == "in review as candidate::userpromote::abc"
    assert page.locator('[data-testid="inbox-uk-promote"]').is_disabled() is True


def _promoted_text(render: Any, status: str) -> str:
    """The card's promotion line for a record whose candidate is already at `status`."""
    _SERVED["uk_records"] = [
        _record(promotion={"candidate_id": "candidate::userpromote::abc", "status": status})
    ]
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _open_uk_tab(page)
    page.locator('[data-testid="inbox-uk-user-id"]').fill("u-1042")
    page.locator('[data-testid="inbox-uk-load"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-card"]')
    assert page.locator('[data-testid="inbox-uk-promote"]').is_disabled() is True
    return page.locator('[data-testid="inbox-uk-promoted"]').text_content() or ""


def test_a_rejected_promotion_says_it_is_closed_and_stays_closed(render: Any) -> None:
    """The dead end is deliberate (design §F.1.c) — re-promoting would hand any reviewer a
    one-click way to re-open work another reviewer deliberately closed — but a reviewer who
    reads "in review as …" over a disabled button concludes the opposite: that the work is
    still moving and the button is merely spent. The card names the closure instead."""
    assert _promoted_text(render, "rejected") == (
        "rejected as candidate::userpromote::abc. A reviewer rejected the promotion, and this "
        "fact cannot be promoted again from here."
    )


def test_an_accepted_promotion_says_so_and_promoted_says_the_yaml_is_out(render: Any) -> None:
    """`validated` and `promoted` are BOTH accepted, and they are not the same news: only
    `promoted` has emitted the YAML a human still has to merge. A reviewer chasing "why is
    this fact not in the corpus yet" needs to know which of the two they are looking at."""
    assert _promoted_text(render, "validated") == (
        "validated as candidate::userpromote::abc. A reviewer accepted the promotion."
    )
    assert _promoted_text(render, "promoted") == (
        "promoted as candidate::userpromote::abc. A reviewer accepted the promotion, and its "
        "YAML has been emitted."
    )


def test_a_status_this_page_has_never_heard_of_asserts_nothing_about_it(render: Any) -> None:
    """The status vocabulary outlives this file: `quarantined` and `retired` are already in the
    enum, and more will follow. An unknown status renders the neutral line and NO outcome
    clause — being told the raw status is a small loss, being told the wrong outcome
    confidently is a reviewer acting on a sentence no code ever checked."""
    text = _promoted_text(render, "quarantined")
    assert text == "quarantined as candidate::userpromote::abc"
    for word in ("rejected", "accepted", "cannot be promoted again", "YAML"):
        assert word not in text, word


def test_the_press_response_and_the_loaded_record_cannot_drift(render: Any) -> None:
    """The 200 from the promote route carries the same `status` the list route does, and a
    second press on a rejected record returns it with `already: true`. Both paths render
    through ONE helper, so the sentence a reviewer sees after pressing is the sentence they
    would have seen on reload — a card that reads "rejected" only after a refresh is a card
    that lied for as long as it was open."""
    _SERVED["uk_records"] = [_record()]
    _SERVED["uk_promote"] = (
        200,
        {
            "candidate_id": "candidate::userpromote::abc",
            "status": "rejected",
            "already": True,
        },
    )
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _open_uk_tab(page)
    page.locator('[data-testid="inbox-uk-user-id"]').fill("u-1042")
    page.locator('[data-testid="inbox-uk-load"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-card"]')

    page.locator('[data-testid="inbox-uk-promote"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-promoted"]:not([hidden])')

    promoted = page.locator('[data-testid="inbox-uk-promoted"]')
    assert (promoted.text_content() or "") == (
        "rejected as candidate::userpromote::abc \u2014 it was already promoted. A reviewer "
        "rejected the promotion, and this fact cannot be promoted again from here."
    )
    # The state modifier is a CLASS, not a second testid: the sentence carries the state for a
    # screen reader and this only stops the closed case from looking like the moving one.
    assert "uk-promoted--rejected" in (promoted.get_attribute("class") or "")
    assert page.locator('[data-testid="inbox-uk-promote"]').is_disabled() is True


def test_promoting_sends_the_pair_and_reports_where_the_fact_went(render: Any) -> None:
    """The body carries BOTH ids: a record id is guessable, and the service refuses the pair
    when the record's owner disagrees with the user the reviewer was looking at (design §D.2).
    The page sends the id it LOADED, so that check has something to catch."""
    _SERVED["uk_records"] = [_record()]
    _SERVED["uk_promote"] = (
        200,
        {
            "candidate_id": "candidate::userpromote::abc",
            "status": "in_review",
            "already": False,
            "entity_scan": {"result": "reject", "hits": [{"field": "statement", "kind": "person_name"}]},
        },
    )
    page = render([_item(payload_view=_HEALTHY_PAYLOAD, template_parts=_PARTS)])
    _open_uk_tab(page)
    page.locator('[data-testid="inbox-uk-user-id"]').fill("u-1042")
    page.locator('[data-testid="inbox-uk-load"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-card"]')

    page.locator('[data-testid="inbox-uk-promote"]').click()
    page.wait_for_selector('[data-testid="inbox-uk-promoted"]:not([hidden])')

    assert "in review as candidate::userpromote::abc" in (
        page.locator('[data-testid="inbox-uk-promoted"]').text_content() or ""
    )
    assert page.locator('[data-testid="inbox-uk-promote"]').is_disabled() is True
    posted = [post for post in _SERVED["posts"] if post["path"].endswith("/user_knowledge/promote")]
    assert len(posted) == 1
    assert json.loads(posted[0]["body"]) == {
        "user_id": "u-1042",
        "record_id": "userknow::u-1042::c1",
    }


def test_structured_rows_are_added_removed_and_sent_as_an_object(render: Any) -> None:
    """`structured` is a flat object of strings, and the reviewer edits it as rows rather than
    as JSON in a textarea — a typo in hand-written JSON comes back as an intake decline about
    the shape, which is a true sentence about the wrong problem. The rows are read back into an
    object at post time, and a removed row is GONE from it (the apply replaces the payload, so
    an absent key is the deletion)."""
    _SERVED["apply_knowledge"] = (
        200,
        {"candidate_id": "candidate::kn::0", "status": "in_review", "outcome": "edited",
         "entity_scan": {"result": "pass", "hits": []}},
    )
    page = render([_knowledge()])
    card = _kn_card(page)
    card.locator('[data-testid="inbox-kn-structured-add"]').click()
    rows = card.locator('[data-testid="inbox-kn-structured-row"]')
    assert rows.count() == 2
    rows.nth(1).locator('[data-testid="inbox-kn-structured-key"]').fill("grain")
    rows.nth(1).locator('[data-testid="inbox-kn-structured-value"]').fill("one row per period")
    rows.nth(0).locator('[data-testid="inbox-kn-structured-remove"]').click()
    assert card.locator('[data-testid="inbox-kn-structured-row"]').count() == 1

    card.locator('[data-testid="inbox-kn-apply"]').click()
    page.wait_for_selector('[data-testid="inbox-kn-reason"]:not([hidden])')

    posted = json.loads(_SERVED["posts"][-1]["body"])["payload"]
    assert posted["structured"] == {"grain": "one row per period"}
    # A clean scan is still SAID — "nothing was flagged" is the fact that decides whether
    # approve is now possible, and silence would read as "the apply did nothing".
    assert "pass" in (card.locator('[data-testid="inbox-kn-reason"]').text_content() or "")


def test_a_wholly_withheld_payload_still_gets_an_editable_form(render: Any) -> None:
    """The unsettled-scan row ships a NOTICE instead of its fields. Every box starts empty —
    there is nothing cleared for display to put in them — and the card says so, because a form
    that merely looked blank would read as a candidate with nothing in it, which is the failure
    the notice exists to prevent."""
    page = render(
        [_knowledge(payload_view={"withheld": "withheld — the entity scan never settled"})],
    )
    card = _kn_card(page)
    assert card.locator('[data-testid="inbox-kn-form"]').count() == 1
    assert card.locator('[data-testid="inbox-kn-field-statement"]').input_value() == ""
    assert card.locator('[data-testid="inbox-kn-structured-row"]').count() == 0
    note = card.locator('[data-testid="inbox-kn-withheld"]').text_content() or ""
    for field in ("statement", "knowledge_type", "related_terms", "structured", "scope"):
        assert field in note, field
