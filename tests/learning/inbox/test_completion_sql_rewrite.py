"""§C.5 — applying an assistant SQL REWRITE through the completer.

`revise` proposes; this is the half that WRITES. A rewrite is not a third merge mode — it is a
different SUBJECT. The accepted SQL is replaced, and everything after it (the D97 totality walk,
the AST rewrite, `explain_ok`, `binds_to_subset_uses`, `read_only_select`,
`check_no_frozen_date_literal`, the leakage scan, dedup, the router) runs against the new query.

THE SAFETY ARGUMENT IS `learning/mint`'S, and these tests are what make it true rather than
claimed. The new `ValidationSnapshot` is stamped `authored=True`, which is what
`writer/routing.py::_is_authored` reads, and the sharpest test below is the one where EVERY
STATIC CHECK PASSES: an ordinary mined candidate in that state auto-lands as `candidate`, and
this one is held at `in_review` with reason `hand_authored`. Without that, "the reviewer sees it
first" would be a sentence in a docstring rather than a property of the router.

THE OTHER HALF IS THE DECLINE PATH. A rewritten candidate that still fails re-validation keeps
the NEW snapshot, because the entries the reviewer left are written against the NEW query — a row
storing the old SQL beside them would render a complaint naming predicates of a query the card no
longer shows.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import (
    CandidateStatus,
    build_declined_envelope,
    mint_review_candidate_id,
)
from data_agent.learning.extractor.models import Decline
from data_agent.learning.generalize import GeneralizeStage
from data_agent.learning.inbox import ParameterizationCompleter, ReviewInbox
from data_agent.learning.inbox.completion import (
    REWRITE_TOOL_CALL_REF,
    CompletionInputError,
)
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.leakage import LeakageGateStage
from data_agent.learning.mint.models import MAX_SQL_CHARS
from data_agent.learning.writer import WriterStage
from data_agent.learning.writer.routing import derive_inbox_reason, route_candidate
from tests._catalog_fixture import fixture_catalog

from ..extractor.helpers import blueprint_raw, make_summary, make_tool_call

TOKEN = "reviewer-secret"
AUTH = {"X-Reviewer-Token": TOKEN}
CID = mint_review_candidate_id("hash-rewrite", 0)

_CATALOG = build_sqlglot_schema_from_catalog(fixture_catalog())
_T = "dbpcm_warehouse.payroll"

# The query the SESSION ran, and the one a rewrite replaces.
ACCEPTED_SQL = (
    f"SELECT SUM(amount) AS total FROM {_T} "
    "WHERE register_type = 'EARN' AND department_code = '0420'"
)
# A CLEAN rewrite: parses, reads catalog columns, no frozen date. Every static check passes,
# which is exactly what makes it the sharpest test of the routing rule.
CLEAN_REWRITE = (
    f"SELECT SUM(amount) AS total FROM {_T} "
    "WHERE register_type = 'EARN' AND department_code = '0420' AND type_code = 'REG'"
)
# The FROZEN RUN DATE — the failure the §C.5 prompt's date rule exists to prevent, and the one
# check a rewrite can break that re-roling literals never could. Every literal predicate here
# HAS an entry, so it clears the totality walk and reaches the static checks, which is the only
# way to see `date_literal_ok` decide anything.
DATED_REWRITE = (
    f"SELECT SUM(amount) AS total FROM {_T} WHERE register_type = 'EARN' "
    "AND dateDiff('day', pay_date, toDateTime64('2026-08-28 00:00:00', 6)) < 30"
)
STAR_REWRITE = f"SELECT * FROM {_T} WHERE register_type = 'EARN'"


def _inline(column: str, value: str, why: str) -> dict:
    return {
        "locator": {"table": _T, "column": column, "value": value},
        "role": "inline",
        "why": why,
    }


def _slot(column: str, value: str) -> dict:
    return {
        "locator": {"table": _T, "column": column, "value": value},
        "role": "slot",
        "slot": {
            "name": column,
            "type": "entity",
            "binds_to": f"{_T}.{column}",
            "required": True,
        },
    }


CLEAN_ENTRIES = [
    _inline("register_type", "EARN", "defines the metric earnings"),
    _slot("department_code", "0420"),
    _inline("type_code", "REG", "regular pay only"),
]
DATED_ENTRIES = [
    _inline("register_type", "EARN", "defines the metric earnings"),
    _inline("pay_date", "30", "a 30-day trailing window"),
]
STAR_ENTRIES = [_inline("register_type", "EARN", "defines the metric earnings")]


def _declined_envelope():
    """A persisted `needs_parameterization` row whose accepted SQL is `ACCEPTED_SQL`."""
    env = build_declined_envelope(
        Decline(
            type="blueprint",
            reason="totality_violation",
            detail="no entry for 2 literal predicate(s)",
            correctable=True,
            corrections_attempted=1,
            raw_payload=blueprint_raw(parameterization=[], source_refs=("tc1",)),
        ),
        make_summary(
            tool_calls=(make_tool_call(ref="tc1", sql=ACCEPTED_SQL),),
            content_hash="hash-rewrite",
        ),
        candidate_id=CID,
    )
    return replace(
        env,
        entity_scan={
            "result": "pass",
            "hits": [],
            "scanned_fields": ["intent"],
            "scanner": "regex+ner",
        },
    )


def _stages(store):
    return (
        GeneralizeStage(catalog_schema=_CATALOG),
        LeakageGateStage(candidate_store=store),
        WriterStage(sampler=lambda env: False),
    )


async def _completer(env=None, *, stages=True):
    store = InMemoryCandidateStore()
    env = env if env is not None else _declined_envelope()
    await store.put(env)
    return (
        ParameterizationCompleter(
            store=store,
            known_rules=frozenset(),
            stages=_stages(store) if stages else (),
        ),
        store,
        env,
    )


def _static(env) -> dict:
    return (env.payload.get("generalization") or {}).get("static_validation") or {}


# --- the snapshot -------------------------------------------------------------


async def test_a_rewrite_builds_an_authored_snapshot_under_one_new_ref() -> None:
    """`learning/mint`'s shape, deliberately: an accepted SQL nobody observed a session
    produce, cited by the one pointer it honestly has. `authored` is the field that makes the
    router hold it, so it is checked here rather than inferred from the status."""
    completer, store, env = await _completer()

    result = await completer.complete(
        env, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
    )

    snapshot = result.envelope.revalidation
    assert snapshot.sql_by_ref == {REWRITE_TOOL_CALL_REF: (CLEAN_REWRITE,)}
    assert [p.tool_call_ref for p in snapshot.evidence] == [REWRITE_TOOL_CALL_REF]
    assert snapshot.authored is True
    assert snapshot.reconstructed is False
    assert result.envelope.payload["source_tool_call_refs"] == [REWRITE_TOOL_CALL_REF]
    # PERSISTED, not merely returned — the next reviser call reads the accepted SQL off the row.
    assert (await store.get(CID)).revalidation.sql_by_ref == {
        REWRITE_TOOL_CALL_REF: (CLEAN_REWRITE,)
    }


async def test_the_session_identity_survives_the_rewrite() -> None:
    """⚠ CARRIED, not cleared. `session_id`/`user_id`/`trace_id` say where this candidate CAME
    FROM, which a rewrite does not alter and an audit grouping by them still needs; the
    `content_hash` and the candidate_id are the row's identity, and this is the same review item
    edited rather than a fork. `accepted_signal` is kept as-is for the reason mint keeps
    `explicit_confirm`: the signal records what the SESSION did, and the new fact lives on
    `authored` where a reader can already see it."""
    completer, _store, env = await _completer()
    before = env.revalidation

    result = await completer.complete(
        env, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
    )

    after = result.envelope.revalidation
    assert (after.session_id, after.user_id, after.trace_id) == (
        before.session_id,
        before.user_id,
        before.trace_id,
    )
    assert after.content_hash == before.content_hash
    assert after.accepted_signal == before.accepted_signal
    assert result.envelope.candidate_id == env.candidate_id
    assert result.envelope.content_hash == env.content_hash


async def test_the_payload_carries_a_durable_rewrite_badge() -> None:
    """The card renders `payload_view`, so the record has to live IN the payload to be
    renderable. The previous query is a DIGEST rather than text: `payload_view` reaches a
    browser, and the old accepted SQL is entity-bearing — a hash answers "which query was it
    before" without putting a second copy of the literals anywhere."""
    completer, store, env = await _completer()

    result = await completer.complete(
        env, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
    )

    badge = result.envelope.payload["sql_rewrite"]
    assert badge["by"] == "assistant"
    assert badge["applied_at"].endswith("+00:00")
    assert badge["previous_sql_sha256"] == hashlib.sha256(
        ACCEPTED_SQL.encode("utf-8")
    ).hexdigest()
    assert ACCEPTED_SQL not in str(badge)
    # ⚠ SURVIVES `to_candidate`. `BlueprintPayload.to_doc` is a CLOSED key set and `_completed`
    # rebuilds the stored payload from it, so an unregistered key would be dropped on the very
    # write that created it — the response would say "rewritten" and the row would carry no
    # trace of it.
    assert (await store.get(CID)).payload["sql_rewrite"] == badge


