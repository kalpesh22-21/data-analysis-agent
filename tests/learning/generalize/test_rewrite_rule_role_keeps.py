"""`role=rule` KEEPS the predicate and annotates the blueprint — ISSUES H3/H5/H6.

## What changed and why

The rewrite used to DELETE a `role=rule` predicate from the template, on the reading
that the runtime would re-apply the rule. It does not. A learned `uses_rules` entry is a
bare catalog id STRING; `runtime/blueprint/rules.py::parse_rule` returns `None` for
anything that is not a dict carrying `resolve_via: resolveValues(col, 'concept')` plus
`table`; and `executor._expand_rules` skips every static entry. So the deleted filter was
never re-applied by anything — a silently dropped filter, the D56 wrong-answer class,
shipped as `outcome: ok`.

The deletion was ALSO unsound as an edit, and the measurements are why this file is long.
Each row below is what the deleting rewrite produced from an ordinary, valid candidate:

    parent shape        | deleting rewrite produced           | class
    --------------------|-------------------------------------|------------------------
    AND conjunct        | sibling kept                        | (the intended case)
    sole WHERE          | `... WHERE  GROUP BY ...`           | UNPARSEABLE
    sole HAVING         | `... GROUP BY dept HAVING`          | UNPARSEABLE
    sub-select WHERE    | `(SELECT ... FROM p WHERE )`        | UNPARSEABLE
    NOT / Paren         | `WHERE NOT` / `WHERE ()`            | UNPARSEABLE
    OR                  | `KeyError: 'expression'`            | RAW EXCEPTION
    sole JOIN-ON        | `FROM p, q`                         | SILENT CROSS JOIN
    sumIf(x, cond)      | `sumIf(x)`                          | SILENT WRONG METRIC
    countIf(cond)       | `countIf()`                         | SILENT WRONG METRIC
    AND in an OR arm    | `a='X1' OR (a='X2')`                | SILENTLY WIDER
    AND under NOT       | `NOT (a='X2')`                      | SILENTLY WIDER
    one member of an IN | whole `IN` list deleted             | SILENTLY WIDER

The first fix attempt was an allowlist of safe parent shapes. Review broke it twice — the
OR-arm/NOT rows and the IN row above are exactly what it let through, and the IN one was
WORSE than the bug it replaced, because before the fix that plan died loudly on an
unparseable `WHERE` and after it the template was valid and wrong. Three findings in one
slice against a list of shapes is the argument for not having the list: KEEPING is total.
It needs no shape analysis, it cannot widen a result, and the template a reviewer
approves is the SQL the analyst accepted.

## What this file pins

That every one of those shapes now comes through UNTOUCHED, that the rule id is still
recorded, and that the guards which are still load-bearing (locate-verify, the inline
pre/post checks, the output gate) still fire.
"""

from __future__ import annotations

import pytest
import sqlglot

from data_agent.learning.generalize.builder import generalize_blueprint
from data_agent.learning.generalize.rewrite import RewriteError, rewrite_sql_to_template

from .helpers import CATALOG

_TABLE = "dbpcm_warehouse.payroll"


def _rule(column: str = "register_type", value: str = "EARN") -> dict:
    return {
        "locator": {"table": _TABLE, "column": column, "value": value},
        "role": "rule",
        "rule_id": "gross_earnings",
    }


def _normalized(sql: str) -> str:
    """The accepted SQL as sqlglot re-renders it — the rewrite parses and re-emits, so
    `IN ('A','B')` comes back `IN ('A', 'B')`. That whitespace is the renderer's, not an
    edit, and comparing against it is what lets these tests assert byte equality."""
    return sqlglot.parse_one(sql, dialect="clickhouse").sql(dialect="clickhouse")


# --- the keeping pin, over every shape the deleting rewrite got wrong --------------


