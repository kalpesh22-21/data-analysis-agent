"""Inbox HTTP service — the reviewer routes + auth gate + error mapping (UI Slice 2).

Over an `InMemoryCandidateStore` (no infra), a `TestClient` exercises the contract
(`docs/decisions/ui-slice2-inbox-contract.md` §2/§2a/§3/§5/§6):

  * `GET /inbox` returns the EXACT §2a wire shape.
  * flag off ⇒ 404 (the surface does not exist).
  * missing `X-Reviewer-Token` ⇒ 401; bad token ⇒ 403 (constant-time compare).
  * unknown id ⇒ 404; illegal transition ⇒ 409.
  * an offline approve of a landing target ⇒ 503 ("landing plane unavailable") —
    the service never fakes a `validated`.
  * reject / retract happy paths mutate the store.

The live approve→retrievable path (which gates on Sub-task 1's landing fix) is left
to integration — not exercised here.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import _build_inbox_from_env, create_inbox_app
from data_agent.learning.promotion import PromotionScheduler
from data_agent.learning.promotion.landing import LandingInvalidError

from ..promotion.helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

# Approve REPLAYS against the warehouse and this service mints no token for it — the reviewer
# pastes one. The offline app has no MCP transport, so the wired probe still runs the replay;
# what the token changes is that the request is not refused before reaching the case under test.
REVIEWER_TRIAL_TOKEN = "reviewer-pasted-token"

FIXTURES = Path(__file__).parents[2] / "fixtures" / "learning"
TOKEN = "reviewer-secret"

# A syntactically-valid inbox item present in every store: an in_review knowledge
# candidate the reject happy-path mutates.
KNOWLEDGE_ID = "candidate::reason-knowledge_pre_gate::0"


def _load_reason_docs() -> dict:
    return json.loads((FIXTURES / "envelopes_each_reason.json").read_text())


def _populate(store: InMemoryCandidateStore, envelopes: list[CandidateEnvelope]) -> None:
    async def _go() -> None:
        for env in envelopes:
            await store.put(env)

    asyncio.run(_go())


def _store_with_all_reasons() -> InMemoryCandidateStore:
    store = InMemoryCandidateStore()
    envelopes = [CandidateEnvelope.from_doc(doc) for doc in _load_reason_docs().values()]
    _populate(store, envelopes)
    return store


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)


def _client(inbox: ReviewInbox, *, write_plane: str = "offline") -> TestClient:
    return TestClient(create_inbox_app(inbox=inbox, write_plane=write_plane))


def _default_client() -> TestClient:
    return _client(ReviewInbox(_store_with_all_reasons()))


AUTH = {"X-Reviewer-Token": TOKEN}


# --- list shape (§2a) ---------------------------------------------------------


def test_list_returns_exact_wire_shape(enabled: None) -> None:
    client = _default_client()
    resp = client.get("/inbox", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 6
    assert len(body["items"]) == 6

    item = next(i for i in body["items"] if i["candidate_id"] == KNOWLEDGE_ID)
    # EXACTLY the wire fields — the §List-API `status` plus the original set, no invented
    # confidence/drift (ui-inbox-type-archive contract §List API).
    assert set(item) == {
        "candidate_id",
        "type",
        "status",
        "reason",
        "summary",
        "payload_view",
        "evidence_refs",
        "entity_scan",
        "dedup",
        "created_at",
        "verified",
        # Plan §4: the review score + its axes. Deliberately part of the EXACT wire shape
        # rather than an extra the UI may ignore — the queue is ordered by it, so a client
        # that cannot see it cannot explain the order it is rendering.
        "score",
        # Plan §4: what the SCHEDULER knew when it routed, which nothing downstream can
        # reconstruct. `"user_corrected"` or null.
        "route_reason",
        # Fail-to-review: the decline a reviewer is being asked to fix. Part of the EXACT
        # shape (null on every other row) rather than a key that appears only on
        # `needs_parameterization` items — a client cannot branch on a field it cannot
        # know exists, and the whole slice is about a surface that said nothing.
        "decline",
        # The generalized template pre-split into text / `{slot}` parts for the reviewer
        # card. Part of the EXACT shape (`[]` on every row without a template) for the
        # `decline` reason above, and derived from the REDACTED `payload_view` — see
        # `test_template_parts_are_tokenized_from_the_redacted_view`, which is the guard
        # that matters, since this field carries inline literals.
        "template_parts",
        # The S4 parameterization judge's verdict (design §D), null on every row where it did
        # not run — which in phase D-1 is most of them. Part of the EXACT shape for the
        # `decline` reason above: a client cannot branch on a field it cannot know exists.
        "param_judge",
        # The reviewer's leakage override, null unless one is in force AND still binds to the
        # current finding. Entity-free (digest, timestamp, count, note) — never a span.
        "leakage_attestation",
    }
    assert set(item["score"]) == {
        "score",
        "novelty",
        "groundedness",
        "session_quality",
        "novelty_measured",
        "quality_measured",
        "groundedness_measured",
        # The conjunction the sort partitions on and the cutoff exempts — on the wire so
        # a UI cannot derive a different one.
        "measured",
    }
    assert item["type"] == "global_knowledge"
    assert item["status"] == "in_review"
    assert item["reason"] == "knowledge_pre_gate"
    assert item["summary"] == "the fiscal year starts in April"
    assert isinstance(item["payload_view"], dict)
    assert isinstance(item["evidence_refs"], list)
    assert item["entity_scan"]["result"] == "pass"
    assert item["dedup"] is None
    assert item["verified"] is False  # an in_review row is never verified
    assert "confidence" not in item and "drift" not in item


def test_list_projects_dedup_and_entity_scan_via_to_doc(enabled: None) -> None:
    client = _default_client()
    body = client.get("/inbox", headers=AUTH).json()
    conflict = next(
        i for i in body["items"] if i["candidate_id"] == "candidate::reason-dedup_conflict::0"
    )
    assert conflict["dedup"]["action"] == "conflict"
    # entity_scan is serialized via .to_doc() (dict, not the dataclass).
    assert isinstance(conflict["entity_scan"], dict)
    assert "hits" in conflict["entity_scan"]


# --- status filter (ui-inbox-type-archive contract §List API) ----------------


def test_list_default_status_is_review_queue(enabled: None) -> None:
    """No `?status=` ⇒ the review queue (in_review), byte-identical to before."""
    store = InMemoryCandidateStore()
    _populate(
        store,
        [
            make_blueprint_candidate(status=CandidateStatus.IN_REVIEW),
            with_type(
                replace(
                    make_blueprint_candidate(status=CandidateStatus.REJECTED),
                    candidate_id="candidate::rejected::0",
                    content_hash="hash-rejected",
                ),
                "global_knowledge",
            ),
        ],
    )
    body = _client(ReviewInbox(store)).get("/inbox", headers=AUTH).json()
    assert body["count"] == 1
    assert all(i["status"] == "in_review" for i in body["items"])


def test_list_status_rejected_returns_only_archived(enabled: None) -> None:
    """`?status=rejected` ⇒ the durable archive view (rejected rows only)."""
    store = InMemoryCandidateStore()
    _populate(
        store,
        [
            make_blueprint_candidate(status=CandidateStatus.IN_REVIEW),
            with_type(
                replace(
                    make_blueprint_candidate(status=CandidateStatus.REJECTED),
                    candidate_id="candidate::rejected::0",
                    content_hash="hash-rejected",
                ),
                "global_knowledge",
            ),
        ],
    )
    resp = _client(ReviewInbox(store)).get(
        "/inbox", headers=AUTH, params={"status": "rejected"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["candidate_id"] == "candidate::rejected::0"
    assert body["items"][0]["status"] == "rejected"


def test_archive_list_returns_newest_reject_first(enabled: None) -> None:
    """`?status=rejected` lists NEWEST-first so the LIMIT trims OLD history, not
    present rejects; the default review queue keeps oldest-first (byte-identical)."""
    store = InMemoryCandidateStore()
    rejects = [
        replace(
            make_blueprint_candidate(status=CandidateStatus.REJECTED),
            candidate_id=f"candidate::rejected::{i}",
            content_hash=f"hash-rejected-{i}",
            created_at=f"2026-07-03T00:00:0{i}+00:00",
        )
        for i in range(3)
    ]
    _populate(store, rejects)
    resp = _client(ReviewInbox(store)).get(
        "/inbox", headers=AUTH, params={"status": "rejected"}
    )
    assert resp.status_code == 200
    ids = [i["candidate_id"] for i in resp.json()["items"]]
    assert ids == [
        "candidate::rejected::2",
        "candidate::rejected::1",
        "candidate::rejected::0",
    ]


def test_reject_then_archive_list_shows_row_with_status_rejected(enabled: None) -> None:
    """End-to-end at the service seam (the unit mirror of the archive-on-reject E2E):
    POST reject moves an in_review item out of the review queue, and
    `GET /inbox?status=rejected` then returns it with `status='rejected'` (the archive
    badge driver) — the row is archived, not deleted."""
    store = _store_with_all_reasons()
    client = _client(ReviewInbox(store))

    assert client.post(f"/inbox/{KNOWLEDGE_ID}/reject", headers=AUTH).status_code == 200

    # Gone from the default review queue.
    review = client.get("/inbox", headers=AUTH).json()
    assert all(i["candidate_id"] != KNOWLEDGE_ID for i in review["items"])

    # Present in the archive with status=rejected (and its type/shape intact).
    archive = client.get("/inbox", headers=AUTH, params={"status": "rejected"}).json()
    row = next(i for i in archive["items"] if i["candidate_id"] == KNOWLEDGE_ID)
    assert row["status"] == "rejected"
    assert row["type"] == "global_knowledge"


def test_list_invalid_status_is_400(enabled: None) -> None:
    """A status outside `_LISTABLE_STATUSES` is a 400 — the inbox never enumerates an
    arbitrary lifecycle state (defense-in-depth behind the BFF's own check).
    `quarantined` and `retired` are real lifecycle states that are NOT listable."""
    client = _default_client()
    resp = client.get("/inbox", headers=AUTH, params={"status": "quarantined"})
    assert resp.status_code == 400
    assert client.get("/inbox", headers=AUTH, params={"status": "retired"}).status_code == 400
    # The message is derived from the allowlist, so it cannot name a stale set.
    assert "promoted" in resp.json()["detail"]


def test_list_status_promoted_returns_the_terminal_set_newest_first(
    enabled: None,
) -> None:
    """`promoted` is terminal but NOT invisible: `promote` re-emits from it idempotently,
    so the YAML behind an abandoned PR is recoverable — by a caller that can still FIND
    the row. Withholding the listing left that affordance reachable by curl and by
    nothing else. Newest-first, like the other unbounded terminal archive, because the
    row you want back is the one you promoted most recently."""
    store = InMemoryCandidateStore()
    _populate(
        store,
        [
            make_blueprint_candidate(status=CandidateStatus.IN_REVIEW),
            *[
                replace(
                    make_blueprint_candidate(status=CandidateStatus.PROMOTED),
                    candidate_id=f"candidate::promoted::{i}",
                    content_hash=f"hash-promoted-{i}",
                    created_at=f"2026-07-04T00:00:0{i}+00:00",
                    verified=True,
                )
                for i in range(3)
            ],
        ],
    )

    resp = _client(ReviewInbox(store)).get(
        "/inbox", headers=AUTH, params={"status": "promoted"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert [i["candidate_id"] for i in body["items"]] == [
        "candidate::promoted::2",
        "candidate::promoted::1",
        "candidate::promoted::0",
    ]
    assert all(i["status"] == "promoted" for i in body["items"])


# --- redaction on the wire boundary (§2a: entity spans MUST be blanked) --------


def _knowledge_with_entity_in_statement() -> CandidateEnvelope:
    """A global_knowledge candidate whose RAW entity value (`E12345`) sits in BOTH the
    statement (→ summary) and a settled S5 hit span — the exact re-leak the review
    flagged. `in_review`, so no promotion-boundary strip has run yet."""
    doc = copy.deepcopy(_load_reason_docs()["knowledge_pre_gate"])
    doc["_id"] = doc["candidate_id"] = "candidate::entity-in-summary::0"
    doc["payload"]["statement"] = "the payroll owner is E12345"
    doc["entity_scan"] = {
        "result": "quarantine",
        "hits": [{"field": "statement", "kind": "employee_code", "span": "E12345"}],
        "scanned_fields": ["statement"],
        "scanner": "regex+ner+llm",
    }
    return CandidateEnvelope.from_doc(doc)


def test_wire_blanks_entity_scan_spans(enabled: None) -> None:
    """CRITICAL: an `in_review` envelope still STORES raw hit spans (only blanked at the
    promotion boundary); the wire projection must blank them (§2a `span == ""`) so the
    reviewer surface never re-leaks the value `payload_view` redacts. The near-miss
    fixture carries a real `E12345` span."""
    client = _default_client()
    body = client.get("/inbox", headers=AUTH).json()
    near_miss = next(
        i for i in body["items"] if i["candidate_id"] == "candidate::reason-leakage_near_miss::0"
    )
    hits = near_miss["entity_scan"]["hits"]
    assert hits, "the near-miss fixture must carry a settled entity hit to be meaningful"
    assert all(h["span"] == "" for h in hits)
    # field/kind (the reviewer's attention context) survive the blanking.
    assert all(h["field"] and h["kind"] for h in hits)


def test_wire_summary_and_scan_never_ship_raw_entity(enabled: None) -> None:
    """HIGH: the summary is derived from the payload and must not bypass its redaction —
    when the entity is IN the statement, the one-liner must show `[redacted]`, never the
    raw value; the scan span is blanked in the same item."""
    store = InMemoryCandidateStore()
    env = _knowledge_with_entity_in_statement()
    _populate(store, [env])
    client = _client(ReviewInbox(store))

    item = client.get("/inbox", headers=AUTH).json()["items"][0]
    assert "E12345" not in item["summary"]
    assert "[redacted]" in item["summary"]
    assert "E12345" not in json.dumps(item["payload_view"])
    assert all(h["span"] == "" for h in item["entity_scan"]["hits"])


# --- auth gate (§3) -----------------------------------------------------------


def test_flag_off_every_route_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_INBOX_ENABLED", raising=False)
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)
    client = _default_client()
    assert client.get("/inbox", headers=AUTH).status_code == 404
    assert client.get("/inbox/health", headers=AUTH).status_code == 404
    assert client.post(f"/inbox/{KNOWLEDGE_ID}/reject", headers=AUTH).status_code == 404


def test_missing_token_401(enabled: None) -> None:
    client = _default_client()
    assert client.get("/inbox").status_code == 401


def test_bad_token_403(enabled: None) -> None:
    client = _default_client()
    resp = client.get("/inbox", headers={"X-Reviewer-Token": "wrong"})
    assert resp.status_code == 403


def test_unset_reviewer_token_fails_closed_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """HIGH (fail-closed auth): with the flag ON but `REVIEWER_TOKEN` UNSET, an empty
    `X-Reviewer-Token:` header must NOT pass (a `compare_digest("", "")` open door on a
    `0.0.0.0` bind = the whole write plane). The service refuses with 503 (token not
    configured), header present or not."""
    monkeypatch.setenv("REVIEW_INBOX_ENABLED", "1")
    monkeypatch.delenv("REVIEWER_TOKEN", raising=False)
    client = _default_client()
    assert client.get("/inbox", headers={"X-Reviewer-Token": ""}).status_code == 503
    assert client.get("/inbox").status_code == 503


def test_health_reports_write_plane(enabled: None) -> None:
    client = _client(ReviewInbox(InMemoryCandidateStore()), write_plane="offline")
    resp = client.get("/inbox/health", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"write_plane": "offline"}


# --- error mapping (§2/§6) ----------------------------------------------------


def test_unknown_id_404(enabled: None) -> None:
    client = _client(ReviewInbox(InMemoryCandidateStore()))
    resp = client.post("/inbox/candidate::nope::0/reject", headers=AUTH)
    assert resp.status_code == 404


def test_illegal_transition_409(enabled: None) -> None:
    # retract requires `validated`; the fixtures are all `in_review` → 409.
    client = _default_client()
    resp = client.post(f"/inbox/{KNOWLEDGE_ID}/retract", headers=AUTH)
    assert resp.status_code == 409


def test_offline_approve_of_landing_target_503(enabled: None) -> None:
    """An approve that would need to LAND but has no write plane wired is refused with
    503 ("landing plane unavailable") — NOT a faked validate (§5/§6). A blueprint that
    passes the replay gate but has `require_landing=True` + no landing writer HOLDS
    `approve_blocked_landing_unavailable`, which the service maps to 503."""
    store = InMemoryCandidateStore()
    env = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)
    _populate(store, [env])
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        policy=promotion_policy(),
        landing_writer=None,
        require_landing=True,
        clock=lambda: "2026-07-09T00:00:00+00:00",
    )
    client = _client(ReviewInbox(store, scheduler=scheduler))

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve",
        json={"token": REVIEWER_TRIAL_TOKEN},
        headers=AUTH,
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "landing plane unavailable"
    # The candidate was NOT faked into `validated`.
    assert asyncio.run(store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


def test_shipped_offline_construction_refuses_landing_approve_503(
    enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH (§5): the SHIPPED offline construction — `_build_inbox_from_env()` with no
    write plane configured — must build a scheduler with `require_landing=True` so a
    landing-target (global_knowledge) approve HOLDS → 503, NOT the default scheduler's
    fake `validated`. Exercises the real construction, not a hand-wired scheduler."""
    # Hermeticity: force OFFLINE regardless of any write-plane env a dev shell exports.
    # `_build_inbox_from_env` reads these two settings to decide the mode — stub them so
    # every credential port is empty (`write_plane_ready` is False) without depending on
    # the exact env-var names.
    monkeypatch.setattr(
        "data_agent.learning.config.LearningSettings",
        lambda: SimpleNamespace(
            # `get_learning_settings()` builds through `load_learning_settings_from_vault()`,
            # which reads this first and returns the env-built object untouched when False.
            vault_enabled=False,
            learning_candidates_username="",
            learning_candidates_password="",
            learning_corpus_username="",
            learning_corpus_password="",
        ),
    )
    monkeypatch.setattr(
        # The Vault-aware LOADER, not the class: it is `@lru_cache`d, so a patch on
        # `RuntimeSettings` underneath it is a no-op whenever the singleton is already
        # warm — and this test would then read the real environment.
        "data_agent.runtime.config.get_runtime_settings",
        lambda: SimpleNamespace(
            mcp_url="",
            token_service_url="",
            token_issuer_api_key="",
            tenant_client_code="",
            tenant_proc_center="",
            tenant_jti="",
            neo4j_url="",
            neo4j_username="",
            neo4j_password="",
            embedding_api_url="",
        ),
    )
    inbox, mode, driver = _build_inbox_from_env()
    assert mode == "offline" and driver is None
    # Seed a global_knowledge candidate into the SHIPPED store (the one the shipped
    # scheduler CAS-writes) so the approve runs through the shipped wiring.
    env = with_type(make_blueprint_candidate(status=CandidateStatus.IN_REVIEW), "global_knowledge")
    asyncio.run(inbox._store.put(env))
    client = _client(inbox)

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve",
        json={"token": REVIEWER_TRIAL_TOKEN},
        headers=AUTH,
    )
    assert resp.status_code == 503
    assert asyncio.run(inbox._store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


def test_approve_of_a_malformed_payload_is_409_not_503(enabled: None) -> None:
    """The other half of the mapping above, and the reason it was added: a landing that
    fails DETERMINISTICALLY (the payload cannot be mapped onto a seed) is not a plane
    outage. It answers 409 with an instruction the reviewer can act on — 503 "landing
    plane unavailable" invited an approve-retry that could never have succeeded."""
    store = InMemoryCandidateStore()
    env = with_type(make_blueprint_candidate(status=CandidateStatus.IN_REVIEW), "global_knowledge")
    _populate(store, [env])
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        policy=promotion_policy(),
        # The real writer's failure mode on a payload with no `statement`: its map
        # step wraps the mapper's ValueError into the TYPED LandingInvalidError.
        landing_writer=FakeLandingWriter(
            fail=LandingInvalidError("empty knowledge statement"), fail_times=99
        ),
        require_landing=True,
        clock=lambda: "2026-07-09T00:00:00+00:00",
    )
    client = _client(ReviewInbox(store, scheduler=scheduler), write_plane="full")

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve",
        json={"token": REVIEWER_TRIAL_TOKEN},
        headers=AUTH,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "candidate payload cannot be landed (malformed for its type); repair it "
        "in the store or reject it — approving again will not help"
    )
    # The candidate was NOT faked into `validated`.
    assert asyncio.run(store.get(env.candidate_id)).status == CandidateStatus.IN_REVIEW


