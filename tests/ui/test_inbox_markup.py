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
    assert "JSON.stringify(withProposedSql({ entries: entries, replace: replaceAll }, sql))" in _HTML
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


# --- the revise modal ---------------------------------------------------------------------


def test_the_revise_proposal_opens_a_modal_rather_than_applying_on_trust() -> None:
    """The inline proposal asked a reviewer to read a row-level diff and commit on it. The
    modal shows the candidate AS IT WOULD BE and lets them adjust first, so the assistant's
    answer is a starting point rather than take-it-or-leave-it.

    On BOTH surfaces. Wiring it only to the review queue put it on the path least likely to
    open it: a queue card is usually already classified and the assistant has nothing to add,
    while a `needs_parameterization` row exists BECAUSE something is unclassified."""
    assert "function openReviseModal(" in _HTML
    assert 'data-testid", "inbox-revise-modal"' in _HTML
    assert 'apply.textContent = "Review the change…"' in _HTML
    assert "openReviseModal(item, parsed, node, function (entries, replace, sql)" in _HTML


def test_the_modal_shows_the_sql_and_offers_no_way_to_edit_it() -> None:
    """⚠ THE GUARANTEE THE REVISER'S TOOL MAKES, held at the UI too. The template is DERIVED
    from the accepted query by AST rewrite, so `explain_ok`, `binds_to_subset_uses` and
    `read_only_select` only mean something because they check a provenance chain back to a
    query that really ran. A hand-edited template leaves all three validating prose.

    Shown in a `<pre>`, never an input — a reviewer must be able to READ the query they are
    classifying without being offered a box that would invalidate the checks."""
    assert '"pre", "modal-sql", template' in _HTML
    assert "Not editable — the template is derived" in _HTML


def test_the_modal_writes_nothing_until_apply_and_goes_through_the_same_validation() -> None:
    """It is a better keyboard, not a second write path: apply posts to `apply_revision`,
    which re-validates exactly as a hand-typed array does."""
    assert "Nothing is written until you apply." in _HTML
    assert "performRevisionApply(item.candidate_id, entries, node, apply, sql)" in _HTML


def test_the_modal_drops_fields_the_chosen_role_does_not_use() -> None:
    """The documented live-model failure, now reachable by a HUMAN: switch a role and leave the
    old boxes populated, and an `inline` entry carries an empty `slot` object — which reads as
    a slot declaration and fails validation. `readModalEntries` rebuilds from the role."""
    assert "ROLE_FIELDS" in _HTML
    assert "var wanted = ROLE_FIELDS[role] || [];" in _HTML


def test_the_modal_can_be_dismissed_without_committing() -> None:
    """Escape and click-outside both cancel. A modal dismissable only by its own button is a
    trap on a surface where walking away is the safe action."""
    assert 'if (ev.target === backdrop) closeReviseModal();' in _HTML
    assert 'ev.key === "Escape"' in _HTML


def test_the_modal_commits_through_the_verb_its_surface_owns() -> None:
    """The review queue REPLACES (`apply_revision` — idempotent, which is what lets it be
    offered on a status whose guard cannot protect an append); the form APPENDS what the
    reviewer was missing (`complete`). Same modal, same edits, different commit."""
    assert "performRevisionApply(item.candidate_id, entries, node, apply, sql)" in _HTML
    assert "performCompletion(item.candidate_id, area.value, replace, node," in _HTML


def test_the_form_modal_writes_back_through_the_visible_textarea() -> None:
    """So the modal never becomes a hidden second source of truth for what is about to be
    posted — the reviewer can still see and edit exactly what will be sent, and
    `performCompletion` stays the one submit path."""
    assert "area.value = JSON.stringify(entries, null, 2)" in _HTML


# --- the SQL rewrite opt-in ---------------------------------------------------------------
#
# The assistant may now be allowed to REPLACE the query, not just re-classify its predicates.
# That breaks the one property every static check leans on — that the template was derived
# from a query the session actually ran — so what is pinned here is that the page never lets
# it happen quietly: the option is opt-in, the consequence is written beside it, the proposal
# announces itself, and the artifact carries the fact permanently.


