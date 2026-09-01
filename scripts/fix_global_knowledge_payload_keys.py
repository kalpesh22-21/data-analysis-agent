#!/usr/bin/env python
"""Rewrite off-contract `global_knowledge` payload keys onto the contract names.

A `global_knowledge` candidate reached the inbox carrying {definition, fact_type, intent,
scope} — four plausible names, none of them the contract's. Intake only checked that the
payload WAS an object, the leakage gate scans a fixed four-field list and so never read
`definition` or `intent` at all, and the defect surfaced for the first time on APPROVE,
where `knowledge_seed_from_candidate` raised on a `statement` that had never been sent. The
scheduler could only report that as a landing failure, i.e. as infra, so the approve
answered 503 and every retry answered 503 again.

Intake now rejects that payload (`extractor/validation.py::_global_knowledge_payload`) and
the scheduler now calls the failure `landing_invalid` → 409. Neither helps a candidate that
is ALREADY in the queue: it was written before the check existed and nothing re-validates a
stored envelope. This script is the one-shot for those rows.

WHAT IT WRITES. `statement` ← the first USABLE of `definition`, `knowledge`,
`knowledge_update` (in that order — `definition` reads most like a settled fact,
`knowledge_update` most like a delta; a usable contract `statement` always wins over any
rename); `knowledge_type` ← `fact_type`; `scope`, `related_terms` and `structured` kept
as-is; and EVERY OTHER KEY DROPPED — including `intent`, which is the one a reader misses.
Dropping is deliberate, and it happens even on a row whose `statement` is already fine: the
inbox summary falls back from `intent` to `statement` (`inbox/models.py::_summary_of`), so
the card still reads, and any key outside the contract five is a text surface the leakage
gate does not scan. Keeping one "just in case" is how the unscanned surface got there.

The status is left at `in_review`. The rewrite makes the candidate APPROVABLE; it does not
approve it, and the human who does is the one attesting to the text.

DRY RUN BY DEFAULT. Pass `--apply` to write. Idempotent either way: a payload already in
its repaired form is never touched, so a re-run after a partial failure resumes rather
than repeats — and never re-derives a statement a human has since edited. Run it while
the inbox is QUIET: the write is a read-modify-`put` of the whole document, so an approve
landing in between would be reverted to `in_review` with the stale fields.

Every row lands in exactly one of three buckets, tallied separately: HEALTHY (usable
statement, on-contract keys — untouched), REWRITTEN (repaired and/or off-contract keys
dropped), UNREPAIRABLE (no statement and nothing to derive one from — a statement is
REPORTED, never invented; reject those rows in the inbox or handle them by hand).

    uv run python scripts/fix_global_knowledge_payload_keys.py                 # report only
    uv run python scripts/fix_global_knowledge_payload_keys.py --apply
    uv run python scripts/fix_global_knowledge_payload_keys.py --candidate-id candidate::x::0
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_agent.learning.candidate.couchbase_candidate_store import (  # noqa: E402
    CouchbaseCandidateStore,
)
from data_agent.learning.candidate.models import CandidateEnvelope, CandidateStatus  # noqa: E402
from data_agent.learning.config import get_learning_settings  # noqa: E402

_logger = logging.getLogger("fix-global-knowledge-keys")

# The off-contract names a `statement` may be derived from, in PRECEDENCE order: first
# usable wins, and a usable contract `statement` beats them all. Only names that carry the
# same meaning under a different word are listed; nothing else is guessed at, because a
# guess here lands text under a key the leakage gate trusts. (`definition` is the shape
# that motivated this script; `knowledge`/`knowledge_update` are the two the live store
# actually holds.)
_STATEMENT_SOURCES = ("definition", "knowledge", "knowledge_update")

# The contract keys kept verbatim when already present — the WHOLE of
# `validation.py::_GLOBAL_KNOWLEDGE_KEYS`, so that what this script writes is exactly what
# intake would now accept. Every other key is dropped.
_KEPT = ("statement", "knowledge_type", "related_terms", "structured", "scope")


def _usable(value: Any) -> bool:
    """The ONE predicate for "can this serve as the statement": a non-blank `str`.

    Used by the keep step and the derive step alike, so a present-but-unusable
    `statement` (an empty string, a number) can never SHADOW a perfectly good
    `definition` sitting next to it.
    """
    return isinstance(value, str) and bool(value.strip())


def rewrite_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The repaired payload, or `None` when there is NOTHING TO WRITE.

    `None` covers two cases the caller tells apart by whether the payload's `statement`
    is usable: HEALTHY (already in repaired form — the idempotence guard, so a re-run
    never re-derives a statement a human has since edited) and UNREPAIRABLE (no usable
    statement and no usable source; a statement is reported, never invented — a
    fabricated one would land in the global index under a human approve that believed
    it was reading the model's words).

    A payload whose `statement` is fine but which carries off-contract keys IS
    rewritten — the extras are the unscanned surfaces this script exists to remove.
    """
    # CONTRACT KEYS FIRST, then the renames FILL WHAT IS STILL MISSING — never the other
    # way round. `statement` is kept only when USABLE (the `_usable` predicate), so an
    # empty-string one falls through to the sources below; the other four are kept on
    # mere presence (they are optional downstream, and deleting content is an
    # unreviewed edit this script refuses to make).
    out: dict[str, Any] = {}
    for key in _KEPT:
        value = payload.get(key)
        if key == "statement":
            if _usable(value):
                out[key] = value
        elif value is not None:
            out[key] = value
    if not _usable(out.get("statement")):
        for source in _STATEMENT_SOURCES:
            if _usable(payload.get(source)):
                out["statement"] = payload[source]
                break
    if "knowledge_type" not in out and _usable(payload.get("fact_type")):
        out["knowledge_type"] = payload["fact_type"]
    if not _usable(out.get("statement")):
        return None  # UNREPAIRABLE — the caller tallies it as such
    if out == payload:
        return None  # HEALTHY — already exactly the repaired form
    return out