async def test_an_ordinary_completion_carries_no_badge_and_no_authored_flag() -> None:
    """The default path is untouched: no `sql`, no rewrite, no hand-authored provenance."""
    completer, _store, env = await _completer()
    result = await completer.complete(
        env,
        entries=[
            _inline("register_type", "EARN", "defines the metric earnings"),
            _slot("department_code", "0420"),
        ],
        replace_all=True,
    )
    assert result.sql_rewritten is False
    assert "sql_rewrite" not in result.envelope.payload
    assert result.envelope.revalidation.authored is False
    assert result.envelope.revalidation.sql_by_ref == {"tc1": (ACCEPTED_SQL,)}


async def test_resending_the_same_sql_is_not_a_rewrite() -> None:
    """⚠ THE ECHO CASE. A form that round-trips the SQL it displayed must not turn every
    ordinary completion into a hand-authored candidate that can no longer auto-land. Compared
    with `last_sql`, the same function the reviser showed the model."""
    completer, _store, env = await _completer()
    result = await completer.complete(
        env,
        entries=[
            _inline("register_type", "EARN", "defines the metric earnings"),
            _slot("department_code", "0420"),
        ],
        replace_all=True,
        rewritten_sql=f"  {ACCEPTED_SQL}  ",
    )
    assert result.sql_rewritten is False
    assert result.envelope.revalidation.authored is False
    assert "sql_rewrite" not in result.envelope.payload


