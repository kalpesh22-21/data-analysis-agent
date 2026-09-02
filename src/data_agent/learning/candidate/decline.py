"""The two stamps a FAIL-TO-REVIEW candidate carries.

A candidate the judge ruled worth extracting that then died on the parameterization form is
persisted for a human to complete. `DeclineBlock` says WHY it stopped, in the words the model
was shown — that text IS the form, naming the uncovered predicates and any catalog rule that
is that filter. `ValidationSnapshot` says WHAT the form will be re-checked against: the
re-validation reads a `SessionSummary`, an in-process value dropped when extraction ends, and
depending on the live session instead would make a review item stop being completable when
that session's TTL expires.

THE SNAPSHOT IS MINIMAL BY DERIVATION: exactly what the re-validation path reads
(`accepted_signal`, `sql_by_ref`, `user_id`, and the session/trace/content ids) and nothing
else. `sql_by_ref` is stored as its OWN result and replayed through `answer_sqls`, because it
maps a ref to a TUPLE and dedupes within a ref — replaying the resolved map is what
reproduces it exactly, where re-deriving the tool trail would invite drift.

EVIDENCE IS POINTERS ONLY: `EvidenceRef.quote` is entity-bearing and lives in
`learning_audit`, never on a candidate (D51/D17). The pointers keep the citation resolvable
and satisfy D31's gate with an explicit marker where the quote was; nothing on the completion
path can invent a citation, because the reviewer never supplies one. Both stamps are
entity-BEARING like the payload, which is why they live in the access-controlled store and
why the wire projection withholds the detail unless the leakage verdict is a pass.
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
    """NORMALIZE, do not trust.

    Every field below is rehydrated JSON from a store humans can write through cbq, so a
    non-string reads as absent rather than reaching a reviewer's browser as a repr.
    """
    return value if isinstance(value, str) else default


@dataclass(frozen=True)
class DeclineBlock:
    """The terminal decline of a merit-passed candidate, as the model saw it.

    `detail` is the extractor's own hint text, ALREADY SANITIZED at its build site — this class
    re-renders nothing and must not, because the value of the text is that it is exactly what the
    model was told. `correction_history` is the messages actually sent, so "the model could not
    fill the form" stays distinguishable from "the model was never asked twice".
    """

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

        `None` rather than a coerced empty block, because absent and "present but unreadable" must
        not collapse: the inbox keys the whole review-item rendering on this field being present, and
        a block with no reason would put a row in front of a reviewer with nothing to act on.
        """
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

    INVARIANT relied on by `from_doc`: a legitimately WRITTEN snapshot always carries a non-empty
    `sql_by_ref` and at least one `evidence` pointer, because a session with no resolvable SQL
    declines `unrewritable_sql` and a candidate with no readable citation declines `no_evidence`,
    and neither reason routes to review. An empty one read back is damage, not a legitimate
    shape.

    Reconstructed by `to_summary`; the fields nothing on that path reads are left EMPTY rather
    than snapshotted. An empty transcript is the honest shape for a value never stored, and
    filling it with plausible content would be worse: a summary that looks complete and is not.
    """

    session_id: str
    user_id: str
    trace_id: str
    content_hash: str
    accepted_signal: str | None
    sql_by_ref: dict[str, tuple[str, ...]] = field(default_factory=dict)
    evidence: tuple[EvidencePointer, ...] = ()
    # ⚠ TRUE when `sql_by_ref` was REBUILT FROM THE CANDIDATE rather than recorded off the
    # session (`generalize/reconstruct.py`). The class docstring calls this "everything
    # re-validation reads off the session"; a reconstruction is NOT that, and the difference
    # has to live in the data rather than only in a migration note.
    #
    # It matters because re-validating against reconstructed SQL is CIRCULAR: the SQL is
    # derived from the very entries the totality walk checks, so the first walk cannot fail.
    # Backfilled candidates had already passed the genuine walk at extraction time, and every
    # subsequent revision is checked properly against the stored query — but a reader deciding
    # how much a `completed` outcome proves needs to be able to tell the two apart.
    reconstructed: bool = False

    # ⚠ TRUE when the SQL was AUTHORED BY AN EXPERT at a minting page rather than observed on a
    # session. A third provenance state, and it needed its own field rather than reusing
    # `reconstructed`, because the two make OPPOSITE claims about the same walk.
    #
    # A reconstruction is circular and its first totality walk cannot fail — the SQL is derived
    # from the entries being checked. An authored snapshot is the reverse: the SQL arrived from
    # outside the entries entirely, so the walk is the STRONGEST it ever is here. Every literal
    # the expert typed must be classified by someone who did not get to see the classification
    # first. Marking a minted snapshot `reconstructed` would tell a reader its walk proved
    # nothing, when in fact that walk is the whole gate.
    #
    # What it does NOT claim is that the query ever ran. `verified` stays False and promotion
    # still replays; see `learning/mint/engine.py`.
    authored: bool = False

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
        """Rebuild the summary the validation path walks.

        See the module docstring for why the SQL comes back through `answer_sqls`.
        """
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
            "reconstructed": self.reconstructed,
            "authored": self.authored,
        }

    @classmethod
    def from_doc(cls, doc: Any) -> ValidationSnapshot | None:
        """Rehydrate, or `None` when the stored shape cannot be re-validated against.

        THE GUARD IS DERIVED FROM THE READS, not from the key that names the record. Checking only
        `session_id` let a document whose `sql_by_ref` came back junk rehydrate with an EMPTY map:
        re-validation then walks no SQL, declines `unrewritable_sql`, and tells a reviewer that a
        query which ran live could not be rewritten — a misdiagnosis blaming the model for a storage
        fault, and unactionable, because nothing the reviewer types fixes a map that is not there.
        The same argument applies to the citations, whose loss produces `no_evidence` on a form that
        carries none to supply.

        So the three things the completion path READS are the three things checked: the identity, the
        SQL it walks, and at least one citation. Any of them unusable ⇒ MISSING ⇒
        `CompletionUnavailableError` → 503. EMPTY is unusable and cannot be a legitimate stored
        value. The per-FIELD normalization inside stays tolerant (one unreadable ref does not cost
        the others); what changed is that the aggregate is judged after it.
        """
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
            reconstructed=bool(doc.get("reconstructed", False)),
            authored=bool(doc.get("authored", False)),
        )


def last_sql(sql_by_ref: dict[str, tuple[str, ...]]) -> str:
    """The accepted SQL to reason about: the last query the snapshot resolved.

    "Latest wins" matches the builder's rule, and this deliberately does NOT borrow S4's
    `_collapse_designations` refusal. That function refuses when it cannot prove one designation
    subsumes the others, because it feeds a REWRITE that would silently drop a constraint.
    Nothing is rewritten from this string — it is context for a model, and the answer to "which
    query is the reviewer looking at" — and the proposal a model produces goes through the real
    rewrite afterwards, where that refusal still stands.

    LIVES HERE, beside `ValidationSnapshot`, because it is now read by three callers with one
    question between them: the reviser (what SQL to show the model), the completer (what a
    rewrite would REPLACE) and the inbox (whether a submitted `sql` is a rewrite at all). Two of
    those decide whether a candidate becomes hand-authored, so a second copy that answered
    "latest" differently would make the same request a rewrite on one path and a no-op on another.
    """
    for sqls in reversed(list(sql_by_ref.values())):
        if sqls:
            return sqls[-1]
    return ""


__all__ = [
    "QUOTE_WITHHELD",
    "DeclineBlock",
    "EvidencePointer",
    "ValidationSnapshot",
    "last_sql",
]
