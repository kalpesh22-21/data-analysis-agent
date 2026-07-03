"""S8 schema-edit PR bot (D53/D18) — Layer-1.

Covers: opens a branch + YAML-patch PR via the INJECTED git client (asserted,
never a real GitHub call), routes to human review (in_review) instead of
auto-committing to the catalog, and on a failed CI gate opens no PR yet still
routes to review — never an auto-commit either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.schema_edit import (
    CheckResult,
    PullRequestResult,
    PullRequestSpec,
    SchemaEditPatch,
    SchemaEditPRStage,
)
from data_agent.learning.stage import StageContext
from data_agent.learning.triage import TriageVerdict

from ..extractor.helpers import make_summary

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "learning"
_KEEP = TriageVerdict(decision="keep", reason="K1", target_hints=("schema",))


def _schema_edit_candidate() -> CandidateEnvelope:
    with (_FIXTURES / "s3_schema_edit.json").open() as fh:
        return CandidateEnvelope.from_doc(json.load(fh))


def _ctx() -> StageContext:
    return StageContext(summary=make_summary(), verdict=_KEEP)


@dataclass
class ScriptedGitClient:
    """A `GitPullRequestClient` double — records every PR request, opens no real
    branch, makes NO network call."""

    specs: list[PullRequestSpec] = field(default_factory=list)
    number: int = 42

    async def open_pull_request(self, spec: PullRequestSpec) -> PullRequestResult:
        self.specs.append(spec)
        return PullRequestResult(
            url=f"https://example.test/pr/{self.number}",
            number=self.number,
            branch=spec.branch,
        )


class ExplodingGitClient:
    """Fails LOUD if called — proves a path that must NOT open a PR never does."""

    async def open_pull_request(self, spec: PullRequestSpec) -> PullRequestResult:
        raise AssertionError("open_pull_request must not be called on this path")


@dataclass
class FailingChecks:
    reason: str = "lint: unknown rule field"

    async def run(self, patch: SchemaEditPatch) -> CheckResult:
        return CheckResult(ok=False, lint_ok=False, explain_ok=True, reason=self.reason)


# --- opens PR, never auto-commits --------------------------------------------


async def test_opens_pr_and_routes_to_review_not_autocommit():
    git = ScriptedGitClient()
    stage = SchemaEditPRStage(git_client=git)
    result = await stage.process(_schema_edit_candidate(), _ctx())

    # the injected git client WAS called exactly once (a real GitHub call would be
    # a different, network-backed client — never wired in a unit test)
    assert len(git.specs) == 1
    spec = git.specs[0]
    assert spec.branch == "learning/schema-edit/hash-schemaedit"
    assert spec.patch.startswith("rules:")  # the proposed YAML patch
    assert spec.base == "main"

    # routes to human review (the MERGE is the gate) — NOT auto-committed
    assert result.control == "route_inbox"
    assert result.envelope.status == "in_review"
    review = result.envelope.payload["schema_edit_review"]
    assert review["pr_opened"] is True
    assert review["pr_number"] == 42
    assert review["pr_url"].endswith("/pr/42")


async def test_pr_body_carries_provenance_and_no_automerge_notice():
    git = ScriptedGitClient()
    stage = SchemaEditPRStage(git_client=git)
    await stage.process(_schema_edit_candidate(), _ctx())
    body = git.specs[0].body
    assert "candidate::hash-schemaedit::0" in body
    assert "NOT auto-merged" in body


# --- failed CI gate: no PR, still human-gated, still no auto-commit -----------


async def test_failed_ci_opens_no_pr_but_still_routes_to_review():
    stage = SchemaEditPRStage(
        git_client=ExplodingGitClient(), checks=FailingChecks()
    )
    result = await stage.process(_schema_edit_candidate(), _ctx())
    assert result.control == "route_inbox"
    assert result.envelope.status == "in_review"
    review = result.envelope.payload["schema_edit_review"]
    assert review["pr_opened"] is False
    assert review["reason"] == "fail_to_review"
    assert review["lint_ok"] is False


# --- pass-through for non-schema_edit ----------------------------------------


async def test_non_schema_edit_passes_through_untouched():
    from dataclasses import replace

    git = ScriptedGitClient()
    stage = SchemaEditPRStage(git_client=git)
    bp = replace(_schema_edit_candidate(), type="blueprint")
    result = await stage.process(bp, _ctx())
    assert result.control == "continue"
    assert git.specs == []  # no PR for a non-schema_edit candidate


# --- patch parsing tolerance -------------------------------------------------


async def test_patch_parses_fixture_and_locked_shapes():
    p1 = SchemaEditPatch.from_payload(_schema_edit_candidate().payload)
    assert p1.edit_kind == "add_rule"
    assert p1.target_catalog == "payroll"
    assert p1.proposed_yaml.startswith("rules:")

    p2 = SchemaEditPatch.from_payload(
        {"edit_type": "add_synonym", "target": {"database": "hr"}, "patch": "x: 1"}
    )
    assert p2.edit_kind == "add_synonym"
    assert p2.target_catalog == "hr"
    assert p2.proposed_yaml == "x: 1"
