"""The repeated-idempotent-read guard — its primitives AND the window-scoped `ReadGuard`
that owns the decision.

A dependency-free leaf, so `loop/agent_loop.py` and `context/discovery_emulation.py`
both depend on it in ONE direction and neither reaches into the other's namespace. Keep
it a leaf: `ReadGuard` deliberately neither builds the data-free guard `TrailEntry`
(that would need `session/models.py` + the store) nor emits the guarded event (the loop
must append THEN emit). It owns the STATE and the DECISION; the loop owns the effects.

`IDEMPOTENT_READ_TOOLS` are the side-effect-free reads whose result depends ONLY on
their arguments, so an identical repeat within a turn returns the data the first call
already fetched. `runQuery`/`runBlueprint`/`askUser`/`resolveValues` are deliberately
EXCLUDED — a repeated `runQuery` may be a distinct legitimate step. `getBlueprint` is
INCLUDED: it is a keyed fetch by id, and the always-`getBlueprint`-before-`runBlueprint`
rule makes it the most repeated read of a blueprint turn.

⚠ THE GUARD'S CONTRACT IS NOT "an identical repeat is always deduped". It is:

    an identical repeat is deduped **unless the first result is no longer
    readable**, in which case it is re-dispatched (capped).

That distinction is the whole safety property. `_seen` is seeded from the PERSISTED
TRAIL, which is not the RENDERED window: `context/budget.py::fit_request_to_budget` pins
only the K most recent current-turn tool pairs and drops older ones under pressure, and
`context/assembly.py` replaces a stranded entry with a data-free sentinel. Without the
exemption the model is told "you already have this" about something it demonstrably
cannot read, and the base prompt's own re-fetch escape is unfollowable because the guard
dedups that re-fetch too. Bounded by `_MAX_TRIMMED_READ_REFETCHES` per signature per
window, because fetch -> trim -> re-fetch -> trim is in aggregate the waste the guard
exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

IDEMPOTENT_READ_TOOLS = frozenset(
    {"getTableSchema", "listTables", "listDatabases", "explainQuery", "getBlueprint"}
)

__all__ = [
    "IDEMPOTENT_READ_TOOLS",
    "ReadDecision",
    "ReadGuard",
    "ReadSignature",
    "idempotent_read_signature",
    "repeated_read_guard_event",
]

ReadSignature = tuple[str, str]
# Structural, not `dispatch.tool_dispatcher.ToolObserver` (an identical alias):
# importing that name would give this leaf a runtime edge to the dispatch package
# for a type alias. The two are the same callable shape, so `AgentLoop._observer`
# passes without a cast.
GuardObserver = Callable[[str, dict[str, Any]], None]


def idempotent_read_signature(tool_name: str, arguments: Mapping[str, Any]) -> ReadSignature:
    """The content key that identifies an already-served idempotent read: the tool name
        plus its canonicalized arguments (stable key order, `str`-coerced for any
        non-JSON-native arg). Two calls with the same key return the same data by
        construction, so the second is a re-fetch.
    """
    return (tool_name, json.dumps(dict(arguments), sort_keys=True, default=str))


def _read_target_attrs(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """`(human-readable target, catalog-safe span attributes)` for one guarded idempotent
        read — the single D25 decision about what a read's IDENTITY may appear as on a span,
        shared by both events so the two can never diverge.

        Only CATALOG-SAFE IDENTIFIER args are surfaced: `database`/`table` (the same scalars
        a real dispatch span already exposes) and `getBlueprint`'s corpus-authored `id`.
        Free-form args — notably `explainQuery`'s `sql` — are deliberately NEVER placed on a
        span, so an `explainQuery` read identifies as the empty target rather than by its
        query text.
    """
    database = arguments.get("database")
    table = arguments.get("table")
    db = database if isinstance(database, str) and database else None
    tbl = table if isinstance(table, str) and table else None
    # `getBlueprint`'s only argument is `id` — matched on the TOOL NAME rather than
    # on the presence of an `id` key, so a future guarded tool that happens to take
    # an `id` cannot start leaking a free-form value onto the span by accident.
    blueprint_id = arguments.get("id") if tool_name == "getBlueprint" else None
    bp = blueprint_id if isinstance(blueprint_id, str) and blueprint_id else None
    if db and tbl:
        target = f"{db}.{tbl}"
    elif tbl:
        target = tbl
    elif db:
        target = db
    elif bp:
        target = bp
    else:
        target = ""
    attrs: dict[str, Any] = {}
    if db:
        attrs["database"] = db
    if tbl:
        attrs["table"] = tbl
    if bp:
        attrs["blueprint_id"] = bp
    return target, attrs


def repeated_read_guard_event(
    tool_name: str, tool_call_id: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the self-describing `loop_repeated_idempotent_read_guarded` observer payload,
        so the exported GUARDRAIL span reads unambiguously as a SECOND, duplicate read that
        was deduped — NOT the first fetch being blocked.

        `guard_reason` says `already_served_and_still_readable`, not merely
        `already_served_this_turn`: since the trim-aware exemption, "already served" is no
        longer sufficient for the guard to fire, and a trace claiming otherwise would
        misdescribe the decision actually made.

        Called by `agent_loop`, not by `ReadGuard.classify`, because the loop must persist
        the data-free marker entry BEFORE it emits this event.
    """
    dedup_target, attrs = _read_target_attrs(tool_name, arguments)
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "deduped": True,
        "guard_reason": "already_served_and_still_readable",
        "dedup_target": dedup_target,
        "note": (
            f"duplicate {tool_name}({dedup_target}) — already served this turn and its "
            "result is still readable above; not re-dispatched"
        ),
    }
    payload.update(attrs)
    return payload


