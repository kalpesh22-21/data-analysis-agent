"""analysis_state.py — the `updateAnalysisState` runtime tool + the evidence validators.

Release 1, docs/decisions/release-1/03-analysis-state.md and
04-evidence-validators.md. The tool and the validators share this module
deliberately: the validators are PURE functions over a trail, so they unit-test
without a store, and the tool is the only caller that needs them.

WHAT IT GUARANTEES, stated precisely (04 §B.5, the Lead-approved contract):

  > For every intent the model chooses to track, Release 1 guarantees a
  > recorded, falsifiable terminal disposition. It does not guarantee that every
  > user intent was detected, nor that a model-declared disposition is
  > semantically true.

Both clauses are load-bearing. Tracking is OPT-IN — no live state means no
enforcement, and a rejected late init leaves the turn running UNPROTECTED by
design. And `completed` means "the model bound a qualifying `ok` call of the right
kind to this deliverable", not "answered correctly": the binding is now
model-DECLARED at call time rather than cited after the fact, which makes it
reachable — it does not make it true.

HOW AN INTENT IS CLOSED (call-time tagging, the amendment this module now
implements). The model tags the work as it dispatches it —
`runBlueprint(id=…, serves_intent="i2")` — and closes the intent with
`{intent_id, status}` and NOTHING ELSE; the runtime resolves the `tool_call_id`
from the tagged trail entry and runs it through the same 04 validators. See
`resolve_evidence`.

WHAT THE MODEL NO LONGER SENDS (2026-08-12). The item schema is now exactly
`{description}` on the first call and `{intent_id, status}` on every later one.
Two fields were REMOVED from the model-facing schema and are now derived:

  - `evidence_tool_call_id` — 9 live citation attempts, 9 hallucinated ids, 0
    successes ever. The tag is the working path, and the one thing citation was
    kept for (ONE call closing TWO intents, which 04 §A permits) is covered by
    the AUTO-BIND BACKSTOP below.
  - `reason_code` — the runtime CLASSIFIES the evidence instead of asking the
    model to label it. 04 §B already refused a block unless the cited call PROVED
    the code, so the model's value was never information: it was a second copy of
    a fact the trail already carried, and the only thing it could add was a
    mismatch. `classify_block_evidence` inverts the validator — the derived code
    is the one whose validator ACCEPTS the call — so there is no second
    implementation of 04 §B to drift.

The PERSISTED document keeps both fields: `TrackedIntent.reason_code` is read by
05's force-block paths (`ENFORCEMENT_EXHAUSTED` / `BUDGET_EXHAUSTED` /
`USER_STOPPED`, written runtime-side and unchanged) and by the rendered state
block. Only the MODEL stopped supplying them.

TOLERANT READING OF THE REMOVED FIELDS. A model reading stale context — its own
earlier calls are replayed to it verbatim — will send `evidence_tool_call_id` or
`reason_code` again. Both are DROPPED SILENTLY on arrival, empty or not, and are
never read: the runtime derives both, so a supplied value can only disagree.
They are NOT rejected as unknown keys (that would fail a call whose intent was
correct); see `_LEGACY_ITEM_KEYS`.

THE AUTO-BIND BACKSTOP (`resolve_evidence`). When an intent is closed and NO call
this turn carries its tag, the runtime binds the evidence itself rather than
refusing bookkeeping the model cannot redo — a call that already ran cannot be
retro-tagged. Rules, in order, over the calls that would VALIDATE as evidence for
the requested status: exactly one UNTAGGED candidate wins; otherwise exactly one
candidate of any tag wins (this is the "one call answered both deliverables"
case); otherwise the update is refused with the fix named. Every auto-bind emits
`loop_analysis_state_auto_bound` so the backstop's usage is measured, not assumed.

WHAT IMMUTABILITY DOES AND DOES NOT CLOSE (03 §C.4). Runtime-assigned ids plus
frozen descriptions plus merge-by-id close the cheap evasion: the model cannot
SILENTLY DROP an ask by shortening the array. They do NOT close manufactured
evidence — three known-permitted routes, verified, none needing warehouse access
or any knowledge of the user's scope:

  - `getTableSchema(<scratch_db>, <anything>)` fails closed with
    `SCRATCH_SESSION_VIOLATION` -> a valid `NO_ACCESS` block, for one METADATA
    call that does not even lock the late-init boundary;
  - any table outside the database allowlist -> `DATABASE_NOT_ALLOWED` (not a
    declarable code, but it shows how cheap a denial is to produce);
  - `SELECT ... WHERE 1=0` -> `row_count == 0` -> a valid
    `REQUIRED_DATA_UNAVAILABLE` block.

WHAT THE MODEL CAN ACTUALLY SEND (found live, gpt-5.5). It cannot omit keys: it
emits every property the flat item schema declares and fills the unused ones with
placeholders — `""` for a string, the FIRST ENUM MEMBER for an enum. Six
consecutive calls were rejected over an `intent_id` of `""` and the feature was
inert for the whole turn, silently. The payload is therefore NORMALISED before
mode inference and validation — "a key carrying no information is absent", by a
general rule derived from what downstream reads require, not by spot-patching the
field that happened to fail. See the block above `_carries_no_information`.
Shrinking the schema to three properties reduces how much of that placeholder
serialisation exists at all; it does not remove the rule, because `description`
still has to be empty on an update and `intent_id` empty on a declaration.

Zero rows is the same shape as an honest empty answer, so the block predicate
fires on correct work too. Both are recorded here as KNOWN-OPEN and measured
(`loop_zero_row_block` vs `loop_zero_row_completion`), not silently implied to be
closed. The adversarial suite asserts them as permitted rather than pretending
otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from data_agent.runtime.context.assembly import IDEMPOTENT_READ_ALREADY_SERVED_CODE
from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolResult,
    _default_observer,
)
from data_agent.runtime.observability import tracing
from data_agent.runtime.sanitize import sanitize_text
from data_agent.runtime.session.models import (
    INTENT_STATUSES,
    MODEL_REASON_CODES,
    AnalysisState,
    ResultPreview,
    TrackedIntent,
    TrailEntry,
    live_analysis_state,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.loop.agent_loop import TurnContext
    from data_agent.runtime.session.store import SessionStore

_logger = logging.getLogger(__name__)

TOOL_NAME = "updateAnalysisState"

# --- error codes -----------------------------------------------------------
# Both are registered in `dispatch/denial_mapping.py` AND carry a `denial_detail`
# on the returned `ToolResult`: `denial_detail` is the ONLY channel that reaches
# the model (`TrailEntry` has no `user_message` field, and
# `context/budget.py::_render_entry` re-derives the text from `error_code`), so a
# specific reason set only on `user_message` is silently dropped.
ANALYSIS_STATE_INVALID_CODE = "ANALYSIS_STATE_INVALID"  # retryable
ANALYSIS_STATE_LATE_INIT_CODE = "ANALYSIS_STATE_LATE_INIT"  # NOT retryable
# A crash that slipped every guard. Reuses the loop's registered code rather than
# inventing an unregistered one that would render as the generic
# "Something went wrong processing that request."
_INTERNAL_ERROR_CODE = "RUNTIME_TOOL_INTERNAL_ERROR"

# --- bounds (03 §A.4) — REJECT, never truncate -----------------------------
# `description` is FROZEN after initialization, so silently trimming it would
# rewrite the record enforcement depends on. `recordAssumptions` truncates
# because its content is advisory; this content is not.
MAX_INTENTS = 8
MAX_DESCRIPTION_CHARS = 500
# Per model RESPONSE, enforced by the loop's partition (03 §E.2). The cap
# exemption must be bounded: N state calls in one response are N full session-doc
# CAS read-modify-writes and N trail entries in the pinned budget region, with
# `guard.exceeded` checked only AFTER each one. Batching is already mandatory, so
# more than one per response is the model misbehaving.
MAX_STATE_CALLS = 2

# --- the late-init locking set (03 §E) -------------------------------------
# EXACTLY these four. EVERYTHING ELSE IS NON-LOCKING — `listDatabases`,
# `listTables`, `getTableSchema`, `explainQuery`, `searchBlueprints`,
# `getBlueprint`, `searchKnowledge`, `askUser`, `recordAssumptions`,
# `answerWithTable` and `updateAnalysisState` itself. The default is stated here
# in code because an implementer guessing "unlisted = locking" would make
# `recordAssumptions` block initialization.
#
# The failure is ASYMMETRIC and the unsafe direction is silence: a rejected
# initialization does not error the turn, it leaves the turn running
# UNPROTECTED. `sampleRows` and `resolveValues` are both plausible "look before
# you decompose" moves that the prompt does not forbid pre-decomposition, so if
# this set proves too aggressive the symptom is unprotected turns, not visible
# failures. Watch `loop_analysis_state_late_init_rejected` when tuning it.
SUBSTANTIVE_TOOLS = frozenset({"runQuery", "runBlueprint", "sampleRows", "resolveValues"})

# --- evidence sets (04 §A / §B.1) ------------------------------------------
# `resolveValues`, `sampleRows`, `searchBlueprints` and `searchKnowledge` are
# deliberately INSUFFICIENT for completion. `getTableSchema` is admitted as an
# accepted trade: it closes the metadata-intent gap without an `intent.kind`
# field, at the cost of letting an analytical intent be completed on a schema
# fetch. The mitigation is the `loop_metadata_evidence_completion` counter, NOT a
# structural rule — a proposed tightening ("reject `getTableSchema` evidence when
# any `runQuery`/`runBlueprint` succeeded this turn") was REJECTED on review: the
# test is per-turn while the concern is per-intent, so it breaks the legitimate
# mixed request. Do not reintroduce it.
COMPLETION_EVIDENCE_TOOLS = frozenset({"runQuery", "runBlueprint", "getTableSchema"})
# --- call-time intent tagging (the PRIMARY completion path) -----------------
# The model names the intent WHEN IT DOES THE WORK — `runQuery(sql=…,
# serves_intent="i2")` — and closes it later with `{intent_id, status}` alone.
#
# WHY THE CITATION PATH COULD NOT STAY PRIMARY. Measured over two live sessions
# after the normalisation fix: 9 completion attempts, 0 successes. The model tried
# the blueprint id (`bp-active-headcount-by-department`), the tool NAME
# (`runBlueprint`), a hallucinated `call_UUGw4Mtd…`, and finally `""` — while the
# real ids were in its context throughout (`budget.py::_render_entry` and
# `agent_loop._tool_trail_entry_to_canonical` both surface them in the standard
# shape). One session spent 5 of its 10 tool calls on rejected completions and
# never answered. The model's instincts were consistently SEMANTIC, so the runtime
# now asks for the identifier it assigned two rounds earlier and resolves the
# opaque one itself.
#
# EXACTLY THE THREE TOOLS `COMPLETION_EVIDENCE_TOOLS` NAMES — aliased rather than
# re-listed, because the tag is only ever read through 04's own validators: a tag
# on a fourth tool could never resolve to anything, so advertising one would be a
# path to nowhere. The same three are also the ones whose FAILED calls 04 §B.1
# accepts as block evidence, so blocking resolves through the identical mechanism.
INTENT_TAGGABLE_TOOLS = COMPLETION_EVIDENCE_TOOLS
SERVES_INTENT_ARG = "serves_intent"
# How an intent's evidence binding was ESTABLISHED (06). A closed, shape-only enum
# on `loop_intent_completed` / `loop_intent_blocked`, so route derivation knows its
# own provenance instead of guessing.
#
# `declared` (the model cited a `tool_call_id` itself) was RETIRED with the
# citation field: it can no longer be produced, so keeping it would report a
# provenance nothing writes. `auto_bound` replaces it and is not the same thing —
# one was the model's claim, the other is the runtime's inference, and the whole
# point of the label is telling those apart.
EVIDENCE_BINDING_TAGGED = "tagged"
EVIDENCE_BINDING_AUTO_BOUND = "auto_bound"
EVIDENCE_BINDINGS = frozenset({EVIDENCE_BINDING_TAGGED, EVIDENCE_BINDING_AUTO_BOUND})
# Emitted once per intent the backstop bound for the model. Shape ONLY — the
# runtime-assigned `intent_id` and nothing else (D25): the alternative worth
# naming is the bound `tool_call_id`, which is fine, and the CANDIDATE COUNT,
# which is not obviously fine and is not needed to answer the question this
# counter exists for ("how often is the model failing to tag its own work?").
#
# ⚠ THE `loop_` PREFIX IS LOAD-BEARING, NOT A NAMING CONVENTION.
# `observability/tracing.py::guardrail_observer` — the observer `app.py` wires in
# production — DROPS every event whose name does not start with `loop_`, silently
# and by design. This was first written WITHOUT the prefix, which meant the counter
# existed in unit tests and emitted NOTHING in production — the exact shape of
# failure the backstop is instrumented to make visible. A raw-recorder test cannot
# see that gap, so `tests/runtime/observability/test_tool_span_wiring_e2e.py`
# asserts this event through `guardrail_observer` itself. Every other event in this
# module already carries the prefix; this one now does too.
ANALYSIS_STATE_AUTO_BOUND_EVENT = "loop_analysis_state_auto_bound"
# Why a `serves_intent` argument was dropped instead of persisted — a closed enum
# of RULE NAMES on `loop_intent_tag_dropped`. THE OFFENDING VALUE IS NEVER EMITTED
# (D25): a valid tag is a runtime-assigned id, but a dropped one is by definition
# arbitrary model text.
TAG_DROP_REASONS = frozenset({"no_live_state", "unknown_intent_id", "not_a_string"})
# What qualifies as an access denial. THE ERROR CODE ESTABLISHES THE DENIAL, NOT
# THE OUTER STATUS (04 §B.1): the dispatcher sets `denied` for an `MCPToolError`,
# but the blueprint tool does not — an `ExecFailed` becomes
# `ToolResult(status="error", ...)` with the inner code passed through verbatim.
# So a `runBlueprint` that hit `COLUMN_SCOPE_VIOLATION` internally persists
# `status="error"`, and gating on `status == "denied"` would mean that in a
# BLUEPRINT-FIRST release the primary route could not produce access evidence at
# all. Keying on the code is safe because the only `ok` entry that carries an
# `error_code` is the idempotent-read guard marker, which is not in this set.
NO_ACCESS_ERROR_CODES = frozenset({"COLUMN_SCOPE_VIOLATION", "SCRATCH_SESSION_VIOLATION"})
# The reason codes the runtime will DERIVE from a block's evidence. Taken from
# `MODEL_REASON_CODES` rather than re-listed: derivation is defined as "the code
# whose 04 §B validator accepts this call", so the set of derivable codes IS the
# set of declarable ones, and a future addition to that enum becomes derivable
# without a second edit here. The two are mutually exclusive by construction
# (`NO_ACCESS` needs `status != "ok"`, `REQUIRED_DATA_UNAVAILABLE` needs `ok`), so
# the iteration order is irrelevant; it is sorted only to be deterministic.
DERIVABLE_REASON_CODES: tuple[str, ...] = tuple(sorted(MODEL_REASON_CODES))

# The one shape the model may send. Unknown keys are REJECTED, not ignored, at
# both levels — a typo'd key that silently vanished would look like a state
# update that landed.
_TOP_LEVEL_KEYS = frozenset({"intents"})
_INIT_ITEM_KEYS = frozenset({"description"})
_UPDATE_ITEM_KEYS = frozenset({"intent_id", "status"})
# REMOVED FROM THE MODEL-FACING SCHEMA, STILL TOLERATED ON THE WIRE. The model no
# longer sees either field — but its own earlier tool calls are replayed to it
# verbatim every round, and a conversation that carried them before this change
# still does. So a replayed `evidence_tool_call_id` / `reason_code` is DROPPED
# SILENTLY, empty or not, rather than rejected as an unknown key: the runtime
# derives both, so a supplied value is never read and can only disagree, and
# refusing the call would cost an answer over a field the model was told about by
# its own history. Dropped BEFORE the mode is inferred, so both halves of the tool
# see the same payload — see `_drop_legacy_fields`.
_LEGACY_ITEM_KEYS = frozenset({"evidence_tool_call_id", "reason_code"})
# Every field the TOOL SCHEMA declares on an intent item. Normalisation (below)
# only ever elides one of THESE: an unknown key survives whatever its value and
# is still rejected, so "unknown keys are rejected, not ignored" holds exactly.
_KNOWN_ITEM_KEYS = _INIT_ITEM_KEYS | _UPDATE_ITEM_KEYS
# The one remaining ENUM field. It needs its own rule because an enum has no empty
# member: a model that cannot omit a key has nothing information-free to put
# there, so it emits the FIRST member (see `_declaration_fields`). `reason_code`
# used to be the second entry and is now dropped outright, one step earlier.
_ENUM_ITEM_KEYS = frozenset({"status"})

# `loop_analysis_state_rejected.reason` is an ENUM of rule names — never the
# offending value (D25). Every rejection path below picks one of these.
REJECTION_REASONS = frozenset(
    {
        "no_turn_context",
        "malformed_arguments",
        "unknown_top_level_key",
        "empty_intents",
        "too_many_intents",
        "unknown_item_key",
        "model_supplied_intent_id",
        "missing_description",
        "description_too_long",
        "second_initialize",
        "missing_intent_id",
        "unknown_intent_id",
        "duplicate_intent_id",
        "description_rewrite",
        "invalid_status",
        "status_on_initialize",
        # A terminal update the runtime could not bind to any qualifying call —
        # the model did the bookkeeping without doing (or tagging) the work, and
        # the auto-bind backstop found no candidate either. `invalid_reason_code`,
        # `evidence_presence` and `invalid_evidence` were RETIRED with the two
        # removed fields: each named a rule about a value the model no longer
        # sends, so each was unreachable, and an unreachable member of a telemetry
        # enum is a bucket that silently never fills.
        "unresolved_evidence",
        # ...and its opposite: SEVERAL calls could have served the intent and none
        # is tagged, so binding one would be a guess. Its own reason rather than
        # `unresolved_evidence` because the fix differs — there is work to point
        # at, it just has to be named.
        "ambiguous_evidence",
        "block_evidence_reused",
        "surplus_state_call",
    }
)


class AnalysisStateRejectedError(Exception):
    """A validation failure. Raised from inside the store's merge callback for
    the state-dependent rules (03 §B.1), so a rejected call aborts the write with
    nothing persisted, and raised locally for the payload-only rules."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# Evidence validators (04) — PURE functions, `None` == valid.
