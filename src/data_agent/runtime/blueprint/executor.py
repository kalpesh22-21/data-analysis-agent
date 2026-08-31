"""blueprint/executor.py — the `BlueprintExecutor` deterministic fast-path engine.

Per run: FETCH the stored DAG authoritatively by id through the `getBlueprint` store read,
scope-checked identically to the model-facing tool (a miss and an out-of-scope blueprint are
the SAME `NOT_FOUND` — the non-oracle, byte-identical); PARSE it into the typed `Blueprint`;
RESOLVE + BIND each slot with the pure resolvers over a scope-enforced DISTINCT domain probe,
binding typed sqlglot-AST literals (D10) and PAUSING before any node runs on an `AskUser`;
DISPATCH the bound SQL through `ToolDispatcher.dispatch("runQuery", …)`, so D57 column scope,
D64 scratch isolation, D5 credential injection and provenance capture all come free and an
inner denial passes through verbatim; and VERIFY (D56) via a scope-enforced
`COUNT(*), COUNT(DISTINCT <grain>)` probe. A verify FAIL — or a grain the probe cannot
compute — NEVER returns the result: it is `Failed(VERIFY_FAILED)` and the model falls back to
the raw loop ("no silent path").

Fail-closed throughout: the executor returns a typed `ExecOutcome` union and never raises for
an expected denial, verify failure or parse failure — only for a genuine bug, which the
tool's crash guard contains.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from sqlglot import exp

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolDispatcher,
    ToolObserver,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.mcp.scratch_client import (
    ScratchClientError,
    ScratchClientProtocol,
)
from data_agent.runtime.provenance.catalog_handle import SemanticCatalogHandle
from data_agent.runtime.retrieval.models import BlueprintDetail, Candidate
from data_agent.runtime.retrieval.scope_filter import is_blueprint_in_scope
from data_agent.runtime.session.models import ResultPreview
from data_agent.sqlparse import is_own_session_scratch_table

from .grain_probe import (
    build_grain_probe_sql,
    map_grain_columns,
    unpack_grain_probe,
)
from .models import (
    DATA_WINDOW_ANCHOR,
    TABLE_CONSUME_REF,
    Blueprint,
    BlueprintParseError,
    Node,
)
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
from .slots import (
    AskUser,
    OmitSlot,
    SlotBinding,
    expand_binding,
    resolve_slot,
    slot_token_names,
)
from .template import (
    SCRATCH_DB,
    TemplateBindError,
    assert_read_only_select,
    bind_template,
    parse_template,
    referenced_slots,
)
from .verify import VerifyOutcome, verify_result
from .when import WhenClauseError, evaluate_when

_logger = logging.getLogger(__name__)

# The D56 grain probe is built by the SHARED helper so the offline S9 replay probe
# can never drift from this live path (grain_probe.py). Keep the module-private
# aliases so the executor's call sites (and its tests) are byte-unchanged.
_map_grain_columns = map_grain_columns
_grain_probe_sql = build_grain_probe_sql
_unpack_grain_probe = unpack_grain_probe

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

# `emit_progress=False` on EVERY inner dispatch below (node query, domain probe,
# grain probe): a blueprint is ONE call from the user's side, so its internal
# runQuery must not paint a "running runQuery…" line on the UI progress stream
# (that stream is what the user reads; the internal step structure — and the fact
# that it is SQL over internal tables — is not theirs to see). Spans, scope
# enforcement and control flow are unchanged.


@dataclass(frozen=True)
class ExecCompleted:
    """A verified blueprint result, ready for the model + persistence (§5.2)."""

    result_full: dict[str, Any]
    preview: ResultPreview
    provenance: frozenset[tuple[str, str]] | None


@dataclass(frozen=True)
class ExecPaused:
    """A pause the executor yields; the loop honors it.

        A slot-resolution `askUser` (`reason="blueprint_slot"`, `awaiting_node=None`) happens
        BEFORE any node runs, so there is no mid-DAG state to rehydrate and the resume re-runs
        via the model loop.

        An approval gate (`blueprint_approval`) or a `when…on_violation:ask`
        (`blueprint_when_ask`) carries `completed_nodes_json` — the SCALAR outputs, provenance,
        SQL and (for a table producer) the materialized `scratch.…` table name + row count of
        the nodes already run — plus `awaiting_node`, so `AgentLoop.resume` re-enters
        `resume()` with completed nodes rehydrated and never re-runs them (exactly-once,
        surviving a process restart). It comes back as persisted, therefore untrusted, JSON:
        the carried table is re-validated for session ownership (D64) AND re-counted live
        before anything binds it, because the intermediate's TTL expires ROWS while leaving
        the table standing. A `resolve_via` degrade does NOT pause; it falls back to the raw
        loop.
    """

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
    """The DAG walker. Stateless per call; every dependency is injected so Layer-1 fakes and
        the live stack use the identical path.
    """

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
        # The D93 scratch-write side-channel client (table-intermediate Slice 2).
        # `None` → a table intermediate stays UNSUPPORTED (clean raw-loop degrade,
        # reversible: a deploy with no scratch surface simply never enables it).
        scratch_client: ScratchClientProtocol | None = None,
        scratch_max_rows: int = 10_000,
        scratch_max_columns: int = 256,
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
        self._scratch_client = scratch_client
        self._scratch_max_rows = scratch_max_rows
        self._scratch_max_columns = scratch_max_columns
        self._observer = observer

    async def execute(
        self,
        *,
        blueprint_id: str,
        slot_bindings: dict[str, Any],
        credentials: RuntimeCredentials,
    ) -> ExecOutcome:
        """Execute one blueprint end-to-end → a typed `ExecOutcome`. A leaf
        blueprint is synthesized into a one-node DAG and walked identically."""
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

        # A LEAF BLUEPRINT *IS* A ONE-NODE DAG. There is exactly one walker: the
        # top-level `sql_template` is synthesized into a single `composes` node
        # HERE and the run continues down `_execute_dag`, so the slot loop, the
        # read-only gate, the bind, the dispatch, the D56 verify and the
        # `result_full` builder exist ONCE (they were hand-copied before, and the
        # copies drifted).
        #
        # Synthesized HERE, never in `Blueprint.parse`: `is_single_node` is read by
        # `resume()`'s corrupt-checkpoint guard, by `_all_referenced_slots`'
        # dead-top-level-template rule and by the learning plane, so the STORED
        # shape must stay honest — only this execution's local copy is rewritten.
        # The node is pause-INCAPABLE by construction: `node_kind` defaults to
        # `query`, `when=None`, `requires_approval=None`, `consumes/output` empty,
        # so no `when` gate, approval gate or scalar-output check can fire on it.
        if blueprint.is_single_node:
            blueprint = replace(
                blueprint,
                sql_template=None,
                composes=(Node(order=0, sql_template=blueprint.sql_template),),
            )
        return await self._execute_dag(
            blueprint=blueprint,
            slot_bindings=slot_bindings,
            credentials=credentials,
            completed={},
            awaiting_node=None,
            approval_answer=None,
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
        """Re-enter a paused mid-DAG blueprint at *awaiting_node* (D45).

                Stateless by construction: everything needed to continue lives in the checkpoint the
                loop passes back (the raw `slot_bindings`, the completed SCALAR outputs, the node to
                resume at), so a FRESH process resumes identically. The loop has already CAS-consumed
                the checkpoint (exactly-once); completed nodes are rehydrated here and NEVER re-run.
        """
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

        # 1. Topo-order + the F2 boundary. A TABLE intermediate (a node output
        # consumed downstream by a table `consumes`) is now MATERIALIZED to a
        # session-scoped scratch table via the D93 side-channel, then the consumer's
        # JOIN is AST-rewritten to it (§2.2). This is supported ONLY when a
        # `scratch_client` is wired; otherwise it stays UNSUPPORTED → raw loop
        # (reversible degrade). SCALAR-converging DAGs execute unchanged.
        topo = _topo_order(blueprint.composes)
        if topo is None:
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)

        # S-read-only (defense-in-depth): the loader validated every template is a
        # single read-only SELECT at WRITE, but a POISONED/legacy READ record was
        # not — re-assert on the PRE-BIND templates of EVERY node before anything
        # resolves, probes, materializes or dispatches. A non-parsing template is
        # NOT rejected here: it falls through to `bind_template`, which fail-closes
        # to SLOT_INVALID (the pinned posture). A template that PARSES but is a
        # DDL/DML/multi-statement construct fails soft to the raw loop
        # (UNSUPPORTED), never dispatched.
        #
        # ORDER MATTERS: after `_topo_order` (a cyclic/dangling DAG is UNSUPPORTED
        # on its shape first) and strictly BEFORE `_resolve_all_slots` — a poisoned
        # template carrying a `binds_to` slot must never fire a DISTINCT domain
        # probe against the warehouse on its way to being rejected.
        for node in topo:
            if not node.sql_template:
                continue
            try:
                pre_bind_tree = parse_template(node.sql_template)
            except TemplateBindError:
                continue  # non-parsing → bind_template fail-closes to SLOT_INVALID
            try:
                assert_read_only_select(pre_bind_tree)
            except TemplateBindError:
                _logger.warning(
                    "blueprint %s node %s template is not a read-only SELECT; UNSUPPORTED",
                    bid,
                    node.order,
                )
                return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)

        table_consumed_orders = _table_consumed_orders(blueprint.composes)
        if table_consumed_orders and self._scratch_client is None:
            _logger.info(
                "blueprint %s needs a table intermediate but no scratch_client is wired; "
                "UNSUPPORTED (raw loop)",
                bid,
            )
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        # A `table`-output node consumed downstream WITHOUT a table `consumes` (a
        # shape the loader forbids, but a poisoned/legacy record could carry) still
        # cannot be scalar-passed → UNSUPPORTED (defense-in-depth, fail-closed).
        if _has_table_intermediate(blueprint.composes) and not table_consumed_orders:
            _logger.info("blueprint %s has a table intermediate with no table consume; UNSUPPORTED", bid)
            return ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        # node order → full "scratch.s_<sid>_bp_<uuid>". Populated live by
        # `_materialize_node` AND — since the mid-DAG-pause fix — re-seeded on resume
        # from the checkpoint (see `_restore_materialized` below).
        #
        # THE SCRATCH TABLE DOES NOT RELIABLY SURVIVE THE PAUSE, and an earlier
        # version of this comment claimed it did. The real semantics, read off the
        # DDL (ch-api `scratch_ingest.build_scratch_create_sql`): the intermediate is
        # `ENGINE = MergeTree … TTL <created_at> + INTERVAL <scratch_ttl_seconds>
        # SECOND` — a ROW-level TTL, not a table TTL. ClickHouse expires ROWS on
        # background merges and never drops the table, so after the TTL the name
        # still RESOLVES (no dispatch error to pass through) while the JOIN sees
        # zero rows, or an arbitrary partially-merged subset. And nothing bounds the
        # window: `scratch_ttl_seconds` defaults to 3600 while the session doc
        # carrying this checkpoint lives `session_ttl_seconds` = 604_800 (7 days),
        # and no layer stamps or checks a pause age. An approval answered the next
        # morning would otherwise return a silently under-counted aggregate that the
        # D56 grain gate — a SHAPE check on the terminal result — validates just as
        # happily, i.e. `status: "verified"` on a wrong answer.
        #
        # So a restored name is never trusted on AGE. It is verified against a LIVE
        # COUNT of the table it names, matched exactly against the row count the
        # producer materialized (carried on the same record).
        materialized: dict[int, str] = {}

        provenances: list[frozenset[tuple[str, str]] | None] = []
        all_referenced = _all_referenced_slots(blueprint)

        # 2. Resolve every slot up-front (deterministic, D49). A slot `askUser`
        # pauses BEFORE any node runs (awaiting_node=None — resume re-runs via the
        # model loop, the Slice-B contract).
        bound_slots, omitted_patterns, slot_outcome = await self._resolve_all_slots(
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
        # The materialized TABLE NAME + row count of a table-producing node ride the
        # same records, for the same reason: a rehydrated producer is skipped below
        # (exactly-once, D45) and so never re-materializes, and without its name the
        # consumer's `$N` had no binding and every paused table-intermediate
        # blueprint died on SLOT_INVALID at resume. They are re-validated, not
        # trusted — `_restore_materialized`.
        node_sqls: list[str] = []
        for order in sorted(running):
            record = running[order]
            provenances.append(record.get("provenance"))
            carried_sql = record.get("sql")
            if isinstance(carried_sql, str):
                node_sqls.append(carried_sql)
        materialized.update(
            await self._restore_materialized(bid, running, table_consumed_orders, credentials)
        )
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
                        # on_deny: skip_remaining — stop. Result ROWS are not carried
                        # across the checkpoint (F2 — the records carry scalars,
                        # provenance, SQL and a table intermediate's name+count, never
                        # a result set), so a denied approval always falls back to the
                        # raw loop rather
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
                node, bound_slots, rule_bindings, node_outputs, omitted_patterns
            )
            if bind_fail is not None:
                _logger.warning(
                    "blueprint %s node %s has referenced token(s) with no binding; "
                    "SLOT_INVALID",
                    bid,
                    node.order,
                )
                return bind_fail
            # Table consumes: rewrite each `scratch.<placeholder>` FROM/JOIN token to
            # the runtime-controlled materialized scratch table (§2.2 step 3). An
            # upstream table not yet materialized → SLOT_INVALID (fail-closed).
            table_bindings, table_fail = _node_table_bindings(node, materialized)
            if table_fail is not None:
                return table_fail
            try:
                node_sql = bind_template(
                    node.sql_template,
                    node_bindings,
                    table_bindings=table_bindings,
                    optional_patterns=omitted_patterns,
                )
            except TemplateBindError:
                _logger.warning("blueprint %s node %s bind failed", bid, node.order)
                return ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)

            self._observer(
                "blueprint_step",
                {"blueprint_id": bid, "step": "executing_node", "node": node.order},
            )
            result = await self._tool_dispatcher.dispatch(
                "runQuery",
                {"sql": node_sql, "limit": self._query_limit},
                credentials,
                emit_progress=False,  # internal blueprint query — see the emit_progress note at the top of this module
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
            # Table producer (§2.2 steps 1-2): a node whose table output is consumed
            # downstream is MATERIALIZED to a session-scoped scratch table via the
            # D93 side-channel, then the consumer's JOIN is rewritten to it. This
            # runs INSIDE the one `runBlueprint` tool call (no extra model-facing
            # budget). Its provenance (warehouse columns, already scope-checked at
            # dispatch) is folded into the union above; scratch columns are excluded
            # downstream by the MCP's D69/OQ-4 filter (§Q1/§5.3).
            if node.order in table_consumed_orders:
                table_name, mat_rows, mat_fail = await self._materialize_node(
                    bid, node, result.result_full, credentials
                )
                if mat_fail is not None:
                    return mat_fail
                # mat_fail is None ⇒ both are set
                assert table_name is not None and mat_rows is not None
                materialized[node.order] = table_name
                # A materialized intermediate is neither a scalar producer nor the
                # terminal — record it (empty scalar output) and move on. The table
                # NAME and its ROW COUNT go on the record so the intermediate can
                # survive a mid-DAG pause: this node is rehydrated-skipped on resume
                # and never re-materializes, and the count is what lets the resume
                # prove the table still holds what it wrote rather than a
                # row-TTL-expired remnant (see `_restore_materialized`).
                running[node.order] = _node_record(
                    {}, result.provenance, node_sql, table_name, mat_rows
                )
                _record_empty_output(node, node_outputs)
                continue
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
    ) -> tuple[dict[str, Any], dict[str, str], ExecOutcome | None]:
        """Resolve every slot to a typed binding (mirrors the Slice-B leaf loop,
        but the referenced-set spans ALL node templates). Returns
        `(bound, omitted_patterns, None)` on success, or
        `(bound, omitted_patterns, ExecPaused|ExecFailed)`. `omitted_patterns` maps
        each omitted optional slot that carries an `optional_pattern` to it (Slice
        C) — applied per-node in `bind_template` for the nodes that reference it."""
        self._observer("blueprint_step", {"blueprint_id": blueprint.id, "step": "resolving_slots"})
        bound: dict[str, Any] = {}
        omitted_patterns: dict[str, str] = {}
        for spec in blueprint.slots:
            raw = slot_bindings.get(spec.name)
            if _is_present(raw) and spec.binds_to:
                domain, probe_entries = await self._probe_domain(spec.binds_to, credentials)
                provenances.extend(probe_entries)
            else:
                domain = None
            outcome = resolve_slot(raw, spec, domain=domain)
            if isinstance(outcome, AskUser):
                return bound, omitted_patterns, ExecPaused(
                    reason="blueprint_slot",
                    pending_question={"question": outcome.question, "options": outcome.options},
                    blueprint_id=blueprint.id,
                    slot_bindings_json=_dumps(slot_bindings),
                )
            if isinstance(outcome, OmitSlot):
                # Slice C: carry an omitted optional slot's `optional_pattern` for
                # EVERY referenced `{token}` (keyed by `slot_token_names` so a
                # `period_range`'s `{name}_start`/`{name}_end` both apply) so the
                # referencing node's `bind_template` replaces each predicate instead
                # of leaving an unbound `{token}`. No pattern / unreferenced → the
                # token stays → that node fails closed to the raw loop (safe default).
                if outcome.optional_pattern is not None:
                    for token in slot_token_names(spec):
                        if token in all_referenced:
                            omitted_patterns[token] = outcome.optional_pattern
                continue
            if isinstance(outcome, SlotBinding):
                # Same all-tokens rule as the single-node path: a `period_range`
                # needs BOTH `{name}_start`/`{name}_end` referenced across the DAG's
                # node templates, or a dropped bound = a dropped filter (D56 class).
                tokens = slot_token_names(spec)
                if tokens - all_referenced:
                    # A resolved slot token referenced by NO node template is a
                    # silent dropped filter (the D56 wrong-answer class) — fail-closed.
                    _logger.warning(
                        "blueprint %s: resolved slot %r token(s) %s referenced by no node; "
                        "SLOT_INVALID",
                        blueprint.id,
                        spec.name,
                        sorted(tokens - all_referenced),
                    )
                    return bound, omitted_patterns, ExecFailed(
                        SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True
                    )
                bound.update(expand_binding(spec, outcome.value))
        return bound, omitted_patterns, None

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
            # The TERMINAL node's SQL — the one whose rows are the blueprint's
            # answer. Exposed explicitly rather than left as "the last element of
            # `sql`": rehydrated nodes (D45 exactly-once resume) are appended to
            # `node_sqls` FIRST, so that positional assumption is not safe. It is
            # what lets `answerWithTable(blueprint_id=…)` resolve a designation to
            # a concrete pageable query WITHOUT re-running the blueprint (see
            # `composite/answer_with_table.py`).
            "terminal_sql": terminal_sql,
            "verify": _verify_block(verify_out, row_count),
        }
        _stamp_window_anchor(
            result_full,
            blueprint,
            template_sql=terminal_template,
            columns=columns,
            rows=rows,
            truncated=truncated,
        )
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
        """Build a mid-DAG `ExecPaused` — the completed-node records (their SCALAR
        outputs, captured provenance, bound SQL, and a table producer's materialized
        `scratch.…` name + row count) plus the node to resume at are serialized into
        the checkpoint (D45 durability, §2.5). Serialization shape and its
        re-validation on the way back live in `_dumps_completed` /
        `_restore_materialized`."""
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
        """Return the scope-enforced DISTINCT domain of `binds_to` ("database.table.column") and
                the provenance entries to fold into the union:

                  - a malformed `binds_to`, or a denied or errored probe -> `(None, [])`: no domain
                    (the resolver binds directly, and the NODE query's own D57 enforcement still
                    gates it — never a fabricated match), and NOTHING is added to the union, since a
                    denied probe read nothing.
                  - a SUCCESSFUL probe -> `(values, [probe.provenance])`, appended UNCONDITIONALLY
                    even when `None`, so a successful probe with undetermined provenance POISONS the
                    union — the same fail-closed rule as the node and grain queries.

                An ordinary dispatched runQuery; it does not count against the model budget.
        """
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
            "runQuery",
            {"sql": probe_sql, "limit": None},
            credentials,
            emit_progress=False,  # internal blueprint query — see the emit_progress note at the top of this module
        )
        if probe.status != "ok":
            return None, []  # a denied/errored probe read nothing → contributes nothing
        _columns, rows, _row_count, _truncated = _unpack_result(probe.result_full)
        values = [str(row[0]) for row in rows if row and row[0] is not None]
        return values, [probe.provenance]  # append UNCONDITIONALLY (None poisons the union)

    async def _restore_materialized(
        self,
        blueprint_id: str,
        completed: dict[int, dict[str, Any]],
        table_consumed_orders: set[int],
        credentials: RuntimeCredentials,
    ) -> dict[int, str]:
        """Re-seed `materialized` on resume from the checkpoint's completed-node records
                — the ONLY way a table intermediate can survive a mid-DAG pause, since its
                producer is rehydrated-skipped and never re-materializes (exactly-once, D45).

                Nothing here is trusted. Three gates, cheapest first, and a failure of any one
                simply does NOT seed that order: the consumer then finds no binding for its
                `$N`, `_node_table_bindings` returns the pre-existing SLOT_INVALID, and the raw
                loop answers. No new error code, no partial bind.

                1. OWNERSHIP (D64) — the trust boundary. Every OTHER value that ever reached
                   `materialized` came straight back from the scratch endpoint, which derives
                   the `s_<sid>_` prefix server-side from X-Session-Id. THIS one comes from
                   persisted checkpoint JSON, which a tampered session document could author,
                   and it flows on into `_node_table_bindings` -> `bind_template` -> an
                   identifier in dispatched SQL. It is AST-quoted, so not an injection surface
                   — but a name reading `scratch.s_<OTHERSID>_bp_…` would be rewritten into the
                   JOIN as a cross-session scratch read. So the SAME ownership rule the
                   provenance extractor enforces on a read is re-applied at the point the
                   untrusted name re-enters the runtime. Defense in depth: the MCP's own D64
                   read gate would also deny it at dispatch; we do not make that the only check.

                   WHAT THIS GATE DOES NOT COVER: it proves the named table belongs to THIS
                   session, not that it is the table this run produced. WITHIN-session
                   integrity belongs to the session document — a tamperer with write access to
                   it can swap in a different own-session scratch table, exactly as they can
                   rewrite the `output` scalars or the carried `sql`. That is the pre-existing
                   trust model for the checkpoint, not something the table carry widens.

                2. A CARRIED ROW COUNT must be present and well-typed. A checkpoint written by
                   code older than this gate has a table but no count; the count cannot be
                   verified, so the name is refused (fail-closed on the deploy boundary).

                3. A LIVE COUNT of the named table must match it EXACTLY. This is the TTL
                   gate. The intermediate carries a ROW-level TTL on a MergeTree (ch-api
                   `build_scratch_create_sql`), so after `scratch_ttl_seconds` (3600 by
                   default, against a 7-day session doc) the table still resolves while its
                   rows are gone — no dispatch error, a smaller JOIN, and a D56 shape check
                   that validates the smaller result happily. Exact equality in BOTH
                   directions distinguishes the two cases an emptiness check would conflate: a
                   producer that legitimately materialized ZERO rows stores 0, probes 0 and
                   passes; one that materialized 812 and expired to 0 — or to 300 mid-merge —
                   is refused.

                Only orders that are actually table-consumed are considered, so a refused or
                irrelevant record costs no dispatch at all.
        """
        restored: dict[int, str] = {}
        for order in sorted(table_consumed_orders):
            record = completed.get(order)
            if record is None:
                continue  # not rehydrated — this producer runs live and materializes itself
            table = record.get("table")
            if not isinstance(table, str) or not _is_own_session_scratch_table(
                table, credentials.session_id
            ):
                # Log it: this is the branch that detects a tampered checkpoint naming
                # another session's scratch table, and the non-raising ownership
                # predicate deliberately swallows the `ScratchSessionError` whose
                # message was the only other signal. Identifier + node order ONLY —
                # never the table name or any warehouse data (D25).
                _logger.warning(
                    "blueprint %s node %s carried a scratch table this session does not "
                    "own (or no table at all); NOT restored — the consumer will fail "
                    "closed to SLOT_INVALID",
                    blueprint_id,
                    order,
                )
                continue
            expected = record.get("row_count")
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                _logger.warning(
                    "blueprint %s node %s carried a scratch table with no usable row "
                    "count (legacy or tampered checkpoint); NOT restored — the table's "
                    "contents cannot be verified against its TTL",
                    blueprint_id,
                    order,
                )
                continue
            live = await self._count_scratch_rows(table, credentials)
            if live != expected:
                _logger.warning(
                    "blueprint %s node %s materialized %d rows but its scratch table now "
                    "counts %s; NOT restored — a row-TTL-expired or altered intermediate "
                    "would under-count the JOIN into a 'verified' wrong answer",
                    blueprint_id,
                    order,
                    expected,
                    live,
                )
                continue
            restored[order] = table
        return restored

    async def _count_scratch_rows(
        self, full_table: str, credentials: RuntimeCredentials
    ) -> int | None:
        """`SELECT COUNT(*)` the named scratch table through the runQuery choke point →
                its live row count, or `None` on ANY surprise (a denial, an error, a vanished
                table, an unreadable result). The caller treats `None` as "do not restore".

                Dispatched exactly as `_probe_domain` dispatches its DISTINCT probe: through
                `ToolDispatcher.dispatch` so D64 scratch isolation, D57 column scope and D5
                credential injection come free, and with `emit_progress=False` so it stays an
                internal query that costs no model budget. The table is built as an AST
                identifier, never string-interpolated — the ownership rule constrains the
                name's SHAPE (`s_<sid>_<suffix>`) but not its character set, so the suffix is
                still endpoint-returned-or-persisted text and gets the same structural
                treatment the JOIN rewrite gives it.

                PROVENANCE: unlike `_probe_domain` (which appends its probe's provenance
                UNCONDITIONALLY on success, so an undetermined footprint POISONS the union),
                this probe contributes NOTHING to `provenances`. Deliberate, and the asymmetry
                is real rather than an oversight: a domain probe READS A WAREHOUSE COLUMN, so
                an undetermined provenance there means "we read warehouse data and cannot say
                what" and must poison. This probe reads exactly one table, in the `scratch`
                database, pinned by the gate above — no warehouse column is touched, scratch
                pairs are excluded from the USES set by construction (D69/OQ-4), and the
                warehouse lineage of the rows it counts is the PRODUCER's provenance, which is
                carried on the same checkpoint record and already folded into the union. So
                folding this probe could only ever add an empty set, or spuriously poison an
                otherwise-honest footprint because an internal integrity check hiccuped. The
                answer's footprint claim is unchanged and still complete either way.

                The number returned is COUNT(*), not a row of data: it is compared against a
                stored integer and never reaches the model, the answer or a binding.
        """
        db, _sep, name = full_table.partition(".")
        probe_sql = (
            exp.select(exp.Count(this=exp.Star()))
            .from_(exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(db)))
            .sql(dialect="clickhouse")
        )
        probe = await self._tool_dispatcher.dispatch(
            "runQuery",
            {"sql": probe_sql, "limit": None},
            credentials,
            emit_progress=False,  # internal blueprint query — see the emit_progress note at the top of this module
        )
        if probe.status != "ok":
            return None
        _columns, rows, _row_count, _truncated = _unpack_result(probe.result_full)
        if len(rows) != 1 or len(rows[0]) != 1:
            return None
        value = rows[0][0]
        if isinstance(value, bool):
            return None  # a bool is not a count, and `int(True)` would say 1
        try:
            # `int(...)` rather than an isinstance check, matching
            # `unpack_grain_probe`: a live ClickHouse may hand a UInt64 back as a
            # decimal STRING over JSON, and refusing that would fail every real
            # resume. A non-numeric value raises and becomes `None`.
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _materialize_node(
        self,
        blueprint_id: str,
        node: Node,
        result_full: Any,
        credentials: RuntimeCredentials,
    ) -> tuple[str | None, int | None, ExecFailed | None]:
        """Materialize a table-output node's result into a session-scoped scratch table via the
                D93 side-channel.

                Returns `(full_scratch_table_name, row_count, None)` on success — the RETURNED
                `scratch.s_<sid>_bp_<uuid>` name, used VERBATIM and never reconstructed, and the
                number of rows ACTUALLY sent to the endpoint — or `(None, None,
                ExecFailed(UNSUPPORTED))` on a structural over-cap or a rejected materialize.
                Both fail closed to the raw loop: never a runaway materialization, never a wrong
                answer.

                The row count is returned (rather than re-derived at the call site from
                `result_full`) so it can only ever be the length of the list this function
                handed the endpoint. It is stored on the checkpoint record and is what a resume
                re-counts the table against — see `_restore_materialized`; a count computed from
                a different vantage point could drift from what was actually written.
        """
        columns, rows, _rc, truncated = _unpack_result(result_full)
        if not columns:
            _logger.info(
                "blueprint node %s produced no columns to materialize; UNSUPPORTED", node.order
            )
            return None, None, ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        # TRUNCATION guard (BLOCKER fix): the MCP's runQuery HARD-caps rows at
        # max_response_rows (service.py `_compact_result`) regardless of any caller
        # LIMIT, and runBlueprint passes no query_limit. A producer returning more
        # than that cap arrives PRE-TRUNCATED (the row/column caps below never see
        # the real size), so materializing it would build a PARTIAL scratch table
        # and the downstream JOIN would aggregate over only the surviving rows —
        # returning a silently under-counted "verified" answer (the exact
        # wrong-answer class D56 exists to block). A truncated intermediate is
        # unmaterializable → fail closed to the raw loop, BEFORE any materialize.
        if truncated:
            _logger.info(
                "blueprint node %s intermediate was TRUNCATED by the runQuery row cap; "
                "UNSUPPORTED (a partial scratch table would under-count the JOIN)",
                node.order,
            )
            return None, None, ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        # Structural guard (mirrors the D89(d) scalar guard): an oversized
        # intermediate → raw loop, never a runaway. An EMPTY result is allowed (an
        # empty scratch table JOINs to nothing — a legitimate "no rows" answer the
        # terminal grain gate still validates).
        if len(rows) > self._scratch_max_rows or len(columns) > self._scratch_max_columns:
            _logger.info(
                "blueprint node %s intermediate over cap (%d rows, %d cols); UNSUPPORTED",
                node.order,
                len(rows),
                len(columns),
            )
            return None, None, ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        typed_cols = _infer_scratch_columns(columns, rows)
        assert self._scratch_client is not None  # gated at _execute_dag entry
        self._observer(
            "blueprint_step",
            {"blueprint_id": blueprint_id, "step": "materializing", "node": node.order},
        )
        try:
            table = await self._scratch_client.materialize(
                typed_cols, rows, jwt=credentials.jwt, session_id=credentials.session_id
            )
        except ScratchClientError as exc:
            # Over-cap server-side, a bad type, or any endpoint rejection → raw loop.
            _logger.info(
                "blueprint node %s materialize rejected (%s); UNSUPPORTED",
                node.order,
                exc.code,
            )
            return None, None, ExecFailed(UNSUPPORTED_CODE, _UNSUPPORTED_MESSAGE, retryable=False)
        return table, len(rows), None

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
            "runQuery",
            {"sql": probe_sql, "limit": None},
            credentials,
            emit_progress=False,  # internal blueprint query — see the emit_progress note at the top of this module
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
        window_anchor=detail.window_anchor,
    )


# A `YYYY-MM-DD` head, optionally followed by a ` ` or `T` separator and ANY tail — the
# tail is discarded, so it is not parsed (`2021-06-01 00:00:00`, `2021-06-01T00:00:00Z`
# and `2021-06-01 whatever` are all the date `2021-06-01`). Deliberate: only the DATE
# part is ever stamped, and a stricter time grammar would buy nothing but a rejection of
# a warehouse timestamp format nobody has seen yet. A value with no such head is not a
# window bound this executor will claim.
_ISO_DATE_HEAD_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T].*)?")


def _iso_date_part(value: Any) -> str | None:
    """The `YYYY-MM-DD` date part of *value*, or `None` when it is not a date.

    `date.fromisoformat` does the calendar validation (`2021-13-45` is refused)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    matched = _ISO_DATE_HEAD_RE.fullmatch(value.strip())
    if matched is None:
        return None
    try:
        return date.fromisoformat(matched.group(1)).isoformat()
    except ValueError:
        return None