# --- the guards ---------------------------------------------------------------


async def test_a_rewrite_without_replace_all_is_refused_and_says_what_to_do() -> None:
    """Every existing entry describes the OLD query, so appending to them produces a
    parameterization half about a string nobody has. NAMED, not merely refused: the reviewer's
    next action is one checkbox, and "invalid" would send them looking through their entries for
    a fault that is not there."""
    completer, store, env = await _completer()
    before = (await store.get(CID)).to_doc()

    with pytest.raises(CompletionInputError) as exc:
        await completer.complete(
            env, entries=CLEAN_ENTRIES, replace_all=False, rewritten_sql=CLEAN_REWRITE
        )

    assert "replace=true" in str(exc.value)
    assert (await store.get(CID)).to_doc() == before  # nothing written


async def test_the_inbox_forces_replace_so_the_guard_is_never_reached_from_the_surface() -> None:
    """The surface AGREES with the guard rather than substituting for it: `complete` normally
    appends, and a rewrite makes appending meaningless, so the verb forces the mode the
    completer would otherwise refuse."""
    _completer_, store, env = await _completer()
    inbox = ReviewInbox(
        store,
        completer=ParameterizationCompleter(
            store=store, known_rules=frozenset(), stages=_stages(store)
        ),
    )
    result = await inbox.complete_parameterization(
        CID, entries=CLEAN_ENTRIES, replace_all=False, rewritten_sql=CLEAN_REWRITE
    )
    assert result.sql_rewritten is True
    # REPLACED, not appended: the array is exactly what was sent, with none of the old entries.
    assert len(result.envelope.payload["parameterization"]) == len(CLEAN_ENTRIES)


# --- the routing rule ---------------------------------------------------------


