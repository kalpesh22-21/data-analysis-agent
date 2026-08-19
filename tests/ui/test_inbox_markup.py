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
    assert statuses <= {
        "in_review",
        "rejected",
        "validated",
        "promoted",
        "needs_parameterization",
    }


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


# --- the promotion hop (M4-P1): verify + promote on the promotable list ---------


def test_the_promotable_list_has_a_status_filter_of_its_own() -> None:
    """`validated` is where auto-landed learning nodes wait for a human to vouch for them
    and take their canon YAML to a PR. The service listed them and the two verbs existed
    for months with no way to reach either from a browser — a tab is the whole difference
    between a hop that exists and a hop anyone can run."""
    assert 'data-status="validated"' in _HTML
    statuses = set(re.findall(r'data-status="([a-z_]+)"', _HTML))
    assert statuses == {
        "in_review",
        "needs_parameterization",
        "validated",
        "promoted",
        "rejected",
    }


def test_verify_and_promote_are_offered_only_on_the_promotable_list() -> None:
    """Both verbs are `validated`-only at the service. The row renderer builds them for a
    validated item, and `enableActions` re-enables them only while the Promotable list is
    the loaded one — so a re-enable after an error on another list cannot hand a reviewer
    a button whose transition the service will refuse."""
    # Anchored on the ACTIONS branch (the same condition also guards the header badge).
    branch = _HTML[_HTML.index("var id = item.candidate_id;") :]
    branch = branch[: branch.index('} else if (item.status === "promoted") {')]
    assert '"verify"' in branch
    assert '"promote"' in branch
    # ...and not the in_review-only adjudication verbs.
    assert '"approve"' not in branch
    assert '"reject"' not in branch
    assert 'var promotable = state.status === "validated";' in _HTML
    # verify is promotable-ONLY; promote is separately scoped (it also runs on the
    # promoted list as the idempotent re-emit — see the promoted-list test below).
    assert 'act === "verify"' in _HTML
    assert "btn.disabled = !promotable;" in _HTML


def test_the_promote_response_is_rendered_as_yaml_plus_pr_metadata() -> None:
    """promote answers a `PromotionEmit`, not the action shape — the YAML IS the payload
    of the hop, and the five metadata fields are the handoff to a human who has to open
    the PR themselves (the service never touches git). A page that only flashed 'promoted'
    would throw away everything the call returned."""
    assert "function renderPromotionEmit(node, emit)" in _HTML
    # The YAML lands in a <pre>, by textContent, with a copy affordance next to it.
    assert 'pre.className = "promotion-yaml"' in _HTML
    assert "pre.textContent = yamlText;" in _HTML
    assert 'data-testid", "inbox-promotion-copy"' in _HTML
    assert "navigator.clipboard.writeText(yamlText)" in _HTML
    for field in (
        "target_path",
        "filename",
        "suggested_branch",
        "commit_message",
        "note",
    ):
        assert f'key: "{field}"' in _HTML, field


def test_a_promote_does_not_refresh_the_panel_away() -> None:
    """The default action path drops the row and refetches the list. For promote that
    would destroy the YAML in the same tick it arrived; for verify it would erase the
    `node_stamped` outcome. Both keep their row instead."""
    assert 'return action === "verify" || action === "promote";' in _HTML
    keeps = _HTML.index("if (keepsRow(action)) {")
    drops = _HTML.index("node.parentNode && node.parentNode.removeChild(node);", keeps)
    assert keeps < drops, "the keep-the-row branch must precede the drop-and-refresh one"
    # ...and the panel renderers are what the branch calls.
    assert "renderPromotionEmit(node, result);" in _HTML
    assert "renderVerifyResult(node, result);" in _HTML


def test_an_unstamped_verify_is_a_visible_warning() -> None:
    """`node_stamped: false` means the vouch did NOT reach the staging node — the service
    fails open, so the envelope moved and the node did not. Silence here would leave a
    reviewer believing a node is human-vouched when nothing on it says so, which is the
    exact failure the verify step exists to prevent."""
    assert "result.node_stamped === false" in _HTML
    assert "node not stamped — re-verify" in _HTML
    assert 'note.className = stamped ? "verify-note" : "verify-note is-warning"' in _HTML


def test_the_promoted_list_offers_only_the_idempotent_re_emit() -> None:
    """A promoted candidate is terminal, but its card is not read-only like the archive:
    `promote` re-emits the same YAML with no status move, which is the only way back to a
    lost PR. It is also the ONLY thing offered — verify and the adjudication verbs have
    all left their legal window, and a button that can only 409 is worse than no button."""
    branch = _HTML[_HTML.index('} else if (item.status === "promoted") {') :]
    branch = branch[: branch.index("} else {")]
    assert '"promote"' in branch
    assert '"Re-emit YAML"' in branch
    for refused in ('"verify"', '"approve"', '"reject"', '"retract"'):
        assert refused not in branch, refused
    # ...and the enable rule lets promote live on BOTH promotion lists, verify on one.
    assert 'var promoted = state.status === "promoted";' in _HTML
    assert 'btn.disabled = !(promotable || promoted);' in _HTML


def test_a_promotable_card_shows_its_verify_state() -> None:
    """`verified` is on every wire item expressly so the UI can tell an auto-landed node
    from a vouched one. It is the difference between the two buttons under the card:
    promote REQUIRES it and 409s without it, and nothing else on the card says which of
    the two is the next step."""
    branch = _HTML[_HTML.index('if (item.status === "validated") {') :]
    branch = branch[: branch.index("header.appendChild(verified);")]
    assert "item.verified" in branch
    assert "verified — ready to promote" in branch
    assert "awaiting verify" in branch
    assert 'data-testid", "inbox-verified"' in _HTML


def test_verify_is_dead_on_a_row_that_just_promoted() -> None:
    """The row survives a promote so the YAML can be copied — which means its buttons are
    re-armed, and a re-armed Verify on a now-`promoted` candidate is a guaranteed 409.
    Promote itself stays live: that is the idempotent re-emit."""
    marker = 'if (action === "promote") {'
    assert marker in _HTML
    branch = _HTML[_HTML.index(marker) :]
    branch = branch[: branch.index("});")]
    assert "verifyBtn.disabled = true" in branch
