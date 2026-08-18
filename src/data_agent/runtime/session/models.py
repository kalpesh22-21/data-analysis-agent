"""Session document dataclasses — the Couchbase doc shape (design §6).

These dataclasses are the in-process representation of the session document;
`to_doc()`/`from_doc()` (de)serialize to the plain-dict shape that is actually
written to/read from Couchbase (or the in-memory fake store). Keeping the
dataclass and the wire-shape separate lets the rest of the runtime work with
typed objects (e.g. `entry.provenance` is a real `frozenset[tuple[str, str]]`,
not a list-of-lists) while the store implementations only ever persist plain
JSON-compatible dicts.

Provenance representation (load-bearing, D44/D63):
    `TrailEntry.provenance` is `frozenset[tuple[str, str]] | None`.
      - `None`  == "undetermined" (provenance could not be computed — e.g. the
        runtime's own independent re-parse of a `runQuery` SQL string failed,
        or a `sampleRows` table was uncatalogued). Per D44
        fail-closed, an undetermined entry is ALWAYS dropped from replay by
        `context/scope_filter.py`, regardless of how open `column_scope` is —
        "never assume in-scope" is read literally here.
      - `frozenset()` (empty, non-None) == "no columns referenced" — the
        tool genuinely exposes no column-level data (`listDatabases`,
        `listTables`, `explainQuery`, or `getTableSchema` — the last returns
        MCP-scope-filtered column METADATA, no cell values, so a fetched schema
        is always replayable) or the SQL genuinely referenced zero catalog
        columns (e.g. `SELECT 1`). An empty-but-determined provenance set is
        trivially a subset of any scope and is always kept.
    On the wire (`to_doc`/`from_doc`), `None` round-trips as JSON `null`;
    `frozenset()` round-trips as `[]`; a non-empty set round-trips as the
    `[["db.table", "column"], ...]` pair-list shown in design §6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ResultPreview:
    """`{columns, row_count, truncated, preview_rows}` — built once at write-time."""

    columns: list[str]
    row_count: int
    truncated: bool
    preview_rows: list[list[Any]]

    def to_doc(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "row_count": self.row_count,
            "truncated": self.truncated,
            "preview_rows": [list(row) for row in self.preview_rows],
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> ResultPreview:
        return cls(
            columns=list(doc["columns"]),
            row_count=int(doc["row_count"]),
            truncated=bool(doc["truncated"]),
            preview_rows=[list(row) for row in doc["preview_rows"]],
        )


@dataclass(frozen=True)
class TurnMessage:
    """A persisted user/assistant message (D22 — thinking is discarded).

    `provenance` (2026-07-01 D44 clarification, load-bearing): the SAME
    replay scope-filter that gates `TrailEntry` also gates conversational
    ASSISTANT messages, so a prior turn's free-text answer ("Jane Doe's
    salary is $85,000...") is never replayed once the user's `column_scope`
    narrows past what that answer was derived from.
      - `role == "user"` messages carry the user's own input, never
        warehouse-derived data — always `frozenset()` (determined-empty,
        i.e. "no restriction"), never `None`, and never dropped by the
        replay filter (`context/scope_filter.py::is_message_in_scope`).
      - `role == "assistant"` messages are tagged, at write time
        (`loop/agent_loop.py`), with the UNION of the column-provenance of
        every `TrailEntry` produced in that message's `turn_index`. If ANY
        of those tool results had undetermined provenance (`None`), the
        assistant message's provenance is `None` too (fail-closed — matches
        `TrailEntry`'s own "never assume in-scope" rule). A turn with no
        tool calls at all (a pure clarification/chat turn) yields
        `frozenset()` (determined-empty) — always kept.
    Wire encoding is identical to `TrailEntry.provenance` (`_provenance_to_doc`/
    `_provenance_from_doc` below): `None` -> JSON `null`, `frozenset()` -> `[]`,
    a non-empty set -> `[["db.table", "column"], ...]`.
    """

    turn_index: int
    role: str  # "user" | "assistant"
    content: str
    ts: str  # ISO-8601
    provenance: frozenset[tuple[str, str]] | None = field(default_factory=frozenset)

    def to_doc(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "provenance": _provenance_to_doc(self.provenance),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> TurnMessage:
        return cls(
            turn_index=int(doc["turn_index"]),
            role=doc["role"],
            content=doc["content"],
            ts=doc["ts"],
            provenance=_provenance_from_doc(doc.get("provenance")),
        )


def _provenance_to_doc(provenance: frozenset[tuple[str, str]] | None) -> list[list[str]] | None:
    if provenance is None:
        return None
    return [[db_table, column] for db_table, column in sorted(provenance)]


def _provenance_from_doc(doc: list[list[str]] | None) -> frozenset[tuple[str, str]] | None:
    if doc is None:
        return None
    return frozenset((pair[0], pair[1]) for pair in doc)


def _answer_table_provenance_item(item: Any) -> frozenset[tuple[str, str]] | None:
    """One table position's USES set, or `None` when this position's payload is not
    a well-formed list of `[db_table, column]` pairs.

    THE TOTALITY HAS TO REACH THE PAIRS, not just the outer list. The outer
    `isinstance` check below was already there and reads as if it covers the field,
    but the conversion it guarded indexed `pair[0]`/`pair[1]` blind: a document
    holding `[[["a"]]]` — one table, one pair, one element — raised `IndexError`
    out of `TrailEntry.from_doc`, out of `SessionDoc.from_doc`, and out of EVERY
    subsequent read of that session. One malformed inner pair bricked the whole
    conversation, which is precisely the outcome the docstring promised could not
    happen.

    `None` for the position is the right degrade, and it is not a loophole: this
    field is an additive optimisation that lets a scope narrowing on reload drop
    the out-of-scope tables individually instead of all of them, and `None` means
    "no per-table lineage here", which the reader already handles as the legacy
    case. The cost of a malformed entry stays inside its own table position.
    """
    if not isinstance(item, list):
        return None
    if not all(isinstance(pair, list | tuple) and len(pair) == 2 for pair in item):
        return None
    return frozenset((pair[0], pair[1]) for pair in item)


def _answer_table_provenance_from_doc(
    doc: Any,
) -> tuple[frozenset[tuple[str, str]] | None, ...] | None:
    """Load `TrailEntry.answer_table_provenance` (08 §D.2).

    Total on any shape: a legacy document has no such key (`None`), anything that is
    not a list is treated the same way, and a malformed table POSITION degrades to
    `None` on its own (`_answer_table_provenance_item`) rather than raising into a
    store read — the field is an additive optimisation of the read path, so a
    malformed one must cost the per-table filter, never the session.

    Its sibling `_provenance_from_doc` is deliberately left strict. `TrailEntry.
    provenance` is not an optimisation: it is what `scope_filter` reads to decide
    whether an entry may be shown at all, `None` there means UNDETERMINED and is
    treated fail-closed, and silently manufacturing that value from a malformed
    payload would turn a corrupt document into a quiet scope decision. Loud is
    correct there; total is correct here.
    """
    if not isinstance(doc, list):
        return None
    return tuple(_answer_table_provenance_item(item) for item in doc)


@dataclass(frozen=True)
class TrailEntry:
    """One tool-call record in `tool_trail` (design §6)."""

    turn_index: int
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    status: str  # "ok" | "denied" | "error"
    error_code: str | None
    provenance: frozenset[tuple[str, str]] | None
    result_preview: ResultPreview | None
    result_full_ref: str | None
    ts: str
    # True ONLY for a SUCCESSFUL, D56-verified `runBlueprint` result — persisted so
    # a replay/resume re-derives the same "authoritative" marker in the model's tool
    # message (D45 determinism). Defaults False for every other entry (runQuery,
    # denials, discovery), so the field is additive and legacy docs load unchanged.
    authoritative: bool = False
    # `denial_detail` (additive, 2026-08): the SPECIFIC model-actionable reason for
    # a non-`ok` entry — the out-of-scope column name, the blueprint that was never
    # run. `None` (the default, and every ordinary denial) means the canned
    # `denial_mapping.py` string derived from `error_code` is the whole story, so
    # legacy documents load byte-identically.
    #
    # It is the ONLY channel by which a specific denial reason reaches the model.
    # `ToolResult.user_message` does not: there is deliberately no field for it here,
    # and `context/budget.py::_render_entry` regenerates the message from
    # `error_code` alone. Anything not persisted here is invisible to the model.
    #
    # Persisting it is scope-safe: a denial carries `provenance=None`, and
    # `scope_filter.filter_trail` exempts a non-`ok` entry only for the CURRENT
    # turn, so a prior-turn denial never replays at all. The detail can only render
    # inside the turn whose scope produced it.
    denial_detail: str | None = None
    # `serves_intent` (additive, Release 1 call-time intent tagging): the
    # `intent_id` of the tracked intent this call was made FOR, as the model named
    # it at the moment it dispatched the work. `None` (the default) for every
    # untagged call, every non-taggable tool, and every document written before the
    # field existed — so legacy docs load byte-identically.
    #
    # It is the PRIMARY way an intent is completed (04, as amended): the model tags
    # the call, then closes the intent with `{intent_id, status}` alone and the
    # runtime resolves the evidence from this field. Requiring it to cite an opaque
    # 24-char `tool_call_id` after the fact failed 9 attempts out of 9 against a
    # live model — it named the blueprint id, the tool NAME, a hallucinated id, and
    # finally `""`, while the real ids sat in its context the whole time.
    #
    # The value is ALWAYS a runtime-assigned id (`i1`, `i2`, …): the loop validates
    # the tag against the live `AnalysisState` before persisting it and drops
    # anything else (see `composite/analysis_state.py::split_serves_intent`), so no
    # model-authored free text can land here and D25-safe telemetry may carry it.
    serves_intent: str | None = None
    # `answer_table_provenance` (additive, 08 §D.2): the D44 USES set of EACH query
    # a successful `answerWithTable` designated, positionally parallel to the
    # tables `resolve_designations` reads back out of `args`. `None` (the default)
    # for every entry of every other tool and for every document written before the
    # field existed — the third time this additive-with-`None` pattern is used
    # (`authoritative`, `denial_detail`, `serves_intent`), so legacy docs load
    # byte-identically.
    #
    # IT IS NOT THIS ENTRY'S PROVENANCE, and the distinction is load-bearing.
    # `provenance` stays `frozenset()` (determined-empty: the call reads no
    # warehouse data), and this field must NEVER be folded into
    # `_compute_turn_provenance_union` — that union is fail-closed, so a single
    # unparseable designated query would collapse it to `None` and drop the turn's
    # whole answer from every later replay. It exists so a scope narrowing can drop
    # the out-of-scope TABLES instead of the whole answer, read through
    # `answer_with_table.is_answer_table_in_scope` by the live path and by
    # `session_history.project_history` alike.
    answer_table_provenance: tuple[frozenset[tuple[str, str]] | None, ...] | None = None
    # `window_note` (additive, J7): the honesty note for a DATA-anchored blueprint window
    # ("it counts back from the latest data on record, not from today's date"). `None`
    # (the default) for every other entry and every document written before the field
    # existed — the fourth use of this additive-with-`None` pattern (`authoritative`,
    # `denial_detail`, `serves_intent`), so legacy docs load byte-identically.
    #
    # PERSISTED, not re-derived at render: the note is a fact about the blueprint that
    # RAN, and `_render_entry` cannot see the corpus. Persisting it is what makes a D45
    # replay re-emit the identical tool message — the same argument that put
    # `authoritative` on this dataclass. It is scope-inert: a closed-vocabulary sentence
    # about a window anchor, carrying no column identifier and no warehouse value.
    window_note: str | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "args": dict(self.args),
            "status": self.status,
            "error_code": self.error_code,
            "provenance": _provenance_to_doc(self.provenance),
            "result_preview": self.result_preview.to_doc() if self.result_preview else None,
            "result_full_ref": self.result_full_ref,
            "ts": self.ts,
            "authoritative": self.authoritative,
            "denial_detail": self.denial_detail,
            "serves_intent": self.serves_intent,
            "answer_table_provenance": (
                None
                if self.answer_table_provenance is None
                else [_provenance_to_doc(item) for item in self.answer_table_provenance]
            ),
            "window_note": self.window_note,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> TrailEntry:
        preview_doc = doc.get("result_preview")
        return cls(
            turn_index=int(doc["turn_index"]),
            tool_call_id=doc["tool_call_id"],
            tool_name=doc["tool_name"],
            args=dict(doc["args"]),
            status=doc["status"],
            error_code=doc.get("error_code"),
            provenance=_provenance_from_doc(doc.get("provenance")),
            result_preview=ResultPreview.from_doc(preview_doc) if preview_doc else None,
            result_full_ref=doc.get("result_full_ref"),
            ts=doc["ts"],
            authoritative=bool(doc.get("authoritative", False)),
            # `.get` (not `[...]`): a document written before this field existed
            # loads with `None` and renders exactly as it always did.
            denial_detail=doc.get("denial_detail"),
            serves_intent=doc.get("serves_intent"),
            answer_table_provenance=_answer_table_provenance_from_doc(
                doc.get("answer_table_provenance")
            ),
            # `.get` + a string test: a pre-J7 document has no key (`None`), and a
            # non-string one loads as `None` rather than as a value the renderer would
            # print at the model verbatim.
            window_note=(
                doc.get("window_note") if isinstance(doc.get("window_note"), str) else None
            ),
        )


# --- analysisState (Release 1, docs/decisions/release-1/03-analysis-state.md) ---
#
# The closed enums live HERE, beside the dataclasses, so the tool, the evidence
# validators, the loop's forced-block paths and the tests all share ONE
# definition. The MODEL/RUNTIME split is STRUCTURAL, not a comment (03 §A.3):
# `validate_block_evidence` allowlists `MODEL_REASON_CODES`, so a runtime-forced
# code can never become model-declarable — including a future fourth one.
INTENT_STATUSES = frozenset({"pending", "completed", "blocked"})
# Declarable BY THE MODEL, on evidence (04 §B.1). Exactly two: both are the
# non-retryable denials. `DATABASE_NOT_ALLOWED`/`TABLE_NOT_FOUND` were dropped on
# review — they are `retryable=True` in `denial_mapping.py` (the codebase's own
# "the model got the name wrong" bucket) and `TABLE_NOT_FOUND` is also how a
# column-scope denial surfaces from `sampleRows`.
MODEL_REASON_CODES = frozenset({"NO_ACCESS", "REQUIRED_DATA_UNAVAILABLE"})
# Written ONLY by the runtime's forced-block paths (05 §F), never by the model.
# `ENFORCEMENT_EXHAUSTED` means "enforcement could not establish a disposition" —
# NOT that the system proved the intent impossible.
RUNTIME_REASON_CODES = frozenset({"BUDGET_EXHAUSTED", "USER_STOPPED", "ENFORCEMENT_EXHAUSTED"})
REASON_CODES = MODEL_REASON_CODES | RUNTIME_REASON_CODES

# ONE forced finalization re-round per (TURN, BUDGET WINDOW) (05 §C). It lives here,
# beside the field it bounds (`SessionDoc.finalization_blocks`) and the enums it sits
# with, because BOTH store implementations enforce it inside their claim and the loop
# reads it for its own accounting — a constant in `agent_loop.py` could not be
# imported by the stores without a cycle (`agent_loop` imports `store`).
MAX_FINALIZATION_BLOCKS_PER_WINDOW = 1

# WHAT a forced re-round was claimed FOR (05 §C.1, §J.3). Two independent gates
# refuse a finish, and as of 2026-08-12 they hold SEPARATE per-window allowances:
#
#   `intents`      — pending intents at either terminal exit (05 §B).
#   `answer_shape` — a bare-text finish holding untabled multi-row results (05 §J).
#
# They shared ONE allowance for exactly one release, and live measurement killed it:
# on three-part questions the intents nudge consumed the window's only grant in 2 of
# 4 runs, leaving the shape gate able to emit `loop_answer_shape_exhausted` and
# nothing else — starved on precisely the multi-deliverable question it was built
# for. A closed enum rather than a free string because it becomes part of a
# PERSISTED key: an unrecognised value would mint a brand-new, unbounded allowance
# and no test would see it.
FinalizationBlockKind = Literal["intents", "answer_shape"]
FINALIZATION_BLOCK_KINDS: tuple[FinalizationBlockKind, ...] = ("intents", "answer_shape")


def finalization_block_key(
    turn_index: int, window_count: int, kind: FinalizationBlockKind
) -> str:
    """The `SessionDoc.finalization_blocks` key — `"0:1:intents"` for turn 0,
    window 1, the pending-intents allowance.

    THE TURN INDEX IS LOAD-BEARING, and 05 §C.1's original "keyed by window"
    was a defect. `finalization_blocks` is persisted on the session document and
    never cleared at a turn boundary, but `AgentLoop.run` starts EVERY external
    turn at `window_count=1` — so a window-only key collides across turns, and from
    a session's SECOND block-spending turn onward the first finalization attempt of
    every turn is refused a re-round it never had. No `loop_finalization_refused`,
    no nudge, and `ENFORCEMENT_EXHAUSTED` written for an intent the model was never
    asked twice about (which also silently inflates 07's headline metric).

    Same class as the bug `live_analysis_state` exists to prevent: a PER-TURN value
    persisted on the session doc with no turn gate. Keying beats clearing — a clear
    needs a turn-boundary hook that does not exist and would have to fire on every
    resume path without resetting the counter mid-turn.

    THE KIND IS THE SAME ARGUMENT ONE LEVEL DOWN (05 §J.3). Two gates that share a
    key share an allowance, and sharing measured badly — so the kind joins the key
    rather than the gates queueing for one counter. Every kind is spelled into the
    key, including `intents`: an unsuffixed key would read as "some allowance" in a
    map that now holds several, and the migration cost is zero because nothing is in
    production.

    `kind` is VALIDATED here rather than trusted. This is the single point where a
    persisted allowance key is minted, so an unrecognised kind — a typo at a call
    site, a stale caller after a rename — must not quietly create an eighth
    unbounded budget. Raising is safe for the runtime: `_grant_forced_reround`
    treats any exception from the claim as "no re-round available" and finalizes.

    Defined here so both store implementations and every test format it one way.
    """
    if kind not in FINALIZATION_BLOCK_KINDS:
        raise ValueError(
            f"unknown finalization block kind {kind!r}; expected one of "
            f"{FINALIZATION_BLOCK_KINDS}"
        )
    return f"{turn_index}:{window_count}:{kind}"


@dataclass(frozen=True)
class TrackedIntent:
    """One tracked deliverable of a multi-intent question (03 §A).

    `intent_id` is RUNTIME-assigned (`i1`, `i2`, … in proposal order) and never
    model-supplied; `description` is FROZEN after initialization — an update may
    only move `status`/`evidence_tool_call_id`/`reason_code`. Both rules exist so
    the model cannot silently DROP an ask it decided not to answer (03 §C.4);
    neither closes manufactured evidence, which is a known-open hole.
    """

    intent_id: str
    description: str
    status: str  # INTENT_STATUSES
    evidence_tool_call_id: str | None = None
    reason_code: str | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "description": self.description,
            "status": self.status,
            "evidence_tool_call_id": self.evidence_tool_call_id,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> TrackedIntent:
        return cls(
            intent_id=doc["intent_id"],
            description=doc["description"],
            status=doc["status"],
            evidence_tool_call_id=doc.get("evidence_tool_call_id"),
            reason_code=doc.get("reason_code"),
        )


@dataclass(frozen=True)
class AnalysisState:
    """The intent ledger for ONE turn — latest-wins on a single `SessionDoc`
    field (03 §B.2), never an append-only stream of trail entries (N rounds
    would put N copies inside `fit_request_to_budget`'s pinned region).

    It carries its own `turn_index` because the field is NOT cleared at the turn
    boundary: everything that reads it goes through `live_analysis_state` below,
    which makes a state from any other turn inert.
    """

    turn_index: int
    intents: tuple[TrackedIntent, ...]

    def to_doc(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "intents": [intent.to_doc() for intent in self.intents],
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> AnalysisState:
        return cls(
            turn_index=int(doc["turn_index"]),
            intents=tuple(TrackedIntent.from_doc(i) for i in doc.get("intents", [])),
        )


@dataclass(frozen=True)
class PauseCheckpoint:
    """`pause_checkpoint` — the D45 exactly-once resume checkpoint.

    The four `blueprint_*` fields are ADDITIVE (runblueprint-design §2.5, D45
    mid-DAG durability): they default to `None` so an `askUser`/`budget_cap`
    checkpoint is byte-identical to before. Slice B writes `blueprint_id` +
    `slot_bindings_json` on a slot-resolution `askUser` pause (`reason=
    "blueprint_slot"`); `completed_nodes_json`/`awaiting_node` carry the mid-DAG
    resume state that Slice C's multi-node/approval pauses populate.
    """

    reason: str  # "askUser" | "budget_cap" | "blueprint_slot" (Slice C: approval/when_ask)
    pending_question: dict[str, Any] | None
    awaiting: str  # "user_answer"
    consumed: bool
    budget_window_count: int = 0
    # --- additive, the runBlueprint brick (D45 mid-DAG durability, §2.5) ---
    blueprint_id: str | None = None
    slot_bindings_json: str | None = None  # the raw model-proposed slot_bindings (deterministic re-fill)
    completed_nodes_json: str | None = None  # [{order, output_scalar}] — SCALAR outputs only (Slice C)
    awaiting_node: int | None = None  # the node order to resume at (Slice C)
    # The `serves_intent` tag of the `runBlueprint` call that paused (additive,
    # call-time intent tagging). A pausing tool writes NO trail entry — the entry is
    # written by `_resume_blueprint` under a FRESH `tool_call_id` after the answer
    # comes back — so without carrying the tag here it is lost at the pause, and the
    # intent that blueprint was run for could only be closed by citing an id the
    # model never chose. `None` for every askUser/budget-cap pause and every
    # untagged call.
    serves_intent: str | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "pending_question": dict(self.pending_question) if self.pending_question else None,
            "awaiting": self.awaiting,
            "consumed": self.consumed,
            "budget_window_count": self.budget_window_count,
            "blueprint_id": self.blueprint_id,
            "slot_bindings_json": self.slot_bindings_json,
            "completed_nodes_json": self.completed_nodes_json,
            "awaiting_node": self.awaiting_node,
            "serves_intent": self.serves_intent,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> PauseCheckpoint:
        awaiting_node = doc.get("awaiting_node")
        return cls(
            reason=doc["reason"],
            pending_question=(
                dict(doc["pending_question"]) if doc.get("pending_question") else None
            ),
            awaiting=doc["awaiting"],
            consumed=bool(doc["consumed"]),
            budget_window_count=int(doc.get("budget_window_count", 0)),
            blueprint_id=doc.get("blueprint_id"),
            slot_bindings_json=doc.get("slot_bindings_json"),
            completed_nodes_json=doc.get("completed_nodes_json"),
            awaiting_node=int(awaiting_node) if awaiting_node is not None else None,
            serves_intent=doc.get("serves_intent"),
        )


@dataclass
class SessionDoc:
    """The full session document (`session::<session_id>`, design §6)."""

    session_id: str
    created_at: str
    last_activity: str
    learning_status: str = "active"
    messages: list[TurnMessage] = field(default_factory=list)
    tool_trail: list[TrailEntry] = field(default_factory=list)
    pause_checkpoint: PauseCheckpoint | None = None
    # `context_summary_cache` was DELETED here (L2, cleanup 2026-08). Tier 2 removed the
    # compaction seam that wrote it; nothing could populate it afterwards, so it
    # round-tripped `None` forever while reading like live per-session state.
    #
    # MIGRATION POSTURE — no migration, and none needed. `from_doc` below names every
    # key it reads (`doc["x"]` / `doc.get("x")`) and never spreads the document into the
    # constructor, so an OLD Couchbase doc still carrying `context_summary_cache` (or
    # any other key this class has never heard of) loads fine and the stray value is
    # IGNORED. Writes are whole-document (`couchbase_store._upsert_doc`/`replace` both
    # dump `to_doc()`), so the key is DROPPED the first time that session is written.
    # Old docs therefore converge on their own; nothing has to sweep them.
    # Additive (Track-B Slice 1, D96): the idempotency key the learning sweeper
    # records at the `pending → queued` transition and the consumer compares on
    # re-delivery. `None` for every session that predates the learning loop and
    # for any session not yet enqueued, so existing docs round-trip unchanged.
    learning_content_hash: str | None = None
    # Additive (Release 1, 03 §B): the intent ledger for the turn that wrote it.
    # NOT cleared at the turn boundary — it is inert for every later turn because
    # every read goes through `live_analysis_state` (03 §A.1). `None` for every
    # single-intent turn and every session that predates the feature.
    analysis_state: AnalysisState | None = None
    # Additive (Release 1, 05 §C.1): how many forced finalization re-rounds have
    # been spent, keyed by BUDGET WINDOW number as a string (`{"2": 1}`). It lives
    # on `SessionDoc` — not on `AnalysisState`, which is model-writable and
    # unknown-key-rejecting — and it must be PERSISTED, because `_run_loop_body`
    # is re-entered on every askUser/blueprint resume while `window_count` stands
    # still, so a counter local to that function makes forced re-rounds unbounded.
    # Written by 05's enforcement path; declared here as part of 03's schema work.
    finalization_blocks: dict[str, int] | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "_id": f"session::{self.session_id}",
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "learning_status": self.learning_status,
            "messages": [m.to_doc() for m in self.messages],
            "tool_trail": [e.to_doc() for e in self.tool_trail],
            "pause_checkpoint": (
                self.pause_checkpoint.to_doc() if self.pause_checkpoint else None
            ),
            "learning_content_hash": self.learning_content_hash,
            "analysis_state": (
                self.analysis_state.to_doc() if self.analysis_state else None
            ),
            "finalization_blocks": (
                dict(self.finalization_blocks) if self.finalization_blocks is not None else None
            ),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> SessionDoc:
        pc_doc = doc.get("pause_checkpoint")
        as_doc = doc.get("analysis_state")
        fb_doc = doc.get("finalization_blocks")
        return cls(
            session_id=doc["session_id"],
            created_at=doc["created_at"],
            last_activity=doc["last_activity"],
            learning_status=doc.get("learning_status", "active"),
            messages=[TurnMessage.from_doc(m) for m in doc.get("messages", [])],
            tool_trail=[TrailEntry.from_doc(e) for e in doc.get("tool_trail", [])],
            pause_checkpoint=PauseCheckpoint.from_doc(pc_doc) if pc_doc else None,
            learning_content_hash=doc.get("learning_content_hash"),
            # `.get` (not `[...]`): a document written before these fields existed
            # loads with `None` and behaves exactly as it always did.
            analysis_state=AnalysisState.from_doc(as_doc) if as_doc else None,
            finalization_blocks=(
                {str(k): int(v) for k, v in fb_doc.items()} if fb_doc else None
            ),
        )


def live_analysis_state(doc: SessionDoc, turn_index: int) -> AnalysisState | None:
    """The `AnalysisState` that GOVERNS *turn_index*, or `None` (03 §A.1).

    **The single most important rule in the feature.** A state persists on the
    session doc after its turn ends, so it is HISTORY for every later turn and
    must be invisible to anything that initializes, validates or enforces. Every
    read in 03 (the tool + the rendered context block), 04 (the evidence
    validators) and 05 (finalization enforcement) goes through this predicate.

    Without the `state.turn_index != turn_index` gate two failures are reachable,
    and the second is the bad one:

      - **Init dies after first use.** If "a state exists" means
        `doc.analysis_state is not None`, then from the session's second turn
        onward every multi-intent turn is refused initialization and the feature
        silently stops working.
      - **A stale state blocks an unrelated turn.** Turn N is multi-intent, the
        model calls `askUser`, the user abandons it and asks something new.
        `AgentLoop.run` does not check for an unconsumed checkpoint, so turn N+1
        begins with turn N's `pending` intents on the doc: enforcement refuses
        turn N+1's finalization, burns its nudge, and writes
        `ENFORCEMENT_EXHAUSTED` onto **turn N's** intents — corrupting the
        abandoned turn's record and emitting bogus telemetry for the live one.
    """
    state = doc.analysis_state
    if state is None or state.turn_index != turn_index:
        return None
    return state
