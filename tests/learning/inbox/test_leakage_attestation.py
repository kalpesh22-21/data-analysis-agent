"""The reviewer's leakage override (option 1, decided 2026-08-28).

Motivated by a live case: a regex+NER scanner read an 8-character leave-type enum inside
`event_type = '<...> Request'` as a `person`, quarantining a time-off blueprint. Nothing in the
plane could clear a verdict, so a false positive blocked the assistant permanently.

This is the ONLY action on the surface that lets a human step past a D17 gate, so the tests are
mostly about what it deliberately does NOT do.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.candidate.verdicts import (
    EntityHit,
    LeakageAttestation,
    LeakageVerdict,
    leakage_fingerprint,
)
from data_agent.learning.inbox import InboxTransitionError, ReviewInbox
from data_agent.learning.inbox.models import InboxItem, _leakage_cleared
from data_agent.learning.promotion.scheduler import _entity_scan_is_clean

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"


def _quarantined(span: str = "Vacation", **over) -> CandidateEnvelope:
    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())["leakage_near_miss"]
    env = CandidateEnvelope.from_doc(base)
    over.setdefault("status", CandidateStatus.IN_REVIEW)
    return replace(
        env,
        entity_scan=LeakageVerdict(
            result="quarantine",
            hits=(EntityHit(field="generalization.sql_template", kind="person", span=span),),
            scanned_fields=("generalization.sql_template",),
            scanner="regex+ner",
        ).to_doc(),
        **over,
    )


async def _inbox(env: CandidateEnvelope) -> tuple[ReviewInbox, InMemoryCandidateStore]:
    store = InMemoryCandidateStore()
    await store.put(env)
    return ReviewInbox(store), store


async def test_an_attestation_clears_the_assistant_gate() -> None:
    env = _quarantined()
    assert _leakage_cleared(env) is False
    inbox, store = await _inbox(env)

    await inbox.attest_scan(env.candidate_id, note="'Vacation' is a leave-type enum")

    assert _leakage_cleared(await store.get(env.candidate_id)) is True


async def test_it_never_rewrites_the_scanners_verdict() -> None:
    """The finding is the record of what a MACHINE saw; a human disagreeing is a second fact.
    Overwriting the first would destroy the only evidence the disagreement is about."""
    env = _quarantined()
    inbox, store = await _inbox(env)
    await inbox.attest_scan(env.candidate_id, note="false positive")
    after = await store.get(env.candidate_id)
    assert after.entity_scan == env.entity_scan
    assert after.entity_scan["result"] == "quarantine"
    assert after.leakage_attestation is not None


async def test_it_does_not_make_the_candidate_auto_promotable() -> None:
    """⚠ THE LINE THE OVERRIDE MUST NOT CROSS.

    `_entity_scan_is_clean` is the AUTOMATIC promotion edge — the one `promotion/scheduler.py`
    says exists for the path where "nobody is looking there". An attestation is a statement
    that somebody looked, so it informs the human-present gate and never that one.
    """
    env = _quarantined()
    inbox, store = await _inbox(env)
    await inbox.attest_scan(env.candidate_id, note="false positive")
    assert _entity_scan_is_clean(await store.get(env.candidate_id)) is False


async def test_it_lapses_when_the_finding_changes() -> None:
    """⚠ THE BINDING, and the reason the attestation stores a fingerprint rather than a bool.

    A reviewer clears a false positive; a later revision introduces a REAL entity and the gate
    re-settles with different hits. A stored "I checked this" must not cover findings nobody
    checked.
    """
    env = _quarantined(span="Vacation")
    inbox, store = await _inbox(env)
    await inbox.attest_scan(env.candidate_id, note="leave-type enum")
    attested = await store.get(env.candidate_id)
    assert _leakage_cleared(attested) is True

    re_settled = replace(
        attested,
        entity_scan=LeakageVerdict(
            result="quarantine",
            hits=(EntityHit(field="intent", kind="person", span="A Real Name"),),
            scanned_fields=("intent",),
            scanner="regex+ner",
        ).to_doc(),
    )
    assert _leakage_cleared(re_settled) is False
    # ...and the card must not claim it was signed off either.
    assert InboxItem.from_envelope(re_settled).leakage_attestation is None


async def test_a_stale_attestation_is_withheld_from_the_wire() -> None:
    """A card showing "attested" for a verdict that has since changed would tell a reviewer
    this finding had been signed off when it has not."""
    stale = replace(
        _quarantined(),
        leakage_attestation=LeakageAttestation(
            scan_fingerprint="not-the-current-one", attested_at="t", note="n", hit_count=1
        ),
    )
    assert InboxItem.from_envelope(stale).leakage_attestation is None
    assert _leakage_cleared(stale) is False


async def test_an_unsettled_scan_cannot_be_attested_to() -> None:
    """Vouching for content NOBODY has scanned is the opposite of the point — it would let a
    reviewer clear a gate on a candidate no machine has looked at."""
    env = replace(_quarantined(), entity_scan={"result": "pending", "hits": []})
    inbox, _ = await _inbox(env)
    with pytest.raises(InboxTransitionError, match="SETTLED"):
        await inbox.attest_scan(env.candidate_id, note="n")


async def test_a_clean_pass_has_nothing_to_attest_to() -> None:
    env = replace(
        _quarantined(),
        entity_scan=LeakageVerdict(result="pass", scanned_fields=("intent",), scanner="r").to_doc(),
    )
    inbox, _ = await _inbox(env)
    with pytest.raises(InboxTransitionError, match="nothing to override"):
        await inbox.attest_scan(env.candidate_id, note="n")


@pytest.mark.parametrize(
    "status", [CandidateStatus.VALIDATED, CandidateStatus.PROMOTED, CandidateStatus.REJECTED]
)
async def test_it_is_refused_past_the_point_of_judgement(status: str) -> None:
    """Same boundary as the reviser: a validated or landed artifact is not edited in place."""
    env = _quarantined(status=status)
    inbox, _ = await _inbox(env)
    with pytest.raises(InboxTransitionError):
        await inbox.attest_scan(env.candidate_id, note="n")


async def test_the_attestation_carries_no_span() -> None:
    """It is shown on a card, so it must be entity-free: a digest, a count, a timestamp and the
    reviewer's own note — never the value being withheld."""
    env = _quarantined(span="Vacation")
    inbox, store = await _inbox(env)
    await inbox.attest_scan(env.candidate_id, note="leave-type enum, not a person")
    doc = InboxItem.from_envelope(await store.get(env.candidate_id)).leakage_attestation.to_doc()
    assert "Vacation" not in json.dumps(doc)
    assert doc["hit_count"] == 1
    assert doc["scan_fingerprint"] == leakage_fingerprint(env.entity_scan)


