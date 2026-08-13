"""`sql_by_ref` — the ONE ref→SQL resolution the post-S2 stages share.

Two stages resolve a model-cited `tool_call_ref` back to the SQL it stands for:
`extractor/validation.py::_validate_totality` (does every literal predicate of the
cited SQL have a parameterization entry? — the D97 gate) and `generalize/stage.py`
(which SQL does S4 rewrite into a template?). Both used to build the same
comprehension over `summary.tool_calls`, and both were wrong in the same way the
moment Release 1 landed: the final answer's SQL rides on an `answerWithTable`
designation (`SessionSummary.answer_sqls`), and `answerWithTable` is not a
`_SQL_TOOLS` member, so its `ToolCallSummary.sql` is `None`. A candidate citing the
answer's ref — the ONLY ref the session has when the designated query was never
dispatched as a `runQuery`, which is the case that projection exists for — resolved
to nothing, and the extractor declined it `unrewritable_sql`.

**A ref maps to a TUPLE, not a string, and the two readers need different things
from that.** One `answerWithTable` call can designate several queries (08 §B.3), so
one ref legitimately stands for more than one SQL:

  * `_validate_totality` checks EVERY SQL a cited ref stands for. Checking one and
    dropping the rest would let the second table's literal predicates through the
    totality gate unexamined — a silently dropped filter, which is the exact class
    the gate exists for.
  * `generalize` produces ONE template from ONE accepted SQL, so its stage collapses
    each ref to a single designation — SUBSET-CHECKED, in
    `stage.py::_collapse_designations`: the last designation wins only when every
    earlier one's literal predicates also appear in it, and otherwise the ref
    resolves to nothing and the candidate fails to review. It is NOT left to the
    strict rewrite to notice; that backstop has a hole for `role=inline` entries,
    documented at the collapse.

Order within a ref is the summary's own — the call's own SQL first, then its
designations in trail order — and an exact-duplicate string is dropped, matching
`loader._answer_sqls`. Refs with no usable SQL are ABSENT rather than mapped to an
empty tuple: every caller reads this through `.get(ref, ())`/truthiness, so absent
and empty are the same answer, and not materializing a row per `askUser` keeps the
map to the refs that can actually be cited.

A ref can also be absent for a SECOND reason, which is benign but worth knowing when
reading a decline: `loader._answer_sqls` dedupes designated SQL across the WHOLE
session, so a later `answerWithTable` that re-shows a table an earlier answer already
designated contributes no entry of its own and its ref never enters this map. A
candidate citing that later ref resolves to nothing and lands "no accepted SQL found
for source refs" → `fail_to_review`. That is the right outcome (a human sees a real
candidate with a citation nobody can resolve) and the cheap fix is on the model's
side — cite the ref that first showed the query.
"""

from __future__ import annotations

from .models import SessionSummary


def sql_by_ref(summary: SessionSummary) -> dict[str, tuple[str, ...]]:
    """Map every citable `tool_call_ref` to the SQL it stands for (see module doc)."""
    resolved: dict[str, tuple[str, ...]] = {}
    for tc in summary.tool_calls:
        if isinstance(tc.sql, str) and tc.sql.strip():
            resolved[tc.tool_call_ref] = (tc.sql,)
    for answer in summary.answer_sqls:
        existing = resolved.get(answer.tool_call_ref, ())
        if answer.sql not in existing:
            resolved[answer.tool_call_ref] = (*existing, answer.sql)
    return resolved


__all__ = ["sql_by_ref"]
