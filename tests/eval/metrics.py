"""The Layer-4 metrics (07 §E) — and the scoped pending sweep.

The metric code is code, so it has its own tests (`test_metrics.py`). Everything
here is a PURE function over things the runtime produced: persisted
`AnalysisState`s, persisted `TrailEntry`s, and observer events. Nothing reads a
fixture, so every function here works unchanged against A2 and, where noted,
against production telemetry.

Four things the first draft got wrong, recorded so they are not re-derived:

  E.1  Dropped-intent is a CONTRACT ASSERTION, not a metric, and it must be
       SCOPED to turns that reached a terminal outcome. Unscoped it fails against
       any real store — abandoned `askUser` pauses, abandoned budget-cap pauses
       and CAS-race resumes all legitimately leave `pending` behind.
  E.2  Buckets are DERIVED from `REASON_CODES`, and folded per `intent_id` FINAL
       STATE. Per-event folding double-counts: one forced block emits both
       `loop_analysis_state_transition` AND `loop_intent_force_blocked`.
  E.3  Multi-intent detection is definitionally 1.0 in A1 and carries no
       information there. Its denominator needs GROUND TRUTH, never self-report —
       the failure being measured IS the model's own misjudgement.
  §C.2 Re-derivation is INTENT-scoped. The turn-scoped form flags the legal
       "blueprint + ad-hoc residual" shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from data_agent.runtime.session.models import (
    REASON_CODES,
    AnalysisState,
    TrailEntry,
)

# The real `TurnStatus` values (`loop/agent_loop.py`). The other two are
# `paused_ask_user` and `paused_budget_cap`, and they are NOT terminal: 05 §F.1
# names three ways a turn legitimately ends with `pending` intents on the doc.
TERMINAL_TURN_STATUSES = frozenset({"done", "stopped_hard_ceiling"})
NON_TERMINAL_TURN_STATUSES = frozenset({"paused_ask_user", "paused_budget_cap"})

TERMINAL_INTENT_STATUSES = frozenset({"completed", "blocked"})


@dataclass(frozen=True)
class TurnRecord:
    """One turn the harness drove: what it returned, and the state it left."""

    case_id: str
    turn_index: int
    status: str
    state: AnalysisState | None


# ---------------------------------------------------------------------------
# E.1 — the scoped pending sweep (a contract assertion, not a metric)
# ---------------------------------------------------------------------------


def pending_on_terminal_turns(records: Iterable[TurnRecord]) -> list[tuple[str, str]]:
    """`[(case_id, intent_id)]` for every intent left `pending` on a turn that
    REACHED A TERMINAL OUTCOME. The release contract says this list is empty.

    The scoping is required, not pedantry (05 §I records that the unscoped form
    nearly shipped). Enforcement is PROVEN at Layer 1; this is a cheap redundant
    sweep over whatever turns the cases happened to produce.

    A turn whose state belongs to a DIFFERENT `turn_index` is history for this
    turn and is skipped — the same `live_analysis_state` rule 03/04/05 all apply.
    """
    violations: list[tuple[str, str]] = []
    for record in records:
        if record.status not in TERMINAL_TURN_STATUSES:
            continue
        state = record.state
        if state is None or state.turn_index != record.turn_index:
            continue
        violations.extend(
            (record.case_id, intent.intent_id)
            for intent in state.intents
            if intent.status == "pending"
        )
    return violations


# ---------------------------------------------------------------------------
# E.2 — blocked / unfulfilled-intent rate, bucketed by reason code
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockedIntentReport:
    tracked: int
    completed: int
    blocked: int
    pending: int
    buckets: dict[str, int]

    @property
    def blocked_rate(self) -> float:
        return (self.blocked / self.tracked) if self.tracked else 0.0


def blocked_intent_report(records: Iterable[TurnRecord]) -> BlockedIntentReport:
    """Fold every tracked intent's FINAL state into per-`reason_code` buckets.

    Buckets are seeded from `REASON_CODES` (03 §A.3) rather than a hard-coded
    list. The enum is final for Release 1 at five, but deriving costs nothing and
    means a later addition cannot silently go uncounted — the draft named three
    of five and omitted `BUDGET_EXHAUSTED` and `USER_STOPPED`.

    PER INTENT, NOT PER EVENT. A single forced block emits BOTH
    `loop_analysis_state_transition` and `loop_intent_force_blocked` (06), so
    counting events double-counts it. Folding the final state also means a
    `pending -> blocked -> completed` sequence counts once, as `completed`.

    Reading the buckets: `NO_ACCESS` is an entitlement story,
    `REQUIRED_DATA_UNAVAILABLE` a data story, and `ENFORCEMENT_EXHAUSTED` means
    ENFORCEMENT COULD NOT ESTABLISH A DISPOSITION — not that the agent failed, and
    not that the intent was proved impossible. It should trend down, but a user
    who withdraws an ask mid-clarification lands there legitimately.
    """
    buckets = dict.fromkeys(sorted(REASON_CODES), 0)
    # A `blocked` intent with an unrecognised code is a real defect (the
    # validators allowlist), so it is counted rather than dropped.
    buckets["UNKNOWN_REASON_CODE"] = 0
    final: dict[tuple[str, int, str], Any] = {}
    for record in records:
        state = record.state
        if state is None or state.turn_index != record.turn_index:
            continue
        for intent in state.intents:
            final[(record.case_id, record.turn_index, intent.intent_id)] = intent

    completed = blocked = pending = 0
    for intent in final.values():
        if intent.status == "completed":
            completed += 1
        elif intent.status == "blocked":
            blocked += 1
            code = intent.reason_code if intent.reason_code in REASON_CODES else None
            buckets[code or "UNKNOWN_REASON_CODE"] += 1
        else:
            pending += 1
    return BlockedIntentReport(
        tracked=len(final),
        completed=completed,
        blocked=blocked,
        pending=pending,
        buckets=buckets,
    )


@dataclass(frozen=True)
class ZeroRowRatio:
    """`REQUIRED_DATA_UNAVAILABLE` fires on any CORRECT query whose answer is
    legitimately empty (04 §B.4), so the count alone says nothing. The RATIO is
    the health signal: an empty result set treated as a BLOCK versus as an ANSWER.
    """

    blocks: int
    completions: int

    @property
    def total(self) -> int:
        return self.blocks + self.completions

    @property
    def block_share(self) -> float | None:
        return (self.blocks / self.total) if self.total else None


def zero_row_ratio(events: Sequence[tuple[str, Mapping[str, Any]]]) -> ZeroRowRatio:
    """Fold `loop_zero_row_block` / `loop_zero_row_completion` (06).

    Deduped per `intent_id`: the loop emits one event per TRANSITION, and an
    intent that flips status twice would otherwise be counted twice.
    """
    blocked: set[str] = set()
    completed: set[str] = set()
    for name, payload in events:
        intent_id = str(payload.get("intent_id"))
        if name == "loop_zero_row_block":
            blocked.add(intent_id)
        elif name == "loop_zero_row_completion":
            completed.add(intent_id)
    return ZeroRowRatio(blocks=len(blocked), completions=len(completed - blocked))


# ---------------------------------------------------------------------------
# E.3 — multi-intent detection rate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectionObservation:
    case_id: str
    # GROUND TRUTH, never self-report. The failure this metric exists for IS the
    # model's own misjudgement, so asking the model whether the request was
    # multi-intent measures nothing. In the harness this is
    # `ground_truth.multi_intent`; in production it is an offline classifier.
    multi_intent: bool
    analysis_state_initialized: bool


@dataclass(frozen=True)
class DetectionRate:
    denominator: int
    numerator: int
    false_positives: int
    single_intent_seen: int

    @property
    def rate(self) -> float | None:
        return (self.numerator / self.denominator) if self.denominator else None

    @property
    def false_positive_rate(self) -> float | None:
        return (
            (self.false_positives / self.single_intent_seen)
            if self.single_intent_seen
            else None
        )


def multi_intent_detection(observations: Iterable[DetectionObservation]) -> DetectionRate:
    """Of requests KNOWN to be multi-intent, what share created an `analysisState`?

    ⚠ IN A1 THIS IS DEFINITIONALLY 1.0 AND CARRIES NO INFORMATION — the numerator
    fires only because the fixture scripts `updateAnalysisState`. Ship the
    definition and test the computation; do NOT print it beside the A1 results as
    though it were measured. Its first real reading is A2, where the MODEL decides
    whether to initialize.

    It also cannot be replaced by `loop_analysis_state_late_init_rejected`, which
    catches only the model that missed the decomposition, LATER realised, and was
    refused. The failure this exists for is the model that never realises at all.

    `multi_intent: false` observations buy the FALSE-POSITIVE check: an
    `analysisState` initialized for a single-intent request burns 03 §E's late-init
    boundary and adds CAS writes for nothing.
    """
    denominator = numerator = false_positives = single_intent_seen = 0
    for observation in observations:
        if observation.multi_intent:
            denominator += 1
            numerator += int(observation.analysis_state_initialized)
        else:
            single_intent_seen += 1
            false_positives += int(observation.analysis_state_initialized)
    return DetectionRate(
        denominator=denominator,
        numerator=numerator,
        false_positives=false_positives,
        single_intent_seen=single_intent_seen,
    )


# ---------------------------------------------------------------------------
# §C.2 — re-derivation after an authoritative result (INTENT-scoped)
# ---------------------------------------------------------------------------


def serves_intent_from_trail(
    trail: Sequence[TrailEntry], turn_index: int
) -> dict[str, set[str]]:
    """`tool_call_id -> {intent_id}`, reconstructed from the persisted trail.

    TWO SOURCES, UNIONED, because the runtime has two ways to bind evidence:

      1. `TrailEntry.serves_intent` — the CALL-TIME TAG the model put on the call
         itself. The primary path since call-time tagging landed, and the direct
         one: no inference, the model said which intent the call was for while it
         was making it. It is also the ONLY source for a tagged call, since a
         tag-closed intent sends no `evidence_tool_call_id` at all.
      2. Every `updateAnalysisState` call leaves a `TrailEntry` whose `args` carry
         the model's own `{intent_id, evidence_tool_call_id}` bindings — the
         explicit citations, which remain valid and are the only way ONE call
         completes SEVERAL intents (04 §A).

    Both are recoverable with no fixture knowledge, which is what makes this usable
    in A2 and in production.

    `loop_intent_completed` is NOT the source: 06 gives it
    `{intent_id, evidence_tool_name, evidence_binding}`, and the tool NAME cannot
    identify which of two `runQuery` calls closed the intent. The final
    `AnalysisState` is not the source either: it is latest-wins, so a binding that
    was later replaced — which is precisely the re-derivation shape — has already
    been overwritten there.

    Only `ok` entries count for source 2: a rejected state call persisted a denial,
    not a binding. Source 1 needs no such filter — the loop only ever persists a
    tag it has already validated against the live state.
    """
    mapping: dict[str, set[str]] = {}
    for entry in trail:
        if entry.turn_index != turn_index:
            continue
        if entry.serves_intent:
            mapping.setdefault(entry.tool_call_id, set()).add(entry.serves_intent)
        if entry.tool_name != "updateAnalysisState":
            continue
        if entry.status != "ok":
            continue
        intents = entry.args.get("intents") if isinstance(entry.args, dict) else None
        if not isinstance(intents, list):
            continue
        for item in intents:
            if not isinstance(item, dict):
                continue
            call_id = item.get("evidence_tool_call_id")
            intent_id = item.get("intent_id")
            if isinstance(call_id, str) and call_id and isinstance(intent_id, str):
                mapping.setdefault(call_id, set()).add(intent_id)
    return mapping


def _intents_for(mapping: Mapping[str, Any], call_id: str) -> set[str]:
    value = mapping.get(call_id)
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return set(value)


@dataclass(frozen=True)
class ReDerivationJudgement:
    """The re-derivation reading, WITH the "nothing to judge" case split out.

    `re_derivation` collapses three different worlds into `False`:

      1. an authoritative blueprint answered an intent and NO later `runQuery`
         touched that intent — the real pass;
      2. no authoritative `runBlueprint` ran at all — there was never anything to
         re-derive;
      3. authoritative blueprints ran but NOTHING BOUND THEM TO AN INTENT (no
         `updateAnalysisState` citation, no call-time `serves_intent` tag), so the
         `∃ b` in the definition is vacuous.

    Worlds 2 and 3 are not evidence of good behaviour — they are the absence of
    evidence, and a predicate that reports them as a pass is reporting a
    measurement it never made. `re_derived is None` says so; the caller decides
    what an unjudgeable turn means for ITS case (a single-intent turn, where the
    runtime is right not to track anything, can fall back to
    `re_derivation_turn_scoped`, which is exact when there is only one intent).
    """

    re_derived: bool | None
    detail: str = ""

    @property
    def judgeable(self) -> bool:
        return self.re_derived is not None


def re_derivation_judgement(
    trail: Sequence[TrailEntry],
    serves_intent: Mapping[str, Any],
    *,
    turn_index: int,
) -> ReDerivationJudgement:
    """`re_derivation`, with the vacuous cases reported rather than swallowed.

    The predicate itself is unchanged — see `re_derivation` for the definition and
    for why it is INTENT-scoped and ordered by trail position.
    """
    scoped = [e for e in trail if e.turn_index == turn_index]
    blueprints = [
        (position, entry)
        for position, entry in enumerate(scoped)
        if entry.tool_name == "runBlueprint" and entry.authoritative is True
    ]
    if not blueprints:
        return ReDerivationJudgement(
            None,
            "no authoritative runBlueprint in the turn — there was nothing to re-derive, "
            "so re-derivation was not judged",
        )
    queries = [
        (position, entry)
        for position, entry in enumerate(scoped)
        if entry.tool_name == "runQuery"
    ]
    bound = 0
    for b_position, b_entry in blueprints:
        b_intents = _intents_for(serves_intent, b_entry.tool_call_id)
        if not b_intents:
            continue
        bound += 1
        for q_position, q_entry in queries:
            if q_position <= b_position:
                continue
            overlap = b_intents & _intents_for(serves_intent, q_entry.tool_call_id)
            if overlap:
                return ReDerivationJudgement(
                    True,
                    "a runQuery re-derived intent(s) "
                    f"{sorted(overlap)} an authoritative blueprint had answered",
                )
    if not bound:
        return ReDerivationJudgement(
            None,
            "turn was untracked (no intent binding on any authoritative runBlueprint — "
            "no updateAnalysisState citation and no call-time serves_intent tag) — "
            "cannot judge re-derivation",
        )
    return ReDerivationJudgement(False)


def re_derivation(
    trail: Sequence[TrailEntry],
    serves_intent: Mapping[str, Any],
    *,
    turn_index: int,
) -> bool:
    """Did a `runQuery` re-derive an intent an authoritative `runBlueprint`
    already answered (spec §8's last line)?

        re_derivation(turn) = ∃ q ∈ runQuery, ∃ b ∈ runBlueprint:
            trail_entry(b).authoritative is True
            and q.serves_intent == b.serves_intent
            and q.ts > b.ts

    INTENT-SCOPED, NOT TURN-SCOPED. The turn-scoped form — "a `runQuery` after an
    authoritative `runBlueprint` in the same turn" — flags the legal shape:
    `prompts.py` says the model MAY run further queries for a DISTINCT part of the
    question the blueprint did not answer. Case 3 is exactly that, and asserts
    `False` here DESPITE a post-blueprint `runQuery`.

    Ordering uses TRAIL POSITION, not `ts`. The trail is append-ordered by the
    loop, while `_now_iso()` is explicitly not guaranteed monotonic within a turn
    (`agent_loop._now_iso`'s own comment), so two calls in one batch can share a
    stamp.

    ⚠ THE BOOLEAN CANNOT DISTINGUISH "no re-derivation" FROM "nothing to judge" —
    an untracked turn is `False` here by vacuity. `re_derivation_judgement` splits
    the two; this stays a plain bool because the A1 fixtures and `test_metrics`
    assert against a declared expectation where the turn is tracked BY
    CONSTRUCTION, and because "did it happen" is the shape production telemetry
    wants.
    """
    return re_derivation_judgement(trail, serves_intent, turn_index=turn_index).re_derived is True


def re_derivation_turn_scoped(trail: Sequence[TrailEntry], *, turn_index: int) -> bool:
    """The REJECTED predicate, kept so a test can prove the two differ.

    Not a metric. It exists because "case 3 passes" is uninteresting on its own —
    what matters is that case 3 passes the intent-scoped predicate while the
    turn-scoped one flags it, which is the whole reason §C.2 was rewritten.
    """
    scoped = [e for e in trail if e.turn_index == turn_index]
    seen_authoritative = False
    for entry in scoped:
        if entry.tool_name == "runBlueprint" and entry.authoritative is True:
            seen_authoritative = True
        elif entry.tool_name == "runQuery" and seen_authoritative:
            return True
    return False


# ---------------------------------------------------------------------------
# The inferred corpus-gap signal (06) — harness-side, never loop-side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusGapSignal:
    """What `NO_APPLICABLE_TOOL` would have asserted, INFERRED rather than
    declared (04 §B.6 / 06):

        a successful `searchBlueprints` in the turn
        AND an intent completing on `evidence_tool_name == "runQuery"`
        ⇒ the corpus was searched and fell through to ad-hoc.

    Derived from events that already exist, so it works in production too — not
    just where a fixture declares the answer.
    """

    searched: bool
    ad_hoc_completions: tuple[str, ...] = ()

    @property
    def fell_through(self) -> bool:
        return self.searched and bool(self.ad_hoc_completions)


def corpus_gap_signal(
    events: Sequence[tuple[str, Mapping[str, Any]]],
) -> CorpusGapSignal:
    searched = any(
        name == "tool_dispatch_ok" and payload.get("tool_name") == "searchBlueprints"
        for name, payload in events
    )
    ad_hoc = tuple(
        str(payload.get("intent_id"))
        for name, payload in events
        if name == "loop_intent_completed" and payload.get("evidence_tool_name") == "runQuery"
    )
    return CorpusGapSignal(searched=searched, ad_hoc_completions=ad_hoc)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class PassRate:
    """A2 is reported as a PASS-RATE OVER N RUNS, not a boolean — the model is
    non-deterministic and a single red run is noise."""

    case_id: str
    runs: int = 0
    passes: int = 0
    failures: list[str] = field(default_factory=list)

    def record(self, ok: bool, detail: str = "") -> None:
        self.runs += 1
        if ok:
            self.passes += 1
        elif detail:
            self.failures.append(detail)

    @property
    def rate(self) -> float:
        return (self.passes / self.runs) if self.runs else 0.0

    def line(self) -> str:
        return f"{self.case_id}: {self.passes}/{self.runs} = {self.rate:.0%}"


def format_blocked_report(report: BlockedIntentReport) -> str:
    parts = [
        f"tracked={report.tracked}",
        f"completed={report.completed}",
        f"blocked={report.blocked}",
        f"pending={report.pending}",
    ]
    parts.extend(f"{code}={count}" for code, count in sorted(report.buckets.items()) if count)
    return " ".join(parts)


__all__ = [
    "NON_TERMINAL_TURN_STATUSES",
    "TERMINAL_INTENT_STATUSES",
    "TERMINAL_TURN_STATUSES",
    "BlockedIntentReport",
    "CorpusGapSignal",
    "DetectionObservation",
    "DetectionRate",
    "PassRate",
    "ReDerivationJudgement",
    "TurnRecord",
    "ZeroRowRatio",
    "blocked_intent_report",
    "corpus_gap_signal",
    "format_blocked_report",
    "multi_intent_detection",
    "pending_on_terminal_turns",
    "re_derivation",
    "re_derivation_judgement",
    "re_derivation_turn_scoped",
    "serves_intent_from_trail",
    "zero_row_ratio",
]