def _data_window_bounds(
    blueprint: Blueprint,
    template_sql: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
) -> tuple[str, str] | None:
    """The `(min, max)` date the returned rows actually cover, or `None` when they do
    not license the claim (J7-anchor). NO new query — this reads the rows the executor
    already holds, mapping the DECLARED grain to its output column with the same
    `_map_grain_columns` the D56 gate uses.

    FAIL-CLOSED to `None` on: no declared grain, a grain of more than one column (which
    of them is the window is not knowable here), an unmappable column, a truncated
    result (the max row is not the max value), no rows, or any value that is not an ISO
    date. A wrong date in the note is worse than no date."""
    grain_columns = blueprint.result_grain.columns
    if len(grain_columns) != 1 or truncated or not rows:
        return None
    mapped = _map_grain_columns(template_sql, grain_columns)
    if mapped is None or len(mapped) != 1 or mapped[0] not in columns:
        return None
    index = columns.index(mapped[0])
    bounds: list[str] = []
    for row in rows:
        if index >= len(row):
            return None
        parsed = _iso_date_part(row[index])
        if parsed is None:
            return None
        bounds.append(parsed)
    # `YYYY-MM-DD` orders lexicographically exactly as it orders chronologically.
    return min(bounds), max(bounds)


def _stamp_window_anchor(
    result_full: dict[str, Any],
    blueprint: Blueprint,
    *,
    template_sql: str,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
) -> None:
    """Record the EXECUTED blueprint's window-anchor declaration on the result, plus the
    CONCRETE window the rows cover when it can be derived (J7 / J7-anchor).

        ONE writer for both finish paths (single-node and DAG `_finalize`) so the two cannot
        describe the same blueprint's window differently. The key is written only when the
        blueprint declares an anchor, so every other result is byte-identical to before.

        This is the value the model-facing note is DERIVED from (`tool.window_note_for_result`)
        rather than a second copy of the note: `result_full` is persisted behind a D46 KV pointer
        and re-read on the D45 resume path, so storing the raw declaration lets a resumed run
        re-derive the identical note instead of carrying prose through the checkpoint. The
        `window_start`/`window_end` dates are stamped for the same reason and computed HERE,
        once, from the terminal rows — never recomputed downstream, where the rows are gone.

        The dates are stamped only for a `data` anchor (a calendar window's bounds are the
        caller's own slot values, already known to the model) and only when
        `_data_window_bounds` can derive them; otherwise the result is exactly what it was
        before this slice and the static note ships.
    """
    if blueprint.window_anchor is None:
        return
    result_full["window_anchor"] = blueprint.window_anchor
    if blueprint.window_anchor != DATA_WINDOW_ANCHOR:
        return
    bounds = _data_window_bounds(blueprint, template_sql, columns, rows, truncated)
    if bounds is None:
        return
    result_full["window_start"], result_full["window_end"] = bounds


