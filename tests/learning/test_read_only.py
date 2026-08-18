"""D72 read-only: a learning transition writes ONLY `learning_status`
(+ `learning_content_hash`) (design §3/§7, task item 12).

The sweeper and consumer are read-only w.r.t. request-path data: after a full
sweep -> consume round-trip, the messages, tool_trail, results, created_at and —
critically — `last_activity` are BYTE-UNCHANGED. Bumping `last_activity` would
resurrect the idle session and defeat idle detection.
"""

from __future__ import annotations

import copy

from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.models import LearningStatus, compute_content_hash
from data_agent.learning.sweeper import LearningSweeper
from data_agent.runtime.session.models import ResultPreview

from .conftest import make_message, make_trail_entry


async def test_full_cycle_mutates_only_lifecycle_flag(store, queue, settings, seed_session):
    preview = ResultPreview(columns=["salary"], row_count=1, truncated=False, preview_rows=[[85000]])
    doc = seed_session(
        store, "sess-1",
        created_at="2000-01-01T00:00:00+00:00",
        last_activity="2000-01-01T00:00:00+00:00",
        messages=[
            make_message(0, "user", "Jane Doe's salary?"),
            make_message(0, "assistant", "Jane Doe earns $85,000."),
        ],
        tool_trail=[
            make_trail_entry(
                args={"sql": "SELECT salary FROM hr.pay WHERE name='Jane Doe'"},
                result_preview=preview,
                result_full_ref="result::abc-123",
                provenance=frozenset({("hr.pay", "salary")}),
            )
        ],
    )
    # Snapshot every request-path field before the learning loop touches it.
    before_messages = copy.deepcopy(doc.messages)
    before_trail = copy.deepcopy(doc.tool_trail)
    before_created = doc.created_at
    before_last_activity = doc.last_activity
    before_checkpoint = copy.deepcopy(doc.pause_checkpoint)
    expected_hash = compute_content_hash(doc)

    sweeper = LearningSweeper(store, queue, settings)
    consumer = LearningConsumer(store, queue, settings)
    await sweeper.run_once()
    await consumer.run_once()

    after = store._docs["sess-1"]

    # ONLY the lifecycle flag + its recorded hash changed.
    assert after.learning_status == LearningStatus.DONE
    assert after.learning_content_hash == expected_hash

    # Everything request-path is byte-identical.
    assert after.messages == before_messages
    assert after.tool_trail == before_trail
    assert after.created_at == before_created
    assert after.last_activity == before_last_activity  # NOT bumped -> no resurrection
    assert after.pause_checkpoint == before_checkpoint
    # The full-result pointer + preview on the trail entry are untouched.
    assert after.tool_trail[0].result_full_ref == "result::abc-123"
    assert after.tool_trail[0].result_preview == preview


async def test_claim_does_not_bump_last_activity(store, queue, settings, seed_session):
    doc = seed_session(store, "sess-1", last_activity="2000-01-01T00:00:00+00:00",
                       messages=[make_message(0, "user", "hi")])
    before = doc.last_activity
    sweeper = LearningSweeper(store, queue, settings)
    await sweeper.run_once()
    # After claim + enqueue the session is `queued` but its idle timestamp is
    # unchanged (a bump would make the next sweep think it was resumed).
    assert store._docs["sess-1"].learning_status == LearningStatus.QUEUED
    assert store._docs["sess-1"].last_activity == before
