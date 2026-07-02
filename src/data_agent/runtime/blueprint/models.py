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

from dataclasses import dataclass, field
from typing import Any

# Valid slot `type`s (D41/D49). Kept as a frozenset so the parse layer and the
# loader validation agree on the closed set.
SLOT_TYPES: frozenset[str] = frozenset(
    {"string", "entity", "enum", "period", "as_of_date", "list"}
)
NODE_KINDS: frozenset[str] = frozenset({"query", "approval"})  # D59c (`guard` cut)
ON_VIOLATION: frozenset[str] = frozenset({"abort", "skip", "ask"})

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

    @classmethod
    def parse(cls, raw: Any) -> SlotSpec:
        if not isinstance(raw, dict):
            raise BlueprintParseError(f"slot must be an object, got {type(raw).__name__}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise BlueprintParseError("slot is missing a non-empty 'name'")
        type_ = raw.get("type")
        if type_ not in SLOT_TYPES:
            raise BlueprintParseError(
                f"slot {name!r} has unknown type {type_!r} (allowed: {sorted(SLOT_TYPES)})"
            )
        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise BlueprintParseError(f"slot {name!r} 'required' must be a boolean")
        binds_to = raw.get("binds_to")
        if binds_to is not None and not isinstance(binds_to, str):
            raise BlueprintParseError(f"slot {name!r} 'binds_to' must be a string")
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
        return cls(
            name=name,
            type=type_,
            required=required,
            binds_to=binds_to,
            enum_values=enum_values,
            optional_pattern=optional_pattern,
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
        if on_violation not in ON_VIOLATION:
            raise BlueprintParseError(
                f"'when.on_violation' must be one of {sorted(ON_VIOLATION)}, got {on_violation!r}"
            )
        message = raw.get("message")
        if message is not None and not isinstance(message, str):
            raise BlueprintParseError("'when.message' must be a string")
        return cls(expr=expr, on_violation=on_violation, message=message)


@dataclass(frozen=True)
class Node:
    """One `composes` DAG node (§1.1). `output` maps each name → 'scalar'|'table'.

    Phase-1 executes single-node blueprints + scalar-converging DAGs; `table`
    intermediates are rejected downstream (F2). This parse layer records the
    shape faithfully; the loader (§1.2) enforces the DAG invariants.
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
        if node_kind not in NODE_KINDS:
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
            isinstance(k, str) and v in ("scalar", "table") for k, v in output_raw.items()
        ):
            raise BlueprintParseError(
                f"node {order} 'output' must map names → 'scalar'|'table'"
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
        slots_raw = list(slots or [])
        if len(slots_raw) > _MAX_SLOTS:
            raise BlueprintParseError(
                f"blueprint declares {len(slots_raw)} slots, exceeding the {_MAX_SLOTS}-slot "
                f"cap (each binds_to slot can fire an inner probe; a Phase-1 blueprint is small)"
            )
        slot_specs = tuple(SlotSpec.parse(s) for s in slots_raw)
        if sql_template is not None and not isinstance(sql_template, str):
            raise BlueprintParseError("'sql_template' must be a string")
        nodes = tuple(Node.parse(n) for n in (composes or []))
        rules = tuple(uses_rules or ())
        if uses_rules is not None and not isinstance(uses_rules, list):
            raise BlueprintParseError("'uses_rules' must be a list")
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
