"""The `composes_json` allowlist, checked against `Node.parse` BY OBSERVATION.

`test_compose_projection.py::test_the_allowlist_is_exactly_what_node_parse_reads`
asserts `_NODE_DOC_FIELDS` against a HAND-WRITTEN literal set of the keys
`Node.parse` reads. That direction is real (it catches an allowlist edit) but it is
blind in the direction that actually breaks landing: add `raw.get("retry_policy")` to
`Node.parse` tomorrow and the test still passes, because BOTH sides of its assertion
are frozen constants — the literal set is a copy of `Node.parse`, not a reading of it.

This file derives the set instead, by handing `Node.parse` a dict that records every
key it touches. A field `Node.parse` starts reading that the allowlist omits now
fails here immediately, which is the failure the allowlist exists to prevent (the
projected node would silently lose it and land degraded).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.extractor.models import ComposeNodePlan
from data_agent.learning.generalize.mapping import (
    _NODE_DOC_FIELDS,
    blueprint_from_generalization,
    blueprint_seed_from_candidate,
)
from data_agent.runtime.blueprint.models import BlueprintParseError, Node

from ..promotion.helpers import FIXTURES
from .helpers import load_expected, load_plan

_LANDING_ID = "bp::sha256:composite-bp"

# Joined in from the S4 `NodeTemplate`s by `_compose_docs`, deliberately NOT copied
# from the plan.
_JOINED_IN_BY_S4 = {"sql_template"}

# Keys `Node.parse` reads in order to REJECT them. Not allowlist candidates — carrying
# one into `composes_json` would land the very thing the parse layer exists to refuse.
#
# HISTORY: this set did not exist until plan §2b added `ref` (a `composes` node naming
# another blueprint, inlined by `corpus_loader.resolve_blueprint_references` at load).
# `Node.parse` reads it only to raise, because a reference surviving to the parse layer
# means resolution was skipped and the node would otherwise parse as a silently
# template-less step. This test failed the moment that read landed, which is the whole
# point of deriving the set by observation — but the fix was NOT to allowlist the key.
# The learning loop authors no references (the S3 `ComposeNodePlan` has no such field),
# and allowlisting `ref` would give a candidate a way to put an unresolved reference
# into the governed corpus. So it is excluded HERE, next to the reason.
_READ_ONLY_TO_REJECT = {"ref"}


class _KeyRecordingDict(dict):
    """A dict that remembers every key looked up through `.get()`/`[]` — `Node.parse`
    reads its input only through those two."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def get(self, key: Any, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self.read.add(key)
        return super().__getitem__(key)


def _keys_node_parse_reads() -> set[str]:
    """Every key `Node.parse` touches on a node exercising ALL of its branches — a
    `when` clause and a `requires_approval` object included, since both are read
    conditionally and would otherwise go unobserved."""
    probe = _KeyRecordingDict(
        {
            "order": 1,
            "node_kind": "approval",
            "feeds_from": [0],
            "consumes": {"t": "$0.total"},
            "output": {"n": "scalar"},
            "sql_template": "SELECT 1 AS n",
            "when": {"expr": "n > 0", "on_violation": "abort", "message": "stop"},
            "requires_approval": {"reason": "spend"},
            # Decoys: present on the plan, must never be read by the runtime parse.
            "step_intent": "free model prose",
            "source_tool_call_ref": "tc1",
        }
    )
    parsed = Node.parse(probe)
    assert parsed.order == 1 and parsed.node_kind == "approval"
    return probe.read


def test_the_allowlist_is_derived_from_what_node_parse_actually_reads() -> None:
    read = _keys_node_parse_reads()
    assert read - _JOINED_IN_BY_S4 - _READ_ONLY_TO_REJECT == set(_NODE_DOC_FIELDS), (
        "`Node.parse` and `_NODE_DOC_FIELDS` disagree. A key Node.parse reads that is "
        "missing from the allowlist is SILENTLY DROPPED at landing; a key in the "
        "allowlist that Node.parse never reads is un-audited weight in the corpus. If "
        "the new key is one Node.parse reads only to REJECT, add it to "
        "`_READ_ONLY_TO_REJECT` with the reason — do NOT allowlist it."
    )


