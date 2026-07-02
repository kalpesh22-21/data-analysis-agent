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

from .models import Blueprint, BlueprintParseError, Node
from .rules import (
    _GAP_THRESHOLD as _DEFAULT_GAP_THRESHOLD,
)
from .rules import (
    _MIN_CONFIDENCE as _DEFAULT_MIN_CONFIDENCE,
)
from .rules import (
    RuleBinding,
    RuleFallback,
    expand_rule,
    parse_rule,
)
from .slots import AskUser, OmitSlot, SlotBinding, resolve_slot
from .template import (
    TemplateBindError,
    assert_read_only_select,
    bind_template,
    parse_template,
    referenced_slots,
)
from .verify import VerifyOutcome, verify_result
from .when import WhenClauseError, evaluate_when

_logger = logging.getLogger(__name__)

# runBlueprint error family (§5.4). Every non-clean outcome either PAUSES (needs
# user input) or falls back to the raw loop — never a wrong answer, never a crash.
NOT_FOUND_CODE = "RUN_BLUEPRINT_NOT_FOUND"
SLOT_INVALID_CODE = "RUN_BLUEPRINT_SLOT_INVALID"
UNSUPPORTED_CODE = "RUN_BLUEPRINT_UNSUPPORTED"
VERIFY_FAILED_CODE = "RUN_BLUEPRINT_VERIFY_FAILED"
# A `when…on_violation: abort` fired, or an approval was denied with
# `skip_remaining` and no partial to return — the fast path stops cleanly and the
# raw loop answers (§2.2 4a / §Q8 skip_remaining). Never a wrong answer.
ABORTED_CODE = "RUN_BLUEPRINT_ABORTED"

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
_ABORTED_MESSAGE = (
    "The fast path stopped before producing an answer — answer this from the raw "
    "tools (getTableSchema / runQuery) instead."
)

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
    """A pause the executor yields; the loop honors it (§2.5).

    Slice B: a slot-resolution `askUser` (`reason="blueprint_slot"`,
    `awaiting_node=None`) — the pause happens BEFORE any node runs, so there is no
    mid-DAG state to rehydrate and resume re-runs via the model loop.

    Slice C (D45 mid-DAG durability): an approval gate (`blueprint_approval`) or a
    `when…on_violation:ask` (`blueprint_when_ask`). These carry
    `completed_nodes_json` (SCALAR outputs + provenance + SQL of the nodes already
    run, B2) + `awaiting_node` (the node order to resume AT) so `AgentLoop.resume`
    re-enters `resume()` here with completed nodes rehydrated — completed nodes
    never re-run (exactly-once, survives a process restart). A `resolve_via`
    degrade does NOT pause (S2 honest-call) — it falls back to the raw loop."""

    reason: str  # "blueprint_slot" | "blueprint_approval" | "blueprint_when_ask"
    pending_question: dict[str, Any]
    blueprint_id: str
    slot_bindings_json: str | None
    completed_nodes_json: str | None = None
    awaiting_node: int | None = None


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
        # The D67 `resolve_via` hook (Slice C, §3.4): the typed
        # `ResolveValuesComposite.resolve()` programmatic entry point (NO model
        # round-trip). `None` means a blueprint carrying a dynamic `resolve_via`
        # rule cannot be expanded → UNSUPPORTED (raw-loop fallback), never a
        # silently-unfiltered query.
        resolve_values: Any = None,
        # D67 concept-subset selection tunables (blueprint/rules.py). Canonical
        # values are `RuntimeSettings.resolve_via_{gap_threshold,min_confidence}`;
        # the defaults here mirror the rules-module fallbacks so a directly
        # constructed executor (tests) behaves identically to the wired one.
        resolve_via_gap_threshold: float = _DEFAULT_GAP_THRESHOLD,
        resolve_via_min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        query_limit: int | None = None,
        preview_row_count: int = 20,
        observer: ToolObserver = _default_observer,
    ) -> None:
        self._tool_dispatcher = tool_dispatcher
        self._vector_index = vector_index
        self._semantic_catalog = semantic_catalog  # Slice C (see above)
        self._resolve_values = resolve_values
        self._resolve_via_gap_threshold = resolve_via_gap_threshold
        self._resolve_via_min_confidence = resolve_via_min_confidence
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

        # A multi-node DAG runs through the Slice-C scalar-passing walk; a
        # single-node blueprint keeps the Slice-B leaf path below (unchanged).
        if not blueprint.is_single_node:
            return await self._execute_dag(
                blueprint=blueprint,
                slot_bindings=slot_bindings,
                credentials=credentials,
                completed={},
                awaiting_node=None,
                approval_answer=None,
            )
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

        # S1: the single-node path is the CANONICAL D67 case (a PAF `*_change_fields`
        # rule filtering one query) — expand any `resolve_via` rule through the same
        # typed `resolve()` hook and merge its IN-list binding, so a loader-valid
        # single-node rule blueprint runs instead of dying SLOT_INVALID on the
        # unbound `{codes}` placeholder. Degrade/empty/denied → raw loop (§3.4).
        rule_bindings, rule_outcome = await self._expand_rules(
            blueprint, slot_bindings, credentials, provenances
        )
        if rule_outcome is not None:
            return rule_outcome
        for binds_name, codes in rule_bindings.items():
            if binds_name in referenced:
                bound[binds_name] = codes

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

    # -- Slice C: multi-node DAG + approval pause/resume + D67 -----------------

    async def resume(
        self,
        *,
        blueprint_id: str,
        slot_bindings: dict[str, Any],
        completed_nodes_json: str | None,
        awaiting_node: int,
        approval_answer: str,
        credentials: RuntimeCredentials,
    ) -> ExecOutcome:
        """Re-enter a paused mid-DAG blueprint at *awaiting_node* (D45, §2.5).

        Stateless by construction — everything needed to continue lives in the
        checkpoint the loop passes back (the raw `slot_bindings`, the completed
        SCALAR outputs, the node to resume at). Nothing is held in memory across
        the pause, so a FRESH process resumes identically (restart-durable). The
        loop has already CAS-consumed the checkpoint (exactly-once); completed
        nodes are rehydrated here and NEVER re-run."""
        detail = await self._vector_index.get_blueprint(blueprint_id)
        if detail is None or not is_blueprint_in_scope(
            Candidate(id=detail.id, kind="blueprint", text=detail.intent, uses=detail.uses),
            credentials.column_scope,
        ):
            return ExecFailed(NOT_FOUND_CODE, _NOT_FOUND_MESSAGE, retryable=True)
        try:
            blueprint = _parse_detail(detail)
        except BlueprintParseError:
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        if blueprint.is_single_node:
            # A single-node blueprint has no mid-DAG pause; a checkpoint pointing
            # at one is corrupt/legacy — fail-closed to the raw loop.
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        completed = _rehydrate_completed(completed_nodes_json)
        return await self._execute_dag(
            blueprint=blueprint,
            slot_bindings=slot_bindings,
            credentials=credentials,
            completed=completed,
            awaiting_node=awaiting_node,
            approval_answer=approval_answer,
        )

    async def _execute_dag(
        self,
        *,
        blueprint: Blueprint,
        slot_bindings: dict[str, Any],
        credentials: RuntimeCredentials,
        completed: dict[int, dict[str, Any]],
        awaiting_node: int | None,
        approval_answer: str | None,
    ) -> ExecOutcome:
        """Walk a scalar-passing DAG (§2.2). Fresh run: `completed={}`,
        `awaiting_node=None`. Resume: `completed` rehydrated + `awaiting_node` set."""
        bid = blueprint.id

        # 1. Topo-order + the F2 boundary: a TABLE intermediate (a node output
        # consumed downstream) needs scratch materialization no Phase-1 MCP tool
        # can do → UNSUPPORTED (raw loop). SCALAR-converging DAGs execute.
        topo = _topo_order(blueprint.composes)
        if topo is None:
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        if _has_table_intermediate(blueprint.composes):
            _logger.info("blueprint %s needs a table intermediate (F2); UNSUPPORTED", bid)
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)

        provenances: list[frozenset[tuple[str, str]] | None] = []
        all_referenced = _all_referenced_slots(blueprint)

        # 2. Resolve every slot up-front (deterministic, D49). A slot `askUser`
        # pauses BEFORE any node runs (awaiting_node=None — resume re-runs via the
        # model loop, the Slice-B contract).
        bound_slots, slot_outcome = await self._resolve_all_slots(
            blueprint, slot_bindings, all_referenced, provenances, credentials
        )
        if slot_outcome is not None:
            return slot_outcome

        # 3. D67 `resolve_via` rules → typed IN-list bindings (§3.4). Degrade →
        # pause; empty/denied → raw loop.
        rule_bindings, rule_outcome = await self._expand_rules(
            blueprint, slot_bindings, credentials, provenances
        )
        if rule_outcome is not None:
            return rule_outcome

        # 4. Walk the nodes in topo order.
        node_outputs: dict[str, Any] = {}
        _load_completed_into_outputs(completed, node_outputs)
        running: dict[int, dict[str, Any]] = dict(completed)
        # B2: the completed nodes' captured provenance + node SQL are carried in
        # the checkpoint (not re-derivable — those nodes never re-run), so the
        # final union + the `sql` transparency list span the WHOLE DAG, not just
        # the post-resume tail. Seed both from the rehydrated records (in order); a
        # `None` provenance POISONS the union exactly as a live undetermined call.
        node_sqls: list[str] = []
        for order in sorted(running):
            record = running[order]
            provenances.append(record.get("provenance"))
            carried_sql = record.get("sql")
            if isinstance(carried_sql, str):
                node_sqls.append(carried_sql)
        terminal: tuple[str, str, Any] | None = None  # (template, bound_sql, result)

        for node in topo:
            if node.order in running:
                continue  # rehydrated — never re-run (exactly-once, D45)

            is_approval_node = node.node_kind == "approval" or bool(node.requires_approval)

            # 4a. `when` gate (D32/§2.6) over upstream outputs.
            if node.when is not None:
                # A `when…on_violation:ask` that paused HERE and is now resuming:
                # honor the user's answer instead of re-evaluating (which would
                # re-pause). Only for a pure `when` node — an approval node's
                # awaiting_node belongs to its own gate (4b).
                resumed_for_when = (
                    node.order == awaiting_node
                    and approval_answer is not None
                    and not is_approval_node
                )
                if resumed_for_when:
                    decision = _approval_decision(approval_answer)
                    if decision == "deny":
                        running[node.order] = _node_record({}, frozenset(), None)
                        _record_empty_output(node, node_outputs)  # user declined → skip
                        continue
                    if decision == "repause":
                        # Unrecognized answer → re-ask the SAME question (never
                        # proceed on an ambiguous reply — B3-parity for when_ask).
                        return self._pause(
                            blueprint,
                            slot_bindings,
                            running,
                            node.order,
                            reason="blueprint_when_ask",
                            question=node.when.message
                            or "This step's precondition wasn't met — should I continue?",
                            options=["yes", "no"],
                        )
                    # decision == "approve" → fall past the gate (do not evaluate).
                else:
                    try:
                        passes = evaluate_when(node.when.expr, node_outputs)
                    except WhenClauseError:
                        _logger.warning("blueprint %s node %s when-eval failed", bid, node.order)
                        return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
                    if not passes:
                        ov = node.when.on_violation
                        if ov == "skip":
                            running[node.order] = _node_record({}, frozenset(), None)
                            _record_empty_output(node, node_outputs)  # skipped → empty
                            continue
                        if ov == "abort":
                            _logger.info("blueprint %s node %s when→abort", bid, node.order)
                            return ExecFailed(ABORTED_CODE, _ABORTED_MESSAGE, retryable=True)
                        # ov == "ask": pause to confirm (D59c on_violation:ask).
                        return self._pause(
                            blueprint,
                            slot_bindings,
                            running,
                            node.order,
                            reason="blueprint_when_ask",
                            question=node.when.message
                            or "This step's precondition wasn't met — should I continue?",
                            options=["yes", "no"],
                        )

            # 4b. Approval gate (D59b) — pause showing UPSTREAM outputs only.
            if is_approval_node:
                resumed_here = node.order == awaiting_node and approval_answer is not None
                decision = (
                    _approval_decision(approval_answer) if resumed_here else "repause"
                )
                if decision != "approve":
                    if decision == "deny":
                        # on_deny: skip_remaining — stop. The pre-pause result is
                        # NOT carried across the checkpoint (scalar-only, F2), so a
                        # denied approval always falls back to the raw loop rather
                        # than returning a best-partial (S4 honest call — a terminal
                        # approval is rejected at load, and every approval decision
                        # happens on RESUME where pre-pause query nodes are
                        # rehydrated-skipped, so a returnable `terminal` never
                        # exists here).
                        _logger.info("blueprint %s approval node %s DENIED", bid, node.order)
                        return ExecFailed(ABORTED_CODE, _ABORTED_MESSAGE, retryable=True)
                    # First encounter OR an ambiguous/garbage reply → (re-)pause the
                    # SAME gate; never proceed on anything but an explicit
                    # affirmative (B3-parity — consent is opt-IN).
                    show = _upstream_show(node, node_outputs)
                    return self._pause(
                        blueprint,
                        slot_bindings,
                        running,
                        node.order,
                        reason="blueprint_approval",
                        question=_approval_question(node),
                        options=["approve", "deny"],
                        show=show,
                    )
                # Approved: an approval-only node (no query) contributes nothing.
                running[node.order] = _node_record({}, frozenset(), None)
                if not node.sql_template:
                    _record_empty_output(node, node_outputs)
                    continue

            # 4c. Query node — bind slots + upstream SCALAR consumes + rule
            # IN-lists into the node template (F1/D10), dispatch through the
            # runQuery choke point (D57/D64/D5 free).
            if not node.sql_template:
                running[node.order] = _node_record({}, frozenset(), None)
                _record_empty_output(node, node_outputs)
                continue
            node_bindings, bind_fail = _node_bindings(
                node, bound_slots, rule_bindings, node_outputs
            )
            if bind_fail is not None:
                return bind_fail
            try:
                node_sql = bind_template(node.sql_template, node_bindings)
            except TemplateBindError:
                _logger.warning("blueprint %s node %s bind failed", bid, node.order)
                return ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)

            self._observer(
                "blueprint_step",
                {"blueprint_id": bid, "step": "executing_node", "node": node.order},
            )
            result = await self._tool_dispatcher.dispatch(
                "runQuery", {"sql": node_sql, "limit": self._query_limit}, credentials
            )
            if result.status != "ok":
                return ExecFailed(
                    error_code=result.error_code or UNSUPPORTED_CODE,
                    user_message=result.user_message or _UNSUPPORTED_MESSAGE,
                    retryable=bool(result.retryable),
                    provenance=result.provenance,
                )
            provenances.append(result.provenance)
            node_sqls.append(node_sql)
            # B1/F2: a node with a declared SCALAR output MUST return a single cell
            # (1 row, 1 column per declared scalar). >1 row (fan-out), 0 rows, a
            # NULL cell, or an extra column → fail-closed to SLOT_INVALID BEFORE the
            # consumer dispatches (the D56 grain gate only guards the TERMINAL node,
            # so a fanned-out INTERMEDIATE would otherwise bind an arbitrary cell
            # downstream and return a "verified" wrong answer, §2.4).
            scalar_out = _extract_scalar_output(node, result.result_full)
            if scalar_out is None:
                _logger.warning(
                    "blueprint %s node %s declared a scalar output but did not return "
                    "a single cell; SLOT_INVALID (no arbitrary fanned-out bind)",
                    bid,
                    node.order,
                )
                return ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)
            running[node.order] = _node_record(scalar_out, result.provenance, node_sql)
            _record_output(node, scalar_out, result.result_full, node_outputs)
            terminal = (node.sql_template, node_sql, result.result_full)

        if terminal is None:
            # Every node skipped/gated — nothing to verify or return.
            return ExecFailed(ABORTED_CODE, _ABORTED_MESSAGE, retryable=True)

        # 5. Verify the FINAL node result (D56) + build the verified answer.
        return await self._finalize(blueprint, terminal, node_sqls, provenances, credentials)

    async def _resolve_all_slots(
        self,
        blueprint: Blueprint,
        slot_bindings: dict[str, Any],
        all_referenced: set[str],
        provenances: list[frozenset[tuple[str, str]] | None],
        credentials: RuntimeCredentials,
    ) -> tuple[dict[str, Any], ExecOutcome | None]:
        """Resolve every slot to a typed binding (mirrors the Slice-B leaf loop,
        but the referenced-set spans ALL node templates). Returns
        `(bound, None)` on success, or `(bound, ExecPaused|ExecFailed)`."""
        self._observer("blueprint_step", {"blueprint_id": blueprint.id, "step": "resolving_slots"})
        bound: dict[str, Any] = {}
        for spec in blueprint.slots:
            raw = slot_bindings.get(spec.name)
            if _is_present(raw) and spec.binds_to:
                domain, probe_entries = await self._probe_domain(spec.binds_to, credentials)
                provenances.extend(probe_entries)
            else:
                domain = None
            outcome = resolve_slot(raw, spec, domain=domain)
            if isinstance(outcome, AskUser):
                return bound, ExecPaused(
                    reason="blueprint_slot",
                    pending_question={"question": outcome.question, "options": outcome.options},
                    blueprint_id=blueprint.id,
                    slot_bindings_json=_dumps(slot_bindings),
                )
            if isinstance(outcome, OmitSlot):
                continue
            if isinstance(outcome, SlotBinding):
                if spec.name not in all_referenced:
                    # A resolved slot referenced by NO node template is a silent
                    # dropped filter (the D56 wrong-answer class) — fail-closed.
                    _logger.warning(
                        "blueprint %s: resolved slot %r referenced by no node; SLOT_INVALID",
                        blueprint.id,
                        spec.name,
                    )
                    return bound, ExecFailed(
                        SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True
                    )
                bound[spec.name] = outcome.value
        return bound, None

    async def _expand_rules(
        self,
        blueprint: Blueprint,
        slot_bindings: dict[str, Any],
        credentials: RuntimeCredentials,
        provenances: list[frozenset[tuple[str, str]] | None],
    ) -> tuple[dict[str, Any], ExecOutcome | None]:
        """Expand every dynamic `resolve_via` rule via the D67 hook (§3.4).
        Returns `({placeholder: [codes]}, None)`, or `({}, ExecFailed)` on a
        degrade/empty/denied resolve (raw loop) or a missing hook. `slot_bindings`
        is accepted for signature symmetry with `_resolve_all_slots`."""
        _ = slot_bindings  # (kept for a symmetric signature; no pause path here)
        rule_bindings: dict[str, Any] = {}
        for raw in blueprint.uses_rules:
            rule = parse_rule(raw)
            if rule is None:
                continue  # static / non-resolvable rule — authored SQL, no action
            if self._resolve_values is None:
                # A dynamic rule with no resolve hook wired → cannot expand; never
                # run the query unfiltered → raw loop.
                _logger.info("blueprint %s has a resolve_via rule but no hook; UNSUPPORTED", blueprint.id)
                return {}, ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
            self._observer(
                "blueprint_step", {"blueprint_id": blueprint.id, "step": "resolving_rule"}
            )
            expansion = await expand_rule(
                rule,
                resolve_hook=self._resolve_values,
                credentials=credentials,
                gap_threshold=self._resolve_via_gap_threshold,
                min_confidence=self._resolve_via_min_confidence,
            )
            if isinstance(expansion, RuleBinding):
                provenances.append(expansion.provenance)
                rule_bindings[expansion.binds] = list(expansion.values)
                # Shape-only rule-resolution telemetry (D67 tuning signal): counts
                # + aggregate ranking scores ONLY, NEVER the resolved code strings
                # (D25 — resolved domain values never enter telemetry). Lets us tune
                # resolve_via_gap_threshold/min_confidence on real Phase-0 traffic.
                self._observer(
                    "blueprint_rule_resolved",
                    {
                        "rule_id": rule.rule_id,
                        "selected_count": expansion.selected_count,
                        "dropped_count": expansion.dropped_count,
                        "top_score": expansion.top_score,
                        "cut_gap": expansion.cut_gap,
                    },
                )
            elif isinstance(expansion, RuleFallback):
                # degrade/empty/denied → never a silently-dropped filter → raw loop.
                if expansion.provenance is not None:
                    provenances.append(expansion.provenance)
                if expansion.reason == "denied":
                    # S6c: surface the INNER denial verbatim (real code/message),
                    # not a generic UNSUPPORTED (§5.4 passthrough).
                    return {}, ExecFailed(
                        error_code=expansion.error_code or UNSUPPORTED_CODE,
                        user_message=expansion.user_message or _UNSUPPORTED_MESSAGE,
                        retryable=bool(expansion.retryable),
                        provenance=expansion.provenance,
                    )
                return {}, ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=True)
        return rule_bindings, None

    async def _finalize(
        self,
        blueprint: Blueprint,
        terminal: tuple[str, str, Any],
        node_sqls: list[str],
        provenances: list[frozenset[tuple[str, str]] | None],
        credentials: RuntimeCredentials,
    ) -> ExecOutcome:
        """Run the D56 gate over the FINAL node result and build the verified
        answer (§2.2 step 5 / §5.2). Identical teeth to the single-node path."""
        terminal_template, terminal_sql, terminal_result = terminal
        self._observer("blueprint_step", {"blueprint_id": blueprint.id, "step": "verifying"})
        verify_out = await self._verify(
            blueprint=blueprint,
            template_sql=terminal_template,
            node_sql=terminal_sql,
            node_result=terminal_result,
            credentials=credentials,
            provenances=provenances,
        )
        if verify_out is None or not verify_out.passed:
            reason = None if verify_out is None else verify_out.reason
            _logger.info(
                "blueprint %s verify FAILED (%s); falling back to raw loop",
                blueprint.id,
                reason,
            )
            return ExecFailed(VERIFY_FAILED_CODE, _VERIFY_FAILED_MESSAGE, retryable=True)

        columns, rows, row_count, truncated = _unpack_result(terminal_result)
        result_full: dict[str, Any] = {
            "blueprint_id": blueprint.id,
            "status": "verified",
            "columns": columns,
            "row_count": row_count,
            "truncated": truncated,
            "preview_rows": rows[: self._preview_row_count],
            "sql": node_sqls,  # every per-node SQL (transparency, D56)
            "verify": {
                "grain_ok": verify_out.grain_ok,
                "grain_checked": verify_out.grain_checked,
                "signature_ok": verify_out.signature_ok,
                "signature_checked": False,
            },
        }
        preview = _build_preview(terminal_result, self._preview_row_count)
        return ExecCompleted(
            result_full=result_full,
            preview=preview,
            provenance=_union_provenance(provenances),
        )

    def _pause(
        self,
        blueprint: Blueprint,
        slot_bindings: dict[str, Any],
        running: dict[int, dict[str, Any]],
        awaiting_node: int,
        *,
        reason: str,
        question: str,
        options: list[str],
        show: dict[str, Any] | None = None,
    ) -> ExecPaused:
        """Build a mid-DAG `ExecPaused` — the completed SCALAR outputs + the node
        to resume at are serialized into the checkpoint (D45 durability, §2.5)."""
        pending: dict[str, Any] = {"question": question, "options": options}
        if show is not None:
            pending["show"] = show
        return ExecPaused(
            reason=reason,
            pending_question=pending,
            blueprint_id=blueprint.id,
            slot_bindings_json=_dumps(slot_bindings),
            completed_nodes_json=_dumps_completed(running),
            awaiting_node=awaiting_node,
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


# -- Slice C DAG helpers (pure) -----------------------------------------------


def _topo_order(nodes: tuple[Node, ...]) -> list[Node] | None:
    """Topologically order *nodes* by `feeds_from` (Kahn, deterministic tie-break
    on `order`). Returns `None` if the graph is not a clean DAG (a dangling ref or
    a cycle) — the loader rejects those at WRITE; this is the READ-side backstop
    (defensive → UNSUPPORTED). Independent branches run sequentially (§2.2)."""
    by_order = {n.order: n for n in nodes}
    if len(by_order) != len(nodes):
        return None  # duplicate order
    indegree = {n.order: 0 for n in nodes}
    children: dict[int, list[int]] = {n.order: [] for n in nodes}
    for n in nodes:
        for parent in n.feeds_from:
            if parent not in by_order:
                return None  # dangling feeds_from
            indegree[n.order] += 1
            children[parent].append(n.order)
    ready = sorted(o for o, d in indegree.items() if d == 0)
    ordered: list[Node] = []
    while ready:
        current = ready.pop(0)
        ordered.append(by_order[current])
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()
    if len(ordered) != len(nodes):
        return None  # a cycle left nodes unresolved
    return ordered


def _has_table_intermediate(nodes: tuple[Node, ...]) -> bool:
    """True iff any node CONSUMED downstream (a `feeds_from` source) declares a
    `table` output — that needs scratch materialization (F2/§2.4), out of scope
    for this brick. A TERMINAL node's table result is fine (it is returned, not
    passed)."""
    upstream: set[int] = set()
    for n in nodes:
        upstream.update(n.feeds_from)
    return any(
        n.order in upstream and any(kind == "table" for kind in n.output.values())
        for n in nodes
    )


def _all_referenced_slots(blueprint: Blueprint) -> set[str]:
    """Every `{slot}` token referenced by the templates the DAG path ACTUALLY runs.

    B4 (hybrid record): when `composes` is non-empty the DAG path runs — the
    top-level `sql_template` is DEAD (never executed). Counting its `{slot}` tokens
    here would let a required slot that lives ONLY in the dead template pass the B3
    referenced-slot backstop while being silently dropped at execution → a
    company-wide "verified" wrong answer. Only count node templates when composes
    is present (the loader also rejects the both-present hybrid outright)."""
    referenced: set[str] = set()
    if blueprint.composes:
        for node in blueprint.composes:
            if node.sql_template:
                referenced |= referenced_slots(node.sql_template)
    elif blueprint.sql_template:
        referenced |= referenced_slots(blueprint.sql_template)
    return referenced


def _extract_scalar_output(node: Node, result_full: Any) -> dict[str, Any] | None:
    """Read a query node's SCALAR output(s) from its single-cell result (§2.4).

    A SCALAR intermediate is a single cell per declared scalar (D59a). B1 (fail-
    closed): returns `None` — a hard contract violation the caller maps to
    SLOT_INVALID — when the node does NOT return exactly one row, when the column
    count does not equal the declared-scalar count, or when a mapped cell is NULL.
    This closes the "silently take `rows[0][0]`" hazard: because the D56 grain gate
    only guards the TERMINAL node, a fanned-out (>1 row) or wide (>1 column)
    intermediate would otherwise bind an ARBITRARY cell downstream and still return
    a "verified" answer. A node with NO declared scalar output yields `{}` (a
    terminal/table result, not an intermediate — unconstrained here)."""
    scalars = [name for name, kind in node.output.items() if kind == "scalar"]
    if not scalars:
        return {}
    columns, rows, _rc, _tr = _unpack_result(result_full)
    if len(rows) != 1:
        return None  # 0 rows OR fan-out — not a single-cell scalar
    first = rows[0]
    # A scalar node must return exactly one column per declared scalar (a single
    # cell each) — an extra column means the result is not the scalar the node
    # promised (ambiguous), fail-closed rather than silently discard a column.
    if len(columns) != len(scalars) or len(first) != len(scalars):
        return None
    col_index = {c: i for i, c in enumerate(columns)}
    out: dict[str, Any] = {}
    if len(scalars) == 1:
        # Single scalar: match by name, else the sole column by position.
        idx = col_index.get(scalars[0], 0)
        value = first[idx]
        if value is None:
            return None  # NULL scalar — cannot bind a typed literal
        out[scalars[0]] = value
        return out
    # Multiple scalars: EACH declared name must map to a distinct result column.
    for name in scalars:
        if name not in col_index:
            return None
        value = first[col_index[name]]
        if value is None:
            return None
        out[name] = value
    return out


def _record_output(
    node: Node, scalar_out: dict[str, Any], result_full: Any, node_outputs: dict[str, Any]
) -> None:
    """Record a node's outputs into the `when`/`consumes` environment: `$N` → the
    full result (so `count($N)`/`empty($N)` see row shape) and `$N.<name>` → each
    scalar cell (so `$N.company_avg > 0` compares the value)."""
    node_outputs[f"${node.order}"] = result_full
    for name, value in scalar_out.items():
        node_outputs[f"${node.order}.{name}"] = value


def _record_empty_output(node: Node, node_outputs: dict[str, Any]) -> None:
    """A skipped/gated node's outputs are empty — `empty($N)` True, `count($N)` 0."""
    node_outputs[f"${node.order}"] = {"row_count": 0, "rows": []}
    for name in node.output:
        node_outputs[f"${node.order}.{name}"] = None


def _node_bindings(
    node: Node,
    bound_slots: dict[str, Any],
    rule_bindings: dict[str, Any],
    node_outputs: dict[str, Any],
) -> tuple[dict[str, Any], ExecFailed | None]:
    """Assemble the exact `{placeholder: value}` set a node template needs from
    (a) upstream scalar `consumes`, (b) resolved slots, (c) `resolve_via` rule
    IN-lists. An unbound placeholder or an unresolvable consume is fail-closed to
    SLOT_INVALID (never a half-bound query)."""
    refs = referenced_slots(node.sql_template or "")
    bindings: dict[str, Any] = {}
    # (a) consumes: {placeholder: "$N.name"} — the upstream SCALAR value.
    for placeholder, ref in node.consumes.items():
        if placeholder not in refs:
            continue  # a declared consume the template does not reference — ignore
        if not isinstance(ref, str) or ref not in node_outputs:
            return {}, ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)
        value = node_outputs[ref]
        if value is None:
            # An upstream scalar that came back NULL cannot be bound as a typed
            # literal — fail-closed rather than emit a broken query.
            return {}, ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)
        bindings[placeholder] = value
    # (b)/(c) slots + rule IN-lists.
    for ref in refs:
        if ref in bindings:
            continue
        if ref in bound_slots:
            bindings[ref] = bound_slots[ref]
        elif ref in rule_bindings:
            bindings[ref] = rule_bindings[ref]
        else:
            return {}, ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)
    return bindings, None