# --- happy paths mutate the store ---------------------------------------------


def test_reject_happy_path_mutates_store(enabled: None) -> None:
    store = _store_with_all_reasons()
    client = _client(ReviewInbox(store))

    resp = client.post(f"/inbox/{KNOWLEDGE_ID}/reject", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "candidate_id": KNOWLEDGE_ID,
        "type": "global_knowledge",
        "status": CandidateStatus.REJECTED,
        "reason": None,
    }
    # Persisted (a negative signal, not a delete — D29).
    assert asyncio.run(store.get(KNOWLEDGE_ID)).status == CandidateStatus.REJECTED
    # And it has left the in_review projection.
    body = client.get("/inbox", headers=AUTH).json()
    assert all(i["candidate_id"] != KNOWLEDGE_ID for i in body["items"])


def test_approve_happy_path_returns_action_result(enabled: None) -> None:
    """Coverage gap (§2a ActionResult on a SUCCESSFUL approve): with a wired landing
    writer the approve of a global_knowledge lands then validates → 200 with the
    minimal ActionResult (status=`validated`, reason=null). Complements the reject/
    retract happy paths, which are the only ActionResult shapes asserted today."""
    store = InMemoryCandidateStore()
    env = with_type(make_blueprint_candidate(status=CandidateStatus.IN_REVIEW), "global_knowledge")
    _populate(store, [env])
    writer = FakeLandingWriter()
    scheduler = PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        policy=promotion_policy(),
        landing_writer=writer,
        require_landing=True,
        clock=lambda: "2026-07-09T00:00:00+00:00",
    )
    client = _client(ReviewInbox(store, scheduler=scheduler), write_plane="full")

    resp = client.post(
        f"/inbox/{env.candidate_id}/approve",
        json={"token": REVIEWER_TRIAL_TOKEN},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "candidate_id": env.candidate_id,
        "type": "global_knowledge",
        "status": CandidateStatus.VALIDATED,
        "reason": None,
    }
    # It actually landed (approve → retrievable is not a no-op) and validated.
    assert env.candidate_id in [e.candidate_id for e in writer.landed]
    assert asyncio.run(store.get(env.candidate_id)).status == CandidateStatus.VALIDATED