async def test_a_clean_rewrite_still_cannot_auto_land() -> None:
    """⚠ THE SHARPEST TEST IN THIS FILE. Every static check PASSES — this is a candidate a
    mined blueprint would auto-land on — and it is held at `in_review` with reason
    `hand_authored`, because the snapshot says a person's assistant wrote the query rather than
    a warehouse answering a user. That asymmetry is the whole §C.5 safety argument."""
    completer, store, env = await _completer()

    result = await completer.complete(
        env, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
    )

    assert result.outcome == "completed"
    assert result.sql_rewritten is True
    assert _static(result.envelope)["outcome"] == "ok"
    assert all(
        _static(result.envelope)[check] is True
        for check in (
            "explain_ok",
            "binds_to_subset_uses",
            "dag_ok",
            "read_only_select",
            "date_literal_ok",
        )
    )
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(result.envelope) == "hand_authored"
    assert (await store.get(CID)).status == CandidateStatus.IN_REVIEW
    # ...and the router says so for the AUTHORED reason specifically, not because something
    # else about the row failed.
    decision = route_candidate(result.envelope, sampled_for_inbox=False)
    assert (decision.status, decision.control, decision.reason) == (
        CandidateStatus.IN_REVIEW,
        "route_inbox",
        "hand_authored",
    )


# --- the checks a rewrite can newly fail --------------------------------------


async def test_a_frozen_run_date_in_the_rewrite_fails_the_date_check() -> None:
    """⚠ THE ONE CLASS A REWRITE CAN INTRODUCE THAT RE-ROLING NEVER COULD. A run date in the
    body makes the blueprint answer a DIFFERENT question every day it ages, silently — the D97
    wrong-answer class. Every literal predicate here HAS an entry, so the totality walk passes
    and the failure is unambiguously `date_literal_ok`, which is what routes it to a human."""
    completer, _store, env = await _completer()

    result = await completer.complete(
        env, entries=DATED_ENTRIES, replace_all=True, rewritten_sql=DATED_REWRITE
    )

    checks = _static(result.envelope)
    assert checks["date_literal_ok"] is False
    assert checks["outcome"] == "fail_to_review"
    assert result.envelope.status == CandidateStatus.IN_REVIEW
    assert derive_inbox_reason(result.envelope) == "fail_to_review"


async def test_a_select_star_rewrite_is_refused_before_it_is_snapshotted() -> None:
    """The check runs on the QUERY at the boundary, not only on the template derived from it.

    It used to be stored and stamped `read_only_select: false` — contained, since the completer
    parses SQL and never executes it, and the approve gate refuses on `static_not_ok`. But a
    stored accepted SQL is not inert: it is quoted back verbatim as "the query that ran" in the
    next revise brief and rendered on a review card. `check_read_only_select` is the SAME
    function the reviser runs before offering a rewrite, so the two halves cannot disagree about
    what this system will hold.

    THE STORE IS ASSERTED UNCHANGED: a guard that refused after `_rewritten_snapshot` had already
    replaced `sql_by_ref` would return the right error and still have done the damage.
    """
    completer, store, env = await _completer()
    before = (await store.get(CID)).to_doc()

    with pytest.raises(CompletionInputError) as exc:
        await completer.complete(
            env, entries=STAR_ENTRIES, replace_all=True, rewritten_sql=STAR_REWRITE
        )

    assert "SELECT *" in str(exc.value)
    assert (await store.get(CID)).to_doc() == before


async def test_the_write_boundary_repeats_every_guard_the_reviser_applies() -> None:
    """⚠ `revise` IS A TYPING AID, NOT A GATE. Anyone holding the reviewer token can post a
    `sql` straight to `complete`/`apply_revision` and skip the assistant entirely, so a check
    that lives only on the propose half protects the one caller that was never the risk.

    Three of them, each refusing a different cost: destroying a composite's per-node queries,
    an unbounded string in a persisted payload, and a statement that should never be quoted
    back to a model as "the query that ran".
    """
    completer, store, env = await _completer()
    before = (await store.get(CID)).to_doc()

    composite = replace(env, payload={**env.payload, "kind": "composite"})
    with pytest.raises(CompletionInputError, match="composite"):
        await completer.complete(
            composite, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
        )
    with pytest.raises(CompletionInputError, match="character limit"):
        await completer.complete(
            env,
            entries=CLEAN_ENTRIES,
            replace_all=True,
            rewritten_sql=CLEAN_REWRITE + " -- " + "x" * MAX_SQL_CHARS,
        )
    with pytest.raises(CompletionInputError, match="read-only SELECT"):
        await completer.complete(
            env,
            entries=CLEAN_ENTRIES,
            replace_all=True,
            rewritten_sql=f"DROP TABLE {_T}",
        )

    assert (await store.get(CID)).to_doc() == before