def _fn_source(name: str) -> str:
    """The source of one top-level function in the page, for "only inside this branch" checks."""
    start = _HTML.index("  function " + name + "(")
    end = _HTML.index("\n  function ", start + 1)
    return _HTML[start:end]


def test_the_rewrite_is_opt_in_and_says_what_it_costs() -> None:
    """DEFAULT-OFF with the consequence next to the box, not in a doc. A reviewer ticking this
    is choosing to keep a blueprint whose SQL no longer matches the session it came from, and
    that sentence has to be readable at the moment of the choice."""
    assert 'data-testid", "inbox-revise-allow-sql"' in _HTML
    assert 'data-testid", "inbox-revise-allow-sql-caution"' in _HTML
    assert "Let the assistant rewrite the SQL" in _HTML
    assert "allowSql.checked = false;" in _HTML
    # The caution, once, as a constant — so the box, the proposal fallback and the modal
    # cannot drift into three different accounts of the same risk.
    assert "A rewritten query is no longer the one the session ran." in _HTML
    assert "cannot auto-land, and should be trial-run before approval." in _HTML


def test_the_revise_request_carries_the_opt_in_explicitly() -> None:
    """`allow_sql` is SENT on every ask, false when untouched. A server that had to read an
    absent field as "no" would be one default away from rewriting a query nobody asked it to
    touch — and the BFF forwards this body verbatim, so this is the shape the reviser sees."""
    assert (
        "JSON.stringify({ feedback: feedback.value, allow_sql: allowSql.checked === true })"
        in _HTML
    )


def test_a_composite_candidate_is_not_offered_the_rewrite() -> None:
    """The server 422s a rewrite asked for on a composite — it composes other blueprints and
    has no single query to replace. The page disables the box and says why, so the reviewer
    learns the reason instead of collecting an error."""
    assert 'item.payload_view.kind === "composite"' in _HTML
    assert "allowSql.disabled = true;" in _HTML
    assert "SQL rewrite is not offered for composite blueprints yet." in _HTML


def test_the_rewrite_panes_exist_only_on_the_sql_changed_branch() -> None:
    """The warning and the before/after panes are reachable ONLY through `sql_changed`. A
    proposal that only re-roles entries must look exactly as it did before this existed —
    otherwise the alarm is on every proposal, which is the same as being on none of them."""
    source = _fn_source("renderSqlRewrite")
    assert 'if (!isObject(payload) || payload.sql_changed !== true) return null;' in source
    for testid in (
        "inbox-revise-sql-warning",
        "inbox-revise-sql-before",
        "inbox-revise-sql-after",
    ):
        assert testid in source, testid
        assert _HTML.count(testid) == 1, f"{testid} is referenced outside the rewrite branch"
    # The alarm is announced, not just coloured.
    assert 'warn.setAttribute("role", "alert");' in source
    # And it quotes the SERVER's caution, falling back to the page's own only when none came.
    assert "payload.caution || SQL_REWRITE_CAUTION" in source


def test_the_proposed_sql_reaches_the_dom_as_text() -> None:
    """Contract §4, at the one new place a server string is rendered: the proposed query is a
    model-authored blob and goes in through `textContent` like everything else. `el()` sets
    `textContent`, and the panes are `<pre>` — never an input, which would invite a hand edit
    the static checks cannot see."""
    source = _fn_source("renderSqlRewrite")
    assert 'el("pre", "", String(payload.sql || ""))' in source
    assert "createElement(\"input\")" not in source
    assert "innerHTML" not in source


def test_the_modal_shows_the_rewritten_sql_and_labels_it() -> None:
    """The modal's `<pre>` is the query the reviewer is about to commit to. Showing the OLD
    template above entries written against a NEW one would be the worst version of this
    screen — an approval of a change that was never read."""
    source = _fn_source("openReviseModal")
    assert "var rewritten = payload.sql_changed === true;" in source
    assert "var template = rewritten ? proposedSql : currentTemplateText(item);" in source
    assert 'data-testid", "inbox-revise-modal-sql-rewritten"' in source
    assert "Rewritten by the assistant" in source
    # The caution is repeated where the commit happens, and the button names what it applies.
    assert 'data-testid", "inbox-revise-modal-sql-caution"' in source
    assert '"Apply, including the rewritten SQL"' in source