def _is_present(raw: Any) -> bool:
    """True iff *raw* is a non-absent slot value (mirrors slots._is_absent) — used
    to skip a domain probe for an absent slot (n3: a missing required slot pauses
    on presence alone and must not waste a warehouse query)."""
    return not (raw is None or (isinstance(raw, str) and raw.strip() == ""))


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


def _verify_block(verify_out: VerifyOutcome, row_count: int) -> dict[str, Any]:
    """The `result_full["verify"]` block — ONE builder for BOTH the single-node and the DAG
        finalize path, so the two can never describe the same gate differently.

        AN EMPTY RESULT IS UNVERIFIABLE, NOT VERIFIED. The D56 grain teeth are
        `row_count == distinct_grain_count`; at zero rows that reads `0 == 0`, which passes for
        every blueprint ever written, correct or not. The check did not catch anything because
        there was nothing to catch, so it is reported as NOT having run (`grain_checked: False`)
        plus an explicit `empty_result: True` marker — which is what lets the badge say
        *empty — unverifiable* instead of *verified*.

        DELIBERATELY UNTOUCHED: `grain_ok` stays as the gate computed it (vacuously True). It,
        with `signature_ok`, is what `tool._is_verified_blueprint_result` reads to set the
        `authoritative` marker, and an empty blueprint result IS still the authoritative answer
        for its intent. Only the VERIFICATION CLAIM is withdrawn, never the no-re-derivation rule.

        On a NON-empty result the returned dict is byte-identical to what it has always been:
        `empty_result` is emitted only when it is true.
    """
    block: dict[str, Any] = {
        "grain_ok": verify_out.grain_ok,
        "grain_checked": verify_out.grain_checked,
        # n4: Slice B does not check a result SIGNATURE yet (no declared signature
        # is stored), so `signature_ok` is vacuously True — flag
        # `signature_checked: False` so the D56 LLM-review input is honest (mirrors
        # `grain_checked`), never implying a check that did not run.
        "signature_ok": verify_out.signature_ok,
        "signature_checked": False,
    }
    if row_count == 0:
        block["grain_checked"] = False
        block["empty_result"] = True
    return block