def _upstream_show(node: Node, node_outputs: dict[str, Any]) -> dict[str, Any]:
    """The D59b approval `show` — UPSTREAM outputs only (the nodes this approval
    node feeds_from). Aggregate scalars the user confirms before proceeding; a
    user-facing prompt value (like an askUser question), never a telemetry leak."""
    show: dict[str, Any] = {}
    for parent in node.feeds_from:
        for key, value in node_outputs.items():
            if key.startswith(f"${parent}.") and value is not None:
                show[key] = value
    return show


def _approval_question(node: Node) -> str:
    """The approval prompt (D59b). Author-supplied `requires_approval.prompt` wins;
    otherwise a safe default. Never echoes a warehouse cell (the `show` carries the
    upstream aggregates the user is confirming)."""
    ra = node.requires_approval or {}
    prompt = ra.get("prompt") if isinstance(ra, dict) else None
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return "This step needs your approval before I continue. Proceed?"


# The affirmative / negative markers for an approval or `when…ask` resume answer
# (B3 — consent is opt-IN). Negatives are checked FIRST so an explicit negation
# ("do not proceed", "I'd rather not") wins over an affirmative substring it
# contains ("proceed"); an unrecognized reply is neither → re-pause (never run).
_APPROVAL_NEGATIVES: tuple[str, ...] = (
    "no", "not", "n't", "deny", "denied", "decline", "never", "reject",
    "cancel", "stop", "abort", "rather not", "won't", "wont",
)
_APPROVAL_AFFIRMATIVES: tuple[str, ...] = (
    "approve", "approved", "yes", "yep", "yeah", "ok", "okay", "proceed",
    "continue", "confirm", "confirmed", "go ahead", "sure", "affirm", "do it",
)