def test_the_sql_is_sent_only_when_the_proposal_rewrote_it() -> None:
    """Both write verbs treat an ABSENT `sql` as "keep the query the session ran". So the
    field is attached only for a proposal the server itself marked `sql_changed` — sending
    `""` on every apply would ask the server to tell "no rewrite" from "rewrite to nothing",
    and one of those readings deletes the query."""
    source = _fn_source("withProposedSql")
    assert 'if (typeof sql === "string" && sql) body.sql = sql;' in source
    # Both senders route through the one guard rather than composing a body each.
    assert "JSON.stringify(withProposedSql({ entries: entries }, sql))" in _HTML
    assert (
        "JSON.stringify(withProposedSql({ entries: entries, replace: replaceAll }, sql))"
        in _HTML
    )
    # ...and the string that goes back is the server's own, carried unedited from the modal.
    assert "commit(entries, payload.replace === true, proposedSql);" in _HTML


def test_a_rewritten_card_says_so_permanently() -> None:
    """⚠ THE BADGE OUTLIVES THE SESSION THAT MADE IT. The modal's caution is seen once, by the
    person who chose the rewrite; every reviewer afterwards would otherwise read the card as a
    faithful record of a query that ran. `payload_view.sql_rewrite` is the server's stamp, and
    the card renders it wherever a decision is taken — the header, and beside Approve."""
    source = _fn_source("renderSqlRewriteBadge")
    assert 'data-testid", "inbox-card-sql-rewritten"' in source
    assert "SQL rewritten by the assistant on " in source
    assert "formatWhen(record.applied_at)" in source
    assert "not the session's query. Trial-run before approving." in source
    # Read through ONE accessor, so the badge and the approve-side line can never disagree
    # about whether this artifact is still the session's query.
    assert "view.sql_rewrite" in _fn_source("sqlRewriteRecord")
    assert "if (sqlRewriteRecord(item)) {" in _HTML
    assert 'data-testid", "inbox-approve-sql-rewritten"' in _HTML


def test_the_rewrite_caution_never_disables_approve() -> None:
    """A caution, not a gate. A reviewer who has trial-run the rewrite is exactly the person
    who should be able to approve it, and a page that blocked them would push the decision to
    someone with less context."""
    branch = _HTML[_HTML.index('makeActionButton(id, "approve", "Approve", li, actions, state.offline)') :]
    branch = branch[: branch.index('makeActionButton(id, "reject"')]
    assert "sqlRewriteRecord(item)" in branch
    # The only thing the branch does is append a line — no disable, no removal.
    assert "disabled" not in branch


def test_a_rewrite_with_no_entries_is_still_applicable() -> None:
    """⚠ AN EMPTY ENTRIES ARRAY IS A VALID ANSWER FOR A REWRITE. D97's totality walk passes a
    query with no literal predicates to classify — a plain aggregate with no WHERE is the
    ordinary case — so the server can legitimately return `sql_changed: true` with
    `entries: []`. Gating the apply button and the modal on `entries` alone would render that
    proposal, warning and all, and leave the reviewer no way to take it.

    The early return survives for the OTHER case: an entries-only proposal with no entries has
    nothing to apply, and a modal over it would be an empty dialog."""
    assert (
        "apply.hidden = !(proposed.length || (parsed && parsed.sql_changed === true));" in _HTML
    )
    source = _fn_source("openReviseModal")
    assert "if (!proposed.length && !rewritten) return;" in source
    # The empty parameterization is stated, not left as a blank modal body — applying it
    # CLEARS the array the old query needed.
    assert 'data-testid", "inbox-revise-modal-no-entries"' in source
    assert "becomes an empty array." in source
    # ...and the on-card note stops calling a real proposal "no suggestion".
    assert "the rewritten query has no literal predicates to classify" in _HTML