def _union_provenance(
    provenances: list[frozenset[tuple[str, str]] | None],
) -> frozenset[tuple[str, str]] | None:
    """Union every inner runQuery's captured provenance. Fail-closed: if ANY inner call had
        undetermined (`None`) provenance the union is `None` — the assistant message then drops
        from D44 replay, matching `_compute_turn_provenance_union`'s posture.

        SCRATCH columns are EXCLUDED (D69): a `scratch.*` pair is session-gated, not scope-gated,
        and the materialized scratch table is an ephemeral projection of already-scope-checked
        warehouse data. Dropping those pairs keeps the persisted footprint HONEST — exactly the
        real warehouse columns the answer depends on. The `None`-poison rule is unchanged.
    """
    acc: set[tuple[str, str]] = set()
    for prov in provenances:
        if prov is None:
            return None
        acc.update(pair for pair in prov if not pair[0].startswith("scratch."))
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


def _table_consumed_orders(nodes: tuple[Node, ...]) -> set[int]:
    """Every upstream node order consumed as a TABLE (`consumes: {ph: "$N"}`) — the
    nodes that must be materialized to scratch before their consumer dispatches."""
    orders: set[int] = set()
    for n in nodes:
        for ref in n.consumes.values():
            match = TABLE_CONSUME_REF.match(str(ref))
            if match is not None:
                orders.add(int(match.group(1)))
    return orders