def _diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    return f"{sorted(before)} -> {sorted(after)}"


async def _targets(
    store: CouchbaseCandidateStore, candidate_id: str | None
) -> list[CandidateEnvelope]:
    """The candidates to consider: one by id, or the `in_review` global_knowledge queue.

    The queue read is deliberately NOT filtered on the payload here — `rewrite_payload` is the
    single place that decides what "broken" means, so the scan and the `--candidate-id` path
    cannot drift apart on it.
    """
    if candidate_id is not None:
        env = await store.get(candidate_id)
        if env is None:
            _logger.error("candidate %s not found", candidate_id)
            return []
        return [env]
    queue = await store.list_by_status(CandidateStatus.IN_REVIEW, limit=500)
    return [env for env in queue if env.type == "global_knowledge"]


# The three outcome buckets (plus the wrong-type skip). Tallied SEPARATELY because they
# demand different things of the operator: healthy = nothing, rewritten = re-review +
# approve, unrepairable = reject or hand-edit — a single "skipped" bucket once reported
# a permanently 409-stuck row as fine.
_HEALTHY = "healthy (usable statement, on-contract keys)"
_REWRITTEN = "rewritten"
_UNREPAIRABLE = "unrepairable: no statement and nothing to derive one from"


async def run(*, apply: bool, candidate_id: str | None) -> int:
    settings = get_learning_settings()
    store = CouchbaseCandidateStore(settings)
    tally: collections.Counter[str] = collections.Counter()
    unrepairable: list[str] = []
    rewrote = 0

    for env in await _targets(store, candidate_id):
        if env.type != "global_knowledge":
            tally[f"skipped (type={env.type})"] += 1
            continue
        rewritten = rewrite_payload(env.payload)
        if rewritten is None:
            if _usable(env.payload.get("statement")):
                tally[_HEALTHY] += 1
                _logger.info("HEALTHY %s — %s", env.candidate_id, sorted(env.payload))
            else:
                # No statement and no source: this row will answer 409 to every approve
                # until a human rejects it (or hand-writes a payload). Listed by id at
                # the end so the operator cannot read a clean report over it.
                tally[_UNREPAIRABLE] += 1
                unrepairable.append(env.candidate_id)
                _logger.warning(
                    "UNREPAIRABLE %s — keys %s hold no usable statement source; "
                    "reject it in the inbox or repair it by hand",
                    env.candidate_id,
                    sorted(env.payload),
                )
            continue
        tally[_REWRITTEN] += 1
        # Every key that does not survive — renamed sources consumed into `statement`
        # and off-contract extras alike — named so the operator sees exactly what text
        # left the envelope.
        dropped = sorted(set(env.payload) - set(rewritten))
        _logger.info(
            "%s %s  keys %s%s",
            "REWROTE" if apply else "WOULD REWRITE",
            env.candidate_id,
            _diff(env.payload, rewritten),
            f"  (removed: {dropped})" if dropped else "",
        )
        if not apply:
            continue
        # Status UNTOUCHED (`in_review`): this makes the candidate approvable, it does not
        # approve it. `dataclasses.replace` keeps every other field — provenance, the
        # settled entity scan, drift — exactly as stored; the document key IS the
        # candidate_id, so the `put` overwrites in place.
        await store.put(replace(env, payload=rewritten))
        rewrote += 1

    print()
    for label, count in tally.most_common():
        print(f"  {count:4}  {label}")
    print()
    print("DRY RUN — pass --apply to write." if not apply else f"Applied to {rewrote}.")
    if unrepairable:
        print("\nUNREPAIRABLE — manual action (reject, or hand-repair) remains for:")
        for cid in unrepairable:
            print(f"  {cid}")
    if tally[_REWRITTEN]:
        print(
            "\n⚠ The text now sitting under `statement` was scanned for entities under a "
            "key the leakage gate does not read, which is to say it was never scanned at "
            "all. Nothing re-runs the gate on a stored envelope, so the HUMAN APPROVE is "
            "the attestation: read the statement on the card before you click it."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the payloads (default: report only)"
    )
    parser.add_argument(
        "--candidate-id",
        default=None,
        help="repair exactly this candidate (default: scan the in_review queue)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(run(apply=args.apply, candidate_id=args.candidate_id))


if __name__ == "__main__":
    raise SystemExit(main())
