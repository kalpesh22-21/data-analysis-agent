"""What an expert submits at the minting page, and what comes back.

THE REQUEST IS NOT A BLUEPRINT. It is the raw material an expert has that the learning loop
normally has to infer from a session trace: the question, how to answer it, what to assume, which
tables to read, and — sometimes — the exact SQL. Turning that into a candidate is
`engine.BlueprintMinter`'s job, and everything after it is the review queue that already exists.

WHY `steps` AND `assumptions` ARE NOT PERSISTED AS THEMSELVES. They exist to decide the SHAPE of
the blueprint — for a composite, which nodes the DAG has and how they depend on each other. That
work happens at DRAFT time and is finished by the time a candidate exists. What survives is folded
into `intent`, which is the field the whole downstream corpus already reads: dedup embeds it,
retrieval matches on it, and the reviewer card leads with it. A parallel `steps` array would be a
second description of the same thing that nothing reads and nothing keeps true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...runtime.blueprint.models import NODE_OUTPUT_KINDS

# An expert writing a blueprint by hand is writing ONE query's worth of intent. These caps are far
# above anything legitimate and exist so a pasted document cannot become a candidate payload.
MAX_QUESTION_CHARS = 2_000
MAX_STEP_CHARS = 500
MAX_STEPS = 24
MAX_SQL_CHARS = 20_000
MAX_TABLES = 32
# ⚠ DERIVED FROM `check_dag`, never restated. The form used to cap steps at MAX_STEPS (24) while
# the DAG validator caps a graph at 16, so a 17-step submission was accepted, paid for a drafting
# turn, was persisted, and only then declined `dag_invalid` — a guaranteed-dead review row and a
# wasted model call, for a limit that was knowable from the form alone. Importing the constant
# means the two can never drift again.
try:  # pragma: no cover - import-shape guard
    from ..generalize.validate import _MAX_NODES as MAX_NODES
except ImportError:  # pragma: no cover
    MAX_NODES = 16
# Mirrors `SCALAR_CONSUME_REF`'s name half — an output name becomes `$0.<name>`, so a name that
# does not match the grammar produces a ref that resolves nowhere and a DAG that fails to load.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MintInputError(ValueError):
    """The submitted form cannot be drafted from. Answers 400 — the expert can fix it."""


class MintConflictError(MintInputError):
    """This submission already has a candidate. Answers 409, not 400.

    A SUBCLASS of `MintInputError` rather than a sibling, because every existing `except
    MintInputError` should still catch it — what it adds is the ability to tell "the form is
    wrong, fix it" (400) from "the form is fine, but its row already exists" (409). Sharing one
    type made an unknown-table submission answer 409, telling an expert their typo was a
    conflict with a candidate that does not exist.
    """


class MintUnavailableError(RuntimeError):
    """The minting plane is not wired (no model, no catalog, no completer). Answers 503."""


@dataclass(frozen=True, slots=True)
class MintNode:
    """One step of a composite blueprint, DECLARED BY THE EXPERT.

    The structure is explicit rather than inferred, and that is a decision about where the
    unvalidated judgement sits. A model asked to decompose prose into a DAG invents the
    dependency edges — which node feeds which — and nothing downstream can check that it got
    them right: `check_dag` proves the graph is well-formed and acyclic, never that it matches
    what the expert meant. An expert who declares "step 3 needs the total from step 1" has
    stated a fact; a model that guesses it has produced a plausible one. So the expert owns the
    shape and the model owns the SQL.

    `consumes` is DERIVED from `feeds_from` rather than typed: the ref grammar (`$0.name`) is
    mechanical, and asking a human to write it correctly buys nothing but typos.
    """

    step_intent: str
    output_name: str = ""
    # WHICH KIND of result flows downstream. `scalar` is one value, referenced by a `{name}`
    # token. `table` is the whole result set, materialized as `scratch.<name>` and referenced as
    # a FROM/JOIN source — which is the primitive for "many values flow from one step to the
    # next", rather than declaring a dozen scalars. Both are `NODE_OUTPUT_KINDS`; the executor
    # and the corpus loader already support each.
    output_kind: str = "scalar"
    sql: str = ""
    # Orders of the EARLIER steps this one needs. Backward edges only, which is what makes the
    # DAG acyclic by construction rather than by check.
    feeds_from: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.step_intent.strip():
            raise MintInputError("every step needs a description of what it answers")
        if len(self.step_intent) > MAX_STEP_CHARS:
            raise MintInputError(f"a step is longer than {MAX_STEP_CHARS} characters")
        if self.output_kind not in NODE_OUTPUT_KINDS:
            raise MintInputError(
                f"a step's result kind must be one of {sorted(NODE_OUTPUT_KINDS)}"
            )
        if len(self.sql) > MAX_SQL_CHARS:
            # THE SAME CAP THE TOP-LEVEL QUERY HAS. Expert-supplied node SQL is used verbatim as
            # accepted SQL, so without this an `exact` composite could carry sixteen unbounded
            # queries past a limit whose whole purpose is that a pasted document cannot become a
            # candidate payload. (Model-written node SQL is already capped in `coerce_node_sql`.)
            raise MintInputError(f"a step's SQL is longer than {MAX_SQL_CHARS} characters")
        if self.output_name and not _IDENTIFIER.match(self.output_name):
            raise MintInputError(
                f"{self.output_name!r} is not a usable output name — letters, digits and "
                "underscores only, not starting with a digit (it becomes a `$n.name` "
                "reference the executor resolves)"
            )

    def output_for(self, order: int) -> str:
        """This node's output name, defaulted from its position when none was given."""
        return self.output_name or f"step_{order}"