def test_retract_happy_path_mutates_store(enabled: None) -> None:
    # Seed a VALIDATED candidate directly so retract's `validated → retired` guard
    # passes without needing a live landing plane for the approve.
    doc = _load_reason_docs()["knowledge_pre_gate"]
    doc["status"] = CandidateStatus.VALIDATED
    store = InMemoryCandidateStore()
    _populate(store, [CandidateEnvelope.from_doc(doc)])
    client = _client(ReviewInbox(store))

    resp = client.post(f"/inbox/{KNOWLEDGE_ID}/retract", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == CandidateStatus.RETIRED
    assert asyncio.run(store.get(KNOWLEDGE_ID)).status == CandidateStatus.RETIRED


# --- Phase-3: validated listing + verify + promote routes ---------------------


def _validated_blueprint(*, verified: bool) -> CandidateEnvelope:
    return replace(
        make_blueprint_candidate(status=CandidateStatus.VALIDATED), verified=verified
    )


def test_list_status_validated_exposes_verified_flag(enabled: None) -> None:
    """`?status=validated` returns the promotable-learning set with `verified` on each
    wire item so the UI can tell a verified node from an auto-landed one."""
    store = InMemoryCandidateStore()
    _populate(
        store,
        [
            replace(_validated_blueprint(verified=True),
                    candidate_id="candidate::v::1", content_hash="h1"),
            replace(_validated_blueprint(verified=False),
                    candidate_id="candidate::v::0", content_hash="h0"),
        ],
    )
    body = _client(ReviewInbox(store)).get(
        "/inbox", headers=AUTH, params={"status": "validated"}
    ).json()
    assert body["count"] == 2
    by_id = {i["candidate_id"]: i for i in body["items"]}
    assert by_id["candidate::v::1"]["verified"] is True
    assert by_id["candidate::v::0"]["verified"] is False
    assert all(i["status"] == "validated" for i in body["items"])


def _writer_scheduler(store):
    return PromotionScheduler(
        store,
        probe=FakeWarehouseProbe(),
        hit_counts=FakeHitCountReader(),
        policy=promotion_policy(),
        landing_writer=FakeLandingWriter(),
        require_landing=True,
        clock=lambda: "2026-08-01T00:00:00+00:00",
    )


def test_verify_route_flips_verified(enabled: None) -> None:
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=False)
    _populate(store, [env])
    client = _client(ReviewInbox(store, scheduler=_writer_scheduler(store)))

    resp = client.post(f"/inbox/{env.candidate_id}/verify", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == CandidateStatus.VALIDATED
    assert resp.json()["node_stamped"] is True  # the neo4j node was stamped
    assert asyncio.run(store.get(env.candidate_id)).verified is True


def test_promote_route_unverified_is_409(enabled: None) -> None:
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=False)
    _populate(store, [env])
    client = _client(ReviewInbox(store))
    resp = client.post(f"/inbox/{env.candidate_id}/promote", headers=AUTH)
    assert resp.status_code == 409