@pytest.mark.parametrize(
    ("sql", "was"),
    [
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE dept = '0420' AND register_type = 'EARN'",
            "the one shape the deleting rewrite handled correctly — and even here the "
            "filter it removed was never re-applied by anything",
            id="and-conjunct",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE register_type = 'EARN' GROUP BY dept",
            "H5: the sole conjunct left `WHERE  GROUP BY`, unparseable, and the "
            "ParseError escaped S4 entirely",
            id="sole-where",
        ),
        pytest.param(
            "SELECT dept, sum(amount) AS t FROM p GROUP BY dept HAVING register_type = 'EARN'",
            "the same, one clause over",
            id="sole-having",
        ),
        pytest.param(
            "SELECT t FROM (SELECT sum(amount) AS t FROM p WHERE register_type = 'EARN') AS s",
            "and again inside a derived table",
            id="sub-select-where",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE dept = '0420' OR register_type = 'EARN'",
            "a bare `KeyError: 'expression'` out of the sqlglot renderer, past every "
            "`except RewriteError` in the plane",
            id="or-parent",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE dept = 'X1' OR (dept = 'X2' AND register_type = 'EARN')",
            "REVIEW BLOCKER 1: an ordinary `And` parent inside an OR arm — the "
            "allowlist dropped it and the template matched every X2 row",
            id="and-in-an-or-arm",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE NOT (dept = 'X2' AND register_type = 'EARN')",
            "REVIEW BLOCKER 1, negated: `NOT (a AND b)` minus `b` excludes strictly less",
            id="and-under-a-not",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE xor(dept = 'X2', register_type = 'EARN')",
            "XOR in function form — infix `a XOR b` does not parse in the ClickHouse "
            "dialect (sqlglot 30.12), `xor(a, b)` gives the same `exp.Xor` node",
            id="xor-function-form",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE NOT register_type = 'EARN'",
            "`WHERE NOT`, unparseable",
            id="not-parent",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE (register_type = 'EARN')",
            "`WHERE ()`, unparseable",
            id="paren",
        ),
        pytest.param(
            "SELECT sum(p.amount) AS t FROM p JOIN q ON p.register_type = 'EARN'",
            "a SILENT CROSS JOIN — `FROM p, q` — which changes the row count and says "
            "nothing",
            id="sole-join-on",
        ),
        pytest.param(
            "SELECT sum(CASE WHEN register_type = 'EARN' THEN amount ELSE 0 END) AS t FROM p",
            "`CASE WHEN  THEN`, unparseable",
            id="case-when",
        ),
        pytest.param(
            "SELECT sumIf(amount, register_type = 'EARN') AS t FROM p",
            "H6: `sumIf(amount)` — VALID SQL computing a different number under the "
            "same alias. The live deductions ratio landed both metrics byte-identical",
            id="sumif",
        ),
        pytest.param(
            "SELECT countIf(register_type = 'EARN') AS t FROM p",
            "`countIf()`, the same class",
            id="countif",
        ),
        pytest.param(
            "SELECT sum(amount) AS t FROM p WHERE register_type IN ('DDUCT','EARN')",
            "REVIEW BLOCKER 2: the locator names one member, `_find_literal` resolves "
            "to the whole `IN` node, and the drop took `'DDUCT'` with it — uncovered by "
            "any rule. Loud (unparseable) before H5 was fixed, silent after",
            id="multi-member-in",
        ),
    ],
)
def test_a_rule_predicate_is_kept_verbatim(sql: str, was: str) -> None:
    """ONE rule, one property: the template is the accepted SQL. No shape analysis, no
    allowlist, nothing to keep complete — which is why this replaced the allowlist rather
    than extending it."""
    assert rewrite_sql_to_template(sql, [_rule()], strict=True) == _normalized(sql), was