# How many times ONE read signature may be re-fetched past the repeated-read guard
# because its result is no longer readable, per budget window.
#
# TWO, matching `_MAX_SURPLUS_STATE_REJECTIONS`'s reasoning: the first exemption
# covers the ordinary case (the pair aged out of the pinned window once), the second
# covers a genuine second trim later in a long turn. A THIRD would mean the item is
# being trimmed as fast as it is re-added — at which point re-adding it cannot help,
# because the budget has already judged that bulk droppable twice, and paying an MCP
# round-trip to reinstate it makes the turn worse rather than better. Beyond the cap
# the guard resumes and the model gets the cheap nudge instead.
#
# It is per WINDOW, not per turn: a budget-cap `continue` resume enters a fresh
# `_run_loop_body` with a fresh allowance, exactly as the finalization re-round does.
# The worst case is therefore `max_budget_windows × 2` re-fetches of one signature
# per turn — bounded, and far from the dozens the guard was built to stop.
_MAX_TRIMMED_READ_REFETCHES = 2


def _trimmed_read_refetch_event(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    granted: int,
    reason: str,
) -> dict[str, Any]:
    """Payload for `loop_trimmed_read_refetch_allowed` / `..._capped` — the counterpart to
        `repeated_read_guard_event`, for the decision NOT to dedup.

        `refetch_count` is the number ALREADY granted for this signature in this window, so
        `0` on the first exemption. The CAPPED event is the signal worth alerting on: it
        means a turn is thrashing, and the real problem is upstream in pinning or budget
        policy, not here. Same D25 posture as the guard event.
    """
    target, attrs = _read_target_attrs(tool_name, arguments)
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "deduped": False,
        "dedup_target": target,
        "reason": reason,
        "refetch_count": granted,
        "refetch_cap": _MAX_TRIMMED_READ_REFETCHES,
        "note": (
            f"{tool_name}({target}) was already served this turn, but its result is no "
            "longer readable in the rebuilt window"
            + (
                "; re-dispatched"
                if granted < _MAX_TRIMMED_READ_REFETCHES
                else "; re-fetch cap reached, deduping instead (the turn is thrashing)"
            )
        ),
    }
    payload.update(attrs)
    return payload


