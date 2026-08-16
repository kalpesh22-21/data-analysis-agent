"""
D62 sqlglot false-reject oracle.

Purpose
-------
Measure the *false-reject rate* of the column-provenance extractor
(`data_agent.sqlparse.provenance.extract_column_provenance`) against a corpus of
real SELECT statements, BEFORE trusting fail-closed enforcement (D63) in
production.

A "false reject" is a SELECT that is genuinely valid and in-scope for the
catalog, but on which the extractor raises `ProvenanceExtractionError` — which
D63 would map to *reject the query*.  If the extractor over-rejects, real,
answerable questions get wrongly blocked.  This oracle quantifies how often that
would happen and surfaces the rejected queries (redacted) so a human can triage
each one as either a true parser gap (a D70 backlog item) or a genuinely
out-of-scope query (a correct reject, not a false one).

What this oracle is NOT
-----------------------
It does NOT decide *by itself* whether a reject is false.  The extractor cannot
know a query's true intent, so every reject requires human triage.  The oracle's
job is measurement + evidence: counts, the reject RATE, and a redacted sample of
each reject.  On a corpus of purely in-scope warehouse queries the reject rate is
an *upper bound* on the false-reject rate (some rejects may be genuinely
out-of-scope).

Classification
--------------
Each query is classified into exactly one `Outcome`:

  - EXTRACTED_OK       -> the extractor returned a USES set (query would pass D63).
  - PARSE_ERROR        -> the extractor raised and the underlying cause was a
                          sqlglot ParseError/TokenError (genuine syntax the
                          ClickHouse dialect could not tokenise).
  - FAIL_CLOSED_REJECT -> the extractor raised for any other reason
                          (uncatalogued table/column, unresolvable reference,
                          lambda-coverage gap, non-SELECT, scratch-session
                          mismatch, ...).

Both PARSE_ERROR and FAIL_CLOSED_REJECT are *rejects* (D63 would block the
query).  They are split apart only because the triage differs: a PARSE_ERROR is
almost always a parser gap (candidate false reject / D70), whereas a
FAIL_CLOSED_REJECT is more often a genuinely out-of-scope query.

Redaction (D25)
---------------
SQL text may embed literal values (WHERE `Name = 'Alice'`).  Rejected samples are
therefore never logged verbatim: each `Classification` carries a sha256
`query_hash`, a literal-*masked* preview (via `_mask_sql` below), and the query
length — enough shape for a human to triage, no raw values.  The reject `reason`
is masked the same way because sqlglot parse errors can echo a fragment of the
offending SQL.

Masking is `data_agent.runtime.observability.redaction.mask_sql`, IMPORTED.  It used
to be a local re-implementation of the same three regexes, on two arguments that no
longer hold:

  * "importing `runtime.observability.redaction` cold trips a pre-existing import
    cycle" — that cycle (`redaction -> context -> dispatch -> redaction`) was fixed at
    the source: `redaction.py::hash_scope` now imports `compute_scope_hash` lazily,
    inside the function, precisely so the module is cold-importable.  Verified: the
    import here is clean.
  * "`sqlparse` is a low-level parsing layer and must not depend upward on `runtime`" —
    true of the sqlparse LIBRARY, and still respected: `sqlparse/__init__.py` exports
    only `provenance`, and nothing on the request path reaches this module.  This
    oracle is an offline measurement harness that merely lives in the package; it
    already depends on far more than a regex.

The copy was worth removing because the mask is a SECURITY contract, not a formatting
detail: the whole D25 claim of this oracle is that a rejected sample carries no raw
values.  Two implementations of that claim means one of them can be tightened (a new
literal form, say a dollar-quoted or hex literal) while the other keeps emitting the
values it no longer masks — and the copy that silently under-masks is this one, whose
output is written to disk for human triage.

`redaction.mask_sql` in turn mirrors clickhouse-api's `app/security.py::
_mask_string_literals`; that one IS a cross-repo mirror and stays a mirror.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import sqlglot.errors

from data_agent.runtime.observability.redaction import mask_sql as _mask_sql
from data_agent.sqlparse.provenance import (
    ProvenanceExtractionError,
    extract_column_provenance,
)

_DEFAULT_PREVIEW_CHARS = 240
_DEFAULT_SAMPLE_LIMIT = 25


class Outcome(StrEnum):
    """The mutually-exclusive result of running one query through the extractor."""

    EXTRACTED_OK = "extracted_ok"
    FAIL_CLOSED_REJECT = "fail_closed_reject"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class Classification:
    """A single query's classification — redacted; safe to log/print (D25)."""

    outcome: Outcome
    query_hash: str
    """sha256 hex digest of the *raw* SQL — a stable, value-free identity."""
    masked_preview: str
    """Literal-masked, truncated SQL (structure only, no raw values)."""
    query_len: int
    """Character length of the raw SQL (shape signal for triage)."""
    reason: str
    """Masked reason for a reject; empty string for EXTRACTED_OK."""
    uses_count: int
    """Number of (table, column) pairs for EXTRACTED_OK; 0 otherwise."""

    @property
    def is_reject(self) -> bool:
        return self.outcome is not Outcome.EXTRACTED_OK


