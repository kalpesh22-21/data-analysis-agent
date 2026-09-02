"""ParameterizationCompleter — the human half of fail-to-review.

A `needs_parameterization` candidate is a form with holes in it: the judge said the work was
worth extracting, and the model could not classify every literal predicate of the accepted
SQL. A reviewer supplies the missing entries and the candidate re-enters the pipeline it fell
out of.

NO BYPASS, and that is the whole design: the completed payload runs the SAME `to_candidate`
validation the corrective turn ran — totality walk included — and then the SAME write-router
stages any extracted candidate runs. The one thing a human is trusted with is CONTENT, never
the checks. NO SESSION STORE either: everything the re-validation reads was snapshotted onto
the envelope at decline time, so a review item does not quietly stop being completable when
the session's TTL expires. FAILURE IS A RESULT, not an exception — a still-incomplete form is
the expected outcome, and exceptions are reserved for operations that could not be attempted
at all.

§C.5 — A REWRITE REPLACES THE ACCEPTED SQL, AND MAKES THE CANDIDATE HAND-AUTHORED. `complete`
takes an optional `rewritten_sql`. When it differs from the SQL the snapshot holds, this builds a
NEW `ValidationSnapshot` around it — one ref, one citation, `authored=True` — and everything
downstream runs against that. This is `learning/mint`'s shape and deliberately so: it is the same
object (an accepted SQL nobody observed a session produce), so it gets the same treatment. The
`authored` flag is what `writer/routing.py::_is_authored` reads, which forces `in_review`; the
rewritten candidate can never auto-land.

TWO THINGS THE REWRITE PATH OWES that the ordinary one does not. It REQUIRES `replace_all` —
every existing entry describes a query that no longer exists, and appending to it produces a
parameterization half about a string nobody has. And it PERSISTS THE NEW SNAPSHOT ON BOTH
OUTCOMES, including a decline: the form a reviewer sees next must show the SQL its entries are
checked against, exactly as a declined mint draft stays on the queue carrying the model's query.
Storing the old SQL beside the new entries would make the next attempt unreadable.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from ..candidate.decline import (
    QUOTE_WITHHELD,
    DeclineBlock,
    EvidencePointer,
    ValidationSnapshot,
    last_sql,
)
from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..candidate.store import CandidateStore
from ..candidate.verdicts import LeakageVerdict
from ..extractor.grounding import RuleIndex
from ..extractor.models import Decline, ExtractedCandidate
from ..extractor.validation import to_candidate
from ..generalize.validate import check_read_only_select
from ..leakage import settle_entity_scan
from ..revise.schema import COMPOSITE_REWRITE_REASON, max_rewrite_sql_chars
from ..stage import CandidateStage, StageContext, run_pipeline
from ..summary.models import SessionSummary
from ..triage import TriageVerdict

_logger = logging.getLogger(__name__)

# The triage verdict the re-run stages are given. NOTHING in the pipeline reads
# `ctx.verdict` today (the stages read `ctx.summary` only), so this is documentation
# with a type: it says, in the one place a future stage would look, how this candidate
# got here. `keep` because the original triage kept it — a completion never revisits
# that decision.
COMPLETION_VERDICT = TriageVerdict(
    decision="keep",
    reason="human_completed_parameterization",
    target_hints=("blueprint",),
)


class CompletionUnavailableError(RuntimeError):
    """The completion could not be ATTEMPTED — no validation plane wired, or the
    candidate carries no re-validation snapshot. Distinct from a re-validation that ran
    and declined, which is an ordinary result."""


class CompletionInputError(ValueError):
    """The reviewer-supplied entries are not a parameterization array at all."""


class CompletionRaceError(RuntimeError):
    """The candidate stopped being a form while this completion was running.

    Its own type because the CALLER must be able to tell it from a wrong-status request: the
    request was legal when it arrived, so the honest answer is "somebody moved it". Both map to
    409; only this one means the reviewer should re-read the row before deciding anything.
    """


@dataclass(frozen=True)
class CompletionResult:
    """What one completion attempt did.

    `outcome="declined"` carries the FRESH decline — subject to the same withholding rule as the
    wire projection — and the envelope is still `needs_parameterization`. `outcome="completed"`
    carries the envelope as the pipeline left it, which may be `candidate` or `in_review`; this
    path asserts nothing about which, because that is the router's decision.
    """

    outcome: Literal["completed", "declined"]
    envelope: CandidateEnvelope
    decline: DeclineBlock | None = None
    # ⚠ Whether THIS attempt replaced the accepted SQL (§C.5). On the result rather than derived
    # by the caller from the payload, because the caller cannot tell: a candidate that was
    # rewritten yesterday carries the same `payload["sql_rewrite"]` record as one rewritten just
    # now, and the surface's job is to say what the reviewer just did.
    sql_rewritten: bool = False


def _merged_parameterization(
    payload: dict[str, Any], entries: list[Any], *, replace_all: bool
) -> list[Any]:
    """The parameterization array the re-validation will walk.

    APPEND by default, REPLACE on request, because they answer two different reviewer tasks:
    `totality_violation` means entries are MISSING, so appending leaves the model's already-valid
    classifications untouched, while `rule_predicate_mismatch` means an entry is WRONG, which no
    amount of appending fixes. Neither mode is trusted — whatever comes out goes through the same
    readers and the same D97 totality walk as model output.
    """
    if replace_all:
        return list(entries)
    existing = payload.get("parameterization")
    return [*(existing if isinstance(existing, list) else []), *entries]


# The single tool-call ref a rewritten candidate's SQL lives under, and the citation that points
# at it. ONE, because a rewrite is one query — mint uses one per node for the same reason, and
# for a single blueprint that is also one. The name says where the SQL came from, which is the
# whole content of the citation: there is no session turn behind it.
REWRITE_TOOL_CALL_REF = "rewrite0"


def _raw_candidate(
    env: CandidateEnvelope, payload: dict[str, Any], snapshot: ValidationSnapshot
) -> dict[str, Any]:
    """Rebuild the raw candidate envelope `to_candidate` reads.

    The evidence QUOTES are not the candidate store's to hold (D51), so each citation is rebuilt
    from its snapshotted pointer with an explicit marker in the quote's place — satisfying D31's
    structural gate honestly, since the citations are the model's own and the reviewer supplies
    none, while keeping the entity-bearing text where it belongs.

    ⚠ THE SNAPSHOT IS PASSED IN, not read off *env*, and that is load-bearing for §C.5: on a
    rewrite the snapshot being validated against is the NEW one, which is not on the envelope
    yet. Reading `env.revalidation` here would cite the old refs while the summary offered the
    new SQL under a different one, and the candidate would decline `no_evidence` — a message
    about citations, on a form whose actual change was the query.
    """
    return {
        "type": env.type,
        "confidence": env.confidence,
        "evidence": [
            {
                "turn_ref": pointer.turn_ref,
                "tool_call_ref": pointer.tool_call_ref,
                "quote": QUOTE_WITHHELD,
            }
            for pointer in snapshot.evidence
        ],
        "rationale": env.extractor_rationale,
        "proposed_action": env.proposed_action,
        "entity_self_check": _entity_self_check(env),
        "depends_on": list(env.depends_on),
        "payload": payload,
    }


def _entity_self_check(env: CandidateEnvelope) -> dict[str, Any]:
    """Rebuild the candidate's entity attestation from wherever it now lives.

    TWO SOURCES, because the field MOVES: `build_declined_envelope` seeds `entity_scan` with the
    model's own self-check, and the S5 gate then overwrites that whole doc with its settled
    `LeakageVerdict`, which has no such key — so reading only the key returned `False` for every
    scanned row. The SETTLED verdict wins where it exists (a machine that looked beats a model
    that said it looked), with the self-check as the fallback for a row nobody scanned. Advisory
    either way, but it must not ASSERT the opposite of what is known.
    """
    scan = env.entity_scan if isinstance(env.entity_scan, dict) else {}
    if LeakageVerdict.is_settled(scan):
        # `found` stays EMPTY on purpose: the verdict's hits carry the raw `span` — the
        # entity value itself — and feeding those back into a candidate payload is the
        # one thing D17 forbids everywhere else in this file.
        return {"contains_entities": scan.get("result") != "pass", "found": []}
    return {
        "contains_entities": bool(scan.get("self_check_contains_entities", False)),
        "found": [],
    }


def sql_rewrite_of(env: CandidateEnvelope, rewritten_sql: str | None) -> str:
    """The submitted query when it is a REWRITE, else `""`.

    THREE THINGS COUNT AS "NOT A REWRITE" and they are collapsed on purpose, because the surface
    that sends this cannot always tell them apart: the field was absent, it was sent empty, or it
    was sent carrying the query already on the candidate. The last is the one that matters — a
    form that round-trips the SQL it displayed must not turn every ordinary completion into a
    hand-authored candidate that can no longer auto-land.

    Compared against `last_sql`, which is the SAME function the reviser showed the model and the
    inbox uses to force `replace`. One answer to "which query is this candidate about", in one
    place; three copies would let a request be a rewrite on one path and a no-op on another.
    """
    submitted = (rewritten_sql or "").strip()
    if not submitted or env.revalidation is None:
        # NO SNAPSHOT ⇒ NOT A REWRITE, rather than an assertion, because this is also read by
        # `ReviewInbox` BEFORE the completer's own precondition runs. The honest failure for a
        # candidate with no snapshot is `CompletionUnavailableError` → 503, which the completer
        # raises a moment later; throwing a different error from a helper deciding a boolean
        # would replace that 503 with a 500 about an assertion.
        return ""
    return "" if submitted == last_sql(env.revalidation.sql_by_ref).strip() else submitted


def _refuse_composite_rewrite(env: CandidateEnvelope) -> None:
    """A composite blueprint has one accepted query PER NODE. Refuse, do not replace.

    ⚠ THE FAILURE THIS PREVENTS IS DATA LOSS, not a bad landing. `_rewritten_snapshot` REPLACES
    the whole `sql_by_ref` map with a single `{"rewrite0": (sql,)}`, so a two-node composite
    whose snapshot held `{"tc1": ..., "tc2": ...}` comes out holding one ref and one query while
    `payload["kind"]` still says `composite` and `payload["composes"]` still describes two steps.
    BOTH nodes' accepted SQL is gone — recorded only as a sha256 of one of them — and the row can
    never be completed correctly again. `dag_ok` then fails, so it does not land; it is simply
    destroyed.

    The reviser refuses the same shape before the model call, sharing the REASON verbatim
    (`COMPOSITE_REWRITE_REASON`) so neither copy can drift into describing a different
    limitation — but each keeps its own next step, because the reviser is answering a ticked
    checkbox and this path may be answering a stale form with no checkbox on it. The check lives
    in both places because the reviser writes nothing: this is the copy that matters, on the door
    a client posting `sql` straight to `complete`/`apply_revision` uses.
    """
    if env.payload.get("kind") == "composite":
        raise CompletionInputError(
            f"{COMPOSITE_REWRITE_REASON}. Revise the roles only, or re-mint the blueprint"
        )


def _refuse_oversized_rewrite(sql: str) -> None:
    """The SAME cap the model boundary applies, applied to the client boundary too.

    ⚠ `learning/mint` — the plane §C.5 borrows its whole safety argument from — caps a
    hand-authored query at BOTH boundaries: `mint/schema.py` rejects an oversized model response
    and `MintRequest.__post_init__` rejects an oversized human submission. The rewrite path had
    only the first half, and the gap is not theoretical: a legitimate SELECT with a 20 001-byte
    comment welded on passes every static check and is stored as a clean `hand_authored`
    candidate ready to approve. That is an unbounded string in a persisted payload, a review card
    and every later prompt that quotes the accepted SQL.

    REFUSED, never truncated, for `_rewritten_sql`'s reason: a truncated query is a DIFFERENT
    query that might still parse, which is the one way this path could hand a reviewer something
    to approve that nobody wrote. The cap is IMPORTED from the same place the model boundary
    imports it, so the two can never drift.
    """
    cap = max_rewrite_sql_chars()
    if len(sql) > cap:
        raise CompletionInputError(
            f"the rewritten SQL is {len(sql)} characters, over the {cap}-character limit a "
            "hand-authored query has; shorten it (a pasted document is not a blueprint)"
        )


def _refuse_non_select_rewrite(sql: str) -> None:
    """The accepted SQL itself must be a single read-only SELECT, not only the template from it.

    ⚠ WHAT THE WRITE PATH USED TO CHECK WAS THE DERIVED TEMPLATE, NEVER THE QUERY. So a client
    posting `DROP TABLE ...` got it PERSISTED into `revalidation.sql_by_ref`. Containment held —
    the completer parses SQL and never executes it, `read_only_select` then fails, and the
    approve gate refuses on `static_not_ok` — but containment is not the whole cost: that string
    becomes "THE ACCEPTED SQL (the query that ran; every literal in it needs a role)" in the very
    next revise brief, quoted verbatim to a model, and it renders on a review card.

    So the query is adjudicated by `check_read_only_select` — the SAME function the reviser runs
    before offering a rewrite and the same one `decide_outcome` runs over the derived template
    afterwards. One definition of "a query this system will hold", checked wherever one arrives.
    """
    if not check_read_only_select(sql):
        raise CompletionInputError(
            "the rewritten SQL is not a single read-only SELECT this system can parse — no "
            "DDL, no DML, no multiple statements, no SELECT *. It was refused rather than "
            "stored, because the accepted SQL is quoted back to the assistant verbatim"
        )


def _inline_literal_fields(payload: dict[str, Any]) -> dict[str, str]:
    """The `role="inline"` locator values, as scannable fields — the declined path's stand-in
    for the template the gate would normally read.

    ⚠ WHY THIS SUBSET AND NOTHING WIDER. On the success path the gate scans
    `generalization.sql_template`, in which every `slot` literal has already been replaced by a
    token and only the `inline` ones remain — because those are precisely the values that survive
    verbatim into the landed global artifact. A DECLINED row has no template (the stages never
    ran), so that surface does not exist and the re-scan settles a verdict having read none of
    the literals.

    An `inline` entry is a reviewer or a model DECLARING "this value stays frozen in the template
    for ever". That declaration is knowable from the payload alone, before any template exists,
    and it is exactly the D17 case. So this scans those values and only those values: the
    verdict a declined row gets now AGREES with the one the same row gets a round later when it
    completes, instead of differing by an accident of which stages happened to have run.

    ⚠ SCANNING MORE WAS TRIED AND REVERTED. The obvious wider surface is the accepted SQL itself,
    and it breaks the loop this feature exists for: raw SQL still carries every SLOT literal —
    a department code, a region, a year — so it quarantines essentially every declined form,
    which WITHHOLDS the decline detail naming the fix and LOCKS the assistant on exactly the
    queue whose purpose is getting help with that row
    (`test_a_declined_rewrite_becomes_the_sql_the_next_revise_proposes_against` shows it
    directly). Deriving the template first is impossible here, precisely because the
    parameterization is incomplete — which is why the row declined.

    THE RESIDUAL, stated rather than implied: a literal the reviewer has not yet classified, and
    an assistant-REWRITTEN query's un-adjudicated text, are still unscanned until the form
    completes. That is the same exposure every mined candidate has had, and it closes the moment
    the template exists.

    Named `parameterization.<i>.locator.value` so a hit reads as what it is — `field` is the half
    of a hit a reviewer still sees when the `span` is withheld.
    """
    entries = payload.get("parameterization")
    if not isinstance(entries, list):
        return {}
    fields: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("role") != "inline":
            continue
        locator = entry.get("locator")
        value = locator.get("value") if isinstance(locator, dict) else None
        if isinstance(value, str) and value:
            fields[f"parameterization.{index}.locator.value"] = value
    return fields


def _stamped(
    payload: dict[str, Any], rewrite_record: dict[str, str] | None
) -> dict[str, Any]:
    """*payload* carrying the §C.5 rewrite badge, or with any inbound one REMOVED.

    ⚠ THE `else` BRANCH IS THE SECURITY-RELEVANT HALF. `sql_rewrite` is an attestation about
    provenance, and this is the one place it may be written, so anything already sitting under
    that key arrived from somewhere not entitled to make the claim. The reachable source is a
    model-authored EXTRACTOR payload: `to_candidate` drops the key on the way through, but a
    declined candidate's payload is stored raw, so a mined row could carry one into the store and
    render "the assistant rewrote this SQL" on its card while `authored=False` still let the
    router auto-land it — a claim and a routing decision disagreeing about one row.

    NOT a client's `entries` merge, which was the first version of this sentence and was wrong:
    `_merged_parameterization` only ever writes `payload["parameterization"]`, so no request body
    reaches this key. Worth stating, because "the request cannot set it" is the kind of claim
    that quietly stops being true when a merge grows a second field.
    """
    out = dict(payload)
    if rewrite_record is not None:
        out["sql_rewrite"] = dict(rewrite_record)
    else:
        out.pop("sql_rewrite", None)
    return out


def _carried_rewrite_record(env: CandidateEnvelope) -> dict[str, str] | None:
    """The rewrite badge an ALREADY-REWRITTEN candidate keeps across an ordinary edit.

    ⚠ GROUNDED IN THE SNAPSHOT, NOT IN THE PAYLOAD, and that is the whole point of the function.
    `payload["sql_rewrite"]` is an attestation about PROVENANCE, so taking it from the payload
    would mean believing whatever the payload says about where its own SQL came from — and an
    extractor payload is model-authored. A mined candidate that emitted a `sql_rewrite` object
    would render "the assistant rewrote this" on a card while `authored=False` let it auto-land:
    a claim and a routing decision disagreeing about the same row.

    The snapshot cannot be spoofed that way — it is written by this module, and only
    `_rewritten_snapshot` puts `rewrite0` in it. So the badge is derived from the two facts that
    made it true (`authored`, and the rewrite ref) rather than carried as text.

    It must be CARRIED at all because a rewritten candidate is still editable: re-roling a
    literal on it goes through `to_candidate`, whose `BlueprintPayload` is a closed key set, and
    a badge that vanished on the second edit would leave the card silent about a query the
    snapshot still says nobody's session ran.
    """
    snapshot = env.revalidation
    if snapshot is None or not snapshot.authored:
        return None
    if REWRITE_TOOL_CALL_REF not in snapshot.sql_by_ref:
        # AUTHORED BUT NOT REWRITTEN — a MINTED blueprint. Its SQL is hand-authored too, and it
        # has its own provenance story; it is not "the assistant rewrote the session's query".
        return None
    existing = env.payload.get("sql_rewrite")
    return dict(existing) if isinstance(existing, dict) else None


def _rewritten_snapshot(before: ValidationSnapshot, sql: str) -> ValidationSnapshot:
    """The snapshot a rewritten candidate is validated against — `learning/mint`'s shape.

    WHAT IS REPLACED: `sql_by_ref` (one ref, the new query), `evidence` (one citation pointing at
    it — the only evidence assistant-authored SQL has, and an honest account of it) and
    `authored`, which is what `writer/routing.py::_is_authored` reads to force `in_review`.

    WHAT IS CARRIED, and why each: `session_id`/`user_id`/`trace_id` still describe the session
    this candidate CAME FROM, which a rewrite does not alter and an audit grouping by it still
    needs. `content_hash` is the row's identity, unchanged for the same reason the candidate_id
    is — this is the same review item, edited. `accepted_signal` is KEPT AS IT WAS: the
    `AcceptedSignal` literal domain is not widened for this (mint keeps `explicit_confirm` for
    the identical reason), because the signal records what the SESSION did, and inventing a
    "rewritten" member would make every reader of that field learn a new case to describe
    something they can already see on `authored`.
    """
    return replace(
        before,
        sql_by_ref={REWRITE_TOOL_CALL_REF: (sql,)},
        evidence=(EvidencePointer(turn_ref=0, tool_call_ref=REWRITE_TOOL_CALL_REF),),
        authored=True,
        # A rewrite is the OPPOSITE of a reconstruction and the two must not both be true: a
        # reconstruction's SQL is derived from the entries being checked (so its walk is
        # circular), while this SQL came from outside them entirely. A candidate that was
        # backfilled and is now rewritten is, from here on, an authored one.
        reconstructed=False,
    )


def _sql_rewrite_record(previous_sql: str) -> dict[str, str]:
    """The durable badge on the payload: this candidate's SQL is not the session's.

    ⚠ THE PREVIOUS QUERY IS RECORDED AS A DIGEST, NOT AS TEXT. The card renders `payload_view`,
    which is redacted but still reaches a browser, and the old query is entity-BEARING — it is
    the accepted SQL with its literals in it. A hash answers the question this record exists for
    ("is the query on this card still the one the session ran, and which one was it before?")
    without putting a second copy of the literals anywhere. It is also stable enough to match a
    row against a trace after the fact, which text pasted into a payload never is.
    """
    return {
        "by": "assistant",
        "applied_at": datetime.now(UTC).isoformat(),
        "previous_sql_sha256": hashlib.sha256(previous_sql.encode("utf-8")).hexdigest(),
    }


async def guarded_put(
    store: CandidateStore,
    before: CandidateEnvelope,
    updated: CandidateEnvelope,
    *,
    operation: str = "completion",
    expect_absent: bool = False,
) -> None:
    """Persist *updated*, unless the row moved out from under us while we were working.

    BEST-EFFORT, and the honest name for it is a NARROWED window rather than a closed one:
    `CandidateStore` has no compare-and-swap, so between this re-read and the `put` a concurrent
    reject can still be overwritten. What it closes is the WIDE window — the whole re-validation,
    which parses SQL, rewrites a template, scans for entities and possibly embeds. The direction
    of the failure decides the posture: silently resurrecting a REJECTED candidate re-enters work
    a human deliberately removed (D29), while refusing a write that raced costs one retry
    against a row the reviewer is about to re-read anyway.

    MODULE-LEVEL because there is now a SECOND long write path into a candidate — the knowledge
    editor (`inbox/knowledge_edit.py`), which validates, scans and stamps before it writes. Two
    copies of this rule would be two answers to "may I still write", and the one that drifted
    would be the one nobody was testing. *operation* names the caller in the message and nothing
    else; the guard itself is identical, which is the point.

    ⚠ *expect_absent* IS THE SAME GUARD FOR A ROW THAT DOES NOT EXIST YET, and it needed saying
    explicitly rather than falling out of the status compare. The default reads a missing row as
    a race — correctly, because a completion's row was there a moment ago and somebody deleted
    it. But the user-knowledge promote path CREATES its row at a deterministic id, so its
    precondition is "still absent", and running it through the default guard would make the
    FIRST press a 409 every time. One function, two preconditions, both stated: what must never
    exist is a caller that writes with no precondition at all, because that is the double-press
    that files two review rows for one fact.
    """
    current = await store.get(before.candidate_id)
    if expect_absent:
        if current is not None:
            raise CompletionRaceError(
                f"candidate {before.candidate_id!r} was created while the {operation} was "
                f"running (now {current.status!r}); nothing was written — re-read the row "
                "before deciding"
            )
        await store.put(updated)
        return
    if current is None or current.status != before.status:
        raise CompletionRaceError(
            f"candidate {before.candidate_id!r} changed while the {operation} was "
            f"running (was {before.status!r}, now "
            f"{current.status if current is not None else 'absent'!r}); nothing was "
            "written — re-read the row before deciding"
        )
    await store.put(updated)


@dataclass(frozen=True)
class ParameterizationCompleter:
    """Re-validate a human-completed form and put it back through the pipeline.

    `known_rules`/`rule_index` MUST come from the same catalog the extractor was grounded
    against: a completer holding a different one would accept rule ids the extractor could not,
    or decline ones it would have taken. `stages` EMPTY is a legal but degraded wiring — the
    candidate re-validates and is persisted at `extracted` with no generalization and an
    unsettled scan, which every downstream guard refuses, so it can never be approved. Legal
    because an offline dev inbox has no write plane; logged, because it is not obvious from the
    200 the reviewer gets.
    """

    store: CandidateStore
    known_rules: frozenset[str] = frozenset()
    rule_index: RuleIndex | None = None
    stages: tuple[CandidateStage, ...] = field(default_factory=tuple)

    async def complete(
        self,
        env: CandidateEnvelope,
        *,
        entries: list[Any],
        replace_all: bool = False,
        rewritten_sql: str | None = None,
    ) -> CompletionResult:
        """Re-validate this form. `rewritten_sql` REPLACES the accepted SQL (§C.5).

        A rewrite is not a third mode of the merge — it is a different SUBJECT. Everything after
        it (the totality walk, the AST rewrite, `check_read_only_select`,
        `check_no_frozen_date_literal`, the leakage scan, dedup) runs against the new query, and
        the snapshot carrying it is stamped `authored=True` so the router can never auto-land it.
        """
        if env.revalidation is None:
            raise CompletionUnavailableError(
                f"completion_unavailable: candidate {env.candidate_id!r} carries no "
                "re-validation snapshot, so the completed form cannot be checked "
                "against the accepted SQL it must cover"
            )
        if not isinstance(entries, list) or not all(
            isinstance(item, dict) for item in entries
        ):
            raise CompletionInputError(
                "entries must be an ARRAY of parameterization objects, each with "
                '"locator", "role" and the field that role requires'
            )

        snapshot = env.revalidation
        rewrite = sql_rewrite_of(env, rewritten_sql)
        payload = dict(env.payload)
        # CARRIED FROM THE SNAPSHOT, NEVER FROM THE PAYLOAD IT ARRIVED IN — see
        # `_carried_rewrite_record`. Recomputed below when THIS attempt is the rewrite.
        rewrite_record = _carried_rewrite_record(env)
        if rewrite:
            # ⚠ EVERY GUARD THE REVISER APPLIES BEFORE OFFERING A REWRITE IS REPEATED HERE,
            # because `revise` only PROPOSES and this is the half that WRITES. Anyone holding
            # the reviewer token can post a `sql` straight to `complete`/`apply_revision` and
            # skip the assistant entirely — the reviser is a typing aid, not a gate — so a check
            # that lives only there protects the one caller that was never the risk.
            #
            # They are stated in the order of what they cost: destroying data, unbounded
            # storage, then storing a statement nothing should quote back to a model.
            _refuse_composite_rewrite(env)
            _refuse_oversized_rewrite(rewrite)
            _refuse_non_select_rewrite(rewrite)
            if not replace_all:
                # NAMED, not merely refused. The reviewer's next action is one checkbox, and a
                # message that only says "invalid" sends them looking through their entries for
                # a fault that is not there.
                raise CompletionInputError(
                    "a SQL rewrite REPLACES the accepted query, so the parameterization must "
                    "be replaced with it — re-send with replace=true and a complete set of "
                    "entries for the NEW query; every existing entry describes the old one"
                )
            snapshot = _rewritten_snapshot(snapshot, rewrite)
            payload["source_tool_call_refs"] = [REWRITE_TOOL_CALL_REF]
            rewrite_record = _sql_rewrite_record(last_sql(env.revalidation.sql_by_ref))

        summary = snapshot.to_summary()
        payload["parameterization"] = _merged_parameterization(
            payload, entries, replace_all=replace_all
        )
        outcome = to_candidate(
            _raw_candidate(env, payload, snapshot),
            summary,
            known_rules=self.known_rules,
            rule_index=self.rule_index,
        )
        if isinstance(outcome, Decline):
            return await self._still_declined(
                env,
                payload,
                outcome,
                summary,
                snapshot,
                rewritten=bool(rewrite),
                rewrite_record=rewrite_record,
            )
        return await self._completed(
            env,
            outcome,
            summary,
            snapshot,
            rewritten=bool(rewrite),
            rewrite_record=rewrite_record,
        )

    async def _still_declined(
        self,
        env: CandidateEnvelope,
        payload: dict[str, Any],
        decline: Decline,
        summary: SessionSummary,
        snapshot: ValidationSnapshot,
        *,
        rewritten: bool = False,
        rewrite_record: dict[str, str] | None = None,
    ) -> CompletionResult:
        """The form is still not complete: keep the row, keep the reviewer's work.

        The MERGED payload is persisted even though it failed, so the next attempt starts from what
        the reviewer already wrote rather than from the model's original. The correction COUNT is
        preserved from the original block: it records what the MODEL was asked, and a human's attempt
        is not a corrective turn.

        THE SCAN IS RE-SETTLED, because the payload CHANGED. The stored verdict was settled about
        different content, and it is exactly what the wire projection consults before showing the
        decline detail to a browser. For today's shapes the re-scan usually returns the same verdict
        (a completion merges `parameterization`, which the gate deliberately does not scan); what
        changes is that the verdict is MEASURED against what is being stored. With no gate wired it
        goes back to `pending`, which fails closed everywhere.
        """
        block = DeclineBlock(
            reason=decline.reason,
            detail=decline.detail,
            corrections_attempted=(
                env.decline.corrections_attempted if env.decline is not None else 0
            ),
            correction_history=(
                env.decline.correction_history if env.decline is not None else ()
            ),
        )
        # ⚠ THE SNAPSHOT IS PERSISTED EVEN THOUGH THE ATTEMPT FAILED, for the same reason the
        # merged payload is: the next attempt must start from what the reviewer left, and the
        # entries they left are written against the REWRITTEN query. Storing the old SQL beside
        # them would put the form and its own error message in disagreement — the complaint
        # would name predicates of a query the card no longer shows. A declined mint draft stays
        # on the queue carrying the model's SQL for exactly this reason.
        # STAMPED HERE, AFTER validation rather than before it — see `_stamped`. On this path
        # the payload is stored as-is (no `to_candidate` round trip), so the record is written
        # onto the dict about to be persisted.
        #
        # ⚠ AND THE OLD `generalization` IS DROPPED, which matters most for a rewrite. It is a
        # DERIVED block — template, `uses`, `canonical_ast_norm`, `static_validation` — computed
        # by S4 from a DIFFERENT payload and, on a rewritten row, from a DIFFERENT accepted SQL.
        # Keeping it beside the new snapshot produced a card showing a stale `sql_template` under
        # a "SQL rewritten" badge, with `static_validation.outcome == "ok"` from the previous
        # round, and — the sharp end — `trial_run` binds the STORED template, so a reviewer
        # testing their rewrite would have run the query it replaced and seen it pass.
        #
        # Unconditional rather than rewrite-only: a declined row has not been generalized FROM
        # THE PAYLOAD IT NOW HOLDS, whatever changed. The honest shape is un-generalized, which
        # is what a `needs_parameterization` row looks like anyway, and every reader already
        # handles an absent block (`_template_parts` renders nothing, `_static_outcome` returns
        # None, `trial_run` answers `no_template`). It comes back when the form completes.
        payload.pop("generalization", None)
        updated = replace(
            env,
            payload=_stamped(payload, rewrite_record),
            decline=block,
            revalidation=snapshot,
        )
        updated = replace(
            updated,
            entity_scan=await settle_entity_scan(
                self.stages,
                updated,
                StageContext(summary=summary, verdict=COMPLETION_VERDICT),
                # ⚠ THE TEMPLATE THE GATE NORMALLY READS DOES NOT EXIST ON THIS PATH, and the
                # `generalization` block was just dropped above. The values a completed row
                # would be scanned through are its `inline` literals; they are knowable now,
                # so they are scanned now — see `_inline_literal_fields`.
                extra_fields=_inline_literal_fields(updated.payload),
            ),
        )
        await self._guarded_put(env, updated)
        _logger.info(
            "learning inbox: completion of %s still declines %s — the candidate stays "
            "at %s with the fresh decline recorded",
            env.candidate_id, decline.reason, CandidateStatus.NEEDS_PARAMETERIZATION,
        )
        return CompletionResult(
            outcome="declined", envelope=updated, decline=block, sql_rewritten=rewritten
        )

    async def _completed(
        self,
        env: CandidateEnvelope,
        candidate: ExtractedCandidate,
        summary: SessionSummary,
        snapshot: ValidationSnapshot,
        *,
        rewritten: bool = False,
        rewrite_record: dict[str, str] | None = None,
    ) -> CompletionResult:
        """Re-validation passed: rebuild an ORDINARY candidate and run the pipeline.

        `replace` on the existing envelope rather than `build_envelope`, deliberately: the identity
        and the history are the ones already in the store — same `candidate_id` (so the review row
        transitions in place instead of forking), `content_hash`, `created_at` and `session_signals`,
        which `build_envelope` would recompute from a RECONSTRUCTED summary whose transcript is empty
        by design.

        TWO FIELDS ARE DELIBERATELY CLEARED: `decline`, because a block left here would keep the
        inbox rendering an outstanding task for ever; and `entity_scan`, back to the S3 `pending`
        sentinel because THE PAYLOAD CHANGED — carrying
        the old verdict forward would let text nobody scanned ride a `pass` settled about different
        content. The status returns to `extracted` for the same reason: it is what the pipeline
        expects to be handed, and the router decides where it goes from there.
        """
        rebuilt = replace(
            env,
            status=CandidateStatus.EXTRACTED,
            # ⚠ STAMPED ON THE VALIDATED DOC, never carried through `to_candidate`.
            # `BlueprintPayload` is a closed key set, so a `sql_rewrite` sent INTO validation is
            # either dropped (silently losing the badge on the write that created it) or has to
            # be registered as a field a model could then set. Writing it here keeps the record
            # SERVER-STAMPED by construction: the only way into it is `_carried_rewrite_record`,
            # which reads the snapshot.
            payload=_stamped(candidate.payload_to_doc(), rewrite_record),
            confidence=candidate.header.confidence,
            proposed_action=candidate.header.proposed_action,
            depends_on=candidate.header.depends_on,
            extractor_rationale=candidate.header.rationale,
            entity_scan={
                "result": "pending",
                "hits": [],
                "self_check_contains_entities": (
                    candidate.header.entity_self_check.contains_entities
                ),
            },
            decline=None,
            # `revalidation` is KEPT, and used to be cleared here on the stated grounds that
            # "its only reader is this path". That stopped being true: the §C reviser reads it
            # to propose against the accepted SQL, now on the review queue as well as the form.
            # Clearing it would make a candidate editable exactly once and then never again —
            # and silently, because the reviser degrades to "no snapshot" rather than erroring.
            #
            # Nothing about the entity posture changes: same field, same access-controlled
            # store, still never projected to the wire. It describes the SESSION, which a
            # completion does not alter, so it stays as true after the round trip as before.
            #
            # §C.5: on a REWRITE it is the new snapshot, which is the one thing a completion CAN
            # alter about the session's account — the accepted SQL is no longer the session's.
            # `_rewritten_snapshot` is where that is spelled out; here it just has to be the
            # snapshot everything downstream was validated against, or the stored row and the
            # verdict on it would describe different queries.
            revalidation=snapshot,
        )
        if not self.stages:
            _logger.warning(
                "learning inbox: completed %s with NO write-router pipeline wired — it "
                "is persisted at %s with no generalization and an unsettled entity "
                "scan, which every approve guard refuses. This is the offline dev "
                "posture; a deployment that means to land completions must wire the "
                "stages.",
                env.candidate_id, CandidateStatus.EXTRACTED,
            )
            await self._guarded_put(env, rebuilt)
            return CompletionResult(
                outcome="completed", envelope=rebuilt, sql_rewritten=rewritten
            )

        outcome = await run_pipeline(
            self.stages,
            rebuilt,
            StageContext(summary=summary, verdict=COMPLETION_VERDICT),
        )
        landed = self._settled(outcome)
        await self._guarded_put(env, landed)
        _logger.info(
            "learning inbox: %s completed by a reviewer and re-ran the write-router "
            "pipeline → status=%s (control=%s)",
            env.candidate_id, landed.status, outcome.control,
        )
        return CompletionResult(
            outcome="completed", envelope=landed, sql_rewritten=rewritten
        )

    @staticmethod
    def _settled(outcome) -> CandidateEnvelope:
        """The envelope this completion leaves in the store — INCLUDING when the pipeline said `drop`.

        `drop` means a stage handled the candidate elsewhere: S6 bumps the existing artifact's count
        and drops the duplicate, or drops it as redundant with the canon. On the EXTRACTION path "do
        not persist" is harmless, because the consumer already wrote the row at `extracted` before
        the stages ran. On THIS path there is a row already, and it says `needs_parameterization`:
        not persisting left it saying that FOR EVER while the response said "completed", so the row
        relisted and every re-completion bumped the same corpus counter again. Not a corner case
        either — a re-processed session mints a fresh review item for a blueprint that may have
        landed on the earlier run, and completing it is GUARANTEED to hit the hard key and drop.

        So the enriched envelope is persisted whatever the control said, at the status the pipeline
        left it. The one thing forced is that it is NOT the review status: a completed form must
        never be a form again.
        """
        env = outcome.envelope
        if env.status == CandidateStatus.NEEDS_PARAMETERIZATION:
            return replace(env, status=CandidateStatus.EXTRACTED)
        return env

    async def _guarded_put(
        self, before: CandidateEnvelope, updated: CandidateEnvelope
    ) -> None:
        """This completer's `guarded_put` — see the module-level function for the rule."""
        await guarded_put(self.store, before, updated)


__all__ = [
    "COMPLETION_VERDICT",
    "REWRITE_TOOL_CALL_REF",
    "CompletionInputError",
    "CompletionRaceError",
    "CompletionResult",
    "CompletionUnavailableError",
    "ParameterizationCompleter",
    "guarded_put",
    "sql_rewrite_of",
]