@dataclass(frozen=True)
class ReadDecision:
    """What `ReadGuard.classify` concluded about ONE tool call in a batch.

        `declined=True` means: do NOT dispatch this call — the model already holds the result
        and can still read it. `signature is None` is exactly "not an idempotent read", which
        is why `is_idempotent_read` is derived rather than stored.

        The trim-aware exemption has ALREADY run by the time this exists, so a
        `declined=False` here for a repeat means the exemption was granted and its event
        already emitted.
    """

    signature: ReadSignature | None
    declined: bool

    @property
    def is_idempotent_read(self) -> bool:
        return self.signature is not None


class ReadGuard:
    """The repeated-idempotent-read guard's STATE and DECISION for one budget window.

        Window-scoped, matching `_MAX_TRIMMED_READ_REFETCHES`'s own lifetime: a budget-cap
        continue enters a fresh `_run_loop_body` and constructs a fresh guard, seeded again
        from the persisted trail. That seeding is what lets a window-local object recognize a
        repeat it did not itself serve — the D45 per-round-trip rebuild would otherwise reset
        the memory on every `send_turn`.

        Four pieces of state, all owned exclusively here:

          - `_seen`: already-served read signatures for this TURN.
          - `_served_call_ids`: `signature -> the tool_call_id of the entry that SERVED it`
            (latest wins) — the pointer the trim-aware exemption tests for readability. A
            data-free guard marker is never recorded as a pointer.
          - `_refetch_exemptions`: exemptions GRANTED per signature in this window.
          - `_served_this_round`: signatures served EARLIER IN THE CURRENT RESPONSE BATCH,
            reset by `begin_round`.

        The guard EMITS the two exemption events itself, but not the guarded event, which the
        loop emits after persisting the marker entry.
    """

    def __init__(self, observer: GuardObserver) -> None:
        self._observer = observer
        self._seen: set[ReadSignature] = set()
        self._served_call_ids: dict[ReadSignature, str] = {}
        self._refetch_exemptions: dict[ReadSignature, int] = {}
        self._served_this_round: set[ReadSignature] = set()
        self._readable_tool_call_ids: frozenset[str] = frozenset()

    def observe_prior_read(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        tool_call_id: str,
        *,
        data_free: bool,
    ) -> None:
        """Seed from ONE persisted trail entry of this turn (the caller's walk is already
                filtered to `turn_index` + `status == "ok"`). Non-read entries are ignored here
                rather than at the call site, so "what counts as a read" stays this module's
                question.

                *data_free* marks the guard's own marker entry: it proves the signature was
                served, but it is NOT where the result lives, so it must not become the pointer
                the readability test follows.
        """
        if tool_name not in IDEMPOTENT_READ_TOOLS:
            return
        signature = idempotent_read_signature(tool_name, arguments)
        self._seen.add(signature)
        if not data_free:
            self._served_call_ids[signature] = tool_call_id

    def seed_emulation(
        self,
        signatures: AbstractSet[ReadSignature],
        served_call_ids: Mapping[ReadSignature, str],
    ) -> None:
        """Seed the emulated-discovery sweep so a model re-call of
                `listDatabases`/`listTables` is served locally instead of re-dispatched to the MCP.

                The pointers matter as much as the signatures: without them the trim-aware
                exemption would read "no readable source" and re-dispatch the very calls the
                sweep exists to avoid. `fit_request_to_budget` pins the emulated pairs by id, so
                they stay readable for the whole window.

                *signatures* is an `AbstractSet` because this method only READS it — the caller
                keeps ownership of its own collection, and nothing here mutates or retains it.
        """
        self._seen.update(signatures)
        self._served_call_ids.update(served_call_ids)

    def begin_round(self, readable_tool_call_ids: frozenset[str]) -> None:
        """Start a response batch: adopt the tool results the model can actually READ this
                round-trip (post-`fit_request_to_budget`, data-free sentinels excluded) and clear
                the per-round served set.

                That reset is load-bearing. `readable_tool_call_ids` describes the window as it
                stood BEFORE this batch ran, so a read dispatched moments ago is necessarily
                absent from it — treating that as "trimmed away" would re-dispatch the second of
                two identical calls in ONE batch, precisely the duplicate the guard exists to
                collapse. Nothing can be trimmed between two calls of the same batch, so "served
                this round" means "will be readable next round".
        """
        self._readable_tool_call_ids = readable_tool_call_ids
        self._served_this_round = set()

    def classify(self, tool_name: str, arguments: Mapping[str, Any]) -> ReadDecision:
        """Decide whether this call is a guarded repeat, applying the trim-aware exemption
                inline.

                *arguments* MUST be the TAG-STRIPPED args (`split_serves_intent`): two identical
                `getTableSchema` fetches tagged for different intents have to dedup to one
                signature.

                A repeat whose SERVING RESULT IS NO LONGER READABLE is exempted and re-dispatched
                for real; when the result IS readable the guard fires exactly as before. The
                guard's premise — "the already-served result is in the history above" — holds for
                the persisted TRAIL but NOT for the RENDERED window, which
                `fit_request_to_budget` trims and `context/assembly.py` may replace with a
                data-free sentinel. `prompts.py` states the re-fetch escape positively BECAUSE
                this exemption makes it true; the two are a pair and must move together.

                UNIFORM across `IDEMPOTENT_READ_TOOLS`, deliberately: the predicate is a property
                of the CONTEXT, not of any tool, and a per-tool carve-out would leave the
                prompt's escape silently working for some reads and not others.

                BOUNDED at `_MAX_TRIMMED_READ_REFETCHES` exemptions per signature per window: a
                read that is fetched, trimmed, re-fetched and trimmed again is re-adding bulk the
                budget has already judged droppable, and past a point the cheap nudge beats
                paying an MCP round-trip.
        """
        if tool_name not in IDEMPOTENT_READ_TOOLS:
            return ReadDecision(signature=None, declined=False)
        signature = idempotent_read_signature(tool_name, arguments)
        declined = signature in self._seen
        if declined and signature not in self._served_this_round:
            served_by = self._served_call_ids.get(signature)
            if served_by is None or served_by not in self._readable_tool_call_ids:
                granted = self._refetch_exemptions.get(signature, 0)
                event = (
                    "loop_trimmed_read_refetch_allowed"
                    if granted < _MAX_TRIMMED_READ_REFETCHES
                    else "loop_trimmed_read_refetch_capped"
                )
                if granted < _MAX_TRIMMED_READ_REFETCHES:
                    self._refetch_exemptions[signature] = granted + 1
                    declined = False
                self._observer(
                    event,
                    _trimmed_read_refetch_event(
                        tool_name,
                        arguments,
                        granted=granted,
                        reason=(
                            "result_not_readable_in_window"
                            if served_by is not None
                            else "no_readable_source"
                        ),
                    ),
                )
        return ReadDecision(signature=signature, declined=declined)

    def record_served(self, decision: ReadDecision, tool_call_id: str) -> None:
        """Record a SUCCESSFUL idempotent read so an identical repeat later this turn is
                caught. The caller passes the decision `classify` already returned rather than
                the arguments again, so the signature recorded is provably the one tested.

                ONLY `ok` READS — the caller's precondition, and the whole reason this is not
                called from `classify`: a denied or errored read is not "already served", so a
                legitimate retry after a transient failure is never suppressed.

                The pointer is OVERWRITTEN, not kept: after a re-fetch it must name the freshest
                serving entry rather than the stale trimmed one whose absence granted the
                exemption.
        """
        signature = decision.signature
        if signature is None:
            return
        self._seen.add(signature)
        self._served_call_ids[signature] = tool_call_id
        self._served_this_round.add(signature)