def test_the_rule_id_is_still_recorded_next_to_the_kept_predicate() -> None:
    """KEPT AND ANNOTATED — the annotation is the half that survives. `uses_rules` is
    what a reviewer reads, what the D48 canonical key hashes, and what a future runtime
    could act on; what changed is that it now agrees with the template instead of
    standing in for something the template no longer said."""
    sql = (
        "SELECT sum(gross_pay) AS t FROM payroll.payroll_fact "
        "WHERE department = '0420' AND record_type = 'EARNING'"
    )
    payload = {
        "kind": "single",
        "intent": "earnings",
        "source_tool_call_refs": ["tc1"],
        "parameterization": [
            {"locator": {"table": "payroll.payroll_fact", "column": "department",
                         "value": "0420"},
             "role": "slot",
             "slot": {"name": "department",
                      "binds_to": "payroll.payroll_fact.department"}},
            {"locator": {"table": "payroll.payroll_fact", "column": "record_type",
                         "value": "EARNING"},
             "role": "rule", "rule_id": "rule.earning_record_type"},
        ],
        "result_signature": {"grain": {"columns": [], "verifiable": False}},
    }
    gen = generalize_blueprint(payload, {"tc1": sql}, CATALOG)

    assert gen.uses_rules == ("rule.earning_record_type",)
    assert "record_type = 'EARNING'" in gen.sql_template  # kept
    assert "{department}" in gen.sql_template  # and the slot still substituted
    assert gen.static_validation.outcome == "ok"


def test_a_rule_literal_that_is_not_in_the_accepted_sql_still_refuses() -> None:
    """LOCATE-VERIFY STAYS. The rewrite no longer edits for `role=rule`, but it still
    LOOKS: the plan is a claim about THIS SQL, and an entry naming a predicate that is
    not in it is wrong whether or not anything would have been edited. Dropping the
    check because the edit went away would turn the plan into decoration."""
    with pytest.raises(RewriteError, match="not found in the accepted SQL"):
        rewrite_sql_to_template(
            "SELECT sum(amount) AS t FROM p WHERE register_type = 'EARN'",
            [_rule(value="NOPE")],
            strict=True,
        )


def test_a_rule_predicate_in_an_aggregate_is_no_longer_a_special_case() -> None:
    """H6 closed by construction rather than by refusal.

    The first fix REFUSED this candidate (a rule may not cover a predicate inside an
    aggregate condition) because deleting the condition redefined the metric. Keeping
    the predicate makes the question moot: the live deductions-to-earnings ratio —
    whose two rule entries both live inside `sumIf` conditions — now generalizes, with
    both metrics intact and distinct.

    This is the case for keeping stated as a capability rather than as a safety
    argument: the allowlist made a whole class of ordinary analyst SQL unlearnable."""
    sql = (
        "SELECT p.employee_code, "
        "sumIf(p.amount, p.register_type = 'DDUCT') / sumIf(p.amount, p.register_type = 'EARN') "
        "AS ratio FROM dbpcm_warehouse.payroll AS p "
        "WHERE p.register_type IN ('DDUCT','EARN') "
        "GROUP BY p.employee_code"
    )
    template = rewrite_sql_to_template(
        sql,
        [
            _rule(value="DDUCT"),
            {"locator": {"table": _TABLE, "column": "register_type", "value": "EARN"},
             "role": "rule", "rule_id": "gross_earnings"},
        ],
        strict=True,
    )
    assert template == _normalized(sql)
    assert "sumIf(p.amount, p.register_type = 'DDUCT')" in template
    assert "sumIf(p.amount, p.register_type = 'EARN')" in template


# --- the guards that are still load-bearing ----------------------------------------
#
# Slot substitution is the only edit left, so these are cheap. They are kept because
# they check the OUTPUT rather than the input's shape: the deleting rewrite's failures
# were all found by them, and the next edit somebody adds will be too.


def _restore_the_deleting_drop(monkeypatch) -> None:
    """`role=rule` as it behaved before this slice: pop the comparison, whatever holds
    it. Used to prove the surviving guards still catch what it did."""
    from data_agent.learning.generalize import rewrite as module

    comparisons = module._COMPARISONS
    original = module._find_literal

    def _deleting_find(ast, column, value):
        literal = original(ast, column, value)
        if literal is not None:
            node = literal
            while node is not None and not isinstance(node, comparisons):
                node = node.parent
            if node is not None:
                node.pop()
        return literal

    monkeypatch.setattr(module, "_find_literal", _deleting_find)