async def test_a_rewrite_whose_literals_are_unclassified_declines_on_the_totality_walk() -> None:
    """THE WALK IS STRICTER HERE THAN ANYWHERE, and this is the case that proves it: the SQL
    came from OUTSIDE the entries, so unlike a reconstruction the first walk can genuinely
    fail. The reviewer's own entries are re-checked against the query they just accepted."""
    completer, store, env = await _completer()

    result = await completer.complete(
        env,
        entries=[_inline("register_type", "EARN", "defines the metric earnings")],
        replace_all=True,
        rewritten_sql=CLEAN_REWRITE,
    )

    assert result.outcome == "declined"
    assert result.decline.reason == "totality_violation"
    assert result.sql_rewritten is True
    assert (await store.get(CID)).status == CandidateStatus.NEEDS_PARAMETERIZATION


async def test_a_declined_rewrite_still_persists_the_new_snapshot() -> None:
    """⚠ THE FORM AND ITS OWN ERROR MESSAGE MUST AGREE. The entries the reviewer left are
    written against the NEW query, so a row storing the OLD SQL beside them renders a complaint
    naming predicates of a query the card no longer shows — and the next revise call would
    propose against the wrong text. A declined mint draft stays on the queue carrying the
    model's SQL for exactly this reason."""
    completer, store, env = await _completer()

    await completer.complete(
        env,
        entries=[_inline("register_type", "EARN", "defines the metric earnings")],
        replace_all=True,
        rewritten_sql=CLEAN_REWRITE,
    )

    stored = await store.get(CID)
    assert stored.revalidation.sql_by_ref == {REWRITE_TOOL_CALL_REF: (CLEAN_REWRITE,)}
    assert stored.revalidation.authored is True
    assert stored.payload["sql_rewrite"]["by"] == "assistant"
    assert stored.decline is not None


# --- a declined row must not carry a generalization of something else ----------


async def test_a_declined_second_rewrite_drops_the_stale_generalization() -> None:
    """⚠ THE REVIEWER'S OWN PROBE, and the defect it found. Rewrite once cleanly (the row lands
    `in_review`, fully generalized); rewrite again with entries that do not cover the new query
    (it declines). The row then held the FIRST rewrite's `generalization` — its template, its
    `uses`, its `static_validation.outcome == "ok"` — beside the SECOND rewrite's snapshot.

    Three things went wrong at once, and the third is the sharp one:

      * the card renders `sql_template` from the payload, so it showed a query that is not the
        one on this row, under a badge announcing that the SQL had been rewritten;
      * `static_validation` said `ok` about a payload that had just been REJECTED;
      * `trial_run` binds the STORED template, so a reviewer testing their new query would have
        run the one it replaced — and watched it pass.

    `generalization` is DERIVED. A declined row has not been generalized from the payload it now
    holds, so the honest shape is un-generalized, which is what a `needs_parameterization` row
    looks like anyway. It comes back when the form completes.
    """
    completer, store, env = await _completer()

    first = await completer.complete(
        env, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
    )
    assert first.envelope.status == CandidateStatus.IN_REVIEW
    assert _static(first.envelope)["outcome"] == "ok"
    stale_template = first.envelope.payload["generalization"]["sql_template"]

    second_sql = CLEAN_REWRITE.replace("type_code = 'REG'", "type_code = 'OVT'")
    second = await completer.complete(
        await store.get(CID),
        # Deliberately short: `type_code` goes unclassified, so the totality walk declines.
        entries=[_inline("register_type", "EARN", "defines the metric earnings")],
        replace_all=True,
        rewritten_sql=second_sql,
    )

    assert second.outcome == "declined"
    stored = await store.get(CID)
    assert stored.payload.get("generalization") is None
    assert stale_template not in str(stored.payload)
    # ...and the snapshot is the NEW query, so the row is internally consistent: no template,
    # and the one accepted SQL its next attempt will be checked against.
    assert stored.revalidation.sql_by_ref == {REWRITE_TOOL_CALL_REF: (second_sql,)}
    assert stored.revalidation.authored is True
    assert stored.payload["sql_rewrite"]["by"] == "assistant"


