"""Seeded inbox app for the Layer-3 review-inbox E2E (Flow 2).

A module-level `app` a uvicorn subprocess (`tests.e2e._seeded_inbox_app:app`)
serves so the reviewer inbox has DETERMINISTIC, infra-free content to drive with
Playwright — no live neo4j / couchbase / MCP / embedding.

Two `in_review` candidates are seeded into an `InMemoryCandidateStore` at import:

  * a `blueprint` (the frozen S4 single-blueprint fixture), and
  * a `global_knowledge` re-type of the same fixture,

with DISTINCT `candidate_id`/`content_hash` so the store holds two separate docs
(the store keys by `candidate_id`; a re-typed clone alone would collide on the
fixture's shared id and only ONE would persist).

The scheduler is wired EXACTLY like
`tests/learning/inbox/test_inbox_service.py::test_approve_happy_path_returns_action_result`
— the `FakeWarehouseProbe` returns the fixture's matching columns (so the
blueprint passes static + golden-replay), and the `FakeLandingWriter` +
`require_landing=True` land BOTH types with no live infra. Human approve
substitutes for the hit-count threshold, so both approve to `validated`.

`write_plane="full"` is the key seam: `GET /inbox/health` reports `full`, so the
inbox page ENABLES the approve button (offline mode pre-disables it). The
`REVIEW_INBOX_ENABLED` + `REVIEWER_TOKEN` auth gate is enforced by the app itself
(set in the subprocess env by the `running_inbox_stack` fixture), exactly as in
production — nothing is bypassed here.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from data_agent.learning.candidate.memory_candidate_store import InMemoryCandidateStore
from data_agent.learning.candidate.models import CandidateStatus
from data_agent.learning.inbox import ReviewInbox
from data_agent.learning.inbox.service import create_inbox_app
from data_agent.learning.promotion import PromotionScheduler

# Import path note: this module is loaded as `tests.e2e._seeded_inbox_app` (uvicorn
# run from the repo root, which is on sys.path), so `tests` is an importable package
# and the promotion helpers resolve as an absolute import.
from tests.learning.promotion.helpers import (
    FakeHitCountReader,
    FakeLandingWriter,
    FakeWarehouseProbe,
    make_blueprint_candidate,
    promotion_policy,
    with_type,
)

# --- seed two distinct in_review candidates ----------------------------------

# The blueprint keeps the frozen fixture's id; the knowledge clone gets a DISTINCT
# candidate_id + content_hash so both persist as separate store docs.
_bp = make_blueprint_candidate(status=CandidateStatus.IN_REVIEW)
_kn = with_type(
    replace(
        make_blueprint_candidate(status=CandidateStatus.IN_REVIEW),
        candidate_id="candidate::hash-fixture-knowledge::0",
        content_hash="hash-fixture-knowledge",
    ),
    "global_knowledge",
)

store = InMemoryCandidateStore()


async def _seed() -> None:
    await store.put(_bp)
    await store.put(_kn)


asyncio.run(_seed())


async def _reseed() -> None:
    """Reset the in-memory store to the pristine two `in_review` seeds. The originals
    (`_bp`/`_kn`) are frozen dataclasses that approve/reject never mutate (they
    `replace()` into new docs), so re-putting them restores the review queue and clears
    any rejected/validated rows a prior test left behind."""
    store._by_id.clear()
    await _seed()

# --- wire the fakes-backed scheduler (no live infra) --------------------------

scheduler = PromotionScheduler(
    store,
    probe=FakeWarehouseProbe(),
    hit_counts=FakeHitCountReader(),
    policy=promotion_policy(),
    landing_writer=FakeLandingWriter(),
    require_landing=True,
    clock=lambda: "2026-07-09T00:00:00+00:00",
)

# `write_plane="full"` ⇒ `GET /inbox/health` reports full ⇒ the UI enables approve.
app = create_inbox_app(inbox=ReviewInbox(store, scheduler=scheduler), write_plane="full")


# TEST-ONLY reset hook (never mounted by production `create_inbox_app`): the session
# store is shared + mutable across the whole `tests/e2e` run, so the Playwright suite
# POSTs here before EACH test to restore the pristine two-candidate seed — making the
# tests order-independent despite the one long-lived subprocess. No reviewer guard:
# it exists only on this seeded app, bound to a fresh local test port.
@app.post("/_test/reseed")
async def _test_reseed() -> dict[str, bool]:
    await _reseed()
    return {"ok": True}