async def test_the_round_trip_survives_the_store() -> None:
    env = _quarantined()
    inbox, store = await _inbox(env)
    await inbox.attest_scan(env.candidate_id, note="n")
    stored = await store.get(env.candidate_id)
    assert CandidateEnvelope.from_doc(stored.to_doc()).leakage_attestation == (
        stored.leakage_attestation
    )


# --- trial run: structure only, and an empty result is NOT a pass -------------


def test_an_empty_result_is_inconclusive_not_verified() -> None:
    """⚠ THE D56 GATE PASSES VACUOUSLY ON NOTHING.

    Zero rows satisfies the grain teeth, and the signature check is SKIPPED when a candidate
    declares no `result_signature.shape` — which most do. So `verify_passed` alone would put a
    green tick on a run that proved only that the SQL parses and is authorized.

    Not hypothetical: on the live stack the dev warehouse's row-level grant returned zero rows
    to the replay tenant (`SELECT count()` came back 0 against 33 real rows), so every trial
    would have reported "✓ ran and verified".
    """
    from data_agent.learning.inbox.inbox import TrialRunResult

    empty = TrialRunResult(ok=True, row_count=0, columns=(), verify_passed=True)
    assert empty.inconclusive is True
    assert empty.to_wire()["inconclusive"] is True

    real = TrialRunResult(
        ok=True, row_count=3, columns=("department", "total_pay"), verify_passed=True
    )
    assert real.inconclusive is False


def test_a_failed_run_is_never_called_inconclusive() -> None:
    """`inconclusive` is a statement about a run that HAPPENED. A refusal already carries its
    own reason, and blurring the two would hide the actionable one."""
    from data_agent.learning.inbox.inbox import TrialRunResult

    assert TrialRunResult(ok=False, reason="missing_bindings").inconclusive is False


