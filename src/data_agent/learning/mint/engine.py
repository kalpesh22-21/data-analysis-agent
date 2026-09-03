"""Turn an expert's submission into a candidate on the review queue.

WHAT THIS MODULE DOES NOT DO is the point of it. It does not validate parameterization, rewrite a
template, scan for entities, dedup, or decide a status. All of that already exists and is already
the thing the review queue trusts, so minting ENTERS it rather than reimplementing it: build an
envelope carrying the expert's SQL as the accepted SQL, persist it at `extracted`, and hand it to
`ParameterizationCompleter`, which is the same machine a human's completed form goes through.

THE TOTALITY WALK IS THE GATE, and it is the reason this design is safe rather than merely short.
D97 walks every literal predicate of the accepted SQL and refuses the candidate unless each one is
accounted for by an entry. For a minted blueprint the accepted SQL came from OUTSIDE the entries —
an expert typed it, or a model drafted it before being asked to classify it — so the walk is doing
real work here, unlike the reconstruction case (`generalize/reconstruct.py`) where it is circular
by construction. A hand-authored blueprint therefore faces a STRICTER structural check than the
mined ones do, not a looser one.

WHAT IT IS NOT: an authoring surface for canon. Nothing here writes a blueprint YAML, sets
`verified`, or promotes. A minted blueprint lands as a candidate and a human still approves it,
with the trial run, the parameterization judge and the promotion replay all in front of it.
"Hand-authored means trusted" is a tempting shortcut and it is wrong for two of the three modes,
because in `pseudo`/`none` mode the SQL is written by a MODEL from the expert's steps.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from data_agent.runtime.blueprint.compiler import _SCRATCH_DB
from data_agent.runtime.blueprint.template import parse_template, referenced_slots
from data_agent.runtime.model.client import ModelClient
from data_agent.sqlparse import ProvenanceExtractionError, extract_column_provenance

from ..candidate.decline import EvidencePointer, ValidationSnapshot
from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..inbox.completion import (
    CompletionResult,
    CompletionUnavailableError,
    ParameterizationCompleter,
)
from .models import (
    MintConflictError,
    MintInputError,
    MintRequest,
    MintResult,
    MintUnavailableError,
)
from .prompt import (
    CLASSIFY_SYSTEM_PROMPT,
    COMPOSITE_SYSTEM_PROMPT,
    DRAFT_SYSTEM_PROMPT,
    mint_brief,
)
from .schema import (
    MintResponseError,
    build_classify_tool,
    build_composite_draft_tool,
    build_draft_tool,
    coerce_mint_response,
    coerce_node_sql,
)

_logger = logging.getLogger(__name__)

# The single synthetic tool-call ref a minted blueprint's SQL is filed under. A real candidate
# keys its SQL by the tool call that produced it; a minted one has no session, so it needs a
# stable name that the evidence pointer and `sql_by_ref` agree on.
MINT_TOOL_CALL_REF = "mint"

# Marks the source session of a minted candidate. Deliberately NOT a plausible session id: every
# reader that groups by session, expires by session, or tries to reload one must be able to tell
# at a glance that there is no session behind this and never was.
MINT_SESSION_PREFIX = "mint::"

# What a minted candidate claims about itself before any judge or gate has looked. Neutral by
# construction: the extractor's confidence is a model's self-report about what it observed in a
# session, and there is no session here. A high value would let a hand-authored blueprint outrank
# mined ones in the review ranking on the strength of nothing.
MINT_CONFIDENCE = 0.5

# The acceptance signal a minted blueprint carries. `explicit_confirm` is the honest member of
# the enum rather than the flattering one: the vocabulary describes HOW A HUMAN SIGNALLED that an
# answer was right, and the weakest member (`no_correction`) means only that nobody objected. An
# expert opening an authoring page, writing the question out and submitting a query for it is the
# most explicit confirmation the enum can express, and it is a stronger signal than most mined
# candidates carry.
#
# What it does NOT assert is that the query RAN. Nothing here executes anything; `verified` stays
# False, the trial run is offered on the card, and promotion still replays. The signal is about
# the human, the replay is about the warehouse, and minting only ever speaks to the first.
MINT_ACCEPTED_SIGNAL = "explicit_confirm"


def _check_nodes(request: MintRequest, sql_per_node: list[str]) -> None:
    """Refuse a composite whose node SQL cannot work, BEFORE anything is persisted.

    TWO CHECKS, both derived from what the DAG will actually do with this SQL:

    1. IT MUST PARSE. The template is produced by AST rewrite, so unparseable SQL declines
       later as `unrewritable_sql: un-parseable SQL at mint2` — a message naming an internal
       ref, about a step the expert can see and name. The common cause is a model writing the
       `$0.name` consume grammar into the query itself; that grammar belongs only in the
       `consumes` map, and the SQL uses the brace token. Saying so here is the difference
       between a fixable complaint and a puzzle.

    2. IT MUST ACTUALLY REFERENCE WHAT IT CLAIMS TO CONSUME. A step declaring `feeds_from` gets
       that value bound at run time; a query that never mentions the token silently ignores it
       and answers a different question. Nothing downstream catches this — `check_dag` validates
       the GRAPH, and the rewrite validates the SQL, but no one compares the two. The edge would
       be a lie that promotes cleanly and returns a wrong number for ever.
    """
    if not request.is_composite:
        return
    # WHOSE FAULT IS IT. In `exact` mode the expert wrote these queries, so a failure here is
    # input they can fix (400); otherwise a model wrote them and it is an off-contract response
    # (502). Reporting an expert's own broken step SQL as "the assistant answered off-contract"
    # sends them to look at the wrong thing.
    fault = MintInputError if request.sql_is_authoritative else MintResponseError
    for order, (node, sql) in enumerate(zip(request.nodes, sql_per_node, strict=True)):
        try:
            parse_template(sql)
        except Exception as exc:  # noqa: BLE001 - reported, never raised onward
            raise fault(
                f"step {order + 1} is not valid SQL ({str(exc)[:120]}). To use an earlier "
                "step's result, write its name in braces — {name} — not the $0.name form, "
                "which is the DAG's own wiring notation and is not SQL."
            ) from None
        if node.output_kind == "scalar":
            probe = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "1", sql)
            try:
                root = sqlglot.parse_one(probe, dialect="clickhouse")
            except Exception:  # parse_template above owns the actionable parse error
                root = None
            select = root if isinstance(root, exp.Select) else None
            if select is not None and select.args.get("group") is not None:
                raise fault(
                    f"step {order + 1} declares one scalar value, but its SQL has GROUP BY "
                    "and can return multiple rows. Declare this step's output as a table, or "
                    "rewrite it to return exactly one value."
                )
        # `referenced_slots` is the runtime's own reader of `{token}` occurrences — the same one
        # binding uses to decide which slots a template actually has. Asking it beats a substring
        # search, which would count a token inside a string literal or a comment.
        present = referenced_slots(sql)
        # THE CONVERSE, and it was missing. Checking only "every declared edge is referenced"
        # let a node carrying `{total}` with NO edge mint `completed`, validate `ok`, and die at
        # the LANDING write — "references undeclared slot(s) ['total']" — past the review valve,
        # after a human approved it. Knowable from the SQL alone, which is the same argument the
        # node cap already makes: a limit the form can see must not cost a model turn and a
        # guaranteed-dead review row.
        #
        # The blueprint's own caller-fillable slots are legitimate, so the declared set is the
        # upstream outputs PLUS whatever the parameterization will introduce — and at this point
        # the parameterization is not yet rewritten, so only the upstream half can be checked
        # here. A token that is neither is the fictional case.
        declared = {request.nodes[e].output_for(e) for e in node.feeds_from}
        for edge in node.feeds_from:
            upstream = request.nodes[edge]
            name = upstream.output_for(edge)
            if upstream.output_kind == "table":
                # A TABLE consume is a FROM/JOIN source, not a bound token — the loader's own
                # rule is that the placeholder appears as `scratch.<name>`. `check_dag` says
                # explicitly that it cannot verify this (it sees the plan, not the templates),
                # so this is the only place it can be caught before landing.
                if f"{_SCRATCH_DB}.{name}" not in sql:
                    raise fault(
                        f"step {order + 1} says it needs step {edge + 1}'s table, but its SQL "
                        f"never reads {_SCRATCH_DB}.{name}. Reference it as a FROM or JOIN "
                        "source."
                    )
                continue
            if name not in present:
                raise fault(
                    f"step {order + 1} says it needs step {edge + 1}, but its SQL never uses "
                    "{" + name + "}. That value would be bound and then ignored, so the step "
                    "would silently answer a different question."
                )
        fictional = sorted(present - declared)
        if fictional:
            raise fault(
                f"step {order + 1} uses "
                + ", ".join("{" + n + "}" for n in fictional)
                + ", but declares no step it comes from. Nothing would fill it and the "
                "blueprint would be refused when it is published — tick the earlier step it "
                "needs, or write the value in directly."
            )


def _composes(request: MintRequest, refs: list[str]) -> list[dict[str, Any]]:
    """The `composes` DAG for a composite submission, built from the expert's declaration.

    Every field the loader and the executor read is derived here rather than asked of a model:
    `order` is the position, `output` is the name the expert gave (or one from the position),
    `consumes` is the mechanical `$n.name` ref for each declared edge, and `feeds_from` is the
    edge list itself. `when` is always None — a `when`-bearing composite cannot promote 1:1 onto
    a runtime `Node` (`_generalize_composite` rejects it), so producing one here would mint a
    candidate guaranteed to fail review.
    """
    nodes: list[dict[str, Any]] = []
    for order, node in enumerate(request.nodes):
        # THE REF GRAMMAR IS CHOSEN BY THE UPSTREAM NODE'S KIND, and the runtime has exactly
        # two: `$0.name` for one scalar, bare `$0` for the whole table. Both are derived here so
        # the expert never types either.
        consumes = {}
        for edge in node.feeds_from:
            upstream = request.nodes[edge]
            name = upstream.output_for(edge)
            consumes[name] = f"${edge}" if upstream.output_kind == "table" else f"${edge}.{name}"
        nodes.append(
            {
                "order": order,
                "node_kind": "query",
                "step_intent": node.step_intent,
                "feeds_from": list(node.feeds_from),
                "consumes": consumes,
                # THE KIND THE EXPERT DECLARED. Both `NODE_OUTPUT_KINDS` members are supported
                # by `check_dag`, the corpus loader and the executor. ⚠ A `table` intermediate
                # nevertheless cannot be PROMOTED yet: S4's `_provenance_uses` walks every node
                # template against the catalog, and `scratch.<name>` is not a catalog table, so
                # the whole composite stamps `explain_ok=False, uses=[]`. See `_provenance_uses`.
                "output": {node.output_for(order): node.output_kind},
                "source_tool_call_ref": refs[order],
                "when": None,
                "requires_approval": None,
            }
        )
    return nodes


def _semantic_type(expression: exp.Expression, schema: dict[str, dict[str, str]]) -> str:
    raw = ""
    column = expression.find(exp.Column)
    if column is not None:
        matches = [
            kind
            for columns in schema.values()
            for name, kind in columns.items()
            if name == column.name
        ]
        raw = matches[0].lower() if len(set(matches)) == 1 else ""
    if isinstance(expression, (exp.AggFunc, exp.Binary)) or expression.find(exp.AggFunc):
        return "number"
    if "datetime" in raw or "timestamp" in raw:
        return "datetime"
    if "date" in raw:
        return "date"
    if any(token in raw for token in ("int", "decimal", "float", "double", "numeric")):
        return "number"
    if "bool" in raw:
        return "boolean"
    if raw:
        return "string"
    return "unknown"


def _infer_result_signature(sql: str, schema: dict[str, dict[str, str]]) -> dict[str, Any] | None:
    """Infer the terminal SELECT contract; scalar aggregates retain the established null form."""
    probe = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "1", sql)
    try:
        root = sqlglot.parse_one(probe, dialect="clickhouse")
    except Exception:
        return None
    select = root if isinstance(root, exp.Select) else root.find(exp.Select)
    if select is None or any(isinstance(item, exp.Star) for item in select.expressions):
        return None
    group = select.args.get("group")
    if (
        group is None
        and select.expressions
        and all(item.find(exp.AggFunc) for item in select.expressions)
    ):
        return None
    shape = []
    expression_names: dict[str, str] = {}
    for index, item in enumerate(select.expressions):
        name = item.alias_or_name or f"column_{index + 1}"
        base = item.this if isinstance(item, exp.Alias) else item
        expression_names[base.sql(dialect="clickhouse")] = name
        shape.append({"column": name, "type": _semantic_type(base, schema)})
    grain: list[str] = []
    if group is not None:
        for grouped in group.expressions:
            key = grouped.sql(dialect="clickhouse")
            grain.append(expression_names.get(key, grouped.name or key))
    return {
        "shape": shape,
        "grain": {"columns": grain, "verifiable": bool(grain)},
        "invariants": [],
    }


def mint_content_hash(request: MintRequest) -> str:
    """The idempotency key for one submission.

    Hashed over WHAT WAS SUBMITTED, not when. Re-submitting an identical form therefore collides
    with the existing candidate rather than forking a second review row for the same blueprint —
    which is the behaviour a double-clicked Draft button needs, and it costs nothing when the
    expert genuinely changes something, because any edit changes the hash.

    The submitter is deliberately EXCLUDED: two experts who independently write the same
    blueprint should meet at one review row, and dedup should be arguing about content.
    """
    # ⚠ SERIALIZED AS JSON, NOT JOINED WITH SEPARATORS. The first version joined the list
    # fields with a space and the groups with a newline, which makes the boundaries ambiguous:
    # steps `("a", "b")` hashed identically to the single step `("a b",)`, and the same held
    # for `tables` and `assumptions`. Two genuinely different submissions therefore shared one
    # content hash — and the hash IS the candidate id, so they would have shared one review row,
    # with the second silently refused as "already minted". JSON quotes and escapes every
    # element, so no value can impersonate a boundary.
    payload = {
        "question": request.question,
        "sql_mode": request.sql_mode,
        "sql": request.sql,
        "tables": list(request.tables),
        "steps": list(request.steps),
        "assumptions": list(request.assumptions),
        # The DAG is part of the submission's identity: the same question answered by a
        # different set of steps is a different blueprint and must not collide with it.
        "nodes": [
            {
                "step_intent": n.step_intent,
                "output": n.output_for(i),
                # THE KIND IS PART OF THE IDENTITY. Flipping a step from scalar to table changes
                # the consume grammar and what the executor passes downstream — a different DAG.
                # Omitting it collided the two onto one candidate id, so an expert correcting the
                # kind on a declined draft got "already minted" and no way forward.
                "output_kind": n.output_kind,
                "feeds_from": sorted(n.feeds_from),
                "sql": n.sql,
            }
            for i, n in enumerate(request.nodes)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(slots=True)
class BlueprintMinter:
    """Draft a blueprint from an expert's submission and put it on the review queue.

    `completer` is REQUIRED and is the whole point — see the module docstring. A minter without
    one could persist an envelope, but that envelope would carry no generalization and an
    unsettled scan, which every downstream guard refuses; it would be a review row that can never
    be approved and cannot be told apart from one that merely has not been looked at yet. So the
    absence is an error at construction rather than a degraded mode.
    """

    model_client: ModelClient
    completer: ParameterizationCompleter
    known_rules: frozenset[str] = frozenset()
    catalog_columns: tuple[str, ...] = ()
    catalog_schema: dict[str, dict[str, str]] = field(default_factory=dict)
    # The cross-tier prior-art lookup, OPTIONAL. Absent means no duplicate warning — the page
    # still mints, which is the right degrade for a warning that was never a gate.
    prior_art: Any = None
    prior_art_limit: int = 5
    # NO `model` FIELD. It was stored and never read — the model is chosen at the composition
    # root, which already logs it, so a second copy here was a value that could only ever go
    # stale. `timeout_seconds` stays because `mint` applies it.
    timeout_seconds: float = 60.0
    _store: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.completer is None:  # pragma: no cover - construction guard
            raise MintUnavailableError(
                "a minter needs a completion plane: the candidate it builds is validated and "
                "routed by the same machine a completed form goes through"
            )
        self._store = self.completer.store

    @property
    def tables(self) -> tuple[str, ...]:
        """The `database.table` names an expert may select on the form.

        DERIVED from `catalog_columns` rather than listed separately, which guarantees the
        property that matters: every table the form offers has a column list behind it. A table
        offered without one is a trap — the expert picks it, the draft is ungrounded for exactly
        that table, and the rewrite rejects the columns the model guessed.
        """
        return tuple(
            sorted({c.rsplit(".", 1)[0] for c in self.catalog_columns if c.count(".") >= 2})
        )

    def _columns_for(self, request: MintRequest) -> tuple[str, ...]:
        """The catalog columns belonging to the tables THIS expert selected.

        Narrowed rather than passed whole: the full catalog is thousands of columns, and a model
        shown all of them will happily read a table the expert did not choose — which the
        column-scope check then rejects, blaming a table nobody picked.
        """
        wanted = tuple(t.strip().lower() for t in request.tables if t.strip())
        if not wanted:
            return ()
        return tuple(
            column
            for column in self.catalog_columns
            if any(column.lower().startswith(f"{table}.") for table in wanted)
        )

    async def find_prior_art(self, question: str) -> tuple[dict[str, Any], ...]:
        """Artifacts that may already answer *question* — canon blueprints AND live candidates.

        ONE search covers both because the index already spans tiers (`tier` says which) and
        already excludes `rejected`/`retired`, which is the correct population: a human declined
        those, and re-surfacing them as "this exists" would argue with that decision.

        FAILS OPEN, loudly. An index that is down must not stop an expert authoring a blueprint;
        the cost of a missed warning is a duplicate a reviewer catches later, while the cost of
        a hard failure is the page not working at all.
        """
        if self.prior_art is None or not question.strip():
            return ()
        # ⚠ THE MAPPING IS INSIDE THE TRY, and that is the whole fix. It used to sit outside,
        # so the fail-open promise covered only the SEARCH: a card whose `confidence` came back
        # None raised `TypeError: NoneType doesn't define __round__` straight out of a warning
        # path and took the whole mint with it. The guard has to cover every read this function
        # performs on a value it did not construct, not just the call that fetches them.
        try:
            cards = await self.prior_art.search(
                question, kinds=("blueprint",), limit=self.prior_art_limit
            )
            return tuple(self._card_doc(card) for card in cards)
        except Exception:  # noqa: BLE001 - a warning may never break the page
            _logger.warning("mint: prior art could not be read", exc_info=True)
            return ()

    @staticmethod
    def _card_doc(card: Any) -> dict[str, Any]:
        """One prior-art card as the page renders it. Every field defensively read."""
        confidence = getattr(card, "confidence", None)
        return {
            "id": str(getattr(card, "id", "") or ""),
            # `tier` is the actionable half: "the canon already has this" and "another expert
            # has one in review" call for different responses from the author.
            "tier": str(getattr(card, "tier", "") or ""),
            "status": str(getattr(card, "status", "") or ""),
            "intent": str(getattr(card, "intent", "") or ""),
            "verified": getattr(card, "verified", None),
            "confidence": (round(confidence, 3) if isinstance(confidence, (int, float)) else None),
        }

    async def _ask_model(
        self, request: MintRequest, *, repair_feedback: str = ""
    ) -> tuple[str, list[str], list[dict[str, Any]], str]:
        """One forced call. Returns `(intent, sql_per_node, entries, rationale)`.

        The SQL comes back as a LIST in node order even for a single blueprint, which is a
        one-element list. Uniform because everything downstream — `sql_by_ref`, the evidence
        pointers, the composes array — is per-node, and a `str | list[str]` seam would put a
        branch at every one of those sites instead of this one.
        """
        authoritative = request.sql_is_authoritative
        composite = request.is_composite
        if composite and authoritative:
            # The expert supplied every node's SQL, so there is nothing to write — only to
            # classify. Reuses the single-blueprint classify tool, which is exactly right: it
            # is the tool with NO field for SQL, and that is the guarantee `exact` mode makes
            # whether there is one query or five.
            tool, system = build_classify_tool(), CLASSIFY_SYSTEM_PROMPT
        elif composite:
            tool, system = build_composite_draft_tool(), COMPOSITE_SYSTEM_PROMPT
        elif authoritative:
            tool, system = build_classify_tool(), CLASSIFY_SYSTEM_PROMPT
        else:
            tool, system = build_draft_tool(), DRAFT_SYSTEM_PROMPT

        brief = mint_brief(
            request,
            known_rules=tuple(sorted(self.known_rules)),
            catalog_columns=self._columns_for(request),
            catalog_schema=self.catalog_schema,
        )
        if repair_feedback:
            brief += (
                "\n\n" + "=" * 72 + "\nYOUR PREVIOUS DRAFT FAILED THE CATALOG CHECK. "
                "Write a corrected complete response; do not defend the old SQL.\n  - "
                + repair_feedback
            )
        # APPLIED, not merely stored. `timeout_seconds` was a dataclass field nothing read, so
        # a deployment that configured it got the client's own default and a minting call could
        # outlive the BFF's hop budget — the browser then sees "service unreachable" while the
        # model is still answering, which is the exact failure `revise` already had.
        result = await asyncio.wait_for(
            self.model_client.send_turn(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": brief},
                ],
                [tool],
            ),
            timeout=self.timeout_seconds,
        )
        if composite and not authoritative:
            intent, _sql, entries, rationale, arguments = coerce_mint_response(
                result, expect_sql=False, allow_nodes=True
            )
            # The SAME arguments dict the intent and entries came from — see
            # `coerce_mint_response` for why re-finding the call here was a cross-wiring bug.
            nodes = coerce_node_sql(arguments.get("nodes"), expected=len(request.nodes))
            return intent, nodes, entries, rationale

        intent, sql, entries, rationale, _arguments = coerce_mint_response(
            result, expect_sql=not authoritative
        )
        if composite:
            # `exact` composite: the expert's own per-node SQL. The model was given the
            # classify tool, so it had no field to return any in.
            return intent, [n.sql.strip() for n in request.nodes], entries, rationale
        if authoritative:
            # Same for a single blueprint — `sql` came back empty BY DESIGN, because the tool
            # has no such field. The accepted SQL is the one the expert vouched for.
            return intent, [request.sql.strip()], entries, rationale
        return intent, ([sql] if sql else []), entries, rationale

    def _draft_sql_error(self, request: MintRequest, sql_per_node: list[str]) -> str:
        """Return a node-specific catalog/provenance error, before a draft is persisted."""
        if not self.catalog_schema:
            return ""
        selected = {table: self.catalog_schema.get(table, {}) for table in request.tables}
        for order, sql in enumerate(sql_per_node):
            # Scalar DAG inputs are values, not warehouse columns. Provenance only needs a
            # parseable stand-in; the real token remains untouched in the accepted SQL.
            probe = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "1", sql)
            declared_scratch = frozenset(
                request.nodes[e].output_for(e)
                for e in (request.nodes[order].feeds_from if request.is_composite else ())
                if request.nodes[e].output_kind == "table"
            )
            try:
                extract_column_provenance(probe, selected, declared_scratch=declared_scratch)
            except ProvenanceExtractionError as exc:
                label = f"step {order + 1}" if request.is_composite else "query"
                return f"{label}: {str(exc)[:500]}"
        return ""

    def _envelope(
        self,
        request: MintRequest,
        *,
        intent: str,
        sql_per_node: list[str],
        rationale: str,
    ) -> CandidateEnvelope:
        """The candidate the completer will adopt, carrying the accepted SQL to validate against.

        `parameterization` is EMPTY here on purpose. The completer is called with
        `replace_all=True`, so the entries it is handed become the whole list; seeding them here
        as well would mean the same array arrived twice by two routes, and the one that lost
        would be silent.
        """
        content_hash = mint_content_hash(request)
        bare = content_hash.removeprefix("sha256:")
        refs = [f"{MINT_TOOL_CALL_REF}{i}" for i in range(len(sql_per_node))]
        snapshot = ValidationSnapshot(
            session_id=f"{MINT_SESSION_PREFIX}{bare[:16]}",
            # No session, so no session owner. Left EMPTY rather than filled with the submitter:
            # `user_id` means "whose session produced this", and answering it with "who typed the
            # form" would make a minted row indistinguishable from a mined one in any audit that
            # groups by it.
            user_id="",
            trace_id="",
            content_hash=content_hash,
            accepted_signal=MINT_ACCEPTED_SIGNAL,
            sql_by_ref={ref: (sql,) for ref, sql in zip(refs, sql_per_node, strict=True)},
            # ONE POINTER PER NODE. D31's gate is structural — it wants citations that resolve —
            # and for a composite the honest citation set is every query the blueprint is made
            # of, not just the first. A single pointer would also make the walk's view of the
            # DAG disagree with `sql_by_ref`.
            evidence=tuple(EvidencePointer(turn_ref=0, tool_call_ref=ref) for ref in refs),
            authored=True,
        )
        return CandidateEnvelope(
            candidate_id=f"candidate::{bare}",
            type="blueprint",
            # `needs_parameterization`, NOT `extracted`, and this is load-bearing rather than
            # cosmetic. `_still_declined` keeps the status it was handed, so entering at
            # `extracted` left a REFUSED draft sitting at `extracted` with a decline block —
            # a row the inbox does not list as a form, carrying a complaint nobody would see.
            #
            # It is also the truthful description of the row in its own right: at this instant
            # the candidate has an accepted SQL and an EMPTY parameterization, which is exactly
            # what the status means. If the process dies between this put and the completer, what
            # is left behind is a form with no complaint yet — and re-submitting the identical
            # form resumes it, because the guard below admits this status.
            status=CandidateStatus.NEEDS_PARAMETERIZATION,
            payload={
                "intent": intent,
                # DERIVED from what the EXPERT declared, never from what the model returned.
                # `kind` decides which generalization path runs and therefore what shape the
                # validator demands; letting a model choose it would let a drafting turn
                # silently reclassify a one-query blueprint as a DAG, or the reverse.
                "kind": "composite" if request.is_composite else "single",
                "accepted_signal": MINT_ACCEPTED_SIGNAL,
                "parameterization": [],
                "source_tool_call_refs": list(refs),
                "result_signature": _infer_result_signature(
                    sql_per_node[-1],
                    {
                        table: getattr(self, "catalog_schema", {}).get(table, {})
                        for table in request.tables
                    },
                ),
                **({"composes": _composes(request, refs)} if request.is_composite else {}),
            },
            source_session=snapshot.session_id,
            source_trace="",
            # No quotes were snapshotted because there is no transcript to quote. D31's structural
            # gate is satisfied by the citation on the snapshot, which points at the SQL itself —
            # the only evidence a hand-authored blueprint has, and an honest account of it.
            evidence_refs=(),
            extractor_rationale=rationale,
            entity_scan={
                "result": "pending",
                "hits": [],
                # A minted blueprint has no model self-check to inherit. FALSE is the honest
                # default and it is not load-bearing: the S5 gate is authoritative and runs over
                # this envelope before it can be approved.
                "self_check_contains_entities": False,
            },
            confidence=MINT_CONFIDENCE,
            proposed_action="add",
            depends_on=(),
            content_hash=content_hash,
            revalidation=snapshot,
        )

    async def mint(self, request: MintRequest) -> MintResult:
        """Draft, validate and file one blueprint. Raises rather than half-writing.

        The order matters: the envelope is PERSISTED before the completer runs, because the
        completer's race guard re-reads the row it is about to write and refuses if the status
        moved. That guard is what makes a double-submitted form safe, and it can only work
        against a row that exists.
        """
        # BOTH CHECKS RUN BEFORE THE MODEL, and for the same reason: everything they need is
        # known from the submission alone, so paying for a drafting turn first buys nothing.
        # The 409 case is the sharper one — a double-clicked Draft button used to bill a full
        # model turn and then discard it to answer a question the content hash had already
        # settled.
        candidate_id = f"candidate::{mint_content_hash(request).removeprefix('sha256:')}"
        if await self._store.get(candidate_id) is not None:  # noqa: SIM102
            # ANY existing row, not just one that has moved on. Re-minting an identical form
            # used to be called "resuming" and was in fact a RESET: `put` replaces the whole
            # envelope, so a reviewer who had been fixing a declined draft through the
            # completion form lost their merged entries and the correction history with them.
            # There is nothing a re-mint can add — the submission is byte-identical, so the
            # model would be asked the same question — and the row is where the work is.
            raise MintConflictError(
                f"this exact submission was already minted as {candidate_id!r} — open that "
                "candidate rather than minting a second copy; editing it there keeps the work "
                "already done on it"
            )

        # SERVER-SIDE, because `mint_schema`'s guarantee — every offered table has columns
        # behind it — was enforced only in the browser. A crafted or stale POST naming an
        # unknown table falls into the brief's "you are working unverified" branch and frees the
        # model to invent columns, producing precisely the validator complaint about a table
        # nobody chose that the grounding exists to prevent.
        if self.catalog_columns:
            offered = set(self.tables)
            unknown = sorted(t for t in request.tables if t not in offered)
            if unknown:
                raise MintInputError(f"these tables are not available here: {', '.join(unknown)}")

        already_exists = await self.find_prior_art(request.question)

        try:
            intent, sql_per_node, entries, rationale = await self._ask_model(request)
        except TimeoutError as exc:
            # A SENTENCE, not an unhandled 500. `asyncio.wait_for` raises this and no route
            # caught it, so a slow model produced a blank error page. The reviser answers the
            # same situation with "the assistant timed out; try again", and an expert who has
            # just typed a page of prose needs at least that much.
            raise MintUnavailableError(
                f"the assistant did not answer within {self.timeout_seconds:.0f}s — nothing "
                "was written, so re-submitting the same form is safe"
            ) from exc
        if not sql_per_node or not all(sql_per_node):
            raise MintResponseError("no accepted SQL could be established for this blueprint")
        if request.sql_is_authoritative:
            _check_nodes(request, sql_per_node)
            complaint = self._draft_sql_error(request, sql_per_node)
            if complaint:
                raise MintInputError(
                    "the submitted SQL does not resolve against the selected catalog schema "
                    f"({complaint}); nothing was written"
                )
        else:
            # DAG/token contract failures are already precise and may depend on structure the
            # model is forbidden to change. Catalog provenance failures are the repairable
            # class: the model can choose a real selected column or table instead.
            _check_nodes(request, sql_per_node)
            complaint = self._draft_sql_error(request, sql_per_node)
            if complaint:
                try:
                    intent, sql_per_node, entries, rationale = await self._ask_model(
                        request, repair_feedback=complaint
                    )
                except TimeoutError as exc:
                    raise MintUnavailableError(
                        f"the assistant did not correct its invalid SQL within "
                        f"{self.timeout_seconds:.0f}s — nothing was written, so re-submitting "
                        "the same form is safe"
                    ) from exc
                if not sql_per_node or not all(sql_per_node):
                    raise MintResponseError(
                        "the corrected draft carried no SQL; nothing was written"
                    )
                try:
                    _check_nodes(request, sql_per_node)
                except MintResponseError as exc:
                    raise MintResponseError(
                        f"the corrected draft is still invalid ({exc}); nothing was written"
                    ) from None
                complaint = self._draft_sql_error(request, sql_per_node)
                if complaint:
                    raise MintResponseError(
                        "the corrected draft still uses SQL outside the selected catalog "
                        f"schema ({complaint}); nothing was written"
                    )
        if not entries:
            _logger.info(
                "mint: the model proposed no parameterization entries; the totality walk will "
                "decline unless the query has no literal predicates at all"
            )

        env = self._envelope(request, intent=intent, sql_per_node=sql_per_node, rationale=rationale)
        # RE-CHECKED immediately before the write. The first check happened before a model call
        # that takes tens of seconds, and the completer one line below narrows exactly this
        # window for its own write — an unguarded `put` here could overwrite a row created (or
        # rejected) while the draft was in flight, which is the D29 resurrection the completer's
        # guard exists to prevent.
        if await self._store.get(env.candidate_id) is not None:
            raise MintConflictError(
                f"{env.candidate_id!r} was created while this draft was being written — open "
                "it rather than overwriting it"
            )
        await self._store.put(env)

        try:
            outcome = await self.completer.complete(env, entries=entries, replace_all=True)
        except CompletionUnavailableError as exc:  # pragma: no cover - wiring guard
            raise MintUnavailableError(str(exc)) from exc
        return self._result(
            outcome,
            accepted_sql="\n\n".join(sql_per_node),
            rationale=rationale,
            entries=entries,
            prior_art=already_exists,
        )

    @staticmethod
    def _result(
        outcome: CompletionResult,
        *,
        accepted_sql: str,
        rationale: str,
        entries: list[dict[str, Any]],
        prior_art: tuple[dict[str, Any], ...] = (),
    ) -> MintResult:
        env = outcome.envelope
        decline = outcome.decline
        warnings: list[str] = []
        if prior_art:
            warnings.append(
                f"{len(prior_art)} existing artifact(s) may already answer this — review them "
                "before approving, and reject this draft if one of them does"
            )
        if outcome.outcome != "completed":
            warnings.append(
                "the draft did not validate — the candidate is on the queue carrying the "
                "validator's complaint, and the assistant can be asked to fix it there"
            )
        return MintResult(
            candidate_id=env.candidate_id,
            outcome=outcome.outcome,
            status=str(env.status),
            accepted_sql=accepted_sql,
            intent=str(env.payload.get("intent") or ""),
            decline_reason=str(getattr(decline, "reason", "") or ""),
            decline_detail=str(getattr(decline, "detail", "") or ""),
            warnings=tuple(warnings),
            rationale=rationale,
            entries=tuple(entries),
            prior_art=prior_art,
        )


__all__ = ["MINT_SESSION_PREFIX", "BlueprintMinter", "mint_content_hash"]