def test_the_output_gate_still_refuses_a_template_that_does_not_parse(monkeypatch):
    """H5's exact shape, driven by restoring the deleting drop. The gate re-parses what
    was rendered — `{slot}` rewritten to `:slot` first, as the runtime binder and
    `structural_key` do — so an unparseable template is a `RewriteError` here rather
    than a `sqlglot.ParseError` two frames later inside `canonical_ast_norm`."""
    _restore_the_deleting_drop(monkeypatch)
    with pytest.raises(RewriteError, match="does not parse") as excinfo:
        rewrite_sql_to_template(
            "SELECT sum(amount) AS t FROM p WHERE register_type = 'EARN' GROUP BY dept",
            [_rule()],
            strict=True,
        )
    assert "WHERE  GROUP BY" in str(excinfo.value)


def test_the_output_gate_still_refuses_an_argument_that_went_missing(monkeypatch):
    """H6's exact shape, same method. This is the half nothing else can see: nothing
    downstream of S4 looks at arity — not the read-only check, not the provenance walk,
    not the corpus loader — and `sumIf(amount)` parses."""
    _restore_the_deleting_drop(monkeypatch)
    with pytest.raises(RewriteError, match="argument was deleted") as excinfo:
        rewrite_sql_to_template(
            "SELECT sumIf(amount, register_type = 'EARN') AS t FROM p",
            [_rule()],
            strict=True,
        )
    assert "sumif/1" in str(excinfo.value)


def test_the_inline_post_condition_still_catches_a_literal_that_vanished():
    """REVIEW BLOCKER 2's own guard, kept as a standing invariant and tested directly.

    The two halves are different claims. The PRE-pass reads the pristine AST and proves
    the plan is honest about the accepted SQL; the POST-condition proves the rewrite was
    honest about the plan. Only the second can catch an edit that removed more than its
    own predicate — which is exactly what a rule locator naming ONE member of an `IN`
    list did: it resolved to the whole list and deleted the literal the INLINE entry had
    declared, silently, stamped `ok`.

    Driven against the functions rather than through a monkeypatched rewrite, because
    the removal is the thing under test and any edit that removes the predicate proves
    the same invariant. It passes trivially today (slot substitution removes nothing);
    it is kept for the next edit somebody adds."""
    from data_agent.learning.generalize.rewrite import (
        _check_inline_literals,
        _recheck_inline_literals,
    )

    sql = "SELECT sum(amount) AS t FROM p WHERE register_type IN ('DDUCT','EARN')"
    inline = {
        "locator": {"table": _TABLE, "column": "register_type", "value": "DDUCT,EARN"},
        "role": "inline",
        "why": "the metric is defined over both",
    }
    ast = sqlglot.parse_one(sql, dialect="clickhouse")

    # The pre-pass locates it in the pristine tree and hands it on for re-checking.
    entries = _check_inline_literals(ast, sql, [inline])
    assert entries == [("register_type", "DDUCT,EARN")]

    # Any edit that takes the predicate with it — this is what the rule drop did.
    ast.find(sqlglot.exp.Where).pop()

    with pytest.raises(RewriteError, match="removed the role=inline literal"):
        _recheck_inline_literals(ast, entries)


def test_a_renderer_failure_is_a_rewrite_error(monkeypatch):
    """`ast.sql()` is not total over an edited tree — the OR drop raised a bare
    `KeyError`, past every `except RewriteError` in the plane. Keeping removed that
    particular shape; the wrap stays because it closes the CLASS."""
    import sqlglot.expressions as exp

    def _boom(self, *args, **kwargs):
        raise KeyError("expression")

    monkeypatch.setattr(exp.Expression, "sql", _boom)
    with pytest.raises(RewriteError, match="could not be rendered"):
        rewrite_sql_to_template(
            "SELECT sum(amount) AS t FROM p WHERE dept = '0420'",
            [{"locator": {"table": _TABLE, "column": "dept", "value": "0420"},
              "role": "slot", "slot": {"name": "dept"}}],
            strict=True,
        )
