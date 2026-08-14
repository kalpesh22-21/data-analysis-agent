"""Static invariants of the fail-to-review surface in `ui/static/inbox.html` (QA).

`ui/static/inbox.html` is hand-written with no build step, so its behaviour is exercised
only by `tests/e2e/test_ui_review_inbox.py` — gated on `RUN_E2E=1`, needing a live stack,
and today covering the review queue and the archive but NOT the
`needs_parameterization` work list. That leaves the page's half of this slice with no
default-run coverage at all, which is how a surface built to stop silent loss goes
silently missing.

What is pinned here is the narrow set of structural facts whose breakage is SILENT — the
page still loads and simply does less, or more, than it claims:

  * the work list is REACHABLE (a status the BFF and the service both allow but the page
    never offers is work nobody sees);
  * the page has NO HTML sink. The decline detail is a sentence of literal values lifted
    from the analyst's own SQL, and it is written into the DOM; `textContent` everywhere
    is the property that makes that safe, and one `innerHTML` added later would undo it
    with nothing failing;
  * the completion POST carries the reviewer's entries in a JSON body to the ONE proxied
    action that accepts one;
  * approve is not offered on a form — the server refuses it, and offering it would
    invite a reviewer to try to skip the validation the whole path exists to run.
"""

from __future__ import annotations

import pathlib
import re

_INBOX = pathlib.Path(__file__).resolve().parents[2] / "ui" / "static" / "inbox.html"
_HTML = _INBOX.read_text(encoding="utf-8")


def test_the_work_list_has_a_status_filter_of_its_own() -> None:
    """`needs_parameterization` is a separate queue precisely because the reviewer's task
    is different from judging an idea. A page that lists only `in_review` and `rejected`
    would leave every form written by the consumer unreachable — the same invisible loss
    the slice was built to end, moved to the last hop."""
    assert 'data-status="needs_parameterization"' in _HTML
    # ...and it is still one of a fixed set, not a free-text status box.
    statuses = set(re.findall(r'data-status="([a-z_]+)"', _HTML))
    assert "needs_parameterization" in statuses
    assert statuses <= {"in_review", "rejected", "validated", "needs_parameterization"}


def test_the_page_has_no_html_sink() -> None:
    """Every server value on this page — the payload view, the decline detail, the fresh
    decline from a completion — reaches the DOM through `textContent`. The decline detail
    is the sharp one: it carries predicate literals from the session's SQL and is written
    verbatim by design, so an `innerHTML` anywhere in this file turns the review surface
    into an injection surface."""
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        occurrences = [
            line
            for line in _HTML.splitlines()
            if sink in line and not line.strip().startswith("//")
        ]
        assert occurrences == [], f"{sink} appears in inbox.html: {occurrences}"


def test_the_completion_posts_the_entries_as_a_json_body() -> None:
    """`complete` is the only inbox action with a request body, and the BFF forwards it
    VERBATIM — so the shape the page sends is the shape the validator sees. A page that
    sent the entries as a query string, or forgot `replace`, would leave the reviewer's
    work in the browser while the candidate declined for entries it never received."""
    assert '"/api/inbox/" + encodeURIComponent(candidateId) + "/complete"' in _HTML
    assert 'JSON.stringify({ entries: entries, replace: replaceAll })' in _HTML
    assert '"Content-Type": "application/json"' in _HTML


def test_the_page_refuses_a_body_that_is_not_an_array_before_sending_it() -> None:
    """A local guard that is NOT a substitute for the server's (which re-checks and 422s)
    but keeps a typo from becoming a round trip. Pinned so it is not removed as
    redundant — it is the only place the reviewer gets told that their JSON is a JSON
    OBJECT rather than an array."""
    assert "Array.isArray(entries)" in _HTML


def test_a_withheld_detail_is_announced_rather_than_rendered_blank() -> None:
    """The server sends `detail: ""` with `detail_withheld: true` when the entity scan
    did not settle a clean pass. Rendering that as an empty block would tell the reviewer
    the row has nothing to say — which is the failure this whole slice is about, one
    layer up — so the page says explicitly that a detail exists and is being held."""
    withheld_at = _HTML.find("decline.detail_withheld")
    detail_at = _HTML.find("inbox-decline-detail")
    assert withheld_at != -1, "the withheld branch is gone"
    assert detail_at != -1
    # The withheld case is checked BEFORE the detail is rendered, not after it.
    assert withheld_at < detail_at
    assert "inbox-decline-withheld" in _HTML


def test_approve_is_never_offered_on_a_form() -> None:
    """Approve is `in_review`-only at the server. The fail-to-review branch of the row
    renderer offers exactly two actions — complete and reject — so the page cannot invite
    a reviewer into a transition that will be refused, or suggest that the validation is
    skippable."""
    branch = _HTML[_HTML.index('if (item.status === "needs_parameterization")') :]
    branch = branch[: branch.index("function ", 1)]
    assert '"reject"' in branch
    assert 'dataset.action = "complete"' in _HTML
    assert '"approve"' not in branch