# --- the trial runs on the REVIEWER'S token, and this surface never mints one ---------


async def test_a_trial_without_a_token_is_refused_rather_than_run_as_the_service() -> None:
    """⚠ THE LOAD-BEARING ONE. There is deliberately no fallback to the deployment principal
    when the box is empty.

    A fallback would be invisible: both paths return the same shape, so a reviewer would see a
    green trial and believe they had tested their OWN access when they had tested the service's.
    Refusing makes "I ran this as me" the only thing a passing trial can mean."""
    from data_agent.learning.inbox import ReviewInbox

    env = _quarantined()
    store = InMemoryCandidateStore()
    await store.put(env)

    # A TRANSPORT IS WIRED, because without one the inbox answers `no_warehouse` and the test
    # would pass while never reaching the blank-token branch it is named for.
    result = await ReviewInbox(store, mcp_client=object()).trial_run(
        env.candidate_id, bindings={}, token=""
    )

    assert result.ok is False
    assert result.reason == "no_token"


async def test_a_blank_token_is_the_same_as_none() -> None:
    """Whitespace is not a credential. Checked because an autofilled or partly-cleared field
    is the realistic way a blank one arrives, and it must not read as "supplied"."""
    from data_agent.learning.inbox import ReviewInbox

    env = _quarantined()
    store = InMemoryCandidateStore()
    await store.put(env)

    result = await ReviewInbox(store, mcp_client=object()).trial_run(
        env.candidate_id, bindings={}, token="   "
    )

    assert result.reason == "no_token"


def test_the_supplied_token_minter_ignores_column_scope_and_hides_the_token() -> None:
    """Two properties of the borrowed-authority path, together because they are the trade:

    it returns the token VERBATIM whatever scope is asked for — which is why the trial cannot
    prove the declared footprint is honest, and why `_assert_template_reads_within_uses` at
    landing remains the check that can — and it never renders the credential, because a
    dataclass-style repr in a traceback is exactly how a bearer token escapes."""
    import asyncio

    from data_agent.learning.promotion.token_minter import SuppliedTokenMinter

    minter = SuppliedTokenMinter("secret-token-value")

    assert minter.binds_session is False
    assert asyncio.run(minter.mint(["a.b.c"], session_id="s")) == "secret-token-value"
    assert asyncio.run(minter.mint([], session_id="s")) == "secret-token-value"
    assert "secret-token-value" not in repr(minter)


def test_a_blank_supplied_token_cannot_be_constructed() -> None:
    """Fails at construction rather than producing a probe that mints an empty Authorization
    header, which the MCP would reject with an error naming neither cause."""
    import pytest

    from data_agent.learning.promotion.token_minter import SuppliedTokenMinter

    with pytest.raises(ValueError):
        SuppliedTokenMinter("  ")


def test_the_trial_result_never_carries_the_token_back() -> None:
    """The wire shape is what reaches a browser and what a proxy may log. A credential echoed
    on the response would be persisted by any of them."""
    from data_agent.learning.inbox.inbox import TrialRunResult

    wire = TrialRunResult(ok=True, columns=("a",), row_count=1).to_wire()

    assert "token" not in wire
    assert "tenant" not in wire


def test_a_trial_failure_reports_the_real_cause_not_the_task_group_wrapper() -> None:
    """The transport raises inside a task group, so `str(exc)` was "unhandled errors in a
    TaskGroup (1 sub-exception)" — a reviewer whose token had expired was told nothing about
    the token, the warehouse, or what to do. The leaf is the only part worth showing.

    Measured live before the fix: a rejected token produced the TaskGroup string; after it,
    `HTTPStatusError: Client error '401 Unauthorized' for url '…/mcp'`.
    """
    from data_agent.learning.inbox.inbox import _explain

    group = ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("401 rejected")])
    assert _explain(group) == "RuntimeError: 401 rejected"
    # Nested groups flatten, and identical leaves are said once — a fan-out that failed the
    # same way five times is one fact, not five.
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("same")] * 3)])
    assert _explain(nested) == "ValueError: same"
    assert _explain(ValueError("plain")) == "ValueError: plain"