def _node_table_bindings(
    node: Node, materialized: dict[int, str]
) -> tuple[dict[str, str], ExecFailed | None]:
    """Map each TABLE consume `{placeholder: "$N"}` to the BARE materialized scratch
    table name (`s_<sid>_bp_<uuid>`) for the AST JOIN rewrite (§2.2 step 3). An
    upstream node not (yet) materialized → SLOT_INVALID (fail-closed — never a JOIN
    against a non-existent scratch table). Scalar consumes are ignored here (they
    are value bindings, handled by `_node_bindings`).

    This is ALSO where every refused RESTORE lands: a checkpoint whose carried table
    is missing, malformed, cross-session, countless, or whose live row count no
    longer matches what the producer wrote (a row-TTL-expired intermediate) is not
    seeded by `_restore_materialized`, so `materialized` simply has no entry for `$N`
    and the run takes this same pre-existing SLOT_INVALID exit — no new error code,
    raw loop answers."""
    bindings: dict[str, str] = {}
    for placeholder, ref in node.consumes.items():
        match = TABLE_CONSUME_REF.match(str(ref))
        if match is None:
            continue  # a scalar `$N.name` consume — not a table binding
        order = int(match.group(1))
        full = materialized.get(order)
        if full is None:
            return {}, ExecFailed(SLOT_INVALID_CODE, _SLOT_INVALID_MESSAGE, retryable=True)
        bindings[placeholder] = full.split(".", 1)[1] if "." in full else full
    return bindings, None