@dataclass(frozen=True, slots=True)
class MintRequest:
    """One expert's submission.

    `sql` carries whatever they had. `sql_mode` says how much to trust it, and that distinction is
    the only thing separating two genuinely different provenance stories:

      "exact"  — this SQL RAN and they vouch for it. It is used VERBATIM as the accepted SQL, and
                 the model is never given a field to rewrite it in. Same guarantee the §C reviser
                 makes, for the same reason: the template is DERIVED from the accepted SQL by AST
                 rewrite, so a model that edits the SQL silently changes what `explain_ok` and
                 `binds_to_subset_uses` are checking.
      "pseudo" — a sketch. The model writes real SQL FROM it, and the expert reviews what it wrote
                 before anything is promoted.
      "none"   — steps only; the model writes the SQL from the steps and the table list.
    """

    question: str
    tables: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    sql: str = ""
    sql_mode: str = "none"
    # NON-EMPTY makes this a COMPOSITE. Kept as a separate field from `steps` rather than
    # promoting every step to a node: a step is prose that shapes ONE query, and a node is a
    # query in its own right with an output other nodes read. Conflating them would silently
    # turn every multi-step single blueprint into a DAG.
    nodes: tuple[MintNode, ...] = ()

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise MintInputError("a question is required — it becomes the blueprint's intent")
        if len(self.question) > MAX_QUESTION_CHARS:
            raise MintInputError(
                f"the question is longer than {MAX_QUESTION_CHARS} characters"
            )
        if self.sql_mode not in ("exact", "pseudo", "none"):
            raise MintInputError("sql_mode must be one of: exact, pseudo, none")
        if self.sql_mode == "exact" and not self.sql.strip() and not self.nodes:
            raise MintInputError(
                "sql_mode='exact' promises a query that ran, but no SQL was submitted"
            )
        if self.sql_mode == "exact" and self.nodes and any(
            not node.sql.strip() for node in self.nodes
        ):
            raise MintInputError(
                "sql_mode='exact' promises queries that ran, so every step needs its own SQL"
            )
        if self.nodes and self.sql.strip():
            # REFUSED rather than ignored. A composite's queries live on its STEPS, so a
            # top-level query is never read — it was shown to the model in the brief and then
            # silently discarded. For a submission that says these queries RAN, quietly
            # dropping one the expert vouched for is the worst available outcome.
            raise MintInputError(
                "this submission has both per-step SQL and a whole-query SQL. A multi-step "
                "blueprint is built from its steps, so the whole-query box must be empty — "
                "move that query into the step it belongs to"
            )
        if len(self.nodes) > MAX_NODES:
            raise MintInputError(
                f"a composite blueprint can have at most {MAX_NODES} steps; this one has "
                f"{len(self.nodes)}"
            )
        for index, node in enumerate(self.nodes):
            for edge in node.feeds_from:
                # BACKWARD-ONLY, checked here so the error names the step the expert can see.
                # `check_dag` would also reject a forward edge, but much later and as
                # `dag_invalid`, which says nothing about which step is wrong.
                if not isinstance(edge, int) or edge < 0 or edge >= index:
                    raise MintInputError(
                        f"step {index + 1} says it needs step {edge + 1}, which is not an "
                        "earlier step — a step can only use results from ones before it"
                    )
        names = [n.output_for(i) for i, n in enumerate(self.nodes)]
        if len(set(names)) != len(names):
            raise MintInputError("two steps share an output name; each must be unique")
        if len(self.sql) > MAX_SQL_CHARS:
            raise MintInputError(f"the SQL is longer than {MAX_SQL_CHARS} characters")
        if not self.tables:
            raise MintInputError(
                "select at least one table — the draft is grounded on the columns they have, "
                "and an ungrounded draft invents columns the validator then rejects"
            )
        if len(self.tables) > MAX_TABLES:
            raise MintInputError(f"more than {MAX_TABLES} tables were selected")
        for label, items in (("steps", self.steps), ("assumptions", self.assumptions)):
            if len(items) > MAX_STEPS:
                raise MintInputError(f"more than {MAX_STEPS} {label} were submitted")
            if any(len(item) > MAX_STEP_CHARS for item in items):
                raise MintInputError(
                    f"one of the {label} is longer than {MAX_STEP_CHARS} characters"
                )

    @property
    def is_composite(self) -> bool:
        """Whether this submission describes a DAG rather than one query."""
        return bool(self.nodes)

    @property
    def sql_is_authoritative(self) -> bool:
        """Whether the submitted SQL is used verbatim rather than as a sketch."""
        return self.sql_mode == "exact"

    @classmethod
    def from_doc(cls, doc: Any) -> MintRequest:
        """Build from a JSON body. EVERY field is coerced — this comes from a browser."""
        if not isinstance(doc, dict):
            raise MintInputError("the request body must be a JSON object")

        def _strings(key: str) -> tuple[str, ...]:
            raw = doc.get(key)
            if raw is None:
                return ()
            if not isinstance(raw, list):
                raise MintInputError(f"{key!r} must be an array of strings")
            out = []
            for item in raw:
                if not isinstance(item, str):
                    raise MintInputError(f"every entry of {key!r} must be a string")
                if item.strip():
                    out.append(item.strip())
            return tuple(out)

        def _text(key: str) -> str:
            raw = doc.get(key, "")
            if raw is None:
                return ""
            if not isinstance(raw, str):
                raise MintInputError(f"{key!r} must be a string")
            return raw

        nodes = []
        raw_nodes = doc.get("nodes") or []
        if not isinstance(raw_nodes, list):
            raise MintInputError("'nodes' must be an array of steps")
        for item in raw_nodes:
            if not isinstance(item, dict):
                raise MintInputError("every entry of 'nodes' must be an object")
            raw_edges = item.get("feeds_from") or []
            if not isinstance(raw_edges, list):
                raise MintInputError("a step's 'feeds_from' must be an array of step numbers")
            edges = []
            for edge in raw_edges:
                if isinstance(edge, bool) or not isinstance(edge, int):
                    raise MintInputError("'feeds_from' entries must be whole step numbers")
                edges.append(edge)
            nodes.append(
                MintNode(
                    step_intent=str(item.get("step_intent") or "").strip(),
                    output_name=str(item.get("output_name") or "").strip(),
                    output_kind=(str(item.get("output_kind") or "scalar").strip() or "scalar"),
                    sql=str(item.get("sql") or "").strip(),
                    feeds_from=tuple(edges),
                )
            )

        return cls(
            nodes=tuple(nodes),
            question=_text("question").strip(),
            tables=_strings("tables"),
            steps=_strings("steps"),
            assumptions=_strings("assumptions"),
            sql=_text("sql").strip(),
            sql_mode=(_text("sql_mode") or "none").strip(),
        )


