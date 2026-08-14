"""The two stamps a FAIL-TO-REVIEW candidate carries (`docs/decisions/
learning-declined-candidate-review.md`).

A candidate the judge ruled worth extracting (`proceeded`) and that then died on the
parameterization form — `totality_violation` / `rule_predicate_mismatch`, after its
corrective rounds — is persisted for a human to complete instead of being discarded.
Two things have to travel with it, and they answer two different questions:

  * `DeclineBlock` — WHY it stopped, in the words the model was shown. The reviewer's
    task is "fill in the form", and the decline detail is the form: it names the
    predicates with no entry and any catalog rule that IS that filter. Without it the
    review item says only that something was missing.
  * `ValidationSnapshot` — WHAT the form will be re-checked against. Re-validation is
    the same `to_candidate` contract the corrective turn ran, and that contract reads
    the `SessionSummary` — an in-process value dropped the moment extraction ends. The
    session itself is not a substitute: it is request-path state under its own TTL and
    its own access boundary, and making the review queue depend on it would mean a
    review item silently stops being completable when the session expires.

**The snapshot is MINIMAL BY DERIVATION, not by taste.** It carries exactly what the
re-validation path reads and nothing else:

    to_candidate            `summary.accepted_signal` (D34 acceptance), and
                            `sql_by_ref(summary)` for the D97 totality walk
    GeneralizeStage         `sql_by_ref(ctx.summary)`
    LeakageGateStage        `ctx.summary.user_id` (reroute scoping)
    DedupStage → judge      `session_id` / `trace_id` / `content_hash`

`sql_by_ref` is stored as its OWN result rather than as the two projections it is
computed from, and the reconstruction replays it through `answer_sqls`. That is not a
shortcut: `sql_by_ref` maps a ref to a TUPLE (one `answerWithTable` can designate
several queries) and dedupes exact repeats within a ref, so replaying the resolved map
through the one field that carries a (ref, sql) pair reproduces the map exactly —
which is the property `test_snapshot_parity` pins. Re-deriving the tool trail would
reproduce a shape nobody reads and invite it to drift from what validation walks.

**Evidence is POINTERS ONLY.** `EvidenceRef.quote` is entity-bearing and lives in
`learning_audit`, never on a candidate (D51/D17) — that rule is older than this slice
and this slice does not get an exemption. What the snapshot keeps is `(turn_ref,
tool_call_ref)`, which is what makes the citation resolvable against the live session,
and re-validation's evidence gate (D31: at least one citation) is satisfied by the
pointers with an explicit marker where the quote was. The gate that mattered — did the
MODEL cite anything — was already answered when the candidate was emitted; nothing on
the completion path can invent a citation, because the reviewer never supplies one.

Both stamps are entity-BEARING in the same way the payload already is (SQL literals,
predicate values), which is why they live in the access-controlled `learning_candidates`
store (D101) and why the wire projection withholds the detail unless the persisted
leakage verdict is a pass (`inbox/models.py::InboxItem.decline_view`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..summary.models import AnswerSql, SessionSummary

# The quote a reconstructed evidence citation carries. The real one is in
# `learning_audit` and is not the candidate store's to hold; this says so in the one
# place a reader of a re-validated candidate would look for it.
QUOTE_WITHHELD = "(quote withheld from the candidate store — see learning_audit, D51)"


def _text(value: Any, default: str = "") -> str:
    """NORMALIZE, do not trust — every field below is rehydrated JSON from a store
    humans can write through cbq. A non-string reads as absent rather than reaching a
    reviewer's browser as a repr, mirroring `CandidateEnvelope.from_doc::route_reason`."""
    return value if isinstance(value, str) else default