def _infer_ch_type(values: list[Any]) -> str:
    """Infer the non-Nullable ClickHouse type for a column's NON-NULL cells.

        Maps NATIVE result types: Bool -> Int64 -> Float64 -> String. A STRING cell maps to
        `String` and is never re-parsed as a number, so an explicit `toString(...)` CAST on a
        join key is HONORED — the scratch column stays String and matches the String warehouse
        key, sidestepping the all-numeric-string mistyping hazard. Any mixed column falls back to
        `String` (fail-safe: the endpoint stores it as data).
    """
    if not values:
        return "String"  # all-NULL → the caller Nullable-wraps this
    if all(isinstance(v, bool) for v in values):
        return "Bool"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "Int64"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "Float64"
    return "String"


def _infer_scratch_columns(
    columns: list[str], rows: list[list[Any]]
) -> list[dict[str, str]]:
    """Infer `[{name, type}]` for a table intermediate (OQ-A default — runtime
    inference reusing admin_ingest's Int64→Float64→String ladder, Nullable-wrapped
    on any NULL). The endpoint RE-VALIDATES every type against its whitelist and
    native-inserts the cells as DATA (never SQL)."""
    typed: list[dict[str, str]] = []
    for idx, name in enumerate(columns):
        cells = [row[idx] for row in rows if idx < len(row)]
        non_null = [v for v in cells if v is not None]
        has_null = len(non_null) != len(cells)
        base = _infer_ch_type(non_null)
        typed.append({"name": name, "type": f"Nullable({base})" if has_null else base})
    return typed