async def test_approve_will_not_replay_without_the_reviewers_token() -> None:
    """⚠ NOTHING ON THIS PLANE MINTS A TOKEN FOR A QUERY THE REVIEWER ASKED FOR.

    Approving runs the golden replay — a real query against the live warehouse. It used to run
    as a service principal this process minted, which made "allowed to review candidates"
    silently mean "allowed to query the warehouse". It now runs as the reviewer, on a token
    they already hold, and refuses rather than substituting.
    """
    from data_agent.learning.candidate.models import CandidateStatus
    from data_agent.learning.inbox import InboxTransitionError, ReviewInbox

    env = replace(_quarantined(), status=CandidateStatus.IN_REVIEW)
    store = InMemoryCandidateStore()
    await store.put(env)
    inbox = ReviewInbox(store, mcp_client=object())

    with pytest.raises(InboxTransitionError, match="approve_needs_token"):
        await inbox.approve(env.candidate_id, token="")
    # Whitespace is not a credential.
    with pytest.raises(InboxTransitionError, match="approve_needs_token"):
        await inbox.approve(env.candidate_id, token="   ")
    # And nothing moved.
    assert (await store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


async def test_a_non_blueprint_approve_needs_no_token_and_is_given_no_probe() -> None:
    """THE OTHER HALF OF THE SAME RULE, and the one that is easy to lose.

    The refusal above is scoped to BLUEPRINTS, because a blueprint approve replays SQL against
    the live warehouse. `global_knowledge` and `schema_edit` query nothing at all, so demanding
    a warehouse credential from their reviewer would expand authority without ever using it —
    and satisfying that demand with the DEPLOYMENT's principal would re-create precisely the
    silent substitution `_probe_for` refuses. So those types approve with NO token and are
    handed NO probe: `probe=None` reaches `apply_human_decision`, which is a different thing
    from "a probe the scheduler happens to own", and the difference is only observable at this
    call.

    Both facts are asserted here because either alone is a false comfort: a token requirement
    that crept back would be caught by the transition failing, and a fallback probe quietly
    substituted would not — the approve would still succeed.
    """
    from data_agent.learning.candidate.models import CandidateStatus
    from data_agent.learning.inbox import ReviewInbox
    from data_agent.learning.inbox.inbox import _NoOpProbe, _ZeroHitCounts
    from data_agent.learning.promotion.scheduler import PromotionScheduler

    base = json.loads((FIXTURES / "envelopes_each_reason.json").read_text())[
        "knowledge_pre_gate"
    ]
    env = CandidateEnvelope.from_doc(base)
    assert env.type == "global_knowledge" and env.status == CandidateStatus.IN_REVIEW

    store = InMemoryCandidateStore()
    await store.put(env)

    class _RecordsTheProbe:
        """A spy in front of the real scheduler — the decision is genuine, the probe is read."""

        def __init__(self, inner) -> None:
            self._inner = inner
            self.probes: list[object] = []

        @property
        def policy(self):
            return self._inner.policy

        @property
        def probe(self):
            return self._inner.probe

        async def apply_human_decision(self, env, action, *, probe=None):
            self.probes.append(probe)
            return await self._inner.apply_human_decision(env, action, probe=probe)

    # The inbox's OWN default scheduler, wrapped — not a substitute one — so what this
    # observes is the wiring an unwired inbox really has.
    spy = _RecordsTheProbe(
        PromotionScheduler(store, probe=_NoOpProbe(), hit_counts=_ZeroHitCounts())
    )
    inbox = ReviewInbox(store, scheduler=spy)

    approved = await inbox.approve(env.candidate_id, token="")  # NO token, and no refusal

    assert approved.status == CandidateStatus.VALIDATED
    assert (await store.get(env.candidate_id)).status == CandidateStatus.VALIDATED
    # Not the scheduler's own probe, and not a minted one: NOTHING.
    assert spy.probes == [None]


def test_the_human_approve_replays_through_the_reviewers_probe() -> None:
    """The token has to reach the REPLAY, not merely be validated at the door.

    `apply_human_decision` takes the caller's probe; the scheduler's own stays the default for
    the UNATTENDED paths (`apply_scheduled`, retract), which have no human to ask.
    """
    import inspect

    from data_agent.learning.promotion.scheduler import PromotionScheduler

    signature = inspect.signature(PromotionScheduler.apply_human_decision)
    assert "probe" in signature.parameters
    source = inspect.getsource(PromotionScheduler.apply_human_decision)
    assert "probe or self._probe" in source
