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
entries — returns a proposal with `entries=()` and a `reason`, because "the assistant had no
suggestion" is an ordinary outcome of a form the model cannot fill in either. The exception is
`ForbiddenTemplateEditError`: a model that tried to write SQL was working against a contract this
system does not have, and that is worth telling the reviewer plainly rather than dressing up as
"no suggestion".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from data_agent.runtime.model.client import ModelClient, begin_turn_client

from ..candidate.models import CandidateEnvelope
from ..observability import revise_span
from .diff import EntryDiff, diff_parameterization
from .prompt import SYSTEM_PROMPT, completion_brief
from .schema import ForbiddenTemplateEditError, build_revise_tool, parse_proposal

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


@dataclass(frozen=True)
class ReviseProposal:
    """What one revise attempt produced. NOTHING has been written.

    `entries` empty means no proposal; `reason` then says why in words a reviewer can read.
    """

    entries: tuple[dict[str, Any], ...] = ()
    replace: bool = False
    rationale: str = ""
    reason: str = ""
    diff: tuple[EntryDiff, ...] = ()

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

    async def propose(self, env: CandidateEnvelope, *, feedback: str) -> ReviseProposal:
        """Propose entries for a declined blueprint. NEVER raises except on a template edit.

        The accepted SQL comes off the envelope's own `ValidationSnapshot`, NOT from a live
        session: `candidate/decline.py` snapshots it at decline time precisely so a review item
        does not stop being completable when the session's TTL expires, and the reviser must
        inherit that property rather than reintroduce the dependency.
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
                    {"role": "system", "content": SYSTEM_PROMPT},
                    # The brief is a SEPARATE user message and the reviewer's own words sit
                    # inside it, clearly labelled — never spliced into the instruction message.
                    # Same placement, and the same reasoning, as the judge's prior-art block.
                    {"role": "user", "content": brief},
                ]
                client = begin_turn_client(self.model_client)
                result = await client.send_turn(messages, [build_revise_tool()])
                parsed = parse_proposal(result)
        except ForbiddenTemplateEditError:
            self._emit(env, "refused_template_edit", feedback=feedback)
            raise
        except TimeoutError:
            _logger.warning(
                "reviser: model call exceeded %.1fs — no proposal", self.timeout_seconds
            )
            self._emit(env, "timeout", feedback=feedback)
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
            self._emit(env, "failed", feedback=feedback)
            return ReviseProposal(reason="the assistant could not be reached; try again")

        if parsed is None:
            self._emit(env, "unusable", feedback=feedback)
            return ReviseProposal(
                reason="the assistant did not return a usable set of entries; try rephrasing"
            )
        entries, replace, rationale = parsed
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
            "proposed",
            feedback=feedback,
            entries=len(entries),
            replace=replace,
            conflicts=len(conflicts),
            rationale=rationale,
            reason=reason or None,
        )
        return ReviseProposal(
            entries=tuple(entries),
            replace=replace,
            rationale=rationale,
            reason=reason,
            diff=rows,
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


def _last_sql(sql_by_ref: dict[str, tuple[str, ...]]) -> str:
    """The accepted SQL to reason about: the last query the snapshot resolved.

    "Latest wins" matches the builder's rule, and this deliberately does NOT borrow S4's
    `_collapse_designations` refusal. That function refuses when it cannot prove one designation
    subsumes the others, because it feeds a REWRITE that would silently drop a constraint.
    Nothing is rewritten from this string — it is context for a model, and the proposal it
    produces goes through the real rewrite afterwards, where that refusal still stands.
    """
    for sqls in reversed(list(sql_by_ref.values())):
        if sqls:
            return sqls[-1]
    return ""