def _all_referenced_slots(blueprint: Blueprint) -> set[str]:
    """Every `{slot}` token referenced by the templates the DAG path ACTUALLY runs.

        When `composes` is non-empty the DAG path runs and the top-level `sql_template` is DEAD.
        Counting its `{slot}` tokens here would let a required slot that lives ONLY in the dead
        template pass the referenced-slot backstop while being silently dropped at execution — a
        company-wide "verified" wrong answer. So only node templates count when composes is
        present; the loader also rejects the both-present hybrid outright.
    """
    referenced: set[str] = set()
    if blueprint.composes:
        for node in blueprint.composes:
            if node.sql_template:
                referenced |= referenced_slots(node.sql_template)
    elif blueprint.sql_template:
        referenced |= referenced_slots(blueprint.sql_template)
    return referenced


def _extract_scalar_output(node: Node, result_full: Any) -> dict[str, Any] | None:
    """Read a query node's SCALAR output(s) from its single-cell result.

        A scalar intermediate is a single cell per declared scalar. Fail-closed: returns `None` —
        a hard contract violation the caller maps to SLOT_INVALID — when the node does NOT return
        exactly one row, when the column count does not equal the declared-scalar count, or when
        a mapped cell is NULL. This closes the "silently take `rows[0][0]`" hazard: the D56 grain
        gate only guards the TERMINAL node, so a fanned-out or wide intermediate would otherwise
        bind an ARBITRARY cell downstream and still return a "verified" answer. A node with NO
        declared scalar output yields `{}`.
    """
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
    omitted_patterns: dict[str, str],
) -> tuple[dict[str, Any], ExecFailed | None]:
    """Assemble the exact `{placeholder: value}` set a node template needs from (a) upstream
        scalar `consumes`, (b) resolved slots, and (c) `resolve_via` rule IN-lists. An unbound
        placeholder or an unresolvable consume is fail-closed to SLOT_INVALID (never a half-bound
        query).

        *omitted_patterns* carries tokens satisfied by an omitted optional slot's
        `optional_pattern` — NOT value-bound here, since the pattern replaces their predicate in
        `bind_template`, so they are excluded from the unbound-token check.
    """
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
        elif ref in omitted_patterns:
            # Slice C: an omitted optional slot's token — satisfied by its
            # `optional_pattern` (which replaces the predicate in `bind_template`),
            # so it needs NO value binding here. Not an unbound-token failure.
            continue
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
    """Classify a resume answer as `"approve"` | `"deny"` | `"repause"`.

        Consent is opt-IN: proceed ONLY on an explicit affirmative. An explicit negation denies
        (raw-loop fallback); anything unrecognized — garbage, silence — re-asks the same gate,
        never proceeding on ambiguity. Negations are matched before affirmatives, so "do not
        proceed" denies rather than the embedded "proceed" approving.
    """
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
    table: str | None = None,
    row_count: int | None = None,
) -> dict[str, Any]:
    """A completed-node record: its SCALAR `output`, the captured `provenance`, the
    bound `sql`, and — for a table PRODUCER only — the full
    `scratch.s_<sid>_bp_<uuid>` name it materialized to plus the number of rows it
    wrote there. All five are carried across a mid-DAG pause (B2) so the resumed
    final union + `sql` transparency span the WHOLE DAG, and so the consumer of a
    table intermediate can still bind its JOIN after the pause (the producer is
    rehydrated-skipped and never re-materializes).

    `table` and `row_count` travel TOGETHER and are only useful together: the name
    alone cannot be trusted, because the intermediate carries a ROW-level TTL and the
    table outlives its own rows. The count is what a resume re-verifies the table
    against (`_restore_materialized`).

    Both are `None` for EVERY non-producer node — a skipped/gated node, an approval
    node, and a scalar producer alike."""
    return {
        "output": output,
        "provenance": provenance,
        "sql": sql,
        "table": table,
        "row_count": row_count,
    }