@dataclass(frozen=True)
class OracleReport:
    """Aggregate measurement over a corpus of queries."""

    total: int
    classifications: list[Classification] = field(default_factory=list)

    @property
    def extracted_ok(self) -> int:
        return sum(1 for c in self.classifications if c.outcome is Outcome.EXTRACTED_OK)

    @property
    def fail_closed_reject(self) -> int:
        return sum(1 for c in self.classifications if c.outcome is Outcome.FAIL_CLOSED_REJECT)

    @property
    def parse_error(self) -> int:
        return sum(1 for c in self.classifications if c.outcome is Outcome.PARSE_ERROR)

    @property
    def reject_count(self) -> int:
        """Total rejects — anything D63 would block (fail-closed + parse-error)."""
        return self.fail_closed_reject + self.parse_error

    @property
    def reject_rate(self) -> float:
        """rejects / total. On an in-scope corpus this is an UPPER BOUND on the
        false-reject rate — some rejects may be genuinely out-of-scope.
        Returns 0.0 for an empty corpus."""
        if self.total == 0:
            return 0.0
        return self.reject_count / self.total

    def rejected_samples(self, limit: int = _DEFAULT_SAMPLE_LIMIT) -> list[Classification]:
        """Return up to *limit* rejected classifications for human triage."""
        rejects = [c for c in self.classifications if c.is_reject]
        return rejects[:limit]

    def render(self, sample_limit: int = _DEFAULT_SAMPLE_LIMIT) -> str:
        """Render a human-readable, redacted report (safe to print/log, D25)."""
        lines = [
            "D62 provenance false-reject oracle report",
            "-" * 60,
            f"  total SELECTs classified : {self.total}",
            f"  extracted_ok             : {self.extracted_ok}",
            f"  fail_closed_reject       : {self.fail_closed_reject}",
            f"  parse_error              : {self.parse_error}",
            f"  reject_count             : {self.reject_count}",
            f"  reject_rate              : {self.reject_rate:.4f}",
        ]
        samples = self.rejected_samples(sample_limit)
        if samples:
            shown = len(samples)
            lines.append(f"  --- rejected samples (redacted; {shown}/{self.reject_count}) ---")
            for i, c in enumerate(samples, start=1):
                lines.append(
                    f"  [{i}] {c.outcome.value} hash={c.query_hash[:12]} len={c.query_len}"
                )
                lines.append(f"      sql   : {c.masked_preview}")
                lines.append(f"      reason: {c.reason}")
        else:
            lines.append("  --- no rejects ---")
        return "\n".join(lines)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preview(sql: str, preview_chars: int) -> str:
    """Literal-masked, single-line, truncated preview of *sql* (D25-safe)."""
    masked = _mask_sql(sql)
    collapsed = " ".join(masked.split())
    if len(collapsed) > preview_chars:
        return collapsed[:preview_chars] + "..."
    return collapsed


def _is_parse_cause(exc: BaseException) -> bool:
    """True iff *exc* (or its wrapped cause) is a genuine sqlglot syntax failure.

    The extractor wraps sqlglot ParseError/TokenError in a
    ProvenanceExtractionError with `from exc`, so we inspect `__cause__`.  A
    genuine tokeniser/parser failure is a distinct triage signal (almost always
    a parser gap) from a semantic fail-closed reject.
    """
    cause = exc.__cause__
    return isinstance(cause, (sqlglot.errors.ParseError, sqlglot.errors.TokenError))


def classify_query(
    sql: str,
    catalog_schema: dict[str, dict[str, str]],
    *,
    session_id: str | None = None,
    preview_chars: int = _DEFAULT_PREVIEW_CHARS,
) -> Classification:
    """Run one query through the extractor and classify the outcome (redacted).

    Never raises for an extractor failure — a raise IS the measurement.  The
    returned `Classification` is safe to log/print (D25): it carries a hash, a
    masked preview, and a masked reason, never raw values.
    """
    query_hash = _sha256(sql)
    preview = _preview(sql, preview_chars)
    query_len = len(sql)

    try:
        uses = extract_column_provenance(sql, catalog_schema, session_id=session_id)
    except ProvenanceExtractionError as exc:
        outcome = Outcome.PARSE_ERROR if _is_parse_cause(exc) else Outcome.FAIL_CLOSED_REJECT
        return Classification(
            outcome=outcome,
            query_hash=query_hash,
            masked_preview=preview,
            query_len=query_len,
            reason=_mask_sql(str(exc)),
            uses_count=0,
        )

    return Classification(
        outcome=Outcome.EXTRACTED_OK,
        query_hash=query_hash,
        masked_preview=preview,
        query_len=query_len,
        reason="",
        uses_count=len(uses),
    )


def run_oracle(
    queries: Iterable[str],
    catalog_schema: dict[str, dict[str, str]],
    *,
    session_id: str | None = None,
    preview_chars: int = _DEFAULT_PREVIEW_CHARS,
) -> OracleReport:
    """Classify every query in *queries* and return an aggregate `OracleReport`.

    `queries` is consumed once; order is preserved in `report.classifications`.
    """
    classifications: list[Classification] = [
        classify_query(
            sql,
            catalog_schema,
            session_id=session_id,
            preview_chars=preview_chars,
        )
        for sql in queries
    ]
    return OracleReport(total=len(classifications), classifications=classifications)


__all__: Sequence[str] = (
    "Outcome",
    "Classification",
    "OracleReport",
    "classify_query",
    "run_oracle",
)
