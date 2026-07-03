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
        or a `sampleRows`/`getTableSchema` table was uncatalogued). Per D44
        fail-closed, an undetermined entry is ALWAYS dropped from replay by
        `context/scope_filter.py`, regardless of how open `column_scope` is —
        "never assume in-scope" is read literally here.
      - `frozenset()` (empty, non-None) == "no columns referenced" — the
        tool genuinely exposes no column-level data (`listDatabases`,
        `listTables`, `explainQuery`) or the SQL genuinely referenced zero
        catalog columns (e.g. `SELECT 1`). An empty-but-determined provenance
        set is trivially a subset of any scope and is always kept.
    On the wire (`to_doc`/`from_doc`), `None` round-trips as JSON `null`;
    `frozenset()` round-trips as `[]`; a non-empty set round-trips as the
    `[["db.table", "column"], ...]` pair-list shown in design §6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    context_summary_cache: dict[str, Any] | None = None
    # Additive (Track-B Slice 1, D96): the idempotency key the learning sweeper
    # records at the `pending → queued` transition and the consumer compares on
    # re-delivery. `None` for every session that predates the learning loop and
    # for any session not yet enqueued, so existing docs round-trip unchanged.
    learning_content_hash: str | None = None

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
            "context_summary_cache": self.context_summary_cache,
            "learning_content_hash": self.learning_content_hash,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> SessionDoc:
        pc_doc = doc.get("pause_checkpoint")
        return cls(
            session_id=doc["session_id"],
            created_at=doc["created_at"],
            last_activity=doc["last_activity"],
            learning_status=doc.get("learning_status", "active"),
            messages=[TurnMessage.from_doc(m) for m in doc.get("messages", [])],
            tool_trail=[TrailEntry.from_doc(e) for e in doc.get("tool_trail", [])],
            pause_checkpoint=PauseCheckpoint.from_doc(pc_doc) if pc_doc else None,
            context_summary_cache=doc.get("context_summary_cache"),
            learning_content_hash=doc.get("learning_content_hash"),
        )
