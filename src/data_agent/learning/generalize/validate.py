"""Static validation — the S4 dry-run stamp (`StaticValidation`, Contract A).

Four deterministic checks over the rewritten template(s):

  * `read_only_select`  — a single read-only SELECT: no `*`, no dict-family funcs (D52);
  * `explain_ok`        — the template resolves against the current catalog (a dry-run
                          parse; the D69 provenance qualify IS the schema check here —
                          an injected MCP `explainQuery` seam may replace it later);
  * `binds_to_subset_uses` — every slot.binds_to ∈ uses (the corpus_loader assertion);
  * `dag_ok`            — composite: orders unique, feeds_from valid, acyclic, under the
                          node cap, scalar-converging (single blueprints trivially pass).

ANY false ⇒ `outcome="fail_to_review"` (D52/D97) with a STABLE machine reason tag for
the first failing check — the value S7 routes on. Never raises; never auto-promotes.
"""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.expressions as exp

# Mirrors runtime/blueprint/models._MAX_SLOTS posture: each node is an unbudgeted
# warehouse query at replay, so a poisoned READ record with N nodes = N queries.
_MAX_NODES = 16

# Stable machine reason tags (S7 routes on these; keep them frozen).
REASON_EXPLAIN = "explain_failed"
REASON_BINDS = "binds_to_not_subset"
REASON_DAG = "dag_invalid"
REASON_READ_ONLY = "not_read_only_select"
REASON_UNREWRITABLE = "unrewritable_sql"
REASON_WHEN_COMPOSITE = "when_bearing_composite"


def _is_dict_family(node: exp.Expression) -> bool:
    """True for a ClickHouse dictionary-family function call (dictGet*, dictHas, …).

    The whole family is `dict`-prefixed; blocking the prefix is the D52 intent (an
    external dictionary read is not a reproducible, catalog-scoped SELECT)."""
    if isinstance(node, exp.Anonymous):
        return str(node.this or "").lower().startswith("dict")
    # Some dict funcs may parse to a named Func subclass exposing sql_name().
    name = getattr(node, "sql_name", None)
    if callable(name):
        try:
            return str(node.sql_name()).lower().startswith("dict")
        except Exception:  # pragma: no cover — defensive
            return False
    return False


def check_read_only_select(template: str) -> bool:
    """A single read-only SELECT with no `*` and no dict-family function (D52)."""
    try:
        ast = sqlglot.parse_one(
            template, dialect="clickhouse", error_level=sqlglot.ErrorLevel.RAISE
        )
    except Exception:
        return False
    if ast is None:
        return False
    if not isinstance(ast, (exp.Select, exp.Union, exp.With, exp.Subquery)):
        return False
    # No bare/table-qualified star in a projection (COUNT(*) is fine — its Star
    # parent is a function, not a Select/Column).
    for star in ast.find_all(exp.Star):
        parent = star.parent
        if parent is not None and isinstance(parent, (exp.Select, exp.Column)):
            return False
    for func in ast.find_all(exp.Func):
        if _is_dict_family(func):
            return False
    return True


def check_dag(composes: list[dict[str, Any]]) -> bool:
    """Validate a composite DAG: unique orders, valid + acyclic `feeds_from`, under
    the node cap, scalar-converging outputs (query/approval nodes only)."""
    if not composes:
        return True
    if len(composes) > _MAX_NODES:
        return False
    orders = [n.get("order") for n in composes]
    if any(not isinstance(o, int) or isinstance(o, bool) for o in orders):
        return False
    if len(set(orders)) != len(orders):
        return False
    order_set = set(orders)
    for node in composes:
        order = node.get("order")
        feeds = node.get("feeds_from") or []
        if not isinstance(feeds, list):
            return False
        for src in feeds:
            if src not in order_set:
                return False  # dangling edge
            if src == order:
                return False  # self-cycle
            if src > order:
                return False  # forward edge (orders are a topological index) → cycle risk
        # Scalar-converge: every declared output must be a scalar (F2 rejects `table`
        # intermediates downstream; a blueprint template must converge to a scalar).
        output = node.get("output") or {}
        if isinstance(output, dict) and any(v != "scalar" for v in output.values()):
            return False
    return True


def decide_outcome(
    *, explain_ok: bool, binds_to_subset_uses: bool, dag_ok: bool, read_only_select: bool
) -> tuple[str, str | None]:
    """Map the four checks to `(outcome, reason)` — first failing check wins, in the
    Contract-A field order (explain → binds → dag → read_only)."""
    for ok, reason in (
        (explain_ok, REASON_EXPLAIN),
        (binds_to_subset_uses, REASON_BINDS),
        (dag_ok, REASON_DAG),
        (read_only_select, REASON_READ_ONLY),
    ):
        if not ok:
            return "fail_to_review", reason
    return "ok", None
