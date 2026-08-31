"""Envelope verdict sub-records — the frozen wire format the write-router stages stamp on.

Each verdict is written by exactly ONE stage and read by others; no stage mutates another
stage's field (the D102 additivity rule). `LeakageVerdict` → `entity_scan` (S5 writes; S3
seeds a `{result:"pending"}` self-check dict, which is why the envelope field stays a plain
dict — this dataclass freezes only the SETTLED shape). `DedupVerdict` → `dedup` (S6).
`DriftStamp` → `drift` (S9). All are pure frozen value objects with `to_doc`/`from_doc`
round-trip fidelity; no stage logic lives here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

# --- Contract B — leakage verdict (S5 WRITES · S7 READS) ----------------------

LeakageResult = Literal["pass", "reroute", "quarantine", "reject"]

# The SETTLED verdict results S5 writes. The S3 pre-leakage self-check uses a
# distinct sentinel (`{"result": "pending", ...}`) whose `hits` are bare strings,
# NOT `EntityHit` dicts — so a caller MUST `LeakageVerdict.is_settled(doc)` before
# `from_doc` to avoid parsing a pending self-check as a settled verdict.
_SETTLED_RESULTS: frozenset[str] = frozenset({"pass", "reroute", "quarantine", "reject"})


@dataclass(frozen=True)
class EntityHit:
    """One entity the leakage scan found in the payload.

    `span` is entity-bearing — audit-store and reviewer context only, stripped before any
    promotion (D17).
    """

    field: str  # where in the payload it was found
    kind: str  # employee_code | dept_code | person | date | region | ...
    span: str  # the offending substring

    def to_doc(self) -> dict[str, Any]:
        return {"field": self.field, "kind": self.kind, "span": self.span}

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> EntityHit:
        return cls(
            field=doc.get("field", ""),
            kind=doc.get("kind", ""),
            span=doc.get("span", ""),
        )


@dataclass(frozen=True)
class LeakageVerdict:
    """The authoritative S5 leakage verdict, serialized into `envelope.entity_scan`."""

    result: LeakageResult
    hits: tuple[EntityHit, ...] = ()
    scanned_fields: tuple[str, ...] = ()  # e.g. ("intent","result_signature") — audit trail
    scanner: str = ""  # "regex+ner+llm" — provenance of the verdict

    def to_doc(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "hits": [h.to_doc() for h in self.hits],
            "scanned_fields": list(self.scanned_fields),
            "scanner": self.scanner,
        }

    @classmethod
    def is_settled(cls, doc: dict[str, Any]) -> bool:
        """True iff *doc* is a SETTLED S5 verdict, False for the S3 `pending` sentinel.

        S5/S7 call this BEFORE `from_doc` to distinguish the two `entity_scan` shapes.
        """
        return doc.get("result") in _SETTLED_RESULTS

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> LeakageVerdict:
        result = doc.get("result")
        if result not in _SETTLED_RESULTS:
            # Fail LOUD: parsing a `pending` self-check (or any unknown result)
            # as a settled verdict would silently yield an out-of-literal
            # `result` and choke on its bare-string `hits`. Guard the boundary.
            raise ValueError(
                f"LeakageVerdict.from_doc: result {result!r} is not a settled "
                f"verdict {sorted(_SETTLED_RESULTS)}; use is_settled() first "
                f"(a pending S3 self-check is not a LeakageVerdict)"
            )
        return cls(
            result=result,
            hits=tuple(EntityHit.from_doc(h) for h in doc.get("hits", []) or []),
            scanned_fields=tuple(doc.get("scanned_fields", []) or []),
            scanner=doc.get("scanner", ""),
        )


# --- Contract C — dedup verdict (S6 WRITES · S7/S9 READ) ----------------------


@dataclass(frozen=True)
class DedupVerdict:
    """The D48 dedup verdict, serialized into `envelope.dedup`.

    Three layers produce one and the `layer` tag says which, because the actions are not
    interchangeable across them: `hard` is the frozen canonical key over the corpus bucket,
    race-safe by construction, and the ONLY layer that may emit `increment`; `structural` is the
    loose cross-authoring-path key through the `PriorArtIndex`, deterministic but reaching
    artifacts the loop does not own, emitting `redundant_with_canon` or `merge`; `soft` is
    embedding near-miss adjudication — a hint, never a drop.

    `redundant_with_canon` exists because `increment` is WRONG against the canon: the hit count
    lives on a learning-corpus artifact, and a git-versioned MCP blueprint has none. The candidate
    is dropped instead, and the verdict is what makes the drop COUNTABLE — a high rate is a
    RETRIEVAL defect surfacing here, not a learning-loop success.
    """

    canonical_key: str  # sha256 over (resolves, uses_rules, result_grain, canonical_ast_norm)
    matched_id: str | None  # existing artifact this collided with, or None
    similarity: float  # 0.0–1.0 (1.0 for a hard-key or structural-key hit)
    action: Literal["insert", "increment", "merge", "conflict", "redundant_with_canon"]
    layer: Literal["hard", "structural", "soft"]  # which layer produced the verdict

    def to_doc(self) -> dict[str, Any]:
        return {
            "canonical_key": self.canonical_key,
            "matched_id": self.matched_id,
            "similarity": self.similarity,
            "action": self.action,
            "layer": self.layer,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> DedupVerdict:
        return cls(
            canonical_key=doc["canonical_key"],
            matched_id=doc.get("matched_id"),
            similarity=float(doc.get("similarity", 0.0)),
            action=doc["action"],
            layer=doc["layer"],
        )


# --- Contract E — drift stamp (S9 WRITES · all READ) --------------------------

DriftStatus = Literal["clean", "suspect", "stale", "unchecked"]


@dataclass(frozen=True)
class DriftStamp:
    """The D43 drift stamp, serialized into `envelope.drift`.

    Defaults to `unchecked`: a pre-S9 candidate is drift-unchecked, never silent-eligible.
    """

    status: DriftStatus = "unchecked"
    last_drift_check_at: str | None = None
    probes: tuple[str, ...] = ()  # grain_integrity | catalog_conformance | rule_currency
    failed_probe: str | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_drift_check_at": self.last_drift_check_at,
            "probes": list(self.probes),
            "failed_probe": self.failed_probe,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> DriftStamp:
        return cls(
            status=doc.get("status", "unchecked"),
            last_drift_check_at=doc.get("last_drift_check_at"),
            probes=tuple(doc.get("probes", []) or []),
            failed_probe=doc.get("failed_probe"),
        )


# --- The human attestation over a leakage finding (reviewer override) ---------


def leakage_fingerprint(scan: Any) -> str:
    """A digest of exactly WHAT a settled verdict found: the (field, kind, span) triples.

    ⚠ THIS IS THE BINDING, and it is the whole safety of the override. An attestation says "I
    read these findings and they are not entities" — a statement about SPECIFIC content, not a
    permanent property of the candidate. Bind it to anything looser and the attestation
    outlives its subject: a reviewer clears a false positive, a revision then introduces a real
    entity, the gate re-settles with new hits, and a stale "I checked this" would keep it open.

    Sorted, so ordering noise from a re-scan cannot invalidate an honest attestation; over the
    SPAN as well as the field/kind, because two different values in the same field are two
    different judgements. `""` for anything unsettled — nothing to attest to.
    """
    if not LeakageVerdict.is_settled(scan):
        return ""
    hits = scan.get("hits") if isinstance(scan, dict) else None
    triples = sorted(
        (
            str(h.get("field") or ""),
            str(h.get("kind") or ""),
            str(h.get("span") or ""),
        )
        for h in (hits or [])
        if isinstance(h, dict)
    )
    payload = json.dumps(
        {"result": (scan or {}).get("result"), "hits": triples}, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LeakageAttestation:
    """A reviewer's statement that a settled leakage finding is a FALSE POSITIVE.

    Additive, and it NEVER rewrites the verdict: the scanner's finding is the durable record of
    what a machine saw, and a human disagreeing with it is a second fact, not a correction of
    the first. Both stay on the envelope.

    ⚠ WHAT IT UNBLOCKS IS DELIBERATELY NARROW — one reader, `inbox/models.py::_leakage_cleared`,
    which gates the assistant and the decline-detail display. It does NOT feed
    `_entity_scan_is_clean`, the AUTOMATIC promotion edge, and the distinction is the one
    `promotion/scheduler.py` already draws: that predicate exists for the path where "nobody is
    looking there", and an attestation is precisely a statement that somebody looked. Nor does
    it stop `redact_payload` — the strip costs nothing if the attestation is right and saves
    everything if it is wrong.

    ⚠ `attested_by` IS NOT A USER. The inbox authenticates with a single shared
    `REVIEWER_TOKEN`; there is no per-reviewer identity to record, and inventing a field that
    looked like one would be worse than admitting it. What is recorded is that an authenticated
    reviewer did this, when, and why — and `note` is mandatory at the route, because an
    override with no stated reason is not an audit trail.
    """

    scan_fingerprint: str  # `leakage_fingerprint` of the verdict this attests to
    attested_at: str  # ISO-8601
    note: str  # the reviewer's reason — REQUIRED; see the class docstring
    hit_count: int = 0  # how many findings were covered, for the card and for queries
    attested_by: str = "reviewer-token"  # see the class docstring: NOT an identity

    def applies_to(self, scan: Any) -> bool:
        """Does this attestation still describe *scan*?

        False the moment the findings change, which is what stops a cleared false positive from
        covering a later real one. An empty fingerprint on either side never matches.
        """
        current = leakage_fingerprint(scan)
        return bool(current) and current == self.scan_fingerprint

    def to_doc(self) -> dict[str, Any]:
        return {
            "scan_fingerprint": self.scan_fingerprint,
            "attested_at": self.attested_at,
            "note": self.note,
            "hit_count": self.hit_count,
            "attested_by": self.attested_by,
        }

    @classmethod
    def from_doc(cls, doc: Any) -> LeakageAttestation | None:
        """Rehydrate, or `None` for anything unusable.

        A fingerprint is the one field with no safe default: without it `applies_to` cannot
        bind, and an attestation that binds to nothing would apply to everything.
        """
        if not isinstance(doc, dict):
            return None
        fingerprint = doc.get("scan_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        count = doc.get("hit_count")
        return cls(
            scan_fingerprint=fingerprint,
            attested_at=str(doc.get("attested_at") or ""),
            note=str(doc.get("note") or ""),
            hit_count=count if isinstance(count, int) and not isinstance(count, bool) else 0,
            attested_by=str(doc.get("attested_by") or "reviewer-token"),
        )
