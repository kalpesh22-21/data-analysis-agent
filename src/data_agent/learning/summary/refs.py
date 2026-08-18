"""`sql_by_ref` — the ONE ref→SQL resolution the post-S2 stages share.

Two stages resolve a model-cited `tool_call_ref` back to the SQL it stands for: the D97
totality gate and the S4 rewrite. Both used to build the same comprehension over
`summary.tool_calls`, and both were wrong the moment Release 1 landed — the final answer's SQL
rides on an `answerWithTable` designation, and that tool is not a SQL tool, so its
`ToolCallSummary.sql` is `None`. A candidate citing the answer's ref, which is the ONLY ref a
session has when the designated query was never dispatched, resolved to nothing.

A REF MAPS TO A TUPLE, not a string, because one `answerWithTable` can designate several
queries — and the two readers need different things from that. `_validate_totality` checks
EVERY SQL a cited ref stands for, since checking one and dropping the rest would let the
second table's predicates through unexamined. `generalize` needs ONE template, so its stage
collapses each ref to a single designation, SUBSET-CHECKED in `_collapse_designations`.

Order within a ref is the summary's own and exact duplicates are dropped. Refs with no usable
SQL are ABSENT rather than mapped to an empty tuple — every caller reads through
`.get(ref, ())`, so absent and empty are the same answer. A ref can also be absent for a
second, benign reason: `loader._answer_sqls` dedupes designated SQL across the WHOLE session,
so a later `answerWithTable` re-showing an earlier table contributes no entry and its ref
never enters this map. A candidate citing it lands `fail_to_review`, which is the right
outcome — a human sees a real candidate with a citation nobody can resolve.
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
