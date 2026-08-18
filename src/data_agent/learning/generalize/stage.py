"""`GeneralizeStage` — the S4 `CandidateStage` (the write-router pipeline seam).

First stage in the frozen order `generalize (S4) → leakage (S5) → dedup (S6) →
writer (S7)` (`learning/stage.py`, D102 §7.1). It reads the freshly-extracted
blueprint envelope + the session's tool trail (accepted SQL), computes the
`BlueprintGeneralization`, and merges it under `payload["generalization"]` — an
ADDITIVE payload key; it mutates no S3 field.

`control` is always `continue`: a `fail_to_review` outcome is an IN-BAND value on
the payload (`static_validation.outcome`) that the S7 writer routes on — S4 never
drops it and never raises (D52/D97). Non-blueprint envelopes pass straight through.

The one non-mechanical thing here is `_collapse_designations`: a cited ref can stand
for SEVERAL queries (a multi-table `answerWithTable`) and the builder rewrites one.
The collapse is subset-CHECKED, and refuses rather than picks when the queries it
would discard constrain something the chosen one does not — see its docstring for
why the strict rewrite cannot be relied on to catch that.

Wiring (NOT done here — a one-line registration at the composition root, D102 §7.1):
    stages=(GeneralizeStage(catalog_schema=build_sqlglot_schema_from_catalog(...)), ...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from ..candidate.models import CandidateEnvelope
from ..extractor.sql_predicates import literal_predicates
from ..stage import StageContext, StageResult
from ..summary.refs import sql_by_ref
from .builder import fail_to_review_generalization, generalize_blueprint

_logger = logging.getLogger(__name__)


def _collapse_designations(sqls: tuple[str, ...]) -> str | None:
    """The ONE accepted SQL a ref resolves to for S4, or `None` when there is no safe
    choice (→ the builder's existing `REASON_UNREWRITABLE` → `fail_to_review`).

    One ref, several queries: a multi-table `answerWithTable` (08 §B.3). The last
    designation wins — `builder._accepted_sql_for_single`'s "latest wins" rule,
    extended from refs to designations — but ONLY when every earlier designation's
    literal predicates also appear in it. Otherwise the earlier query constrains
    something the chosen one does not, and rewriting from the chosen one alone would
    emit a template that silently drops that constraint: the D56 wrong-answer class,
    landed as `outcome: ok`.

    IT CANNOT BE LEFT TO THE STRICT REWRITE, which was the first cut of this and is
    wrong for one specific reason. `_validate_totality` counts ANY parameterization
    entry as covering a predicate regardless of role, and the rewrite sees ONE
    designation — the chosen one. So a plan whose only entry for designation 1's
    `country = 'IE'` is inline passes totality, rewrites cleanly from designation 2, and
    ships a template that has no country filter and a plan that says it does.

    THE ARGUMENT NARROWED, AND STILL HOLDS (H3, 2026-08-17). The strict rewrite no
    longer SKIPS inline entries: `_check_inline_literals` requires an inline literal to
    be present in the SQL it is rewriting, so the single-designation half of that hole
    is closed at the rewrite now, and the two layers agree on the `country` case rather
    than disagreeing. What the rewrite still cannot see is the DISCARDED designations —
    it is handed one query and has no way to know another one was dropped, whatever the
    roles say — so this check remains the only layer that can compare them. A
    multi-designation plan that loses a `role=slot` or `role=rule` constraint is caught
    by both; one that loses an inline constraint is caught here.

    THE COMPARISON IS EXACT `LiteralPredicate` equality — same enumerator as the
    totality gate (`extractor/sql_predicates.py`), so the two layers see the same
    predicates — and deliberately NOT that gate's looser `_table_compatible` /
    case-insensitive column matching. The two ask different questions: totality asks
    whether a human-reviewable plan accounts for a predicate, where near-matching is
    tolerable; this asks whether a constraint is literally present in the query we are
    about to rewrite, where any doubt must refuse. An un-parseable designation refuses
    for the same reason — an unknown predicate set is not a subset of anything.
    """
    if len(sqls) == 1:
        return sqls[0]
    chosen = sqls[-1]
    chosen_predicates = literal_predicates(chosen)
    if chosen_predicates is None:
        return None
    covered = set(chosen_predicates)
    for earlier in sqls[:-1]:
        predicates = literal_predicates(earlier)
        if predicates is None or not covered.issuperset(predicates):
            return None
    return chosen


@dataclass(frozen=True)
class GeneralizeStage:
    """Deterministic (NO LLM) generalize + AST-rewrite + static-validate stage.

    `catalog_schema`: the D69 `database.table` → `{column: type}` catalog the
    provenance extractor qualifies against (injected at the composition root)."""

    catalog_schema: dict[str, dict[str, str]]
    stage_id: str = "generalize"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        if env.type != "blueprint":
            return StageResult(envelope=env, control="continue")

        # `summary/refs.py` resolves an `answerWithTable` ref to the SQL that call
        # DESIGNATED, not just a runQuery ref to the SQL it ran — a designated query
        # need never have been dispatched, so without it a candidate sourced from the
        # answer has nothing to rewrite and fails to review with no SQL named.
        #
        # It maps a ref to N SQLs (one multi-table answer designates several) and the
        # builder rewrites ONE accepted SQL into ONE template, so a ref must collapse
        # to one — under `_collapse_designations`, which REFUSES rather than picks
        # when picking would lose a constraint.
        resolved: dict[str, str | None] = {
            ref: _collapse_designations(sqls) for ref, sqls in sql_by_ref(ctx.summary).items()
        }

        # THE CONTRACT ABOVE ("S4 never raises") IS ENFORCED HERE, not assumed. The
        # builder makes every failure it anticipates in-band, and one it did not —
        # a `role=rule` predicate whose deletion left un-normalizable SQL — escaped
        # as a `sqlglot.ParseError` all the way out of this stage, where it stopped
        # the pipeline for a candidate the design says gets REVIEWED. The blast
        # radius is the reason for the belt: on the consumer path the message is
        # never ACKed and the whole SESSION redelivers into the dead-letter queue,
        # and on the reviewer completion path it is a permanent 500 on a form whose
        # only fault is the shape of its SQL. Narrow on purpose — it wraps the one
        # call, and it stamps the same in-band verdict the anticipated faults get.
        try:
            generalization = generalize_blueprint(
                env.payload, resolved, self.catalog_schema
            )
        except Exception:
            _logger.exception(
                "S4 generalize raised for candidate %s — stamping fail_to_review "
                "in-band; the candidate goes to review rather than stopping the "
                "pipeline",
                env.candidate_id,
            )
            generalization = fail_to_review_generalization(env.payload)

        new_payload = dict(env.payload)
        new_payload["generalization"] = generalization.to_doc()
        enriched = replace(env, payload=new_payload)
        return StageResult(envelope=enriched, control="continue")
