"""Entity redaction on the promotion boundary (D17) — the SINGLE strip used by both
the human-approve path (`promotion.scheduler`) and the reviewer projection
(`inbox.models`), so the "no entity crosses into a global store" invariant is
enforced identically wherever a candidate is promoted or surfaced.

Two complementary strips, both keyed off the settled `LeakageVerdict.hits`:

  * `blank_scan_spans` — blank the audit `entity_scan` hit `span`s (keep field/kind
    for the audit trail) so the verdict record stops carrying the raw value.
  * `redact_payload` — remove the entity substrings from EVERY text leaf of the
    payload, so the promotable payload a physical promoter reads (and the reviewer
    view) is entity-free — not just the audit spans (the earlier gap, QA-Q3/Q7).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import CandidateEnvelope
from .verdicts import EntityHit, LeakageVerdict

# The token a stripped entity span is replaced with in the payload. Non-empty so a
# reviewer can see that a value WAS present (and where) without the raw value.
_REDACTED = "[redacted]"


def _entity_spans(env: CandidateEnvelope) -> tuple[str, ...]:
    """The distinct, non-empty entity `span`s the settled S5 verdict found."""
    scan = env.entity_scan
    if not LeakageVerdict.is_settled(scan):
        return ()
    verdict = LeakageVerdict.from_doc(scan)
    return tuple({h.span for h in verdict.hits if h.span})


def redact_payload(payload: dict[str, Any], spans: tuple[str, ...]) -> dict[str, Any]:
    """Return a copy of *payload* with every occurrence of each entity *span*
    removed from all string leaves (recursively). No-op when there are no spans."""
    if not spans:
        return dict(payload)

    def _redact(obj: Any) -> Any:
        if isinstance(obj, str):
            out = obj
            for span in spans:
                out = out.replace(span, _REDACTED)
            return out
        if isinstance(obj, dict):
            return {k: _redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(v) for v in obj]
        return obj

    return _redact(payload)


def blank_scan_spans(env: CandidateEnvelope) -> CandidateEnvelope:
    """Return *env* with the audit `entity_scan` hit spans blanked (field/kind kept).
    No-op when `entity_scan` is unsettled or carries no hits."""
    scan = env.entity_scan
    if not LeakageVerdict.is_settled(scan):
        return env
    verdict = LeakageVerdict.from_doc(scan)
    if not verdict.hits:
        return env
    blanked = LeakageVerdict(
        result=verdict.result,
        hits=tuple(EntityHit(field=h.field, kind=h.kind, span="") for h in verdict.hits),
        scanned_fields=verdict.scanned_fields,
        scanner=verdict.scanner,
    )
    return replace(env, entity_scan=blanked.to_doc())


def strip_entity_bearing(env: CandidateEnvelope) -> CandidateEnvelope:
    """Strip entity-bearing data from BOTH the payload AND the audit `entity_scan`
    spans (D17) — the full strip applied before a candidate is stamped `validated`.
    The payload strip closes the gap where blanking only the audit spans left the
    raw entity in `payload.intent` for a physical promoter to read (QA-Q3)."""
    spans = _entity_spans(env)
    stripped = replace(env, payload=redact_payload(env.payload, spans))
    return blank_scan_spans(stripped)


def entity_free_payload_view(env: CandidateEnvelope) -> dict[str, Any]:
    """The reviewer payload view with entity spans redacted (QA-Q7) — the inbox
    surface must not re-expose the raw value even while the candidate is `in_review`."""
    return redact_payload(env.payload, _entity_spans(env))