#
# TWO VALIDATORS, AND THEY ARE NOT INTERCHANGEABLE. Reusing one for both is the
# most likely implementation error: completion requires a SUCCESSFUL call, while
# `NO_ACCESS` evidence is a FAILED one.
#
# Each returns a REASON STRING rather than a bool, because that string becomes
# the `denial_detail` that tells the model WHY — `classify_denial`'s canned text
# cannot name the cited call, the wrong tool, or the deduped original.
# ---------------------------------------------------------------------------


def _find_entry(
    tool_call_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> TrailEntry | None:
    """The cited entry, IF it belongs to *turn_index*.

    The `entry.turn_index == turn_index` conjunct is written explicitly into both
    predicates rather than assumed: it is the same rule as `live_analysis_state`
    — never accept evidence from a turn other than the one being enforced.
    `turn_index` (not the budget window) is the right unit, so evidence survives
    a pause and a budget-window resume within the same external turn.
    """
    for entry in trail:
        if entry.tool_call_id == tool_call_id and entry.turn_index == turn_index:
            return entry
    return None


def _missing_entry_reason(
    tool_call_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> str:
    """The reason for an id that resolves to no current-turn entry.

    NO LONGER REACHABLE THROUGH THE TOOL, and kept anyway. Every id these
    validators now see comes from a trail entry the runtime just selected, so it is
    on this turn by construction. It stays because the validators are PURE and are
    the written form of 04 §A/§B — a validator that silently assumed its caller had
    already done the lookup would be a validator that stops being reusable, and
    this branch is what makes "evidence must come from the turn being enforced"
    true on the function's own terms.

    An id that exists at ANOTHER `turn_index` is precisely diagnosable. An id
    that exists NOWHERE is genuinely ambiguous to the runtime, and the message
    says so in both halves, because the correct model behaviour differs:

      - "not dispatched yet" — 03 §E.2 dispatches `updateAnalysisState` FIRST in
        every batch, so `[runQuery, updateAnalysisState(evidence=<that call>)]`
        (the natural shape) can NEVER validate: the evidence entry does not exist
        when the state call runs. The fix is to cite it next round.
      - "unknown id" — the id was invented. The fix is to stop citing it.

    LIMIT, stated rather than hidden: the runtime cannot mechanically tell these
    two apart. Distinguishing them needs the ids of the OTHER calls in the same
    model response, and `TurnContext` deliberately carries `turn_index` only (03
    §C.1) — a side channel carrying the batch would be exactly the coupling that
    decision refused. So the two cases share one message that names both.
    """
    if any(entry.tool_call_id == tool_call_id for entry in trail):
        return (
            f"tool call '{tool_call_id}' belongs to an earlier turn, not this one. "
            "Evidence must come from a call made while answering the current question."
        )
    return (
        f"no tool call '{tool_call_id}' has been dispatched yet on this turn. If you "
        "are citing a call you made in this same message, it has not run yet — state "
        "updates are dispatched before everything else, so cite it in your NEXT "
        "message. If you did not make that call, the id is unknown: cite a call that "
        "actually ran."
    )


def _deduped_original_reason(entry: TrailEntry, trail: Sequence[TrailEntry]) -> str:
    """The reason for citing an idempotent-read GUARD marker, made actionable.

    `getTableSchema` is in `IDEMPOTENT_READ_TOOLS`, so an identical repeat is not
    re-dispatched — it is persisted as a data-free entry with `status="ok"` and
    the `IDEMPOTENT_READ_ALREADY_SERVED` marker. That entry passes every other
    completion condition having fetched NOTHING, which is why condition 5 exists.

    Since this validator holds the WHOLE trail it can also locate the original
    and name it. The loop cannot: its `seen_read_calls` is a set of signatures
    with no ids attached.
    """
    original_id = _find_deduped_original_id(entry, trail)
    if original_id is not None:
        return (
            f"tool call '{entry.tool_call_id}' was a duplicate read that was not "
            f"re-dispatched, so it fetched nothing. Cite '{original_id}' — the call "
            "that actually served this result."
        )
    return (
        f"tool call '{entry.tool_call_id}' was a duplicate read that was not "
        "re-dispatched, so it fetched nothing. Cite the earlier call that actually "
        "served this result."
    )


def _find_deduped_original_id(entry: TrailEntry, trail: Sequence[TrailEntry]) -> str | None:
    """The `tool_call_id` of the call that actually served *entry*'s result.

    Deferred import (this is the only site): `loop/read_guard.py` is the canonical
    signature helper, but importing anything from `data_agent.runtime.loop` at
    module scope would run `loop/__init__.py` -> `agent_loop` -> back into this
    module while it is still half-initialised, whenever this module is the FIRST
    one imported (which every unit test of the validators does). Re-implementing
    the signature here instead would silently diverge from the guard's own
    canonicalisation the day either changes.
    """
    from data_agent.runtime.loop.read_guard import idempotent_read_signature

    signature = idempotent_read_signature(entry.tool_name, entry.args)
    for candidate in trail:
        if (
            candidate.turn_index == entry.turn_index
            and candidate.status == "ok"
            and candidate.error_code != IDEMPOTENT_READ_ALREADY_SERVED_CODE
            and candidate.tool_name == entry.tool_name
            and idempotent_read_signature(candidate.tool_name, candidate.args) == signature
        ):
            return candidate.tool_call_id
    return None


def validate_completion_evidence(
    tool_call_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:
    """`None` when *tool_call_id* is acceptable evidence that an intent was
    ANSWERED; otherwise a model-facing reason string (04 §A).

    All of: (1) the entry is from THIS turn, (2) `status == "ok"`, (3) the tool is
    one of `runQuery`/`runBlueprint`/`getTableSchema`, (4) a `runBlueprint` is
    additionally `authoritative` — a blueprint can return `ok` with an unclean
    verify block, so this catches what (2) does not — and (5) it is not the
    idempotent-read guard marker.

    EVIDENCE REUSE ACROSS INTENTS IS ALLOWED here (telemetry-flagged only): one
    query genuinely answers "headcount and average salary by department". Blocking
    is deliberately asymmetric — see `validate_block_evidence`.
    """
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        return "a tool call id is required to complete an intent."
    entry = _find_entry(tool_call_id, trail, turn_index)
    if entry is None:
        return _missing_entry_reason(tool_call_id, trail, turn_index)
    if entry.status != "ok":
        return (
            f"tool call '{tool_call_id}' did not succeed (status={entry.status}). "
            "Completing an intent needs a successful call; a failed one can only "
            "support a blocked intent."
        )
    if entry.tool_name not in COMPLETION_EVIDENCE_TOOLS:
        return (
            f"a {entry.tool_name} result is not evidence that an intent was answered. "
            "Cite a runQuery, a verified runBlueprint, or a getTableSchema."
        )
    if entry.tool_name == "runBlueprint" and not entry.authoritative:
        return (
            f"blueprint run '{tool_call_id}' did not pass verification, so it is not "
            "an authoritative answer. Re-run it or answer from the raw tools."
        )
    if entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE:
        return _deduped_original_reason(entry, trail)
    return None


def validate_block_evidence(
    tool_call_id: str, reason_code: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:
    """`None` when *tool_call_id* supports blocking an intent for *reason_code*;
    otherwise a model-facing reason string (04 §B).

    `reason_code` is an ALLOWLIST (`MODEL_REASON_CODES`), never a blocklist of the
    runtime-forced codes. Blocklisting would make any FUTURE runtime code
    model-declarable the day it lands — and the allowlist rejects `None`, `""` and
    unknown strings for free. Nothing is queued to be added, which is precisely
    when a blocklist looks safe and quietly stops being so.

    05's runtime force-block path (`BUDGET_EXHAUSTED` / `USER_STOPPED` /
    `ENFORCEMENT_EXHAUSTED`) BYPASSES this validator entirely and writes those
    codes directly. Routing them through here gets them rejected by the allowlist.
    """
    if not isinstance(reason_code, str) or reason_code not in MODEL_REASON_CODES:
        return (
            f"'{reason_code}' is not a reason you may declare. Block an intent only as "
            f"{' or '.join(sorted(MODEL_REASON_CODES))}, each backed by the call that "
            "shows it."
        )
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        return "a tool call id is required to block an intent."
    entry = _find_entry(tool_call_id, trail, turn_index)
    if entry is None:
        return _missing_entry_reason(tool_call_id, trail, turn_index)

    if reason_code == "NO_ACCESS":
        # `status != "ok"` — NOT `== "denied"`. See NO_ACCESS_ERROR_CODES above:
        # a blueprint that hit an access denial internally surfaces as
        # `status="error"` with the inner code passed through, and in a
        # blueprint-first release that is the primary route.
        if entry.status == "ok":
            return (
                f"tool call '{tool_call_id}' succeeded, so it does not show that access "
                "was denied."
            )
        if entry.error_code not in NO_ACCESS_ERROR_CODES:
            return (
                f"tool call '{tool_call_id}' failed with {entry.error_code}, which is not "
                "an access denial — it is a call you can correct and retry. NO_ACCESS "
                "needs a call refused for permissions."
            )
        return None

    # REQUIRED_DATA_UNAVAILABLE — a SUCCESSFUL call that returned no rows.
    if entry.status != "ok":
        return (
            f"tool call '{tool_call_id}' did not succeed, so it does not show that the "
            "data is absent. REQUIRED_DATA_UNAVAILABLE needs a successful query that "
            "returned no rows."
        )
    if entry.error_code == IDEMPOTENT_READ_ALREADY_SERVED_CODE:
        return _deduped_original_reason(entry, trail)
    # The null test PRECEDES the `.row_count` dereference and is the safety
    # property here — denied entries, guard markers, and error entries from the
    # read tools / resolveValues all carry `result_preview=None`. The marker
    # clause above is defence in depth, not the ordering rule; do not "simplify"
    # this into a bare `entry.result_preview.row_count == 0`.
    if entry.result_preview is None:
        return (
            f"tool call '{tool_call_id}' returned no result at all, so it cannot show "
            "that the data is absent."
        )
    if entry.result_preview.row_count != 0:
        return (
            f"tool call '{tool_call_id}' returned "
            f"{entry.result_preview.row_count} row(s), so the data is not unavailable."
        )
    return None


def classify_block_evidence(
    tool_call_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> str | None:
    """The reason code *tool_call_id* PROVES, or `None` if it proves neither.

    THE INVERSION OF `validate_block_evidence`, AND DELIBERATELY NOT A SECOND
    IMPLEMENTATION OF IT. 04 §B already refused a block unless the evidence
    established the declared code; asking which code it establishes is the same
    question read backwards, so this asks the validator itself rather than
    re-deriving "refused with an access code" and "successful with zero rows" from
    the entry. The two cannot drift, because there is only one of them.

    This is what replaced the model-supplied `reason_code`. The model's value was
    never information — the validator recomputed the same fact from the trail and
    refused any disagreement — so removing it removes a field that could only ever
    be wrong, not a judgement the runtime lacked.

    `DERIVABLE_REASON_CODES` is mutually exclusive, so at most one code can match
    and the first hit is the answer, not a preference.
    """
    for code in DERIVABLE_REASON_CODES:
        if validate_block_evidence(tool_call_id, code, trail, turn_index) is None:
            return code
    return None


# ---------------------------------------------------------------------------
# Call-time intent tagging — the split at dispatch, and the resolution at close.
# ---------------------------------------------------------------------------


def split_serves_intent(
    tool_name: str, arguments: Any, live: AnalysisState | None
) -> tuple[Any, str | None, str | None]:
    """Split `serves_intent` out of a model tool call's *arguments*.

    Returns `(arguments_without_the_tag, tag_or_None, drop_reason_or_None)`.

    ⚠ THE STRIP IS THE LOAD-BEARING HALF. `serves_intent` is a RUNTIME concept:
    `runQuery` and `getTableSchema` are dispatched to the live MCP server, which
    rejects an argument its own schema does not declare, and `runBlueprint`'s
    executor validates its arguments too. So the tag must come off BEFORE either
    sees it — which is why this returns the cleaned arguments rather than merely
    reading the tag. It happens ONCE, at the top of the loop's dispatch body, so
    the cleaned dict is also what the read-dedup signature is computed over (two
    identical schema fetches tagged for different intents must still dedup) and
    what lands in `TrailEntry.args`.
    Doc 07's harness already stripped the fixture-level tag before dispatch; this
    is the same move, now for a real field.

    THE TAG IS VALIDATED LENIENTLY AND NEVER FAILS THE WORK (03's degrade-not-fail
    convention). An unknown or stale id yields `(clean_args, None, reason)`: the
    call still dispatches, the entry is recorded UNTAGGED, and the caller emits
    `loop_intent_tag_dropped`. Refusing a real query over a bookkeeping typo would
    be strictly worse than an untagged entry — the whole point of this release is
    that a rejected bookkeeping call costs the user an answer.

    Only the three `INTENT_TAGGABLE_TOOLS` are touched. On any other tool the
    arguments are returned untouched, so a stray `serves_intent` there fails
    exactly as it does today rather than being silently accepted.
    """
    if tool_name not in INTENT_TAGGABLE_TOOLS or not isinstance(arguments, dict):
        return arguments, None, None
    if SERVES_INTENT_ARG not in arguments:
        return arguments, None, None
    raw = arguments[SERVES_INTENT_ARG]
    cleaned = {k: v for k, v in arguments.items() if k != SERVES_INTENT_ARG}
    if _carries_no_information(raw):
        # `""`/`"  "`/`null` is the placeholder a model that cannot omit keys
        # sends — the same shape `_carries_no_information` handles on the state
        # payload, and the same rule: a key carrying no information is ABSENT, not
        # a drop worth reporting.
        return cleaned, None, None
    if not isinstance(raw, str):
        return cleaned, None, "not_a_string"
    tag = raw.strip()
    if live is None:
        return cleaned, None, "no_live_state"
    if tag not in {intent.intent_id for intent in live.intents}:
        return cleaned, None, "unknown_intent_id"
    return cleaned, tag, None


def _tagged_entries(
    intent_id: str, trail: Sequence[TrailEntry], turn_index: int
) -> list[TrailEntry]:
    """Every entry on *turn_index* the model tagged for *intent_id*, in trail
    order. Append order, not `ts`: `_now_iso()` is not guaranteed monotonic within
    a turn, so two calls in one batch can share a stamp (the same reason 07's
    re-derivation predicate orders by trail position)."""
    return [
        entry
        for entry in trail
        if entry.turn_index == turn_index and entry.serves_intent == intent_id
    ]


def _tagged_failure_reason(
    entry: TrailEntry, status: str, trail: Sequence[TrailEntry], turn_index: int
) -> str:
    """04's OWN reason why the most recent tagged call does not support *status*.

    Far more actionable than "no tagged work": it names the verification failure,
    the deduped original, or the wrong-direction status.

    For a block there is no declared code to validate against any more, so the
    probe code is chosen by which direction the entry points — a call that FAILED
    is asked about `NO_ACCESS` (its message names the error code that is not an
    access denial), a call that SUCCEEDED about `REQUIRED_DATA_UNAVAILABLE` (its
    message names the row count, the guard marker, or the missing result). Still
    04's text, still one implementation.
    """
    if status == "completed":
        return validate_completion_evidence(entry.tool_call_id, trail, turn_index) or ""
    probe = "NO_ACCESS" if entry.status != "ok" else "REQUIRED_DATA_UNAVAILABLE"
    return validate_block_evidence(entry.tool_call_id, probe, trail, turn_index) or ""


def auto_bind_candidates(
    status: str, trail: Sequence[TrailEntry], turn_index: int
) -> list[TrailEntry]:
    """Every call on *turn_index* that would VALIDATE as evidence for *status*.

    THE POOL IS DEFINED BY THE VALIDATORS, NOT BY A HAND-WRITTEN LIST. Binding a
    call the validators would refuse is the one thing the backstop must never do:
    it would persist evidence that fails 04's own conditions, which is precisely
    the guarantee this release exists to keep.

    For COMPLETION the pool is additionally narrowed to `SUBSTANTIVE_TOOLS` — real
    work, not a look-around. The intersection with `COMPLETION_EVIDENCE_TOOLS` is
    `runQuery` + `runBlueprint`, so `getTableSchema` is deliberately OUT: it is
    admitted as completion evidence only as an accepted trade (see
    `COMPLETION_EVIDENCE_TOOLS`), and auto-binding a schema fetch to an analytical
    intent would spend that trade without the model ever having claimed it. A
    metadata intent closed on a schema fetch still works — it just has to be
    TAGGED, which is the primary path anyway.

    For BLOCKING the restriction is PER DERIVED CODE, which is the asymmetry 04 §B
    already draws between the two:

      - `NO_ACCESS` — UNRESTRICTED, exactly as `validate_block_evidence` is. A
        denial is a denial whichever tool hit it, and 04 §B.4 already records that
        the cheapest route is a `getTableSchema` metadata probe; excluding metadata
        tools here would not close that (the probe is one call either way) and
        would break the legitimate case where a schema fetch is genuinely the call
        that was refused.
      - `REQUIRED_DATA_UNAVAILABLE` — SUBSTANTIVE TOOLS ONLY. "The data is not
        there" is a claim about a QUERY, and `_build_preview`'s bare-list branch
        (`tool_dispatcher.py`) gives an empty `listDatabases`/`listTables` a
        `row_count` of 0 — so without this clause an empty table listing is a
        free, fully-"evidenced" block, bound by the backstop with no model claim
        at all. That is not the 04 §B.4 hole being re-priced; it is a NEW one this
        release would have opened, since citing that id was the only previous route
        and it was the route that never worked. A listing is discovery, not
        evidence of absence.

    THE TAGGED PATH IS DELIBERATELY NOT NARROWED. There the model has explicitly
    claimed the call for the intent, which is the same reasoning that lets a TAGGED
    `getTableSchema` complete an intent while an auto-bound one cannot: the trade is
    the model's to claim, not the runtime's to make on its behalf.
    """
    if status == "completed":
        return [
            entry
            for entry in trail
            if entry.turn_index == turn_index
            and entry.tool_name in SUBSTANTIVE_TOOLS
            and validate_completion_evidence(entry.tool_call_id, trail, turn_index) is None
        ]
    candidates: list[TrailEntry] = []
    for entry in trail:
        if entry.turn_index != turn_index:
            continue
        derived = classify_block_evidence(entry.tool_call_id, trail, turn_index)
        if derived is None:
            continue
        if derived == "REQUIRED_DATA_UNAVAILABLE" and entry.tool_name not in SUBSTANTIVE_TOOLS:
            continue
        candidates.append(entry)
    return candidates


def resolve_evidence(
    intent_id: str, status: str, trail: Sequence[TrailEntry], turn_index: int
) -> tuple[str, str | None, str]:
    """The evidence backing *intent_id*: `(tool_call_id, reason_code, binding)`.

    `reason_code` is the DERIVED one for a block and `None` for a completion;
    `binding` is one of `EVIDENCE_BINDINGS`. Raises `AnalysisStateRejectedError`
    when nothing can be bound — the update is refused, nothing is persisted, and
    the detail names the fix.

    TWO PATHS, IN ORDER.

    1. THE TAG (primary). Candidates are the calls the model tagged
       `serves_intent=<intent_id>` this turn, walked NEWEST FIRST so a retry
       supersedes the attempt before it — but "most recent" means most recent
       QUALIFYING call, so a later failed attempt does not strand an intent whose
       earlier work succeeded. Each is run through 04's own validators, so a
       `runBlueprint` that failed verification, an idempotent-read guard marker, a
       failed call tagged for a completion and a successful one tagged for a block
       are all still refused. If the model tagged work and NONE of it supports the
       disposition, that is a claim about specific calls and it is refused with
       04's reason for the most recent one — the backstop does not rescue it,
       because binding some OTHER call would answer a question the model did not
       ask.

    2. THE AUTO-BIND BACKSTOP, only when NOTHING is tagged for the intent. Its
       whole justification is that a call which already ran cannot be retro-tagged:
       without it, an untagged-but-correct turn is refused bookkeeping it has no
       way to satisfy, and the release's own measurement is that a rejected
       bookkeeping call costs the user an answer.

         rule 1 — exactly ONE candidate is untagged      -> bind it
         rule 2 — exactly ONE candidate exists at all     -> bind it
         rule 3 — otherwise                               -> refuse

       Rule 2 is what makes ONE CALL ANSWERING TWO DELIVERABLES work now that
       citation is gone: the single call is tagged for the first intent, so it has
       no untagged candidate for the second, and rule 2 binds the same id to both.
       04 §A permits exactly that reuse for completion (flagged by
       `loop_evidence_reused`, never refused); 04 §B.3 still refuses it for
       BLOCKING, over the merged state, whichever path established the binding.
    """
    tagged = _tagged_entries(intent_id, trail, turn_index)
    if tagged:
        for entry in reversed(tagged):
            if status == "completed":
                if validate_completion_evidence(entry.tool_call_id, trail, turn_index) is None:
                    return entry.tool_call_id, None, EVIDENCE_BINDING_TAGGED
            else:
                derived = classify_block_evidence(entry.tool_call_id, trail, turn_index)
                if derived is not None:
                    return entry.tool_call_id, derived, EVIDENCE_BINDING_TAGGED
        raise _reject(
            "unresolved_evidence",
            f"intent '{intent_id}': the work you tagged for it does not support that. "
            f"{_tagged_failure_reason(tagged[-1], status, trail, turn_index)}",
        )

    candidates = auto_bind_candidates(status, trail, turn_index)
    untagged = [entry for entry in candidates if entry.serves_intent is None]
    if len(untagged) == 1:
        chosen = untagged[0]
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        raise _reject(*_unbindable(intent_id, status, candidates))
    derived = (
        None
        if status == "completed"
        else classify_block_evidence(chosen.tool_call_id, trail, turn_index)
    )
    return chosen.tool_call_id, derived, EVIDENCE_BINDING_AUTO_BOUND


def _unbindable(
    intent_id: str, status: str, candidates: Sequence[TrailEntry]
) -> tuple[str, str]:
    """`(reason, detail)` for an intent nothing can be bound to.

    NOTHING vs TOO MANY are different failures with different fixes, so they get
    different reasons and different text. Both messages name the tag, because
    tagging is the only action that resolves either — and both say NEXT MESSAGE,
    because 03 §E.2 dispatches state calls before everything else in the batch, so
    a call made in this same response has not run yet.
    """
    if candidates:
        return (
            "ambiguous_evidence",
            f"intent '{intent_id}': no call on this turn is tagged for it, and more than "
            "one call could have served it, so the runtime will not guess which. Tag the "
            f"call that served this intent with serves_intent='{intent_id}' and mark it "
            f"{status} in your NEXT message.",
        )
    if status == "completed":
        return (
            "unresolved_evidence",
            f"intent '{intent_id}': no call on this turn is tagged for it and nothing this "
            "turn answered it. Run the runQuery, runBlueprint or getTableSchema that "
            f"answers it with serves_intent='{intent_id}', then mark it completed in your "
            "NEXT message.",
        )
    return (
        "unresolved_evidence",
        f"intent '{intent_id}': no call on this turn is tagged for it, and nothing this "
        "turn was refused for permissions or came back empty. Tag the call that shows why "
        f"it cannot be done with serves_intent='{intent_id}' and mark it blocked in your "
        "NEXT message.",
    )


def find_locking_tool(trail: Sequence[TrailEntry], turn_index: int) -> str | None:
    """The first SUBSTANTIVE tool already run on *turn_index*, or `None` (03 §E).

    Emulated discovery is safe by construction: those entries are ephemeral and
    never persisted, so they cannot reach the trail this reads.
    """
    for entry in trail:
        if entry.turn_index == turn_index and entry.tool_name in SUBSTANTIVE_TOOLS:
            return entry.tool_name
    return None


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def _reject(reason: str, detail: str) -> AnalysisStateRejectedError:
    return AnalysisStateRejectedError(reason, detail)


# ---------------------------------------------------------------------------
# Normalisation — A KEY CARRYING NO INFORMATION IS ABSENT
#
# THE MODEL CANNOT OMIT KEYS. Found live (gpt-5.5, "active headcount by
# department, and average salary by department"): it emitted every property this
# tool's flat item schema declares and filled the ones it was not using with
# PLACEHOLDERS — `""` for a string, and the FIRST ENUM MEMBER for an enum:
#
#   {"description": "Active headcount by department.", "intent_id": "",
#    "evidence_tool_call_id": "", "reason_code": "NO_ACCESS", "status": "pending"}
#
# That is a semantically correct INITIALIZE. It was rejected as
# `model_supplied_intent_id` — as were the five retries after it, each with a
# clear `denial_detail` in front of the model. No state was ever created, six
# tool calls were burned, and the turn answered anyway: the whole feature was
# INERT and failed SILENTLY. The schema description already said "Do not invent
# ids: the runtime assigns them", so this is not fixable with wording; the
# validator has to accept the only argument shape the model can produce.
#
# The rule is derived from what downstream READS require (the repo's own
# recorded lesson — spot-patching the named field that happened to fail is what
# missed this class four rounds running), and it has one clause per JSON type:
#
#   1. A STRING with no content — `""`, whitespace, `null` — is ABSENT.
#      `intent_id` and `description` are `.strip()`-checked before use, so `""` is
#      definitionally not a value in either.
#   2. An ENUM the item's shape CANNOT READ is a placeholder, because an enum
#      has no empty member for the model to fall back on. `status` cannot be read
#      on a declaration (every intent starts `pending`).
#
# A THIRD, BLUNTER CLAUSE arrived with the 2026-08-12 trim: `evidence_tool_call_id`
# and `reason_code` are no longer declared at all, so on the wire they can only be
# stale replay. They are dropped WHATEVER they hold — `_drop_legacy_fields`, one
# step before clause 1 — since the runtime derives both and a supplied value is
# never read. The payload above therefore normalises to `{"description": "Active
# headcount by department."}` on today's build.
#
# What is NOT elided, because it is a claim rather than a placeholder: a
# NON-EMPTY `intent_id` on initialize (still `model_supplied_intent_id` — ids
# stay runtime-assigned), a non-`pending` `status` on initialize, and a non-empty
# `description` on update. A string field can express emptiness, so a value in one
# is deliberate.
# ---------------------------------------------------------------------------


def _carries_no_information(value: Any) -> bool:
    """Clause 1: `None`, `""` and `"   "` are the values that are not values."""
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _drop_legacy_fields(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The TOLERANT-READING clause: `evidence_tool_call_id` and `reason_code` are
    dropped on arrival, whatever they hold, in BOTH modes.

    Not an elision of placeholders — an elision of the whole field. Neither is in
    the model-facing schema any more, so anything arriving under those names is
    stale replay of the model's own earlier calls, and the runtime derives both
    facts from the trail. Silently dropping is the only behaviour that cannot cost
    an answer: rejecting would fail a correct update over a field the model was
    shown by its own history, and READING one would let a mismatch (a `NO_ACCESS`
    label on a zero-row call) reach the ledger the validators exist to keep honest.

    Runs BEFORE `_elide_empty_fields`, so those two names never reach the
    unknown-key checks in either mode.
    """
    return [
        {key: value for key, value in item.items() if key not in _LEGACY_ITEM_KEYS}
        for item in items
    ]


def _elide_empty_fields(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clause 1, applied to BOTH modes before the mode is even inferred.

    So an update carrying `{"intent_id": "i2", "status": "completed",
    "description": ""}` is not rejected for a `description` it did not really send.
    """
    return [
        {
            key: value
            for key, value in item.items()
            if not (key in _KNOWN_ITEM_KEYS and _carries_no_information(value))
        }
        for item in items
    ]


def _declaration_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Clause 2 on an INITIALIZE, where the one enum cannot be read: every declared
    intent starts `pending`.

    `status == "pending"` is accepted and ignored — it is the first enum member,
    i.e. the model's filler, and it agrees with what the runtime writes anyway.
    Any OTHER status is a real contradiction (nothing can be completed or blocked
    at the moment it is declared) and is REJECTED, not normalised away.
    """
    status = item.get("status")
    if status is not None and status != "pending":
        raise _reject(
            "status_on_initialize",
            f"a newly declared intent cannot already be '{status}' — every intent starts "
            "pending. Declare them, then update their status by 'intent_id' once you "
            "have the evidence.",
        )
    return {key: value for key, value in item.items() if key not in _ENUM_ITEM_KEYS}


def _require_intent_items(model_args: Any) -> list[dict[str, Any]]:
    """Shape-check the payload itself. Nothing here depends on the current state
    or on the trail, so it runs OUTSIDE the store's merge callback."""
    if not isinstance(model_args, dict):
        raise _reject(
            "malformed_arguments",
            "updateAnalysisState takes an object with one key, 'intents'.",
        )
    unknown = sorted(set(model_args) - _TOP_LEVEL_KEYS)
    if unknown:
        raise _reject(
            "unknown_top_level_key",
            f"unknown argument(s) {unknown}. updateAnalysisState takes only 'intents'.",
        )
    raw = model_args.get("intents")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise _reject(
            "malformed_arguments", "'intents' must be a list of objects."
        )
    if not raw:
        raise _reject("empty_intents", "'intents' must contain at least one intent.")
    if len(raw) > MAX_INTENTS:
        # REJECT, never truncate (03 §A.4).
        raise _reject(
            "too_many_intents",
            f"{len(raw)} intents were supplied but at most {MAX_INTENTS} may be tracked. "
            "Track the distinct deliverables the user asked for, not every sub-step.",
        )
    # Normalisation, here rather than in either mode's validator: both clauses are
    # mode-INDEPENDENT and must land before the mode is inferred, so both halves of
    # the tool see the same payload — first the two retired fields, then "a key
    # carrying no information is absent".
    return _elide_empty_fields(_drop_legacy_fields(list(raw)))


def _validate_initialize_items(items: list[dict[str, Any]]) -> list[str]:
    """Initialize accepts ONE key per item: `description`. Ids do not exist yet
    (they are assigned below), and evidence cannot: 03 §E.2 dispatches state calls
    first, so no call cited here could have run. Every intent therefore starts
    `pending`, and the non-overlapping shapes make a mis-inferred mode visible
    rather than silent.

    `_declaration_fields` runs first on each item: `status` is elided as a
    placeholder (a non-`pending` one raises there), so what reaches the key check
    below is what the model actually MEANT to send."""
    descriptions: list[str] = []
    for raw_item in items:
        item = _declaration_fields(raw_item)
        unknown = sorted(set(item) - _INIT_ITEM_KEYS)
        if "intent_id" in unknown:
            raise _reject(
                "model_supplied_intent_id",
                "intent ids are assigned by the runtime — do not supply 'intent_id' when "
                "you first declare the intents. The result of this call tells you the ids.",
            )
        if unknown:
            raise _reject(
                "unknown_item_key",
                f"unknown field(s) {unknown} when declaring intents. Declaring an intent "
                "takes only 'description'; statuses come in a later call, once you know "
                "the ids.",
            )
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise _reject(
                "missing_description",
                "every intent needs a non-empty 'description' — one short sentence naming "
                "the deliverable, in the user's own terms.",
            )
        description = description.strip()
        if len(description) > MAX_DESCRIPTION_CHARS:
            # REJECT, not trim: the description is FROZEN after this call and is
            # the record enforcement reads, so a silent truncation would rewrite
            # what the user asked for.
            raise _reject(
                "description_too_long",
                f"an intent description is {len(description)} characters; the limit is "
                f"{MAX_DESCRIPTION_CHARS}. Shorten it — one sentence naming the "
                "deliverable.",
            )
        descriptions.append(description)
    return descriptions


def _validate_update_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Update accepts EXACTLY `intent_id` + `status`. It may NOT carry a
    `description`: rewriting one is how the model would launder a hard ask into an
    easy one, and adding/deleting is closed structurally by merge-by-id.

    THERE IS NO EVIDENCE FIELD LEFT TO SHAPE-CHECK. Both retired fields were
    dropped in `_drop_legacy_fields` before this ran, and the whole evidence
    obligation now lives against the TRAIL in `_bind_evidence_against_trail` —
    which is strictly stronger than the payload rule it replaced: that one could
    only check that a non-empty string was present, this one has to find a call
    that validates."""
    # A payload shaped ENTIRELY like an initialize (descriptions, no ids) sent
    # while a live state exists is a RE-DECLARATION attempt, not a botched update.
    # Both are rejected either way, but "you already declared these, update them by
    # id" is the message that tells the model what to do next; falling through to
    # the per-item description rule would say "cannot be rewritten" about a
    # description it never sent.
    if all("description" in item and "intent_id" not in item for item in items):
        raise _reject(
            "second_initialize",
            "the intents for this question are already declared and cannot be "
            "re-declared. Update them by 'intent_id' instead.",
        )
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in items:
        unknown = sorted(set(item) - _UPDATE_ITEM_KEYS)
        if "description" in unknown:
            raise _reject(
                "description_rewrite",
                "an intent's description is fixed once declared and cannot be rewritten. "
                "Send only 'intent_id' and the new 'status'.",
            )
        if unknown:
            raise _reject(
                "unknown_item_key",
                f"unknown field(s) {unknown}. An update takes 'intent_id' and 'status', "
                "and nothing else — the runtime finds the call that evidences the intent "
                "and, for a blocked one, works out the reason from it.",
            )
        intent_id = item.get("intent_id")
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise _reject(
                "missing_intent_id",
                "every update needs the 'intent_id' of an intent you already declared.",
            )
        intent_id = intent_id.strip()
        if intent_id in seen_ids:
            raise _reject(
                "duplicate_intent_id",
                f"intent '{intent_id}' appears twice in one call — send one update per "
                "intent.",
            )
        seen_ids.add(intent_id)

        status = item.get("status")
        if status not in INTENT_STATUSES:
            raise _reject(
                "invalid_status",
                f"'{status}' is not a status. Use one of "
                f"{', '.join(sorted(INTENT_STATUSES))}.",
            )
        # Both evidence fields start as `None` and are FILLED IN by
        # `_bind_evidence_against_trail` — every downstream reader (the merge, 04
        # §B.3's distinctness check, the rendered block, the telemetry) sees the
        # same two keys it always did, sourced from the trail instead of the model.
        validated.append(
            {
                "intent_id": intent_id,
                "status": status,
                "evidence_tool_call_id": None,
                "reason_code": None,
            }
        )
    return validated


def _bind_evidence_against_trail(
    updates: list[dict[str, Any]],
    trail: Sequence[TrailEntry],
    turn_index: int,
    live: AnalysisState,
) -> dict[str, str]:
    """Fill in `evidence_tool_call_id` and `reason_code` for every terminal update,
    and refuse the ones nothing can be bound to. Returns
    `{intent_id: EVIDENCE_BINDING_*}` for the ones this call actually resolved.

    THE MODEL SUPPLIES NEITHER FIELD ANY MORE, so this is the ONLY place either is
    established for a model-declared disposition. `resolve_evidence` does the work
    (tag first, auto-bind backstop second) and raises when it cannot; the reason
    code for a block is DERIVED from the bound call rather than declared, so it
    cannot disagree with the evidence it sits next to.

    RE-AFFIRMING A DISPOSITION IS A NO-OP. An intent already in the requested
    terminal status, already carrying evidence, keeps that evidence and is not
    re-resolved. Live models re-send the whole intent list every round, and without
    this an already-closed intent could be refused later in the turn — the trail
    grows, so a bind that was unambiguous in round 2 can be ambiguous in round 4,
    and that refusal would take the OTHER intents in the same batch down with it.
    It launders nothing: a status CHANGE (including completed -> blocked) misses
    this branch and is resolved in full.

    ⚠ THE NO-OP REQUIRES EVIDENCE TO BE PRESENT, WHICH EXCLUDES A FORCE-BLOCKED
    INTENT. 05's runtime force-block paths write `reason_code` with
    `evidence_tool_call_id` left `None`, so re-sending `{intent_id, status:
    "blocked"}` for one of those falls through to a FULL re-resolution and is very
    likely refused. That is unreachable today — all three force paths
    (`BUDGET_EXHAUSTED`, `USER_STOPPED`, `ENFORCEMENT_EXHAUSTED`) are terminal, so
    no further model turn follows them — and the condition is written as
    "evidence present" deliberately rather than "same status", because a no-op on
    a disposition with NO binding would be the one shape that lets a terminal
    status stand with nothing behind it. If a resumable force path is ever added,
    this is the branch to revisit: the fix is to treat a runtime-written
    disposition as immutable here, not to relax the evidence test.

    Depends only on the payload, the trail and the state read, so it runs OUTSIDE
    the merge callback — once, not once per CAS retry. The merge re-checks the ids
    it needs against what is actually stored.
    """
    by_id = {intent.intent_id: intent for intent in live.intents}
    bindings: dict[str, str] = {}
    for update in updates:
        status = update["status"]
        if status not in ("completed", "blocked"):
            continue
        intent_id = update["intent_id"]
        current = by_id.get(intent_id)
        if (
            current is not None
            and current.status == status
            and current.evidence_tool_call_id
        ):
            update["evidence_tool_call_id"] = current.evidence_tool_call_id
            update["reason_code"] = current.reason_code
            continue
        tool_call_id, reason_code, binding = resolve_evidence(
            intent_id, status, trail, turn_index
        )
        update["evidence_tool_call_id"] = tool_call_id
        update["reason_code"] = reason_code
        bindings[intent_id] = binding
    return bindings


def _merged_intents(
    live: AnalysisState, updates: list[dict[str, Any]]
) -> tuple[TrackedIntent, ...]:
    """Apply *updates* to *live* by id.

    Merge-by-id is what makes the drop evasion structurally impossible: an intent
    the model simply stops mentioning keeps its previous disposition, and an
    unknown id is rejected rather than added. There is no "full replace" shape to
    shorten.
    """
    by_id = {intent.intent_id: intent for intent in live.intents}
    for update in updates:
        intent_id = update["intent_id"]
        current = by_id.get(intent_id)
        if current is None:
            raise _reject(
                "unknown_intent_id",
                f"there is no intent '{intent_id}'. The intents you declared are: "
                f"{', '.join(i.intent_id for i in live.intents)}.",
            )
        by_id[intent_id] = TrackedIntent(
            intent_id=current.intent_id,
            # FROZEN — carried over from the declaration, never from the payload.
            description=current.description,
            status=update["status"],
            evidence_tool_call_id=update["evidence_tool_call_id"],
            reason_code=update["reason_code"],
        )
    # Declaration order is preserved (`live.intents` order), so ids stay
    # sequential and the rendered block reads the same way every round.
    return tuple(by_id[intent.intent_id] for intent in live.intents)


def _check_block_evidence_distinct(intents: tuple[TrackedIntent, ...]) -> None:
    """BLOCK EVIDENCE MUST BE DISTINCT PER INTENT (04 §B.3, Lead-approved).

    Reuse is right for completion and wrong for blocking: a denial arises from one
    specific SQL and column set and asserts nothing about a DIFFERENT deliverable.
    Without this rule the cost of bulk-blocking is O(1) — one
    `getTableSchema(<scratch_db>, "x")` yields `SCRATCH_SESSION_VIOLATION`, and
    eight `{intent_id, status: "blocked"}` items all bind to it (the auto-bind
    backstop's rule 1 would hand every one of them that single untagged denial),
    so finalization proceeds on a fully "evidenced" record. This is exactly why
    the rule is checked over the MERGED state rather than at bind time: the
    backstop cannot see the other intents in the batch, and does not need to.

    Purely mechanical, no semantics. It does not stop manufacture; it raises the
    price from O(1) to O(n) calls and makes bulk-blocking visible instead of
    hiding it behind a shared id. Checked over the MERGED state, so a second call
    cannot reuse an id an earlier call already spent. The asymmetry with
    completion is deliberate.
    """
    seen: dict[str, str] = {}
    for intent in intents:
        if intent.status != "blocked" or not intent.evidence_tool_call_id:
            continue
        owner = seen.get(intent.evidence_tool_call_id)
        if owner is not None:
            raise _reject(
                "block_evidence_reused",
                f"tool call '{intent.evidence_tool_call_id}' is already the evidence that "
                f"blocked intent '{owner}'. One denial does not block a different "
                f"deliverable — show separately why '{intent.intent_id}' cannot be done.",
            )
        seen[intent.evidence_tool_call_id] = intent.intent_id


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def surplus_state_call_rejected() -> ToolResult:
    """The result the LOOP returns for a state call beyond `MAX_STATE_CALLS` in
    one model response (03 §E.2). Built here so the code, the retryability and
    the model-facing text stay with every other rejection in this module."""
    return _error_result(
        ANALYSIS_STATE_INVALID_CODE,
        (
            f"only {MAX_STATE_CALLS} updateAnalysisState calls are accepted per message, "
            "and this one is beyond that. Batch every intent you want to change into a "
            "single call."
        ),
        retryable=True,
    )


def _error_result(code: str, detail: str, *, retryable: bool) -> ToolResult:
    return ToolResult(
        status="error",
        tool_name=TOOL_NAME,
        error_code=code,
        retryable=retryable,
        user_message=detail,
        # DETERMINED-EMPTY, never `None` (03 §C.5). `None` means UNDETERMINED and
        # is dropped fail-closed from replay; for an `ok` entry that means the D94
        # stranded sentinel replaces the model's own state confirmation with
        # "result withheld… Do not retry". The long version of this is in
        # `composite/record_assumptions.py`; it is not repeated here.
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
        # The ONLY channel that reaches the model.
        denial_detail=detail,
    )


def _state_result(state: AnalysisState) -> ToolResult:
    """The success result — the FULL state including the assigned ids.

    A bare confirmation is not enough: this result is the only way the model
    learns the ids it must use to update anything, and the rendered context block
    (03 §D) repeats them every round so they survive a rebuild.
    """
    result_full: dict[str, Any] = {
        "turn_index": state.turn_index,
        "intent_count": len(state.intents),
        "intents": [intent.to_doc() for intent in state.intents],
    }
    preview = ResultPreview(
        columns=["intent_id", "description", "status", "evidence_tool_call_id", "reason_code"],
        row_count=len(state.intents),
        truncated=False,
        preview_rows=[
            [
                intent.intent_id,
                intent.description,
                intent.status,
                intent.evidence_tool_call_id,
                intent.reason_code,
            ]
            for intent in state.intents
        ],
    )
    return ToolResult(
        status="ok",
        tool_name=TOOL_NAME,
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=frozenset(),  # 03 §C.5 — determined-empty, never None.
        result_preview=preview,
        result_full=result_full,
    )


class UpdateAnalysisStateTool:
    """The `updateAnalysisState(intents)` runtime tool.

    It takes `observer` AND `tracer` and SELF-EMITS `tool_dispatch_start`/`ok`/
    `error`, like the three retrieval read tools — NOT like the two composite
    runtime tools it otherwise resembles. `RecordAssumptionsTool()` and
    `AnswerWithTableTool()` take neither, and `_run_runtime_tool` emits no
    dispatch events on their behalf, so mirroring that precedent literally would
    ship a silently-mute tool for the one feature whose telemetry is the whole
    point of measuring it.

    It reads the TRAIL ITSELF at execution (`session_store.load_trail`), filtered
    to `turn_index`. The trail the loop already holds is unusable: its only load
    sits ABOVE the round-trip loop and is immediately reduced to signatures, so a
    snapshot from there contains nothing from the current window — evidence
    written in round 1 and cited in round 2 would fail as "unknown id", on every
    turn, while looking correctly wired. `MAX_STATE_CALLS` bounds this at two
    reads per round-trip, and none at all on turns without a state call.
    """

    tool_name = TOOL_NAME

    def __init__(
        self,
        *,
        session_store: SessionStore,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
    ) -> None:
        self._session_store = session_store
        self._observer = observer
        self._tracer = tracer

    async def run(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None = None,
    ) -> ToolResult:
        self._observer("tool_dispatch_start", {"tool_name": TOOL_NAME})
        # D25: the span carries SHAPE ONLY. `description` is model-authored text
        # derived from the user's question, so it never reaches a span — not even
        # under `otlp_disable_redaction`, which is why this builds its own args
        # dict instead of forwarding `model_args`.
        span_args = {"intent_count": _safe_intent_count(model_args)}
        if self._tracer is None:
            result = await self._guarded(model_args, credentials, turn)
        else:
            with tracing.tool_span(
                self._tracer,
                tool_name=TOOL_NAME,
                args=span_args,
                status="ok",
                error_code=None,
            ) as span:
                result = await self._guarded(model_args, credentials, turn)
                span.set_attribute("tool.status", result.status)
                if result.error_code is not None:
                    span.set_attribute("tool.error_code", result.error_code)
        if result.status == "ok":
            self._observer("tool_dispatch_ok", {"tool_name": TOOL_NAME})
        else:
            self._observer(
                "tool_dispatch_error",
                {"tool_name": TOOL_NAME, "error_code": result.error_code},
            )
        return result

    async def _guarded(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None,
    ) -> ToolResult:
        try:
            return await self._execute(model_args, credentials, turn)
        except AnalysisStateRejectedError as rejected:
            self._observer(
                "loop_analysis_state_rejected",
                {
                    "reason": rejected.reason,
                    "intent_count": _safe_intent_count(model_args),
                },
            )
            return _error_result(
                ANALYSIS_STATE_INVALID_CODE, rejected.detail, retryable=True
            )
        except Exception:  # noqa: BLE001 - never abort the turn / leak str(exc)
            _logger.exception(
                "updateAnalysisState internal error (session=%s)", credentials.session_id
            )
            return _error_result(
                _INTERNAL_ERROR_CODE,
                "Tracking the analysis state hit an internal error.",
                retryable=False,
            )

    async def _execute(
        self,
        model_args: dict[str, Any],
        credentials: RuntimeCredentials,
        turn: TurnContext | None,
    ) -> ToolResult:
        if turn is None:
            # Degrade-not-fail, but never silently: writing state under a guessed
            # turn index would put it on the wrong turn, where `live_analysis_state`
            # makes it inert — enforcement would then pass while looking wired.
            _logger.error(
                "updateAnalysisState ran without a TurnContext (session=%s) — the loop "
                "must pass one; refusing rather than writing state to an unknown turn",
                credentials.session_id,
            )
            self._observer(
                "loop_analysis_state_rejected",
                {"reason": "no_turn_context", "intent_count": _safe_intent_count(model_args)},
            )
            return _error_result(
                _INTERNAL_ERROR_CODE,
                "Tracking the analysis state is not available on this turn.",
                retryable=False,
            )
        turn_index = turn.turn_index
        session_id = credentials.session_id

        items = _require_intent_items(model_args)
        # ONE read for both the state and the trail. `load_trail` would re-read the
        # same document, and the tool needs the doc anyway to infer its mode — so
        # this is `load_trail`'s read, not an extra one.
        #
        # The trail is handed on WHOLE, not pre-filtered to `turn_index`. Both
        # validators filter internally, and the unfiltered list is what lets them
        # tell "that call belongs to an earlier turn" (precisely diagnosable) from
        # "no such call anywhere" (genuinely ambiguous). Pre-filtering here would
        # collapse the two into the weaker message.
        doc = await self._session_store.get_or_create_session(session_id)
        live = live_analysis_state(doc, turn_index)
        trail = list(doc.tool_trail)

        if live is None:
            return await self._initialize(session_id, turn_index, items, trail)
        return await self._update(session_id, turn_index, items, trail, live)

    async def _initialize(
        self,
        session_id: str,
        turn_index: int,
        items: list[dict[str, Any]],
        trail: Sequence[TrailEntry],
    ) -> ToolResult:
        """INITIALIZE — inferred (`live_analysis_state` is `None`), never declared."""
        descriptions = _validate_initialize_items(items)

        blocking_tool = find_locking_tool(trail, turn_index)
        if blocking_tool is not None:
            self._observer(
                "loop_analysis_state_late_init_rejected",
                {"proposed_count": len(descriptions), "blocking_tool_name": blocking_tool},
            )
            # NOT retryable — the boundary has passed and no retry helps. The
            # detail carries the PROPOSED DESCRIPTIONS so the model can still see
            # what it was about to track; `denial_detail` renders only on non-`ok`
            # entries and `filter_trail`'s current-turn exemption is status-gated,
            # so this text cannot outlive the turn.
            listed = "; ".join(
                sanitize_text(d, MAX_DESCRIPTION_CHARS) for d in descriptions
            )
            return _error_result(
                ANALYSIS_STATE_LATE_INIT_CODE,
                (
                    f"the analysis is already under way ({blocking_tool} has run), so the "
                    "intents cannot be declared now. Declare them before you start "
                    f"querying. You proposed: {listed}"
                ),
                retryable=False,
            )

        def _merge(current: AnalysisState | None) -> AnalysisState:
            # Re-checked INSIDE the retry: a concurrent state call could have
            # initialized between the read above and this write, and the
            # single-initialize rule must hold against what is actually stored.
            if current is not None:
                raise _reject(
                    "second_initialize",
                    "the intents for this question are already declared and cannot be "
                    "re-declared. Update them by 'intent_id' instead.",
                )
            return AnalysisState(
                turn_index=turn_index,
                intents=tuple(
                    # Sequential ids in proposal order. Chosen for token cost and
                    # model legibility — NOT for determinism: ids are persisted, so
                    # `uuid4()` would be exactly as stable across a rebuild, a pause
                    # and a restart.
                    TrackedIntent(
                        intent_id=f"i{position}", description=description, status="pending"
                    )
                    for position, description in enumerate(descriptions, start=1)
                ),
            )

        state = await self._session_store.apply_analysis_state(session_id, turn_index, _merge)
        self._observer(
            "loop_analysis_state_initialized",
            {"intent_count": len(state.intents), "turn_index": turn_index},
        )
        return _state_result(state)

    async def _update(
        self,
        session_id: str,
        turn_index: int,
        items: list[dict[str, Any]],
        trail: Sequence[TrailEntry],
        live: AnalysisState,
    ) -> ToolResult:
        """UPDATE — batched by design: several intents per call. Load-bearing.
        With automatic blueprint-flipping cut, one call per intent would
        reintroduce exactly the bookkeeping overhead that trim exists to remove."""
        updates = _validate_update_items(items)
        # Resolves the evidence INTO `updates` (and refuses what cannot be bound),
        # so everything downstream — the merge, 04 §B.3's distinctness check, the
        # rendered state block, the telemetry — sees a real `tool_call_id` and a
        # derived `reason_code` regardless of which path established them.
        bindings = _bind_evidence_against_trail(updates, trail, turn_index, live)

        # The BEFORE snapshot for the transition events is captured inside the
        # merge, not from the `live` read above: on a CAS retry the write is
        # applied to a different document, and telemetry that reported the
        # transitions of a state that was never written would be quietly wrong.
        # Reset per invocation for the same reason the store's own callback is.
        previous: dict[str, TrackedIntent] = {}

        def _merge(current: AnalysisState | None) -> AnalysisState:
            previous.clear()
            if current is not None:
                previous.update({intent.intent_id: intent for intent in current.intents})
            if current is None:
                # The state vanished between the read and the write (only a
                # concurrent turn boundary can do this). Refuse rather than
                # silently re-creating a state whose ids the model never saw.
                raise _reject(
                    "unknown_intent_id",
                    "there are no declared intents to update — declare them first.",
                )
            merged = _merged_intents(current, updates)
            _check_block_evidence_distinct(merged)
            return AnalysisState(turn_index=turn_index, intents=merged)

        state = await self._session_store.apply_analysis_state(session_id, turn_index, _merge)
        # AFTER the write, never before: the merge can still refuse (an unknown id,
        # 04 §B.3's distinctness rule), and an `loop_analysis_state_auto_bound` for a
        # binding that was rolled back would make the backstop look busier than it
        # is — which is the one thing this counter exists to measure honestly.
        # D25: shape only, and `intent_id` is runtime-assigned.
        for intent_id in sorted(
            i for i, binding in bindings.items() if binding == EVIDENCE_BINDING_AUTO_BOUND
        ):
            self._observer(ANALYSIS_STATE_AUTO_BOUND_EVENT, {"intent_id": intent_id})
        self._emit_transition_events(previous, state, trail, turn_index, bindings)
        return _state_result(state)

    def _emit_transition_events(
        self,
        previous: dict[str, TrackedIntent],
        state: AnalysisState,
        trail: Sequence[TrailEntry],
        turn_index: int,
        bindings: dict[str, str],
    ) -> None:
        """Shape-only telemetry for what this call actually changed (06).

        D25: `intent_id` is runtime-assigned and carries no user content, so it is
        safe. `description` is model-authored from the user's question and is
        NEVER emitted — not on any event, in any payload.
        """
        evidence_owners: dict[str, list[str]] = {}
        for intent in state.intents:
            if intent.evidence_tool_call_id:
                evidence_owners.setdefault(intent.evidence_tool_call_id, []).append(
                    intent.intent_id
                )
        # Scoped to this turn, matching what the validators accepted — an id from
        # another turn must not supply the `evidence_tool_name` label.
        by_id = {
            entry.tool_call_id: entry for entry in trail if entry.turn_index == turn_index
        }

        for intent in state.intents:
            before = previous.get(intent.intent_id)
            if before is not None and before.status == intent.status:
                continue
            self._observer(
                "loop_analysis_state_transition",
                {
                    "intent_id": intent.intent_id,
                    "from_status": before.status if before else None,
                    "to_status": intent.status,
                    "reason_code": intent.reason_code,
                },
            )
            entry = by_id.get(intent.evidence_tool_call_id or "")
            zero_rows = (
                entry is not None
                and entry.result_preview is not None
                and entry.result_preview.row_count == 0
            )
            if intent.status == "completed":
                # `evidence_tool_name` is what makes the ROUTE derivable:
                # runBlueprint => blueprint, runQuery => ad-hoc,
                # getTableSchema => metadata.
                self._observer(
                    "loop_intent_completed",
                    {
                        "intent_id": intent.intent_id,
                        "evidence_tool_name": entry.tool_name if entry else None,
                        # HOW the binding was established (06) — a closed,
                        # shape-only enum, so route derivation knows its own
                        # provenance instead of inferring it. `None` only when this
                        # call did not itself set the disposition (a status that
                        # changed for another reason).
                        "evidence_binding": bindings.get(intent.intent_id),
                    },
                )
                if entry is not None and entry.tool_name == "getTableSchema":
                    # The mitigation for the accepted trade in
                    # COMPLETION_EVIDENCE_TOOLS — a counter, not a structural rule.
                    self._observer(
                        "loop_metadata_evidence_completion", {"intent_id": intent.intent_id}
                    )
                if zero_rows:
                    self._observer(
                        "loop_zero_row_completion", {"intent_id": intent.intent_id}
                    )
            elif intent.status == "blocked":
                # THE BLOCK'S ROUTE, mirroring `loop_intent_completed` — and for
                # the same reason, one step further. 04 §B.4's CHEAPEST
                # manufacture route is `getTableSchema(<scratch_db>, <anything>)`
                # => `SCRATCH_SESSION_VIOLATION` => a valid `NO_ACCESS`, one
                # metadata call that does not even lock the late-init boundary.
                # Without `evidence_tool_name` here, that block emits EXACTLY the
                # same events as one earned by a `runBlueprint` that hit
                # `COLUMN_SCOPE_VIOLATION` on the user's own data — and since the
                # release's stated mitigation for the surviving holes IS
                # measurement, an unmeasurable hole is unmitigated.
                #
                # A separate event rather than a key on
                # `loop_analysis_state_transition`: 05's runtime force-block path
                # emits that same transition event and has no evidence at all, so
                # the key would be `None` there and the two would need telling
                # apart anyway. This one fires ONLY for a model-declared block,
                # which is exactly the population being measured
                # (`loop_intent_force_blocked` is the runtime's own counterpart).
                # `reason_code` is a closed enum and `evidence_tool_name` a tool
                # name — both already on the D25 attribute allowlist. The
                # `evidence_tool_call_id` is deliberately NOT emitted here: it is
                # model-supplied text.
                self._observer(
                    "loop_intent_blocked",
                    {
                        "intent_id": intent.intent_id,
                        "reason_code": intent.reason_code,
                        "evidence_tool_name": entry.tool_name if entry else None,
                        "evidence_binding": bindings.get(intent.intent_id),
                    },
                )
                if zero_rows:
                    # The 04 §B.4 ratio: an empty result set treated as a BLOCK
                    # versus as an ANSWER. Zero rows fires on honest work ("who
                    # left last month" when nobody did), and `blocked` is the
                    # cheaper of the two for the model, so this pair is how the
                    # skew gets measured rather than assumed.
                    self._observer("loop_zero_row_block", {"intent_id": intent.intent_id})

            if (
                intent.status == "completed"
                and intent.evidence_tool_call_id
                and len(evidence_owners.get(intent.evidence_tool_call_id, [])) > 1
            ):
                # Permitted for completion (one query can genuinely answer two
                # asks) — flagged, not refused. Blocking reuse is refused above.
                #
                # It fires WHICHEVER PATH established each binding: `evidence_owners`
                # is built from the MERGED state, where a tag-resolved binding is a
                # real `tool_call_id` however it was established. That matters
                # because reuse is now necessarily MIXED — a single-valued tag cannot
                # name two intents, so the second intent reaches the same id through
                # the auto-bind backstop's rule 2 — and 04 §A's stated mitigation for
                # permitting reuse at all is this counter. It must not go dark just
                # because the path that produces it changed.
                self._observer(
                    "loop_evidence_reused",
                    {
                        "intent_id": intent.intent_id,
                        "tool_call_id": intent.evidence_tool_call_id,
                    },
                )


def _safe_intent_count(model_args: Any) -> int:
    """The shape-only count for telemetry, tolerant of any malformed payload —
    a rejection event must never itself raise."""
    if isinstance(model_args, dict):
        intents = model_args.get("intents")
        if isinstance(intents, list):
            return len(intents)
    return 0


# The `user`-role context block that shows the model its own state (and its ids)
# every round-trip lives in `context/assembly.py::render_analysis_state_block`,
# not here: this module imports `IDEMPOTENT_READ_ALREADY_SERVED_CODE` from that
# one, so the renderer has to sit on the assembly side of that edge.

__all__ = [
    "ANALYSIS_STATE_AUTO_BOUND_EVENT",
    "ANALYSIS_STATE_INVALID_CODE",
    "ANALYSIS_STATE_LATE_INIT_CODE",
    "COMPLETION_EVIDENCE_TOOLS",
    "DERIVABLE_REASON_CODES",
    "EVIDENCE_BINDINGS",
    "EVIDENCE_BINDING_AUTO_BOUND",
    "EVIDENCE_BINDING_TAGGED",
    "INTENT_TAGGABLE_TOOLS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INTENTS",
    "MAX_STATE_CALLS",
    "NO_ACCESS_ERROR_CODES",
    "REJECTION_REASONS",
    "SERVES_INTENT_ARG",
    "SUBSTANTIVE_TOOLS",
    "TAG_DROP_REASONS",
    "TOOL_NAME",
    "AnalysisStateRejectedError",
    "UpdateAnalysisStateTool",
    "auto_bind_candidates",
    "classify_block_evidence",
    "find_locking_tool",
    "resolve_evidence",
    "split_serves_intent",
    "surplus_state_call_rejected",
    "validate_block_evidence",
    "validate_completion_evidence",
]