def test_promote_route_returns_yaml_and_moves_to_promoted(enabled: None) -> None:
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=True)
    _populate(store, [env])
    client = _client(ReviewInbox(store))

    resp = client.post(f"/inbox/{env.candidate_id}/promote", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "yaml", "filename", "target_path", "suggested_branch", "commit_message", "note"
    }
    assert body["target_path"] == "app/corpus/data/blueprints/"
    assert body["yaml"].startswith("id:")
    assert asyncio.run(store.get(env.candidate_id)).status == CandidateStatus.PROMOTED


def test_promote_route_malformed_body_is_422(enabled: None) -> None:
    """A malformed body (a dict `doc_id`, not a string) is rejected by the Pydantic
    `PromoteRequest` model with 422 — never serialized into the emitted YAML."""
    store = InMemoryCandidateStore()
    env = _validated_blueprint(verified=True)
    _populate(store, [env])
    client = _client(ReviewInbox(store))
    resp = client.post(
        f"/inbox/{env.candidate_id}/promote", headers=AUTH, json={"doc_id": {"x": 1}}
    )
    assert resp.status_code == 422


def test_promote_route_unserializable_candidate_is_422(enabled: None) -> None:
    """A validated+verified candidate that can't be serialized to MCP YAML (a knowledge
    candidate with no `statement`) is a 422, not a 500 — fail-loud but mapped."""
    store = InMemoryCandidateStore()
    # A `global_knowledge` candidate whose payload has no `statement` → the knowledge seed
    # builder raises ValueError → the route maps it to 422.
    env = with_type(_validated_blueprint(verified=True), "global_knowledge")
    _populate(store, [env])
    client = _client(ReviewInbox(store))
    resp = client.post(f"/inbox/{env.candidate_id}/promote", headers=AUTH)
    assert resp.status_code == 422