def _approval_decision(answer: str | None) -> str:
    """Classify a resume answer as `"approve"` | `"deny"` | `"repause"` (B3).

    Consent is opt-IN: proceed ONLY on an explicit affirmative. An explicit
    negation → deny (raw-loop fallback); anything unrecognized (garbage, silence)
    → `"repause"` (re-ask the same gate — never proceed on ambiguity). Negations
    are matched before affirmatives so "do not proceed" denies rather than the
    embedded "proceed" approving."""
    if answer is None:
        return "repause"
    normalized = answer.strip().casefold()
    if not normalized:
        return "repause"
    if any(marker in normalized for marker in _APPROVAL_NEGATIVES):
        return "deny"
    if normalized.startswith(_APPROVAL_AFFIRMATIVES):
        return "approve"
    return "repause"


def _node_record(
    output: dict[str, Any],
    provenance: frozenset[tuple[str, str]] | None,
    sql: str | None,
) -> dict[str, Any]:
    """A completed-node record: its SCALAR `output`, the captured `provenance`, and
    the bound `sql` — ALL three carried across a mid-DAG pause (B2) so the resumed
    final union + `sql` transparency span the WHOLE DAG, not just the resumed tail."""
    return {"output": output, "provenance": provenance, "sql": sql}


def _prov_to_jsonable(
    provenance: frozenset[tuple[str, str]] | None,
) -> list[list[str]] | None:
    """A provenance frozenset → a JSON list of `[db_table, column]` (order-sorted),
    or `null` for an UNDETERMINED (`None`) provenance — which MUST rehydrate back to
    `None` so it still poisons the union (B2 fail-closed, the Slice-B B2 rule)."""
    if provenance is None:
        return None
    return [list(pair) for pair in sorted(provenance)]


