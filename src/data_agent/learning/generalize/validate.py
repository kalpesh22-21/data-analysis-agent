"""Static validation — the S4 dry-run stamp (`StaticValidation`, Contract A).

Four deterministic checks over the rewritten template(s):

  * `read_only_select`  — a single read-only SELECT: no `*`, no dict-family funcs (D52);
  * `explain_ok`        — the template resolves against the current catalog (a dry-run
                          parse; the D69 provenance qualify IS the schema check here —
                          an injected MCP `explainQuery` seam may replace it later);
  * `binds_to_subset_uses` — every slot.binds_to ∈ uses (the corpus_loader assertion);
  * `dag_ok`            — composite: orders unique, feeds_from valid, acyclic, under the
                          node cap, executable output kinds, and every `consumes` ref
                          resolvable (single blueprints trivially pass).

ANY false ⇒ `outcome="fail_to_review"` (D52/D97) with a STABLE machine reason tag for
the first failing check — the value S7 routes on. Never raises; never auto-promotes.
"""

from __future__ import annotations

from typing import Any

import sqlglot
import sqlglot.expressions as exp

from data_agent.runtime.blueprint.models import (
    NODE_OUTPUT_KINDS,
    SCALAR_CONSUME_REF,
    TABLE_CONSUME_REF,
)

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
    """Validate a composite DAG offline, mirroring the two authorities that will see
    it later: the corpus LOADER at landing (`_validate_blueprint_dag`) and the
    EXECUTOR at replay. Never raises — every malformed shape is `False`, because the
    caller turns that into `fail_to_review/dag_invalid`, the human valve. Being
    LOOSER than the loader means a candidate promotes and then dies at the landing
    write, past that valve; being STRICTER than the executor silently loses a
    capability. Checked:

      * orders unique + integral, node count under the cap;
      * `feeds_from` edges resolve, no self-edge, no forward edge (⇒ acyclic);
      * `output` maps names → a `NODE_OUTPUT_KINDS` value. Scalar-only until table
        intermediates landed in the executor (`_materialize_node`, §2.3) — the canon's
        `bp-earnings-by-department-via-scratch-join` was a DAG the runtime runs and
        the loop could not emit;
      * every `consumes` ref parses as ONE of the two grammars and resolves —
        loader §1.2(h): `$N` requires N ∈ feeds_from AND a declared `table` output;
        `$N.name` requires N ∈ feeds_from AND a declared SCALAR output named `name`;
        a ref in neither grammar is rejected outright;
      * a `table` output consumed downstream is passed AS a table. This mirrors
        `executor.py:534` EXACTLY, whole-DAG rather than per-node: the executor asks
        "is there a table intermediate and NO table consume anywhere", so a node
        declaring both a table and a scalar output and consumed only for its scalar
        still runs, provided some pair in the DAG does pass a table. A per-node rule
        would fail that shape to review though the runtime and the loader accept it.

    NOT checked (structurally invisible here): the loader's rule that a table-consume
    placeholder must appear as a `scratch.<placeholder>` FROM/JOIN source. That needs
    the node TEMPLATES, and this sees the S3 plan — `builder` joins S4's templates in
    afterwards. Pinned in `tests/learning/generalize/test_dag_loader_parity_qa.py`."""
    if not composes:
        return True
    if len(composes) > _MAX_NODES:
        return False
    if any(not isinstance(n, dict) for n in composes):
        return False  # a poisoned READ record: `.get` on a non-dict would raise
    orders = [n.get("order") for n in composes]
    if any(not isinstance(o, int) or isinstance(o, bool) for o in orders):
        return False
    if len(set(orders)) != len(orders):
        return False
    order_set = set(orders)

    # Pass 1 — output kinds, indexed by order for the consume cross-checks below.
    outputs_by_order: dict[int, dict[str, str]] = {}
    for node in composes:
        output = node.get("output") or {}
        if not isinstance(output, dict):
            return False
        for name, kind in output.items():
            # `isinstance` BEFORE the frozenset test: `kind not in NODE_OUTPUT_KINDS`
            # hashes `kind`, so an unhashable `output: {"o": []}` (legal JSON, and this
            # reads a rehydrated candidate record) would raise TypeError out of a
            # function contracted never to raise.
            if not isinstance(name, str) or not isinstance(kind, str):
                return False
            if kind not in NODE_OUTPUT_KINDS:
                return False
        outputs_by_order[node["order"]] = output

    # Pass 2 — edges and consume references.
    fed_orders: set[int] = set()  # every node some other node feeds from
    table_consumed: set[int] = set()  # every node passed downstream AS a table
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
            fed_orders.add(src)
        consumes = node.get("consumes") or {}
        if not isinstance(consumes, dict):
            return False
        for ref in consumes.values():
            ref_str = str(ref)
            table_match = TABLE_CONSUME_REF.match(ref_str)
            if table_match is not None:
                src_order = int(table_match.group(1))
                if src_order not in feeds:
                    return False
                if "table" not in outputs_by_order.get(src_order, {}).values():
                    return False
                table_consumed.add(src_order)
                continue
            scalar_match = SCALAR_CONSUME_REF.match(ref_str)
            if scalar_match is None:
                return False  # neither grammar — the loader rejects this outright
            src_order, out_name = int(scalar_match.group(1)), scalar_match.group(2)
            if src_order not in feeds:
                return False
            if outputs_by_order.get(src_order, {}).get(out_name) != "scalar":
                return False

    has_table_intermediate = any(
        order in fed_orders and "table" in outputs_by_order.get(order, {}).values()
        for order in order_set
    )
    if has_table_intermediate and not table_consumed:
        return False  # no table consume anywhere → the executor reports UNSUPPORTED
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