async def test_an_inline_entity_in_a_declined_rewrite_is_caught_without_a_template() -> None:
    """The declined path scans what WOULD survive into the template, since the template it would
    normally be read through does not exist yet.

    An `inline` entry declares that a value stays frozen in the landed artifact for ever, so an
    entity there is the D17 case whether or not S4 has run. Catching it now means the verdict a
    declined row carries agrees with the one it gets a round later, instead of differing by an
    accident of which stages happened to have run — and `_leakage_cleared` reads that verdict
    before the assistant is allowed to quote this row's literals to a model.
    """
    completer, store, env = await _completer()

    result = await completer.complete(
        env,
        entries=[
            _inline("register_type", "EARN", "defines the metric earnings"),
            # An entity, FROZEN: it would ride into the global artifact verbatim.
            _inline("department_code", "Ana Beatriz Ferreira", "the named owner"),
        ],
        replace_all=True,
        rewritten_sql=CLEAN_REWRITE,
    )

    assert result.outcome == "declined"
    stored = await store.get(CID)
    assert stored.payload.get("generalization") is None  # no template to have been scanned
    assert stored.entity_scan["result"] != "pass"
    assert any(
        f.endswith(".locator.value") for f in stored.entity_scan["scanned_fields"]
    ), stored.entity_scan["scanned_fields"]


# --- the badge is an attestation, so only the server may make it ---------------


async def test_a_model_emitted_sql_rewrite_badge_never_reaches_the_store() -> None:
    """⚠ THE BADGE IS A CLAIM ABOUT PROVENANCE, so it may not come from the thing whose
    provenance it describes.

    `payload["sql_rewrite"]` renders as "the assistant rewrote this SQL" on a review card. If a
    MODEL could set it, a mined candidate would carry that claim while `authored=False` let the
    router auto-land it — the card and the routing decision disagreeing about one row, with the
    card telling the more alarming story and nobody stopping to check.

    So `BlueprintPayload` has no such field, the key does not survive `payload_to_doc()`, and the
    record is stamped afterwards from the `ValidationSnapshot` — the one structure a model cannot
    write to. Dropped rather than declined: the key is inert junk, not a fault the rest of a good
    blueprint should die for.
    """
    completer, store, env = await _completer()
    poisoned = replace(
        env,
        payload={**env.payload, "sql_rewrite": {"by": "assistant", "applied_at": "yesterday"}},
    )

    result = await completer.complete(
        poisoned,
        entries=[
            _inline("register_type", "EARN", "defines the metric earnings"),
            _slot("department_code", "0420"),
        ],
        replace_all=True,
    )

    assert result.sql_rewritten is False
    assert "sql_rewrite" not in result.envelope.payload
    assert "sql_rewrite" not in (await store.get(CID)).payload
    assert result.envelope.revalidation.authored is False


async def test_an_extractor_payload_carrying_the_badge_is_validated_without_it() -> None:
    """The same guarantee one level down, at the validator every mined candidate goes through.

    `to_candidate` is where model-authored JSON becomes a typed payload, and it is the choke
    point: a key it does not model cannot reach any store on any path, not just this one.
    """
    from data_agent.learning.extractor.validation import to_candidate

    raw = blueprint_raw(parameterization=[], source_refs=("tc1",))
    raw["payload"]["sql_rewrite"] = {
        "by": "assistant",
        "applied_at": "2020-01-01T00:00:00+00:00",
        "previous_sql_sha256": "deadbeef",
    }
    summary = make_summary(
        tool_calls=(make_tool_call(ref="tc1", sql="SELECT 1 AS n"),), content_hash="h"
    )

    outcome = to_candidate(raw, summary, known_rules=frozenset())

    # It validates on its merits (or declines on them) — but either way the claim is gone.
    if not isinstance(outcome, Decline):
        assert "sql_rewrite" not in outcome.payload_to_doc()