def _is_own_session_scratch_table(full_table: str, session_id: str | None) -> bool:
    """True iff *full_table* is a FULLY-qualified `scratch.<name>` owned by
    *session_id* — the gate a checkpoint-carried materialized table name must pass
    before it is bound into a JOIN (D64).

    Two independent conditions, both fail-closed:
      1. exactly `<db>.<name>` with `db == SCRATCH_DB`. Exactly two dot-parts, so a
         crafted `scratch.s_<sid>_bp_1.something` (which sqlglot would happily emit
         as ONE quoted identifier) is rejected, as is any other database — the
         producer only ever writes to `scratch`;
      2. `<name>` passes the shared D64 ownership rule for this session.

    The rule in (2) is NOT re-implemented here: it is `sqlparse`'s
    `is_own_session_scratch_table`, the same exact-session-extraction the provenance
    extractor enforces on every scratch READ. Two copies of an ownership check drift;
    this one has already been tightened once (a former `startswith` prefix test), and
    a second copy would have kept the loose form alive.
    """
    parts = full_table.split(".")
    if len(parts) != 2 or parts[0] != SCRATCH_DB:
        return False
    return is_own_session_scratch_table(parts[1], session_id)


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
    `[{order, output, provenance, sql, table, row_count}]` list (deterministic,
    restart-durable). Provenance + SQL are carried so a resumed DAG's final
    union/transparency cover the pre-pause nodes that never re-run.

    `table` + `row_count` (the producer's materialized `scratch.…` name and the
    number of rows it wrote; `null` on every other node) are carried so the
    post-pause consumer can bind its JOIN to a table it can PROVE is intact. Both are
    needed: the intermediate's TTL is ROW-level on a MergeTree, so the table survives
    the pause while its rows may not, and only a live count matched against this
    stored one tells the two apart."""
    payload = [
        {
            "order": order,
            "output": running[order].get("output", {}),
            "provenance": _prov_to_jsonable(running[order].get("provenance")),
            "sql": running[order].get("sql"),
            "table": running[order].get("table"),
            "row_count": running[order].get("row_count"),
        }
        for order in sorted(running)
    ]
    return _dumps(payload)


def _rehydrate_completed(completed_nodes_json: str | None) -> dict[int, dict[str, Any]]:
    """Reverse `_dumps_completed` on resume →
    `{order: {output, provenance, sql, table, row_count}}`. A malformed/absent payload
    rehydrates as empty (defensive — the walk re-runs from the top, still correct
    because every node is read-only + idempotent). A record with a missing/`null`
    provenance rehydrates as `None` (poisons the union — the pre-pause footprint is
    undetermined, so the answer must drop). A `table` that is not a `str`, or a
    `row_count` that is not a non-negative `int` (missing, `null`, a bool, a float, a
    string, an object), rehydrates as `None`.

    TYPE is all that is checked here. This payload is untrusted persisted JSON, so
    the D64 OWNERSHIP of the name and the LIVE row count of the table it names are
    both re-checked in `_restore_materialized` — the point where the values would
    otherwise re-enter the runtime."""
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
                table = entry.get("table")
                rows = entry.get("row_count")
                completed[order] = _node_record(
                    dict(output) if isinstance(output, dict) else {},
                    _prov_from_jsonable(entry.get("provenance")),
                    sql if isinstance(sql, str) else None,
                    table if isinstance(table, str) else None,
                    rows if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0 else None,
                )
    return completed


def _load_completed_into_outputs(
    completed: dict[int, dict[str, Any]], node_outputs: dict[str, Any]
) -> None:
    """Rehydrate completed nodes' SCALAR outputs into the `when`/`consumes` env on
    resume. Only scalar OUTPUTS feed this env — no result ROWS cross the checkpoint
    (F2 buys this simplification) — so `$N` is a synthetic row-shape marker
    (non-empty ⇒ row_count 1) for `count`/`empty`, and `$N.<name>` is the stored
    scalar. Unchanged by the table-intermediate resume fix: a producer's materialized
    table NAME also crosses the checkpoint now, but it is a JOIN binding, not a
    value, and is consumed by `_node_table_bindings` — never by this env."""
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