def _prov_from_jsonable(raw: Any) -> frozenset[tuple[str, str]] | None:
    """Reverse `_prov_to_jsonable`. `null`/malformed → `None` (poisons the union —
    fail-closed toward dropping the answer under scope narrowing, never fail-open)."""
    if not isinstance(raw, list):
        return None
    pairs: set[tuple[str, str]] = set()
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.add((str(item[0]), str(item[1])))
        else:
            return None  # a malformed entry is undetermined → poison
    return frozenset(pairs)


def _dumps_completed(running: dict[int, dict[str, Any]]) -> str | None:
    """Serialize the completed nodes for the checkpoint (§2.5/B2) — an order-sorted
    `[{order, output, provenance, sql}]` list (deterministic, restart-durable).
    Provenance + SQL are carried so a resumed DAG's final union/transparency cover
    the pre-pause nodes that never re-run."""
    payload = [
        {
            "order": order,
            "output": running[order].get("output", {}),
            "provenance": _prov_to_jsonable(running[order].get("provenance")),
            "sql": running[order].get("sql"),
        }
        for order in sorted(running)
    ]
    return _dumps(payload)


def _rehydrate_completed(completed_nodes_json: str | None) -> dict[int, dict[str, Any]]:
    """Reverse `_dumps_completed` on resume → `{order: {output, provenance, sql}}`.
    A malformed/absent payload rehydrates as empty (defensive — the walk re-runs
    from the top, still correct because every node is read-only + idempotent).
    A record with a missing/`null` provenance rehydrates as `None` (poisons the
    union — the pre-pause footprint is undetermined, so the answer must drop)."""
    if not completed_nodes_json:
        return {}
    try:
        payload = json.loads(completed_nodes_json)
    except (TypeError, ValueError):
        return {}
    completed: dict[int, dict[str, Any]] = {}
    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            order = entry.get("order")
            output = entry.get("output")
            if isinstance(order, int) and not isinstance(order, bool):
                sql = entry.get("sql")
                completed[order] = _node_record(
                    dict(output) if isinstance(output, dict) else {},
                    _prov_from_jsonable(entry.get("provenance")),
                    sql if isinstance(sql, str) else None,
                )
    return completed


def _load_completed_into_outputs(
    completed: dict[int, dict[str, Any]], node_outputs: dict[str, Any]
) -> None:
    """Rehydrate completed nodes' SCALAR outputs into the `when`/`consumes` env on
    resume. Only scalars survive the checkpoint (F2 buys this simplification), so
    `$N` is a synthetic row-shape marker (non-empty ⇒ row_count 1) for
    `count`/`empty`, and `$N.<name>` is the stored scalar."""
    for order, record in completed.items():
        output = record.get("output", {})
        has_value = any(v is not None for v in output.values())
        node_outputs[f"${order}"] = {"row_count": 1 if has_value else 0, "rows": []}
        for name, value in output.items():
            node_outputs[f"${order}.{name}"] = value


__all__ = [
    "ABORTED_CODE",
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
