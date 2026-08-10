"""blueprint/models.py — the typed parse layer over the full-DAG JSON (§2.1).

The corpus stores the DAG as additive JSON-string properties on the `:Blueprint`
node (§1.1); `getBlueprint`/`BlueprintDetail` carry them back JSON-decoded. This
module parses those decoded structures into the frozen typed value objects the
executor (Slice B) and the pure functions (`slots`/`when`/`verify`) consume.

Parsing is STRUCTURAL and fail-loud: a malformed shape raises `BlueprintParseError`
(the same posture the corpus loader takes at WRITE, §1.2 — but this is the READ
side, defense-in-depth against a corrupt/legacy stored value). Everything here is
pure: no I/O, no LLM, no SQL execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Valid slot `type`s (D41/D49). Kept as a frozenset so the parse layer and the
# loader validation agree on the closed set.
SLOT_TYPES: frozenset[str] = frozenset(
    {"string", "entity", "enum", "period", "as_of_date", "list",
     "relative_window", "period_range"}
)

# One plain-English gloss per SLOT_TYPE, surfaced by `getBlueprint` so the MODEL
# understands the slot vocabulary (a `period` is a warehouse pay-period key, NOT a
# calendar date; a `relative_window` is a bare integer N, not "6 months"; etc.).
# INVARIANT: `set(SLOT_TYPE_GLOSS) == set(SLOT_TYPES)` — a new slot type cannot
# ship un-glossed (parity test in `tests/runtime/blueprint/test_models.py`).
SLOT_TYPE_GLOSS: dict[str, str] = {
    "string": "a named value (e.g. a specific department).",
    "entity": "a named value (e.g. a specific department or employee).",
    "enum": "one value from a fixed, closed set of allowed options.",
    "period": "a warehouse pay-period key, NOT a free calendar date.",
    "as_of_date": (
        "a warehouse pay-period key to evaluate as of, NOT a free calendar date."
    ),
    "list": "a set of values, matched as IN(...).",
    "relative_window": (
        "a whole number N of units (e.g. \"last N months\" -> pass the integer 6, "
        "not \"6 months\")."
    ),
    "period_range": "an explicit {start, end} date range.",
}

# Fallback gloss for a slot whose `type` is missing or unknown (a corrupt/legacy
# stored slot): `getBlueprint` still includes the slot, glossed generically rather
# than dropping it. Deliberately NOT a `SLOT_TYPE_GLOSS` key (keeps the parity).
GENERIC_SLOT_TYPE_GLOSS = "a value for this slot."


def slot_type_gloss(type_: Any) -> str:
    """The plain-English gloss for a slot `type` — the generic fallback for a
    missing/unknown type so a slot is never dropped from `getBlueprint`."""
    if isinstance(type_, str) and type_ in SLOT_TYPE_GLOSS:
        return SLOT_TYPE_GLOSS[type_]
    return GENERIC_SLOT_TYPE_GLOSS


NODE_KINDS: frozenset[str] = frozenset({"query", "approval"})  # D59c (`guard` cut)
ON_VIOLATION: frozenset[str] = frozenset({"abort", "skip", "ask"})

# The CLOSED set of `output` kinds a node may declare — the two things the executor
# knows how to pass downstream: a `scalar` (bound as a typed literal into the
# consumer's `{placeholder}`) and a `table` (materialized to a session scratch table
# and AST-JOINed into the consumer, `executor._materialize_node`, §2.3). A frozenset
# for the same reason as `NODE_KINDS`: the parse layer, the loader and the OFFLINE
# learning validator (`learning/generalize/validate.py::check_dag`) must agree on one
# set — they drifted once (learning stayed scalar-only after table intermediates
# landed) and the loop could not emit a shape the runtime executes.
NODE_OUTPUT_KINDS: frozenset[str] = frozenset({"scalar", "table"})

# The two `consumes` reference grammars (§2.3) — the node contract, so they live with
# the node model rather than in each consumer:
#
#   TABLE_CONSUME_REF   `$1`            node 1's WHOLE table output, materialized to
#                                       scratch and injected as a `scratch.<placeholder>`
#                                       FROM/JOIN token.
#   SCALAR_CONSUME_REF  `$3.company_avg` one NAMED scalar output, bound as a typed
#                                       literal into `{placeholder}`.
#
# SINGLE SOURCE: the executor (replay), the corpus loader (landing) and the offline
# S4 validator all match refs with THESE objects. They were three hand-copied regexes;
# the loop had copied only the table one, which is why S4 could stamp `dag_ok=True` on
# a consume shape the loader rejects. Identity + behaviour pinned in
# `tests/learning/generalize/test_dag_loader_parity_qa.py`.
TABLE_CONSUME_REF = re.compile(r"^\$(\d+)$")
SCALAR_CONSUME_REF = re.compile(r"^\$(\d+)\.([A-Za-z_][A-Za-z0-9_]*)$")

# `relative_window` integer bounds (D49). The CEILING is a HARD safety cap: an
# authored `max_value` may narrow the window but never widen it past this, so an
# absurd `INTERVAL 999999 MONTH` can neither be authored (SlotSpec.parse gate) nor
# bound (the resolver clamps to it). `slots.py` mirrors these as its resolver
# defaults; kept here so the parse-time bounds gate and the resolver agree.
RELATIVE_WINDOW_FLOOR = 1
RELATIVE_WINDOW_CEILING = 120

# Hard cap on declared `slots` (reviewer S3). Each `binds_to` slot can fire an
# unbudgeted inner DISTINCT domain probe at execution, so a poisoned READ record
# with N slots = N warehouse queries — cap it (mirrors the `composes` node cap in
# the corpus loader). A Phase-1 blueprint has a handful of slots; 16 is generous.
_MAX_SLOTS = 16


class BlueprintParseError(Exception):
    """A stored blueprint DAG structure is malformed (fail-loud, §2.1)."""


@dataclass(frozen=True)
class SlotSpec:
    """One typed slot parameter (`slots_json` entry, §1.1 / 04-blueprints §Model)."""

    name: str
    type: str
    required: bool = True
    binds_to: str | None = None  # "database.table.column" the value validates against
    enum_values: tuple[str, ...] | None = None  # closed set for `type: enum`
    optional_pattern: str | None = None  # SQL fragment for an absent optional slot
    # Inclusive bounds for a `relative_window` integer (trailing "last N <unit>",
    # D41/D49). Meaningful ONLY for `relative_window`; ignored for every other type.
    # `None` defers to the resolver's safety defaults (lo=1, hi=120 hard ceiling so
    # an absurd `INTERVAL 999999 MONTH` can't be authored or bound).
    min_value: int | None = None
    max_value: int | None = None

    @classmethod
    def parse(cls, raw: Any) -> SlotSpec:
        if not isinstance(raw, dict):
            raise BlueprintParseError(f"slot must be an object, got {type(raw).__name__}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise BlueprintParseError("slot is missing a non-empty 'name'")
        type_ = raw.get("type")
        # `isinstance` FIRST: `x not in <frozenset>` hashes x, so an unhashable stored
        # value (`"type": []`) would raise TypeError — escaping every `except
        # BlueprintParseError` on the read path and aborting the whole corpus load with
        # an un-wrapped third-party error. Same guard on every closed-set test below.
        if not isinstance(type_, str) or type_ not in SLOT_TYPES:
            raise BlueprintParseError(
                f"slot {name!r} has unknown type {type_!r} (allowed: {sorted(SLOT_TYPES)})"
            )
        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise BlueprintParseError(f"slot {name!r} 'required' must be a boolean")
        binds_to = raw.get("binds_to")
        if binds_to is not None and not isinstance(binds_to, str):
            raise BlueprintParseError(f"slot {name!r} 'binds_to' must be a string")
        # L3: the windowed-period types never consume a warehouse DOMAIN (their
        # values are validated structurally, not matched against a DISTINCT set), so
        # a `binds_to` here would only fire a useless probe — reject it at parse.
        if binds_to is not None and type_ in ("relative_window", "period_range"):
            raise BlueprintParseError(
                f"slot {name!r} of type {type_!r} must not declare 'binds_to' — a "
                "windowed-period slot consumes no domain (it would fire a useless probe)"
            )
        enum_values_raw = raw.get("enum_values")
        enum_values: tuple[str, ...] | None = None
        if enum_values_raw is not None:
            if not isinstance(enum_values_raw, list) or not all(
                isinstance(v, str) for v in enum_values_raw
            ):
                raise BlueprintParseError(f"slot {name!r} 'enum_values' must be a list of strings")
            enum_values = tuple(enum_values_raw)
        if type_ == "enum" and not enum_values:
            raise BlueprintParseError(f"enum slot {name!r} requires non-empty 'enum_values'")
        optional_pattern = raw.get("optional_pattern")
        if optional_pattern is not None and not isinstance(optional_pattern, str):
            raise BlueprintParseError(f"slot {name!r} 'optional_pattern' must be a string")
        # `min_value`/`max_value` — ints when present (only meaningful for
        # `relative_window`; carried but unused for other types). A bool is an int
        # subclass, so reject it explicitly (a `true` bound is an authoring bug).
        min_value = raw.get("min_value")
        if min_value is not None and (not isinstance(min_value, int) or isinstance(min_value, bool)):
            raise BlueprintParseError(f"slot {name!r} 'min_value' must be an integer")
        max_value = raw.get("max_value")
        if max_value is not None and (not isinstance(max_value, int) or isinstance(max_value, bool)):
            raise BlueprintParseError(f"slot {name!r} 'max_value' must be an integer")
        # H1b: an authored bound must satisfy `1 <= min_value <= max_value <=
        # RELATIVE_WINDOW_CEILING` so an absurd `max_value: 999999` or an inverted
        # `min > max` NEVER loads (the ceiling is a HARD cap, D49). Checked at WRITE
        # (BlueprintParseError) — the resolver additionally clamps at READ (H1a).
        lo = min_value if min_value is not None else RELATIVE_WINDOW_FLOOR
        hi = max_value if max_value is not None else RELATIVE_WINDOW_CEILING
        if min_value is not None or max_value is not None:
            if not (RELATIVE_WINDOW_FLOOR <= lo <= hi <= RELATIVE_WINDOW_CEILING):
                raise BlueprintParseError(
                    f"slot {name!r} bounds must satisfy "
                    f"{RELATIVE_WINDOW_FLOOR} <= min_value <= max_value <= "
                    f"{RELATIVE_WINDOW_CEILING}, got min={min_value!r} max={max_value!r}"
                )
        return cls(
            name=name,
            type=type_,
            required=required,
            binds_to=binds_to,
            enum_values=enum_values,
            optional_pattern=optional_pattern,
            min_value=min_value,
            max_value=max_value,
        )


@dataclass(frozen=True)
class WhenClause:
    """A node precondition (`when`, §2.6 / 04-blueprints §Control-flow)."""

    expr: str
    on_violation: str
    message: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> WhenClause:
        if not isinstance(raw, dict):
            raise BlueprintParseError("'when' must be an object")
        expr = raw.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            raise BlueprintParseError("'when' requires a non-empty 'expr'")
        on_violation = raw.get("on_violation")
        if not isinstance(on_violation, str) or on_violation not in ON_VIOLATION:
            raise BlueprintParseError(
                f"'when.on_violation' must be one of {sorted(ON_VIOLATION)}, got {on_violation!r}"
            )
        message = raw.get("message")
        if message is not None and not isinstance(message, str):
            raise BlueprintParseError("'when.message' must be a string")
        return cls(expr=expr, on_violation=on_violation, message=message)


@dataclass(frozen=True)
class Node:
    """One `composes` DAG node (§1.1). `output` maps each name → a `NODE_OUTPUT_KINDS`
    value ('scalar' | 'table').

    Both kinds now execute: scalar-converging DAGs bind literals, and a `table`
    intermediate consumed as `{ph: "$N"}` is materialized to session scratch (§2.3)
    when a `scratch_client` is wired (the original F2 scalar-only boundary was lifted
    there; without a scratch client the executor still degrades to UNSUPPORTED). This
    parse layer records the shape faithfully; the loader (§1.2) enforces the DAG
    invariants (including the table-consume ⇄ table-output pairing).
    """

    order: int
    node_kind: str = "query"
    feeds_from: tuple[int, ...] = ()
    consumes: dict[str, Any] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)
    sql_template: str | None = None
    when: WhenClause | None = None
    requires_approval: dict[str, Any] | None = None

    @classmethod
    def parse(cls, raw: Any) -> Node:
        if not isinstance(raw, dict):
            raise BlueprintParseError("compose node must be an object")
        order = raw.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            raise BlueprintParseError("compose node requires an integer 'order'")
        node_kind = raw.get("node_kind", "query")
        if not isinstance(node_kind, str) or node_kind not in NODE_KINDS:
            raise BlueprintParseError(
                f"node {order} has unknown node_kind {node_kind!r} (allowed: {sorted(NODE_KINDS)})"
            )
        feeds_raw = raw.get("feeds_from", []) or []
        if not isinstance(feeds_raw, list) or not all(
            isinstance(f, int) and not isinstance(f, bool) for f in feeds_raw
        ):
            raise BlueprintParseError(f"node {order} 'feeds_from' must be a list of integers")
        consumes = raw.get("consumes") or {}
        if not isinstance(consumes, dict):
            raise BlueprintParseError(f"node {order} 'consumes' must be an object")
        output_raw = raw.get("output") or {}
        if not isinstance(output_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v in NODE_OUTPUT_KINDS
            for k, v in output_raw.items()
        ):
            raise BlueprintParseError(
                f"node {order} 'output' must map names → "
                f"{'|'.join(repr(k) for k in sorted(NODE_OUTPUT_KINDS))}"
            )
        sql_template = raw.get("sql_template")
        if sql_template is not None and not isinstance(sql_template, str):
            raise BlueprintParseError(f"node {order} 'sql_template' must be a string")
        when = WhenClause.parse(raw["when"]) if raw.get("when") is not None else None
        requires_approval = raw.get("requires_approval")
        if requires_approval is not None and not isinstance(requires_approval, dict):
            raise BlueprintParseError(f"node {order} 'requires_approval' must be an object")
        return cls(
            order=order,
            node_kind=node_kind,
            feeds_from=tuple(feeds_raw),
            consumes=dict(consumes),
            output=dict(output_raw),
            sql_template=sql_template,
            when=when,
            requires_approval=requires_approval,
        )


@dataclass(frozen=True)
class ResultGrain:
    """The blueprint's DECLARED result grain (`result_grain_json`, §1.1 / §4.3).

    D56 checks the result's row-count against `COUNT(DISTINCT columns)`; a
    blueprint that declares its own grain UNVERIFIABLE (`verifiable=False`) skips
    the row-count teeth (the `grain_verifiable:false` skip rule, §4.2) but still
    passes the signature check.
    """

    columns: tuple[str, ...] = ()
    verifiable: bool = True

    @classmethod
    def parse(cls, raw: Any) -> ResultGrain:
        # A bare list is the common authoring shape (["EmployeeCode","pay_period"]);
        # a dict `{columns:[...], verifiable:bool}` allows the explicit skip flag.
        if raw is None:
            return cls(columns=(), verifiable=True)
        if isinstance(raw, list):
            columns = raw
            verifiable = True
        elif isinstance(raw, dict):
            columns = raw.get("columns", []) or []
            verifiable = raw.get("verifiable", True)
            if not isinstance(verifiable, bool):
                raise BlueprintParseError("result_grain 'verifiable' must be a boolean")
        else:
            raise BlueprintParseError("result_grain must be a list or an object")
        if not isinstance(columns, list) or not all(isinstance(c, str) and c for c in columns):
            raise BlueprintParseError("result_grain columns must be a list of non-empty strings")
        return cls(columns=tuple(columns), verifiable=verifiable)


@dataclass(frozen=True)
class Blueprint:
    """The parsed, executable-shape blueprint DAG (§2.1). Slice A produces it;
    the Slice-B executor walks it. Single-node blueprints carry a top-level
    `sql_template` and an empty `composes`."""

    id: str
    intent: str
    resolves: dict[str, str] = field(default_factory=dict)
    slots: tuple[SlotSpec, ...] = ()
    uses_rules: tuple[Any, ...] = ()
    sql_template: str | None = None
    composes: tuple[Node, ...] = ()
    result_grain: ResultGrain = field(default_factory=ResultGrain)

    def slot(self, name: str) -> SlotSpec | None:
        return next((s for s in self.slots if s.name == name), None)

    @property
    def is_single_node(self) -> bool:
        """A leaf blueprint: a top-level `sql_template`, no `composes` DAG."""
        return bool(self.sql_template) and not self.composes

    @classmethod
    def parse(
        cls,
        *,
        id: str,
        intent: str,
        resolves: Any = None,
        slots: Any = None,
        uses_rules: Any = None,
        sql_template: Any = None,
        composes: Any = None,
        result_grain: Any = None,
    ) -> Blueprint:
        resolves_map: dict[str, str] = {}
        if resolves is not None:
            if not isinstance(resolves, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in resolves.items()
            ):
                raise BlueprintParseError("'resolves' must be an object of string→string")
            resolves_map = dict(resolves)
        # The three ARRAY properties, type-checked BEFORE anything iterates them: a
        # non-iterable (`slots=5`, `composes=5`) raises `TypeError: 'int' object is not
        # iterable`, which is NOT a `BlueprintParseError` — it escapes the loader's
        # `except BlueprintParseError` and aborts `load_corpus` un-wrapped. That bricks
        # the corpus indefinitely (the hydration cache re-arms and retries the same
        # poisoned entry every turn), which is exactly the failure this fail-loud parse
        # layer exists to convert into one clean, attributable error.
        #
        # `uses_rules` HAD this check — but on the line AFTER `tuple(uses_rules or ())`,
        # so it was dead for the only input that needed it. Order is the whole fix.
        for label, value, allowed in (
            ("slots", slots, (list, tuple)),
            ("composes", composes, (list, tuple)),
            ("uses_rules", uses_rules, (list,)),  # kept list-only, as authored
        ):
            if value is not None and not isinstance(value, allowed):
                raise BlueprintParseError(
                    f"'{label}' must be a {' or '.join(t.__name__ for t in allowed)}, "
                    f"got {type(value).__name__}"
                )
        slots_raw = list(slots or [])
        if len(slots_raw) > _MAX_SLOTS:
            raise BlueprintParseError(
                f"blueprint declares {len(slots_raw)} slots, exceeding the {_MAX_SLOTS}-slot "
                f"cap (each binds_to slot can fire an inner probe; a Phase-1 blueprint is small)"
            )
        slot_specs = tuple(SlotSpec.parse(s) for s in slots_raw)
        # M1 (read-side backstop): duplicate slot names silently collide their bind
        # tokens (e.g. a second `w` slot would shadow the first's validated value, or
        # a `string` slot `w_start` could shadow a `period_range` slot `w`'s expanded
        # start bound). Reject any duplicate name here; the loader adds the richer
        # token-level collision gate at WRITE.
        seen_names: set[str] = set()
        for spec in slot_specs:
            if spec.name in seen_names:
                raise BlueprintParseError(f"duplicate slot name {spec.name!r}")
            seen_names.add(spec.name)
        if sql_template is not None and not isinstance(sql_template, str):
            raise BlueprintParseError("'sql_template' must be a string")
        nodes = tuple(Node.parse(n) for n in (composes or []))
        rules = tuple(uses_rules or ())
        return cls(
            id=id,
            intent=intent,
            resolves=resolves_map,
            slots=slot_specs,
            uses_rules=rules,
            sql_template=sql_template,
            composes=nodes,
            result_grain=ResultGrain.parse(result_grain),
        )