@dataclass(frozen=True)
class DeclineBlock:
    """The terminal decline of a merit-passed candidate, as the model saw it.

    `detail` is the extractor's own hint text, ALREADY SANITIZED at its build site
    (`extractor/validation.py::_quoted`/`_flattened`: single-line literals, bounded,
    quotes doubled) — this class re-renders nothing and must not, because the value of
    the text is that it is exactly what the model was told.

    `correction_history` is the messages that were actually sent, so "the model could
    not fill the form" stays distinguishable from "the model was never asked twice"."""

    reason: str
    detail: str = ""
    corrections_attempted: int = 0
    correction_history: tuple[str, ...] = ()

    def to_doc(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "corrections_attempted": self.corrections_attempted,
            "correction_history": list(self.correction_history),
        }

    @classmethod
    def from_doc(cls, doc: Any) -> DeclineBlock | None:
        """Rehydrate, or `None` when the stored shape is not one of ours.

        `None` rather than a coerced empty block, because absent and "present but
        unreadable" must not collapse: the inbox keys the whole review-item rendering on
        this field being present, and a block with no reason would put a row in front of
        a reviewer with nothing to act on."""
        if not isinstance(doc, dict):
            return None
        reason = _text(doc.get("reason"))
        if not reason:
            return None
        history = doc.get("correction_history")
        return cls(
            reason=reason,
            detail=_text(doc.get("detail")),
            corrections_attempted=(
                doc["corrections_attempted"]
                if isinstance(doc.get("corrections_attempted"), int)
                and not isinstance(doc.get("corrections_attempted"), bool)
                else 0
            ),
            correction_history=tuple(
                _text(item) for item in history if isinstance(item, str)
            )
            if isinstance(history, list)
            else (),
        )


@dataclass(frozen=True)
class EvidencePointer:
    """One citation, minus the quote (see the module doc)."""

    turn_ref: int
    tool_call_ref: str

    def to_doc(self) -> dict[str, Any]:
        return {"turn_ref": self.turn_ref, "tool_call_ref": self.tool_call_ref}


