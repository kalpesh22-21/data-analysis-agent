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


# --- trial tenants: an allowlist, and column scope is never selectable --------


def test_the_deployment_default_leads_the_tenant_list() -> None:
    """`""` is the tenant the PROMOTION replay uses, so it is the only choice that predicts
    the real gate. It leads so the honest prediction is offered first and the alternatives
    read as "and what about…"."""
    from data_agent.learning.inbox import ReviewInbox

    inbox = ReviewInbox(InMemoryCandidateStore(), trial_probes={"CLIENT_B/PC02": object()})
    assert inbox.trial_tenants()[0] == ""
    assert "CLIENT_B/PC02" in inbox.trial_tenants()


def test_no_configured_tenants_means_the_default_only() -> None:
    """Unchanged behaviour until an operator opts in."""
    from data_agent.learning.inbox import ReviewInbox

    assert ReviewInbox(InMemoryCandidateStore()).trial_tenants() == ("",)


async def test_a_tenant_outside_the_allowlist_is_refused() -> None:
    """⚠ THE BROWSER SENDS A LABEL, NEVER CLAIMS. A reviewer choosing a tenant is asking
    "does it work for them too"; a reviewer CONSTRUCTING tenant claims would be minting
    authority. The lookup is what keeps those different."""
    from data_agent.learning.inbox import ReviewInbox

    env = _quarantined()
    store = InMemoryCandidateStore()
    await store.put(env)
    inbox = ReviewInbox(store)
    result = await inbox.trial_run(env.candidate_id, bindings={}, tenant="CLIENT_X/PC99")
    assert result.ok is False
    assert result.reason == "unknown_tenant"
