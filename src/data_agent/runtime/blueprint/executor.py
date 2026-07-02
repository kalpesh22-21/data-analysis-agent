"""blueprint/executor.py — the single-node `BlueprintExecutor` (runblueprint-design §2, Slice B).

The deterministic fast-path engine. Slice B executes a SINGLE-node (leaf)
blueprint end-to-end:

  1. **Fetch** the stored DAG authoritatively by id via the `getBlueprint` store
     read, scope-checked identically to the model-facing tool (§2.2 step 1). A
     miss OR an out-of-scope blueprint is the SAME `NOT_FOUND` (the D88(b)
     non-oracle — no scope probe, byte-identical).
  2. **Parse** the stored JSON into the typed `Blueprint` (Slice-A `models`).
     A multi-node DAG (`composes`) or a table-passing blueprint is `UNSUPPORTED`
     in Slice B (F2 — Slice C) → fall back to the raw loop.
  3. **Resolve + bind** each slot: the Slice-A pure resolvers (`slots.resolve_slot`)
     over a scope-enforced DISTINCT domain probe (when the slot declares
     `binds_to`) → typed sqlglot-AST-literal binding into the template
     (`template.bind_template`, F1/D10). A resolver `AskUser` → PAUSE (a
     `Paused` outcome the loop honors, §2.5) BEFORE any node runs.
  4. **Dispatch** the bound SQL through `ToolDispatcher.dispatch("runQuery", …)`
     so D57 column-scope, D64 scratch-isolation, D5 credential injection, and
     provenance capture come free and identically to `resolveValues` (§2.3).
     An inner denial passes through verbatim (never bypassed).
  5. **Verify** (D56, §4): a scope-enforced `COUNT(*), COUNT(DISTINCT <grain>)`
     probe over the final SQL → the Slice-A `verify.verify_result` gate. A FAIL
     (or a grain the probe cannot compute) NEVER returns the result — it is a
     `Failed(VERIFY_FAILED)` → the raw loop (D56 "no silent path", §4.4).

Fail-closed throughout (B4 discipline): a raising executor must not crash the
turn — the tool's guard (`tool.py`) contains it. The executor itself returns a
typed `ExecOutcome` union; it never raises for an expected denial/verify/parse
failure, only for a genuine bug (which the tool's B4 guard catches).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.provenance.catalog_handle import SemanticCatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
from data_agent.runtime.retrieval.scope_filter import is_blueprint_in_scope
from data_agent.runtime.session.models import ResultPreview

from .models import Blueprint, BlueprintParseError
from .slots import AskUser, OmitSlot, SlotBinding, resolve_slot
from .template import (
    TemplateBindError,
    assert_read_only_select,
    bind_template,
    parse_template,
    referenced_slots,
)
from .verify import VerifyOutcome, verify_result

_logger = logging.getLogger(__name__)

# runBlueprint error family (§5.4). Every non-clean outcome either PAUSES (needs
# user input) or falls back to the raw loop — never a wrong answer, never a crash.
NOT_FOUND_CODE = "RUN_BLUEPRINT_NOT_FOUND"
SLOT_INVALID_CODE = "RUN_BLUEPRINT_SLOT_INVALID"
UNSUPPORTED_CODE = "RUN_BLUEPRINT_UNSUPPORTED"
VERIFY_FAILED_CODE = "RUN_BLUEPRINT_VERIFY_FAILED"

_NOT_FOUND_MESSAGE = "That blueprint is not available. Search for one with searchBlueprints."
_UNSUPPORTED_MESSAGE = (
    "This blueprint can't run on the fast path yet — answer it with the raw tools "
    "(getTableSchema / runQuery)."
)
_VERIFY_FAILED_MESSAGE = (
    "The fast path produced a result that failed verification, so it was not returned. "
    "Answer this from the raw tools (getTableSchema / runQuery) instead."
)
_SLOT_INVALID_MESSAGE = "A value for this blueprint could not be used. Please rephrase or retry."

# The DISTINCT-domain probe row cap — bounds a slot existence/mapping probe so a
# high-cardinality column never pulls an unbounded domain. A value not in the
# first N distincts resolves as no_match → askUser (never a silent guess).
_DOMAIN_PROBE_LIMIT = 500


@dataclass(frozen=True)
class ExecCompleted:
    """A verified single-node result, ready for the model + persistence (§5.2)."""

    result_full: dict[str, Any]
    preview: ResultPreview
    provenance: frozenset[tuple[str, str]] | None


@dataclass(frozen=True)
class ExecPaused:
    """A slot-resolution `askUser` — the executor yields; the loop pauses (§2.5)."""

    reason: str  # "blueprint_slot"
    pending_question: dict[str, Any]
    blueprint_id: str
    slot_bindings_json: str | None


@dataclass(frozen=True)
class ExecFailed:
    """A fail-closed outcome — a runBlueprint-family denial OR an inner
    passthrough. Never a wrong answer: the tool maps it to an error `ToolResult`
    that routes the model to the raw loop (§5.4)."""

    error_code: str
    user_message: str
    retryable: bool
    provenance: frozenset[tuple[str, str]] | None = None


ExecOutcome = ExecCompleted | ExecPaused | ExecFailed


class BlueprintExecutor:
    """The single-node DAG walker (Slice B). Stateless per call; every dependency
    is injected so Layer-1 fakes and the live stack use the identical path."""

    def __init__(
        self,
        *,
        tool_dispatcher: ToolDispatcher,
        vector_index: Any,  # VectorIndex protocol (get_blueprint) — avoids an import cycle
        # RESERVED FOR SLICE C (n2): the D65 temporal gate + the measure-agg
        # structural check read grain/temporal/measures from here. Slice B's D56
        # gate verifies against the blueprint's OWN declared `result_grain`
        # (stored on the blueprint), so this handle is accepted-but-unread today —
        # wired now so the composition root need not change when Slice C lands.
        semantic_catalog: SemanticCatalogHandle | None = None,
        query_limit: int | None = None,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
    ) -> None:
        self._tool_dispatcher = tool_dispatcher
        self._vector_index = vector_index
        self._semantic_catalog = semantic_catalog  # Slice C (see above)
        self._query_limit = query_limit
        self._preview_row_count = preview_row_count
        self._observer = observer

    async def execute(
        self,
        *,
        blueprint_id: str,
        slot_bindings: dict[str, Any],
        credentials: RuntimeCredentials,
    ) -> ExecOutcome:
        """Execute one single-node blueprint end-to-end → a typed `ExecOutcome`."""
        # 1. Fetch + scope-check (non-oracle: absent == out-of-scope == NOT_FOUND).
        detail = await self._vector_index.get_blueprint(blueprint_id)
        if detail is None or not is_blueprint_in_scope(
            Candidate(id=detail.id, kind="blueprint", text=detail.intent, uses=detail.uses),
            credentials.column_scope,
        ):
            return ExecFailed(NOT_FOUND_CODE, _NOT_FOUND_MESSAGE, retryable=True)

        # 2. Parse the stored DAG into the typed Blueprint.
        try:
            blueprint = _parse_detail(detail)
        except BlueprintParseError:
            # A corrupt/legacy stored DAG — do not crash; fall back to the raw loop.
            _logger.warning("blueprint %s failed to parse; UNSUPPORTED", blueprint_id)
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)

        # Slice B: single-node only. A multi-node DAG needs scalar/table passing
        # (F2, Slice C) → UNSUPPORTED, fall back to the raw loop.
        if not blueprint.is_single_node:
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        template_sql = blueprint.sql_template
        assert template_sql is not None  # is_single_node guarantees it

        # S-read-only (defense-in-depth): the loader validated the template is a
        # single read-only SELECT at WRITE, but a POISONED/legacy READ record was
        # not — re-assert on the PRE-BIND template before we resolve, probe, or
        # dispatch anything (matches the grain-probe path). A non-parsing template
        # falls through to the bind step (→ SLOT_INVALID); a template that PARSES
        # but is a DDL/DML/multi-statement construct fails soft to the raw loop
        # (UNSUPPORTED), never dispatched.
        try:
            pre_bind_tree = parse_template(template_sql)
        except TemplateBindError:
            pre_bind_tree = None  # non-parsing → bind_template fail-closes to SLOT_INVALID
        if pre_bind_tree is not None:
            try:
                assert_read_only_select(pre_bind_tree)
            except TemplateBindError:
                _logger.warning(
                    "blueprint %s template is not a read-only SELECT; UNSUPPORTED", blueprint_id
                )
                return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)

        self._observer("blueprint_step", {"blueprint_id": blueprint_id, "step": "resolving_slots"})

        # 3. Resolve + bind slots. Provenance accumulates across EVERY inner
        # dispatched runQuery (domain probes + node query + grain probe), §5.3.
        referenced = referenced_slots(template_sql)
        provenances: list[frozenset[tuple[str, str]] | None] = []
        bound: dict[str, Any] = {}
        for spec in blueprint.slots:
            raw = slot_bindings.get(spec.name)
            # n3: fire the DISTINCT domain probe only when the value is PRESENT — a
            # missing required slot pauses on presence alone and must not waste a
            # warehouse query. §5.3 (B2 fix): when a probe DID run, its provenance
            # is appended UNCONDITIONALLY (even `None`) so a successful probe with
            # undetermined provenance POISONS the union — the same fail-closed rule
            # as the node/grain queries. A skipped/denied probe read nothing → adds
            # nothing.
            if _is_present(raw) and spec.binds_to:
                domain, probe_entries = await self._probe_domain(spec.binds_to, credentials)
                provenances.extend(probe_entries)
            else:
                domain = None
            outcome = resolve_slot(raw, spec, domain=domain)
            if isinstance(outcome, AskUser):
                # A required slot missing / ambiguous value → PAUSE, before any
                # node runs (§2.2 step 2 / §2.5). Raw slot_bindings persist for a
                # deterministic re-fill on resume (server-side only, not telemetry).
                return ExecPaused(
                    reason="blueprint_slot",
                    pending_question={"question": outcome.question, "options": outcome.options},
                    blueprint_id=blueprint_id,
                    slot_bindings_json=_dumps(slot_bindings),
                )
            if isinstance(outcome, OmitSlot):
                continue  # absent optional slot — optional_pattern assembly is Slice C
            if isinstance(outcome, SlotBinding):
                # B3: a resolved value whose `{slot}` the template does not
                # reference must NEVER be silently dropped — dropping it would run
                # the query WITHOUT the user's intended filter and return
                # company-wide numbers that still pass the grain gate (the exact
                # wrong-answer class D56 exists to block). Fail-closed to
                # SLOT_INVALID → raw loop. (The loader also rejects this at write;
                # this is the READ-side backstop for a poisoned/legacy record.)
                if spec.name not in referenced:
                    _logger.warning(
                        "blueprint %s: resolved slot %r is not referenced by the template; "
                        "SLOT_INVALID (never a silent filter drop)",
                        blueprint_id,
                        spec.name,
                    )
                    return ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)
                bound[spec.name] = outcome.value

        # 4. Bind the typed AST literals into the template (F1/D10). A bind
        # failure (unbound {slot}, extra binding, unbindable value) is fail-closed.
        try:
            node_sql = bind_template(template_sql, bound)
        except TemplateBindError:
            _logger.warning("blueprint %s template bind failed", blueprint_id)
            return ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)

        self._observer("blueprint_step", {"blueprint_id": blueprint_id, "step": "executing"})

        # 5. Dispatch the node query through the runQuery choke point (D57/D64/D5).
        node = await self._tool_dispatcher.dispatch(
            "runQuery", {"sql": node_sql, "limit": self._query_limit}, credentials
        )
        if node.status != "ok":
            # Inner denial/error passes through verbatim (§5.4) — the tool relabels
            # tool_name="runBlueprint"; never bypassed.
            return ExecFailed(
                error_code=node.error_code or UNSUPPORTED_CODE,
                user_message=node.user_message or _UNSUPPORTED_MESSAGE,
                retryable=bool(node.retryable),
                provenance=node.provenance,
            )
        provenances.append(node.provenance)

        # 6. D56 verify gate — the grain-integrity probe (§4.2) + signature check.
        self._observer("blueprint_step", {"blueprint_id": blueprint_id, "step": "verifying"})
        verify_out = await self._verify(
            blueprint=blueprint,
            template_sql=template_sql,
            node_sql=node_sql,
            node_result=node.result_full,
            credentials=credentials,
            provenances=provenances,
        )
        if verify_out is None:
            # A grain probe was DENIED/errored — fail-closed, do not return.
            return ExecFailed(VERIFY_FAILED_CODE, _VERIFY_FAILED_MESSAGE, retryable=True)
        if not verify_out.passed:
            # D56 "no unverified return": block + route to the raw loop (§4.4).
            _logger.info(
                "blueprint %s verify FAILED (%s); falling back to raw loop",
                blueprint_id,
                verify_out.reason,
            )
            return ExecFailed(VERIFY_FAILED_CODE, _VERIFY_FAILED_MESSAGE, retryable=True)

        # 7. Build the verified result (§5.2) + the union provenance (fail-closed).
        columns, rows, row_count, truncated = _unpack_result(node.result_full)
        result_full: dict[str, Any] = {
            "blueprint_id": blueprint_id,
            "status": "verified",
            "columns": columns,
            "row_count": row_count,
            "truncated": truncated,
            "preview_rows": rows[: self._preview_row_count],
            "sql": [node_sql],  # per-node SQL for transparency (D56 "SQL stays visible")
            "verify": {
                "grain_ok": verify_out.grain_ok,
                "grain_checked": verify_out.grain_checked,
                # n4: Slice B does not check a result SIGNATURE yet (no declared
                # signature is stored), so `signature_ok` is vacuously True — flag
                # `signature_checked: False` so the D56 LLM-review input is honest
                # (mirrors `grain_checked`), never implying a check that did not run.
                "signature_ok": verify_out.signature_ok,
                "signature_checked": False,
            },
        }
        preview = _build_preview(node.result_full, self._preview_row_count)
        return ExecCompleted(
            result_full=result_full,
            preview=preview,
            provenance=_union_provenance(provenances),
        )

    # -- helpers --------------------------------------------------------------

    async def _probe_domain(
        self, binds_to: str, credentials: RuntimeCredentials
    ) -> tuple[list[str] | None, list[frozenset[tuple[str, str]] | None]]:
        """Return the scope-enforced DISTINCT domain of `binds_to`
        ("database.table.column") and the provenance entries to fold into the
        union (§5.3, B2):
          - malformed `binds_to` / denied / errored probe → `(None, [])`: no
            domain (the resolver binds directly; the NODE query's own D57
            enforcement still gates it — never a fabricated match) and NOTHING is
            added to the union (a denied probe read nothing).
          - a SUCCESSFUL probe → `(values, [probe.provenance])`: the probe
            provenance is appended UNCONDITIONALLY (even when `None`) so a
            successful probe with undetermined provenance POISONS the union, the
            same fail-closed rule as the node/grain queries.
        An ordinary dispatched runQuery (scope-enforced); it does NOT count against
        the model budget (§3.2)."""
        db_table, sep, column = binds_to.rpartition(".")
        if not sep or not db_table or not column:
            return None, []
        probe_sql = (
            exp.select(exp.column(column))
            .distinct()
            .from_(db_table)
            .limit(_DOMAIN_PROBE_LIMIT)
            .sql(dialect="clickhouse")
        )
        probe = await self._tool_dispatcher.dispatch(
            "runQuery", {"sql": probe_sql, "limit": None}, credentials
        )
        if probe.status != "ok":
            return None, []  # a denied/errored probe read nothing → contributes nothing
        _columns, rows, _row_count, _truncated = _unpack_result(probe.result_full)
        values = [str(row[0]) for row in rows if row and row[0] is not None]
        return values, [probe.provenance]  # append UNCONDITIONALLY (None poisons the union)

    async def _verify(
        self,
        *,
        blueprint: Blueprint,
        template_sql: str,
        node_sql: str,
        node_result: Any,
        credentials: RuntimeCredentials,
        provenances: list[frozenset[tuple[str, str]] | None],
    ) -> VerifyOutcome | None:
        """Run the D56 deterministic gate (§4). Returns the `VerifyOutcome`, or
        `None` when a required grain probe could not be dispatched (fail-closed —
        the caller treats it as a verify failure). The row-count teeth are SKIPPED
        (vacuously ok) for an empty/unverifiable declared grain (§4.2)."""
        grain = blueprint.result_grain
        columns, _rows, _row_count, _truncated = _unpack_result(node_result)

        if not grain.columns or not grain.verifiable:
            # Skip the row-count teeth — verify.py returns grain_ok/skipped.
            return verify_result(
                result_grain=grain,
                row_count=len(_rows),
                distinct_grain_count=None,
                columns=columns,
            )

        # V1: map grain→output columns from the PRE-BIND template (not the bound
        # SQL) so a slot VALUE can never perturb an output name, and fail-closed on
        # any ambiguity. The output aliases are identical pre/post bind (binding
        # only substitutes `{slot}` literals), so the mapped names are valid in the
        # bound grain-probe subquery.
        grain_cols = _map_grain_columns(template_sql, grain.columns)
        if grain_cols is None:
            # A declared grain column has no matching output column — the check
            # cannot run; verify.py fail-closes on distinct=None (never a silent pass).
            return verify_result(
                result_grain=grain,
                row_count=len(_rows),
                distinct_grain_count=None,
                columns=columns,
            )

        probe_sql = _grain_probe_sql(node_sql, grain_cols)
        probe = await self._tool_dispatcher.dispatch(
            "runQuery", {"sql": probe_sql, "limit": None}, credentials
        )
        if probe.status != "ok":
            return None  # a denied/errored grain probe → fail-closed at the caller
        provenances.append(probe.provenance)
        total, distinct = _unpack_grain_probe(probe.result_full)
        if total is None or distinct is None:
            return verify_result(
                result_grain=grain,
                row_count=len(_rows),
                distinct_grain_count=None,
                columns=columns,
            )
        return verify_result(
            result_grain=grain,
            row_count=total,
            distinct_grain_count=distinct,
            columns=columns,
        )


def _parse_detail(detail: BlueprintDetail) -> Blueprint:
    """Parse the stored `BlueprintDetail` (JSON-decoded) into the typed Blueprint."""
    return Blueprint.parse(
        id=detail.id,
        intent=detail.intent,
        resolves=detail.resolves,
        slots=detail.slots,
        uses_rules=detail.uses_rules,
        sql_template=detail.sql_template,
        composes=detail.composes,
        result_grain=detail.result_grain,
    )


def _map_grain_columns(template_or_sql: str, grain_columns: tuple[str, ...]) -> list[str] | None:
    """Map each DECLARED grain column to an OUTPUT column name (a template aliases
    `Department AS department`, but the declared grain is `Department`). Parsed via
    `parse_template` so a `{slot}` template parses too — V1: the caller passes the
    PRE-BIND template, so an output name can never be a bound slot VALUE.

    Matching is fail-closed (V1): an EXACT output-name match wins; otherwise a
    case-insensitive match is accepted ONLY when it resolves to a SINGLE output
    column. An ambiguous casefold COLLISION (declared "Dept" vs outputs "dept" AND
    "DEPT") → `None` (the verify gate must not `COUNT(DISTINCT)` a GUESSED column);
    a declared grain column with NO output match → `None` too. Either `None` makes
    the executor pass `distinct=None` → verify.py withholds the result."""
    try:
        tree = parse_template(template_or_sql)
    except TemplateBindError:
        return None
    outputs = list(getattr(tree, "named_selects", []) or [])
    exact = set(outputs)
    # casefold key → the DISTINCT output names that collapse to it (order-preserved).
    casefold_candidates: dict[str, list[str]] = {}
    for name in outputs:
        bucket = casefold_candidates.setdefault(name.casefold(), [])
        if name not in bucket:
            bucket.append(name)
    mapped: list[str] = []
    for col in grain_columns:
        if col in exact:
            mapped.append(col)
            continue
        candidates = casefold_candidates.get(col.casefold(), [])
        if len(candidates) == 1:
            mapped.append(candidates[0])  # unambiguous case-insensitive match
        else:
            return None  # ambiguous collision OR no match → fail-closed
    return mapped


def _is_present(raw: Any) -> bool:
    """True iff *raw* is a non-absent slot value (mirrors slots._is_absent) — used
    to skip a domain probe for an absent slot (n3: a missing required slot pauses
    on presence alone and must not waste a warehouse query)."""
    return not (raw is None or (isinstance(raw, str) and raw.strip() == ""))


def _grain_probe_sql(node_sql: str, grain_output_cols: list[str]) -> str:
    """Build the scope-enforceable `SELECT COUNT(*), COUNT(DISTINCT <grain>) FROM
    (<final SQL>)` probe (§4.2) — the fan-out canary. Built via the AST so the
    inner SQL is embedded structurally, never string-spliced."""
    inner = sqlglot.parse_one(node_sql, dialect="clickhouse")
    assert_read_only_select(inner)  # defense-in-depth: the probe subquery is a read
    subquery = exp.Subquery(
        this=inner, alias=exp.TableAlias(this=exp.to_identifier("__bp_sub"))
    )
    count_star = exp.Count(this=exp.Star())
    distinct = exp.Count(
        this=exp.Distinct(expressions=[exp.column(col) for col in grain_output_cols])
    )
    select = exp.select(
        exp.alias_(count_star, "__bp_n"), exp.alias_(distinct, "__bp_d")
    ).from_(subquery)
    return select.sql(dialect="clickhouse")


def _unpack_result(raw: Any) -> tuple[list[str], list[list[Any]], int, bool]:
    """Structurally unpack a `{columns, rows, row_count, truncated}` runQuery
    result, defaulting every surprise (B4-parity — never raise here)."""
    if not isinstance(raw, dict):
        return [], [], 0, False
    columns = raw.get("columns")
    rows = raw.get("rows")
    columns = list(columns) if isinstance(columns, list) else []
    rows = [list(r) for r in rows if isinstance(r, list | tuple)] if isinstance(rows, list) else []
    row_count = raw.get("row_count")
    row_count = int(row_count) if isinstance(row_count, int) and not isinstance(row_count, bool) else len(rows)
    truncated = bool(raw.get("truncated", False))
    return columns, rows, row_count, truncated


def _unpack_grain_probe(raw: Any) -> tuple[int | None, int | None]:
    """Pull `(total, distinct)` from the single-row grain-probe result. Any
    structural surprise → `(None, None)` (fail-closed at the caller)."""
    _columns, rows, _row_count, _truncated = _unpack_result(raw)
    if not rows or len(rows[0]) < 2:
        return None, None
    total, distinct = rows[0][0], rows[0][1]
    try:
        return int(total), int(distinct)
    except (TypeError, ValueError):
        return None, None


def _union_provenance(
    provenances: list[frozenset[tuple[str, str]] | None],
) -> frozenset[tuple[str, str]] | None:
    """Union every inner runQuery's captured provenance (§5.3). Fail-closed: if
    ANY inner call had undetermined (`None`) provenance, the union is `None` (the
    assistant message drops from D44 replay), matching
    `_compute_turn_provenance_union`'s posture."""
    acc: set[tuple[str, str]] = set()
    for prov in provenances:
        if prov is None:
            return None
        acc.update(prov)
    return frozenset(acc)


def _dumps(value: Any) -> str | None:
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return None


__all__ = [
    "NOT_FOUND_CODE",
    "SLOT_INVALID_CODE",
    "UNSUPPORTED_CODE",
    "VERIFY_FAILED_CODE",
    "BlueprintExecutor",
    "ExecCompleted",
    "ExecFailed",
    "ExecOutcome",
    "ExecPaused",
]
