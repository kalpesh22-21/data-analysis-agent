#!/usr/bin/env python
"""Backfill `ValidationSnapshot` onto candidates extracted before it was stamped.

`build_candidate_envelope` now records the validation context on every kept candidate, but
candidates written before that carry none — so the §C reviser cannot propose against them and
the card says only "this candidate carries no re-validation snapshot". This walks the review
queues and rebuilds one FROM THE CANDIDATE ITSELF
(`learning/generalize/reconstruct.py`).

WHY NOT FROM THE SESSION. The obvious repair is to reload the source session and rebuild the
summary, and it is worse on two counts: sessions expire (measured on the live stack, 10 of 12
sampled were already gone at a 168h TTL), and it makes the migration depend on retention. The
candidate carries the template plus one value per slot, which is everything the rewrite
consumed, so reconstruction reached 20 of 27 where the session route reached 15.

⚠ WHAT IT WRITES IS MARKED `reconstructed=True`, and that flag is the point. Re-validating
against SQL derived from a candidate's own entries is circular — the first totality walk cannot
fail. These candidates already passed the genuine walk at extraction time; the reconstruction
records the query as it stood then, and every SUBSEQUENT revision is checked properly against
it. A reader deciding what a later `completed` outcome proves has to be able to tell a
reconstructed snapshot from a recorded one.

DRY RUN BY DEFAULT. Pass `--apply` to write. Idempotent either way: a candidate that already
has a snapshot is never touched, so a re-run after a partial failure resumes rather than
repeats.

    uv run python scripts/backfill_revalidation_snapshots.py            # report only
    uv run python scripts/backfill_revalidation_snapshots.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_agent.learning.candidate.couchbase_candidate_store import (  # noqa: E402
    CouchbaseCandidateStore,
)
from data_agent.learning.candidate.decline import (  # noqa: E402
    EvidencePointer,
    ValidationSnapshot,
)
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus  # noqa: E402
from data_agent.learning.config import get_learning_settings  # noqa: E402
from data_agent.learning.generalize.reconstruct import (  # noqa: E402
    reconstruct_accepted_sql,
)

_logger = logging.getLogger("backfill")

# The queues a reviser can act on. `validated`/`promoted` are deliberately excluded: they are
# past the point of editing, so a snapshot would buy nothing and the write would touch
# artifacts the corpus may be serving.
_STATUSES = (CandidateStatus.IN_REVIEW, CandidateStatus.NEEDS_PARAMETERIZATION)


def build_snapshot(env: CandidateEnvelope) -> ValidationSnapshot | None:
    """The reconstructed snapshot for *env*, or `None` when it cannot be rebuilt.

    The evidence pointers come from the envelope's own citations so the rebuilt snapshot
    satisfies D31's structural gate the same way a recorded one does; the QUOTES stay in
    `learning_audit` and are never copied here (D51).

    Keyed on the LAST `source_tool_call_ref`, matching the builder's "latest wins" rule — the
    same ref `summary/refs.py` would have resolved the accepted SQL under.
    """
    sql = reconstruct_accepted_sql(env.payload)
    if sql is None:
        return None
    refs = env.payload.get("source_tool_call_refs")
    ref = refs[-1] if isinstance(refs, list) and refs else "tc1"
    return ValidationSnapshot(
        session_id=env.source_session,
        # The session's user is NOT recoverable from the candidate — it was never stamped on
        # one. Left empty rather than guessed: `to_summary` only feeds it back to the
        # validation path, which does not read it, and a fabricated owner would be worse than
        # an absent one.
        user_id="",
        trace_id=env.source_trace,
        content_hash=env.content_hash,
        accepted_signal=env.payload.get("accepted_signal"),
        sql_by_ref={str(ref): (sql,)},
        evidence=(EvidencePointer(turn_ref=0, tool_call_ref=str(ref)),),
        reconstructed=True,
    )


async def run(*, apply: bool) -> int:
    settings = get_learning_settings()
    store = CouchbaseCandidateStore(settings)
    tally: collections.Counter[str] = collections.Counter()

    for status in _STATUSES:
        for env in await store.list_by_status(status, limit=500):
            if env.revalidation is not None:
                tally["already had one"] += 1
                continue
            snapshot = build_snapshot(env)
            if snapshot is None:
                tally["cannot rebuild (no single sql_template)"] += 1
                _logger.info("SKIP %s — nothing to invert", env.candidate_id)
                continue
            tally["rebuilt"] += 1
            if not apply:
                _logger.info("WOULD BACKFILL %s", env.candidate_id)
                continue
            await store.put(replace(env, revalidation=snapshot))
            _logger.info("BACKFILLED %s", env.candidate_id)

    print()
    for label, count in tally.most_common():
        print(f"  {count:4}  {label}")
    print()
    print("DRY RUN — pass --apply to write." if not apply else "Applied.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the snapshots (default: report only)"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