@dataclass(frozen=True)
class ValidationSnapshot:
    """Everything re-validation + the write-router stages read off the session.

    INVARIANT, relied on by `from_doc`: a snapshot that was legitimately WRITTEN always
    carries a non-empty `sql_by_ref` and at least one `evidence` pointer. Both follow
    from the route's entry conditions — a session with no resolvable SQL declines
    `unrewritable_sql`, and a candidate with no readable citation declines `no_evidence`,
    and neither of those reasons routes to review. An empty one read back from the store
    is therefore damage, not a legitimate shape, and is reported as missing.

    Reconstructed into a `SessionSummary` by `to_summary`; the fields nothing on that
    path reads (`turns`, `blueprint_usages`, `askuser_exchanges`, `failed_fixed_sql`,
    `scope_ref`) are left EMPTY rather than snapshotted. An empty transcript is the
    honest shape for a value that was never stored, and a reconstruction that filled
    them with plausible content would be the one thing worse: a summary that looks
    complete and is not."""

    session_id: str
    user_id: str
    trace_id: str
    content_hash: str
    accepted_signal: str | None
    sql_by_ref: dict[str, tuple[str, ...]] = field(default_factory=dict)
    evidence: tuple[EvidencePointer, ...] = ()

    @classmethod
    def from_summary(
        cls, summary: SessionSummary, *, evidence: tuple[EvidencePointer, ...] = ()
    ) -> ValidationSnapshot:
        from ..summary.refs import sql_by_ref

        return cls(
            session_id=summary.session_id,
            user_id=summary.user_id,
            trace_id=summary.trace_id,
            content_hash=summary.content_hash,
            accepted_signal=summary.accepted_signal,
            sql_by_ref=dict(sql_by_ref(summary)),
            evidence=evidence,
        )

    def to_summary(self) -> SessionSummary:
        """Rebuild the summary the validation path walks (see the module doc for why
        the SQL comes back through `answer_sqls`)."""
        return SessionSummary(
            session_id=self.session_id,
            user_id=self.user_id,
            scope_ref="",
            trace_id=self.trace_id,
            content_hash=self.content_hash,
            turns=(),
            tool_calls=(),
            blueprint_usages=(),
            askuser_exchanges=(),
            failed_fixed_sql=(),
            accepted_signal=self.accepted_signal,  # type: ignore[arg-type]
            answer_sqls=tuple(
                AnswerSql(tool_call_ref=ref, sql=sql, blueprint_id=None)
                for ref, sqls in self.sql_by_ref.items()
                for sql in sqls
            ),
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "content_hash": self.content_hash,
            "accepted_signal": self.accepted_signal,
            "sql_by_ref": {ref: list(sqls) for ref, sqls in self.sql_by_ref.items()},
            "evidence": [e.to_doc() for e in self.evidence],
        }

    @classmethod
    def from_doc(cls, doc: Any) -> ValidationSnapshot | None:
        """Rehydrate, or `None` when the stored shape cannot be re-validated against.

        **THE GUARD IS DERIVED FROM THE READS, not from the key that names the record.**
        An earlier cut checked only `session_id` — the field that IDENTIFIES a snapshot —
        and so a document whose `sql_by_ref` came back junk rehydrated into a snapshot
        with an EMPTY map, which is exactly the outcome this docstring claimed to prevent:
        re-validation then walks no SQL, declines `unrewritable_sql`, and tells a reviewer
        that the accepted query — a query that ran live and answered a user — could not be
        rewritten. That is a misdiagnosis blaming the model for a storage fault, and it is
        unactionable: nothing the reviewer can type fixes a map that is not there. The
        same argument applies to the citations, whose loss produces `no_evidence` on a
        form that carries no citations to supply.

        So the three things the completion path actually READS are the three things
        checked: the identity, the SQL it walks, and at least one citation. Any of them
        unusable ⇒ MISSING ⇒ `CompletionUnavailableError` → 503, "this cannot be
        checked", which is honest and already wired.

        EMPTY IS UNUSABLE, and it cannot be a legitimate stored value: a candidate whose
        session resolved no SQL declines `unrewritable_sql` at extraction and never routes
        to review at all, and one whose citations were unreadable declines `no_evidence`
        before the totality walk is ever reached. Both checks run BEFORE the two reasons
        that route here, so every legitimately-persisted snapshot has a non-empty map and
        at least one pointer. An empty one is damage by construction.

        The per-FIELD normalization inside is unchanged and still tolerant (one unreadable
        ref does not cost the others); what changed is that the aggregate is judged after
        it."""
        if not isinstance(doc, dict):
            return None
        session_id = _text(doc.get("session_id"))
        if not session_id:
            return None
        raw_map = doc.get("sql_by_ref")
        sql_by_ref: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_map, dict):
            for ref, sqls in raw_map.items():
                if not isinstance(ref, str) or not isinstance(sqls, list):
                    continue
                kept = tuple(sql for sql in sqls if isinstance(sql, str) and sql.strip())
                if kept:
                    sql_by_ref[ref] = kept
        raw_evidence = doc.get("evidence")
        evidence: list[EvidencePointer] = []
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if not isinstance(item, dict):
                    continue
                turn_ref = item.get("turn_ref")
                tool_call_ref = item.get("tool_call_ref")
                if isinstance(turn_ref, bool) or not isinstance(turn_ref, int):
                    continue
                if not isinstance(tool_call_ref, str):
                    continue
                evidence.append(EvidencePointer(turn_ref, tool_call_ref))
        if not sql_by_ref or not evidence:
            return None
        accepted = doc.get("accepted_signal")
        return cls(
            session_id=session_id,
            user_id=_text(doc.get("user_id")),
            trace_id=_text(doc.get("trace_id")),
            content_hash=_text(doc.get("content_hash")),
            accepted_signal=accepted if isinstance(accepted, str) else None,
            sql_by_ref=sql_by_ref,
            evidence=tuple(evidence),
        )


__all__ = [
    "QUOTE_WITHHELD",
    "DeclineBlock",
    "EvidencePointer",
    "ValidationSnapshot",
]
