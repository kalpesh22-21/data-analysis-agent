"""BlueprintReviser — a reviewer's sentence in, a parameterization proposal out.

WHAT IT IS NOT: a writer. It touches no store, moves no status and produces no side effect. It
returns a `ReviseProposal` the reviewer then applies through the EXISTING `complete` route, so
the model's output faces the identical `to_candidate` re-validation — D97 totality walk included
— and the identical write-router stages that a hand-typed array faces.

That split is the whole safety argument, and it is deliberately not an optimization anyone
should collapse later. `inbox/completion.py` states the rule this preserves: *"The one thing a
human is trusted with is CONTENT, never the checks."* A model is trusted with less than a human,
so it certainly does not get a shortcut around them. The reviser is a typing aid for a form that
is currently a raw-JSON textarea.

FAIL-SOFT, with one exception. Every failure — no client, timeout, malformed response, no usable
entries, an unusable rewrite — returns a proposal with `entries=()` (or with `sql_changed=False`)
and a `reason`, because "the assistant had no suggestion" is an ordinary outcome of a form the
model cannot fill in either. The exception is `ForbiddenTemplateEditError`: a model that wrote a
field this request had no contract for was working against a system that does not exist, and that
is worth telling the reviewer plainly rather than dressing up as "no suggestion".

§C.5 — SQL REWRITE, OPT-IN. With `allow_sql=True` the model may return a COMPLETE replacement
query. The proposal then carries it, forces `replace`, and carries a `caution` the surface shows
verbatim. Nothing about the split above changes: this still writes nothing, and the rewrite is
applied through `complete`/`apply_revision` like any other proposal. What changes is what the
checks are then checking — assistant-authored SQL rather than a query the warehouse answered —
which is why the completer stamps `authored=True` and the router forces `in_review`. The safety
argument becomes `learning/mint`'s, and it is the same argument: hand-authored SQL faces the
STRICTER totality walk (the query came from outside the entries) and can never auto-land.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from typing import Any

from data_agent.runtime.model.client import ModelClient, begin_turn_client

from ..candidate.decline import last_sql as _last_sql
from ..candidate.models import CandidateEnvelope
from ..extractor.validation import validate_parameterization_totality
from ..generalize.validate import check_read_only_select, frozen_date_literals
from ..observability import revise_span
from .diff import EntryDiff, diff_parameterization
from .prompt import completion_brief, system_prompt
from .schema import (
    COMPOSITE_REWRITE_REASON,
    ForbiddenTemplateEditError,
    SqlRewriteUnsupportedError,
    build_revise_tool,
    parse_proposal,
)

_logger = logging.getLogger(__name__)

# A reviewer's instruction, not a document. Capped before it reaches a prompt so an oversized
# paste cannot push the brief — the part carrying the actual validator complaint — out of the
# model's attention or out of its context.
MAX_FEEDBACK_CHARS = 2_000


def _transportable(value: Any) -> Any:
    """*value* with lone surrogates stripped from every string leaf, recursively.

    ⚠ A TRANSPORT GUARD, NOT A CONTENT ONE, and the distinction is the whole reason it lives
    here rather than in `parse_proposal`. Entry contents are deliberately NOT validated at the
    parse boundary — `to_candidate` owns per-field validation, and a second vocabulary for it is
    the mistake this package's docstring names. But `to_wire` puts those entries on a response
    body BEFORE `to_candidate` ever sees them, and a lone surrogate is unencodable as UTF-8: the
    reviewer gets a bodyless 500 from a route whose entire failure design is "no suggestion is a
    200 with a reason", having done nothing wrong.

    So the sweep is applied to the WIRE PROJECTION and nothing else. The dataclass keeps what
    the model said; only the transmitted copy is made encodable. An entry whose literal carried
    a surrogate could never have matched the accepted SQL anyway — the warehouse cannot produce
    one — so it declines legibly at the totality walk instead of 500ing at the socket.

    Narrow ON PURPOSE: surrogates only, not the five-category flatten `_clean` applies to model
    prose. Prose is reshaped for logs and rendering; an entry is applied verbatim, and reshaping
    it further here would change what the reviewer commits.
    """
    if isinstance(value, str):
        return "".join(ch for ch in value if not 0xD800 <= ord(ch) <= 0xDFFF)
    if isinstance(value, dict):
        return {k: _transportable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_transportable(v) for v in value]
    return value


class ReviserUnavailableError(RuntimeError):
    """No reviser is wired in this deployment. Distinct from a reviser that ran and had
    nothing to propose, which is an ordinary result."""


# ⚠ SHOWN VERBATIM BESIDE THE APPLY BUTTON when the assistant rewrote the query. It states the
# three things that are now true and were not before — the provenance is gone, the checks have
# changed subject, and nothing here can land without a human — in that order, because the third
# is the mitigation for the first two and reads as reassurance only after them.
SQL_REWRITE_CAUTION = (
    "The assistant rewrote the SQL. This is no longer the query the session ran: every check "
    "will now validate assistant-authored SQL, the candidate cannot auto-land, and you should "
    "trial-run it before approving."
)


@dataclass(frozen=True)
class ReviseProposal:
    """What one revise attempt produced. NOTHING has been written.

    `entries` empty means no proposal; `reason` then says why in words a reviewer can read.

    `sql` is non-empty ONLY when `sql_changed` — the two are set together, and a reader must
    never have to decide whether a `sql` equal to the current query means "rewritten to itself".
    `caution` is non-empty on exactly the same condition, so a surface that renders it renders
    the warning exactly when there is something to warn about.
    """

    entries: tuple[dict[str, Any], ...] = ()
    replace: bool = False
    rationale: str = ""
    reason: str = ""
    diff: tuple[EntryDiff, ...] = ()
    sql: str = ""
    sql_changed: bool = False
    caution: str = ""

    @property
    def has_proposal(self) -> bool:
        return bool(self.entries)

    def to_wire(self) -> dict[str, Any]:
        """The response body. Deliberately flat and deliberately WITHOUT a candidate status:
        nothing moved, and a status here would imply otherwise.

        Swept for transport (`_transportable`) — the one guard on this path that is about the
        socket rather than about the content.
        """
        return _transportable(
            {
                "entries": [dict(entry) for entry in self.entries],
                "replace": self.replace,
                "rationale": self.rationale,
                "reason": self.reason,
                "diff": [row.to_doc() for row in self.diff],
                # ADDITIVE, and always present rather than conditional. A field that appears
                # only sometimes makes the absence of a caution indistinguishable from an older
                # server that could not produce one, which is the wrong default for a warning.
                "sql_changed": self.sql_changed,
                "sql": self.sql,
                "caution": self.caution,
            }
        )


@dataclass(frozen=True)
class BlueprintReviser:
    """One forced-tool model turn that proposes parameterization entries."""

    model_client: ModelClient
    known_rules: frozenset[str] = frozenset()
    catalog_schema: dict[str, dict[str, str]] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    model: str = ""
    # Injected like every other collaborator on this plane; `None` = no tracing.
    tracer: object | None = None
    # The D25 gate, decided at the composition root. ⚠ The verbose payload here is the
    # sharpest on the plane — a human's free text and model prose about the UNREDACTED
    # accepted SQL — so it is never defaulted on.
    trace_verbose: bool = False

    async def propose(
        self, env: CandidateEnvelope, *, feedback: str, allow_sql: bool = False
    ) -> ReviseProposal:
        """Propose entries for a declined blueprint. NEVER raises except on a forbidden field.

        The accepted SQL comes off the envelope's own `ValidationSnapshot`, NOT from a live
        session: `candidate/decline.py` snapshots it at decline time precisely so a review item
        does not stop being completable when the session's TTL expires, and the reviser must
        inherit that property rather than reintroduce the dependency.

        `allow_sql` is the reviewer's §C.5 opt-in, DEFAULT FALSE and never inferred. It changes
        the tool, the system prompt and the parse guard together — offering the field while
        telling the model it has none, or the reverse, is the contradiction the three are kept
        in step to avoid.
        """
        snapshot = env.revalidation
        if snapshot is None:
            self._emit(env, "no_snapshot", feedback=feedback)
            # The same precondition `ParameterizationCompleter.complete` opens with. Surfaced
            # as a reason rather than an exception because the caller's next move is identical
            # either way: tell the reviewer there is nothing to work from.
            return ReviseProposal(
                reason=(
                    "this candidate carries no re-validation snapshot, so there is no "
                    "accepted SQL to propose entries against"
                )
            )

        if allow_sql and env.payload.get("kind") == "composite":
            # BEFORE THE MODEL CALL, because nothing about the answer could change the outcome.
            # A composite blueprint's SQL lives on its NODES — one query per step, wired by a
            # DAG the expert declared — so "the SQL" is not a single string a reviser could
            # return, and a whole-query `sql` would be a field nothing reads. `mint/schema.py`
            # refuses the identical shape for the identical reason.
            self._emit(env, "sql_rewrite_unsupported", feedback=feedback, allow_sql=True)
            raise SqlRewriteUnsupportedError(
                f"{COMPOSITE_REWRITE_REASON}. Untick the option to revise roles only"
            )

        accepted_sql = _last_sql(snapshot.sql_by_ref)
        brief = completion_brief(
            env.payload,
            accepted_sql=accepted_sql,
            decline_reason=env.decline.reason if env.decline is not None else "",
            decline_detail=env.decline.detail if env.decline is not None else "",
            feedback=(feedback or "")[:MAX_FEEDBACK_CHARS],
            known_rules=tuple(sorted(self.known_rules)),
            catalog_columns=self._columns_for(accepted_sql),
        )

        try:
            async with asyncio.timeout(self.timeout_seconds):
                messages = [
                    {"role": "system", "content": system_prompt(allow_sql=allow_sql)},
                    # The brief is a SEPARATE user message and the reviewer's own words sit
                    # inside it, clearly labelled — never spliced into the instruction message.
                    # Same placement, and the same reasoning, as the judge's prior-art block.
                    {"role": "user", "content": brief},
                ]
                client = begin_turn_client(self.model_client)
                result = await client.send_turn(
                    messages, [build_revise_tool(allow_sql=allow_sql)]
                )
                parsed = parse_proposal(result, allow_sql=allow_sql)
        except ForbiddenTemplateEditError:
            self._emit(env, "refused_template_edit", feedback=feedback, allow_sql=allow_sql)
            raise
        except TimeoutError:
            _logger.warning(
                "reviser: model call exceeded %.1fs — no proposal", self.timeout_seconds
            )
            self._emit(env, "timeout", feedback=feedback, allow_sql=allow_sql)
            return ReviseProposal(reason="the assistant timed out; try again")
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # The three that must still propagate — cancellation is how a shutdown reaches an
            # awaiting request handler, and the other two are the operator stopping the process.
            raise
        except BaseException:  # noqa: BLE001 — a typing aid may not 500 the review surface
            # WIDER THAN `Exception`, for the reason `judge/judge.py` records from QA: a
            # provider SDK raising a bare `BaseException` escaped every narrower handler. Here
            # the blast radius is one request rather than a batch of sessions, but the
            # principle is the same — an optional convenience must not be able to take down
            # the surface it is attached to.
            _logger.warning("reviser: model call raised — no proposal", exc_info=True)
            self._emit(env, "failed", feedback=feedback, allow_sql=allow_sql)
            return ReviseProposal(reason="the assistant could not be reached; try again")

        if parsed is None:
            self._emit(env, "unusable", feedback=feedback, allow_sql=allow_sql)
            return ReviseProposal(
                reason="the assistant did not return a usable set of entries; try rephrasing"
            )
        entries, replace, rationale, proposed_sql = parsed

        # THE REWRITE, adjudicated by the check that will READ it. `check_read_only_select` is
        # the same function `decide_outcome` runs over the derived template later, so a query
        # that fails here would have failed there — the only difference is that here it costs a
        # sentence on a 200 instead of a `fail_to_review` on a candidate the reviewer already
        # committed to. Derived from the downstream read rather than re-implemented, per the
        # project rule this plane keeps re-learning.
        sql_changed = bool(proposed_sql) and proposed_sql != accepted_sql.strip()
        if sql_changed and not check_read_only_select(proposed_sql):
            self._emit(env, "sql_rewrite_unusable", feedback=feedback, allow_sql=True)
            return ReviseProposal(
                reason=(
                    "the assistant returned a replacement query that is not a single "
                    "read-only SELECT this system can parse, so it was discarded rather than "
                    "offered; try again, or untick the SQL option and ask it to re-role the "
                    "literals instead"
                )
            )
        frozen = frozen_date_literals(proposed_sql) if sql_changed else ()
        if frozen:
            # ⚠ THE SECOND GUARD, AND THE ONE THE REQUIREMENT IS ACTUALLY ABOUT. Until this
            # existed, `read_only_select` was a GUARD here and the frozen-run-date rule was only
            # a PARAGRAPH (`DATE_RULE`) in the rewrite prompt — so a model that pasted today's
            # date into a `dateDiff` produced a proposal INDISTINGUISHABLE from a good one, and
            # the reviewer learned about it after applying, from a `fail_to_review` they then had
            # to decode.
            #
            # RUN ON THE RAW QUERY, NOT ON A TEMPLATE, and the two agree for every shape this can
            # see. `frozen_date_literals` flags a date-shaped STRING that no comparison the S3
            # enumerator recognizes adjudicated. A literal an entry DOES adjudicate is gone from
            # the template (it became a `{slot}`) and passes here too, because being adjudicated
            # is exactly conditions (a)+(b) — so the raw query cannot flag something the template
            # would not. Deriving the template first would mean rebuilding the builder's
            # `ParamPlan` construction inside a typing aid: a second implementation of the
            # derivation, which is the thing this package's docstring refuses.
            #
            # NAMES THE LITERAL, because "your query froze the run date" sends a reviewer
            # diffing two queries by eye while the string itself is something they can find.
            self._emit(env, "sql_rewrite_unusable", feedback=feedback, allow_sql=True)
            return ReviseProposal(
                reason=(
                    "the assistant's replacement query freezes "
                    + ", ".join(repr(literal) for literal in frozen)
                    + " into the query itself, so the blueprint would answer a DIFFERENT "
                    "question every day it ages — a checker refuses it downstream, so it was "
                    "discarded rather than offered. Ask for a relative window instead "
                    "(today()/now() with dateDiff), or untick the SQL option"
                )
            )
        if sql_changed:
            # FORCED, not requested. Every existing entry describes the OLD query; appending
            # against a query none of them belong to produces a payload whose parameterization
            # is half about a string nobody has any more. The completer refuses a rewrite
            # without `replace_all` for the same reason, so this is the surface agreeing with
            # the guard rather than substituting for it.
            replace = True

            proposed_payload = dict(env.payload)
            proposed_payload.setdefault("kind", "single")
            proposed_payload.setdefault("accepted_signal", snapshot.accepted_signal)
            proposed_payload.setdefault("source_tool_call_refs", list(snapshot.sql_by_ref))
            proposed_payload["parameterization"] = list(entries)
            rewritten_snapshot = dataclass_replace(
                snapshot,
                sql_by_ref={ref: (proposed_sql,) for ref in snapshot.sql_by_ref},
            )
            totality_decline = validate_parameterization_totality(
                proposed_payload, rewritten_snapshot.to_summary()
            )
            if totality_decline is not None:
                self._emit(env, "sql_rewrite_unusable", feedback=feedback, allow_sql=True)
                return ReviseProposal(
                    reason=(
                        "the assistant's replacement SQL and parameterization do not agree: "
                        f"{totality_decline.detail}. The proposal was discarded before apply; "
                        "ask the assistant to classify every literal predicate in the new SQL"
                    )
                )

        current = env.payload.get("parameterization")
        rows = diff_parameterization(
            current if isinstance(current, list) else [], entries, replace=replace
        )
        # A conflict is not a reason to withhold the proposal — the entries are still the right
        # content, and the reviewer can tick `replace`. It IS a reason to say so on the card,
        # because the alternative is applying an append that declines for a reason the diff
        # showed no sign of.
        conflicts = [row.locator for row in rows if row.kind == "conflict"]
        reason = (
            "this proposal changes entries that already exist, which appending cannot do — "
            f"tick 'replace the whole array' before completing ({', '.join(conflicts)})"
            if conflicts
            else ""
        )
        self._emit(
            env,
            "proposed_sql_rewrite" if sql_changed else "proposed",
            feedback=feedback,
            entries=len(entries),
            replace=replace,
            conflicts=len(conflicts),
            rationale=rationale,
            reason=reason or None,
            allow_sql=allow_sql,
        )
        return ReviseProposal(
            entries=tuple(entries),
            replace=replace,
            rationale=rationale,
            reason=reason,
            diff=rows,
            sql=proposed_sql if sql_changed else "",
            sql_changed=sql_changed,
            caution=SQL_REWRITE_CAUTION if sql_changed else "",
        )

    def refuse_withheld(self, env: CandidateEnvelope, *, reason: str) -> ReviseProposal:
        """The leakage-gate refusal, as a proposal — and as a SPAN.

        Lives here rather than at the caller so every `learning.revise` outcome is emitted from
        one place. A refusal that produced no span would be indistinguishable from the
        assistant never having been asked, which is the wrong reading of the one outcome that
        means a guard worked.
        """
        self._emit(env, "withheld_scan")
        return ReviseProposal(reason=reason)

    def _emit(self, env: CandidateEnvelope, outcome: str, **attrs: Any) -> None:
        """One `learning.revise` span, or nothing when no tracer is wired.

        Emitted on EVERY return path, because this is the only model call on the plane a human
        triggers and it WRITES NOTHING — so without a span a refused or empty proposal leaves
        no record anywhere that the assistant was even asked. "It keeps suggesting nothing" is
        precisely the complaint an operator would otherwise have no way to substantiate.

        The outcomes are kept apart because they need different fixes: `no_snapshot` is a
        MIGRATION signal (the candidate predates the stamp), `withheld_scan` is the leakage
        gate doing its job, and only `unusable`/`timeout`/`failed` are the model.
        """
        if self.tracer is None:
            return
        with revise_span(
            self.tracer,  # type: ignore[arg-type]
            candidate_id=env.candidate_id,
            outcome=outcome,
            status=env.status,
            model=self.model,
            verbose=self.trace_verbose,
            **attrs,
        ):
            return

    def _columns_for(self, accepted_sql: str) -> tuple[str, ...]:
        """`database.table.column` names a slot may legally bind to.

        Narrowed to the tables the accepted SQL mentions, by substring — crude on purpose. The
        purpose is GROUNDING, not access control (the D44 provenance machinery owns that), and
        the failure modes of crudeness both point the safe way: a table matched too eagerly
        offers columns the model will not find a literal for, and one missed falls back to the
        whole catalog, which is what an ungrounded prompt already had.
        """
        if not self.catalog_schema:
            return ()
        haystack = accepted_sql.lower()
        matched = [t for t in self.catalog_schema if t.lower() in haystack] or list(
            self.catalog_schema
        )
        return tuple(
            sorted(
                f"{table}.{column}"
                for table in matched
                for column in self.catalog_schema.get(table, {})
            )
        )


# ⚠ `_last_sql` MOVED to `candidate/decline.py::last_sql` and is imported above under its old
# name. Three callers now ask the same question of a snapshot — this engine, the completer, and
# the inbox deciding whether a submitted `sql` is a rewrite — and two of them decide whether a
# candidate becomes hand-authored. A second copy would let the same request be a rewrite on one
# path and a no-op on another.