async def test_the_badge_survives_an_ordinary_edit_of_an_already_rewritten_row() -> None:
    """⚠ THE COMPLEMENT, and the reason the record is CARRIED rather than merely stamped.

    A rewritten candidate is still editable: re-roling one literal on it goes through
    `to_candidate`, whose payload is a closed key set. A badge that vanished on the second edit
    would leave the card silent about a query the snapshot still says nobody's session ran —
    the same disagreement as the poisoned case, arrived at from the other direction.

    Carried from the SNAPSHOT (`authored` + the `rewrite0` ref), never from the payload text,
    so "keep the badge" cannot be spoofed by the same route "add a badge" was.
    """
    completer, store, env = await _completer()
    first = await completer.complete(
        env, entries=CLEAN_ENTRIES, replace_all=True, rewritten_sql=CLEAN_REWRITE
    )
    badge = first.envelope.payload["sql_rewrite"]

    second = await completer.complete(
        await store.get(CID),
        entries=[
            _slot("register_type", "EARN"),  # re-roled: was inline
            _slot("department_code", "0420"),
            _inline("type_code", "REG", "regular pay only"),
        ],
        replace_all=True,
    )

    assert second.sql_rewritten is False  # this attempt changed no SQL...
    assert second.envelope.payload["sql_rewrite"] == badge  # ...but the row is still rewritten
    assert (await store.get(CID)).revalidation.authored is True


# --- the HTTP surface ---------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


async def _client(env=None):
    store = InMemoryCandidateStore()
    await store.put(env if env is not None else _declined_envelope())
    inbox = ReviewInbox(
        store,
        completer=ParameterizationCompleter(
            store=store, known_rules=frozenset(), stages=_stages(store)
        ),
    )
    return TestClient(create_inbox_app(inbox=inbox, write_plane="offline")), store


async def test_complete_accepts_a_rewrite_and_reports_it(enabled: None) -> None:
    """The contract the page is built against: `sql` in, `sql_rewritten` out. Reported off the
    RESULT rather than derived from the payload, because a candidate rewritten yesterday carries
    the same badge as one rewritten just now."""
    client, store = await _client()

    resp = client.post(
        f"/inbox/{CID}/complete",
        json={"entries": CLEAN_ENTRIES, "sql": CLEAN_REWRITE},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["sql_rewritten"] is True
    assert body["status"] == CandidateStatus.IN_REVIEW
    assert (await store.get(CID)).revalidation.authored is True


async def test_complete_without_sql_reports_no_rewrite(enabled: None) -> None:
    """The field is ADDITIVE: a client that never sends it gets exactly the old behaviour, and
    the new key answers `false` rather than being absent."""
    client, _ = await _client()
    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [
                _inline("register_type", "EARN", "defines the metric earnings"),
                _slot("department_code", "0420"),
            ],
            "replace": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sql_rewritten"] is False


async def test_apply_revision_accepts_a_rewrite_on_the_review_queue(enabled: None) -> None:
    """`apply_revision` needs no forcing — it is already replace-only, which is exactly what a
    rewrite requires. What changes is the SUBJECT of the re-adjudication."""
    env = replace(_declined_envelope(), status=CandidateStatus.IN_REVIEW, decline=None)
    client, store = await _client(env)

    resp = client.post(
        f"/inbox/{CID}/apply_revision",
        json={"entries": CLEAN_ENTRIES, "sql": CLEAN_REWRITE},
        headers=AUTH,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sql_rewritten"] is True
    stored = await store.get(CID)
    assert stored.status == CandidateStatus.IN_REVIEW
    assert stored.revalidation.sql_by_ref == {REWRITE_TOOL_CALL_REF: (CLEAN_REWRITE,)}


async def test_a_still_declining_rewrite_is_a_200_with_the_fresh_reason(enabled: None) -> None:
    """Unchanged posture: the reviewer sent a well-formed attempt, and the pipeline's answer is
    the RESULT they need to make the next one."""
    client, _ = await _client()
    resp = client.post(
        f"/inbox/{CID}/complete",
        json={
            "entries": [_inline("register_type", "EARN", "defines the metric earnings")],
            "sql": CLEAN_REWRITE,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "declined"
    assert resp.json()["sql_rewritten"] is True
    assert resp.json()["decline"]["reason"] == "totality_violation"