def test_a_landed_candidate_can_never_carry_an_unresolved_blueprint_reference() -> None:
    """The other side of `_READ_ONLY_TO_REJECT`: the landing projection must not pass a
    `ref` through, and the parse layer must refuse one if it somehow did.

    Blueprint references (plan §2b) are an MCP-canon authoring feature resolved by the
    corpus loader; the learning tier neither authors nor resolves them. A candidate that
    smuggled one in would land a compose node with no SQL at all."""
    marker = "bp-some-other-blueprint"
    env = _composite_candidate(ref={"blueprint": marker})
    seed = blueprint_seed_from_candidate(env, id=_LANDING_ID)
    assert all("ref" not in node for node in seed.composes)
    assert marker not in json.dumps(seed.composes)

    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 0, "ref": {"blueprint": marker}})


def test_node_parse_reads_nothing_the_projection_deliberately_drops() -> None:
    read = _keys_node_parse_reads()
    assert "step_intent" not in read
    assert "source_tool_call_ref" not in read


def _composite_candidate(**node_overrides: Any) -> CandidateEnvelope:
    doc = copy.deepcopy(
        json.loads((FIXTURES / "s4_enriched_blueprint.json").read_text())["single"]["envelope"]
    )
    payload = copy.deepcopy(load_plan()["composite"])
    payload["generalization"] = copy.deepcopy(load_expected()["composite"]["generalization"])
    for node in payload["composes"]:
        node.update(node_overrides)
    doc["payload"] = payload
    return CandidateEnvelope.from_doc(doc)


@pytest.mark.parametrize(
    "field",
    [
        # Every field a future `ComposeNodePlan` could plausibly grow, plus the two
        # already-dropped ones re-checked from the other side.
        "step_intent",
        "source_tool_call_ref",
        "retry_policy",
        "cost_estimate",
        "analyst_note",
        "sid",
        "_debug",
    ],
)
def test_no_unallowlisted_plan_field_reaches_either_landing_surface(field: str) -> None:
    """Both surfaces `_compose_docs` feeds: the EXECUTABLE `Blueprint` and the LANDED
    `BlueprintSeed`. A leak into either is a leak into the governed corpus."""
    marker = "employee-4711-secret"
    env = _composite_candidate(**{field: marker})

    seed = blueprint_seed_from_candidate(env, id=_LANDING_ID)
    assert marker not in json.dumps(seed.composes)
    assert all(field not in node for node in seed.composes)

    from data_agent.learning.candidate.generalization import BlueprintGeneralization

    gen = BlueprintGeneralization.from_doc(env.payload["generalization"])
    blueprint = blueprint_from_generalization(env.payload, gen, id=_LANDING_ID)
    assert all(not hasattr(node, field) for node in blueprint.composes)


def test_every_allowlisted_field_actually_survives_the_projection() -> None:
    """The other half: an allowlist entry that silently fails to carry would be an
    invisible drop. Assert each one arrives with its plan value."""
    env = _composite_candidate(
        node_kind="approval",
        requires_approval={"reason": "spend over threshold"},
    )
    landed = blueprint_seed_from_candidate(env, id=_LANDING_ID).composes
    assert landed
    for node, plan_node in zip(landed, env.payload["composes"], strict=True):
        for field in _NODE_DOC_FIELDS:
            assert field in node, f"allowlisted {field!r} did not survive the projection"
            assert node[field] == plan_node[field]


def test_the_plan_model_carries_every_allowlisted_field() -> None:
    """The allowlist is applied with `if key in node`, so a field the PLAN never emits
    is silently absent rather than an error. Pin that all seven are real
    `ComposeNodePlan.to_doc()` keys — otherwise the allowlist could quietly describe a
    field that no longer exists."""
    plan_keys = set(
        ComposeNodePlan(order=0, node_kind="query", step_intent="").to_doc()
    )
    assert set(_NODE_DOC_FIELDS) <= plan_keys


def test_the_plans_when_field_is_a_string_but_node_parse_needs_an_object() -> None:
    """DOCUMENTED, not filed: `when` is allowlisted (Node.parse reads it) but the S3
    `ComposeNodePlan.when` is a bare `str`, which `WhenClause.parse` refuses. Today
    nothing hits it — `builder._generalize_composite` declines ANY when-bearing
    composite with `when_bearing_composite` before the projection runs — so `when` is
    an allowlist entry that is currently unreachable-by-construction. Whoever lifts
    that decline must type-map `when` in `_compose_docs`, not just delete the guard."""
    with pytest.raises(BlueprintParseError):
        Node.parse({"order": 0, "when": "row_count > 0"})