@dataclass(frozen=True, slots=True)
class MintResult:
    """The outcome of one submission.

    `outcome` mirrors the completer's vocabulary deliberately — a minted blueprint takes the same
    path a completed form does, so "completed" and "declined" mean here exactly what they mean
    there, and the card the expert lands on is the same card.
    """

    candidate_id: str
    outcome: str  # "completed" | "declined"
    status: str
    accepted_sql: str
    intent: str
    decline_reason: str = ""
    decline_detail: str = ""
    warnings: tuple[str, ...] = ()
    rationale: str = ""
    entries: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # Existing artifacts that may already answer this question. A WARNING, never a block: a
    # near-duplicate intent with genuinely different SQL is a real and legitimate case (the
    # same question at a different grain, or over a different date basis), and hard-refusing
    # would leave the expert no route at all. Surfaced so the decision is theirs and informed.
    prior_art: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_doc(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "outcome": self.outcome,
            "status": self.status,
            "accepted_sql": self.accepted_sql,
            "intent": self.intent,
            "decline_reason": self.decline_reason,
            "decline_detail": self.decline_detail,
            "warnings": list(self.warnings),
            "rationale": self.rationale,
            "entries": [dict(e) for e in self.entries],
            "prior_art": [dict(c) for c in self.prior_art],
        }


__all__ = [
    "MAX_QUESTION_CHARS",
    "MintConflictError",
    "MAX_SQL_CHARS",
    "MintNode",
    "MintInputError",
    "MintRequest",
    "MintResult",
    "MintUnavailableError",
]
