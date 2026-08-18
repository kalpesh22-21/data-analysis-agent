"""SchemaEditPRStage — the S8 schema-edit PR bot (`CandidateStage`, D53/D18).

The highest-stakes target: a `schema_edit` grounds `getTableSchema` for ALL users, so it is
NEVER auto-committed (D18). At the writer position this turns the candidate into a branch +
YAML-patch PR via the injected git client, gated by the injected CI checks, and routes it to
the review inbox — the human MERGE of the PR is the real gate (D53). CI passing opens the PR
and records its ref; CI failing opens none and records the reason, routing `fail_to_review`.
Either way: never a catalog write. Every non-`schema_edit` candidate passes straight through.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..candidate.models import CandidateEnvelope, CandidateStatus
from ..stage import StageContext, StageResult
from .client import AllPassChecks, GitPullRequestClient, SchemaEditChecks
from .models import PullRequestSpec, SchemaEditPatch


def _branch_name(env: CandidateEnvelope) -> str:
    """Deterministic branch id so a re-run targets the SAME branch (idempotent)."""
    return f"learning/schema-edit/{env.content_hash}"


def _catalog_path(patch: SchemaEditPatch) -> str:
    """The catalog YAML file the patch merges into.

    A placeholder mapping — the real path resolver lands with the real git wiring.
    """
    catalog = patch.target_catalog or "catalog"
    return f"catalog/{catalog}/semantic.yaml"


@dataclass(frozen=True)
class SchemaEditPRStage:
    """The injected S8 schema-edit PR-bot stage. `git_client` is REQUIRED (an
    injected scripted double in tests); `checks` defaults to the all-pass CI gate."""

    git_client: GitPullRequestClient
    checks: SchemaEditChecks = AllPassChecks()
    stage_id: str = "schema_edit_writer"

    async def process(
        self, env: CandidateEnvelope, ctx: StageContext
    ) -> StageResult:
        if env.type != "schema_edit":
            return StageResult(envelope=env, control="continue")

        patch = SchemaEditPatch.from_payload(env.payload)
        check = await self.checks.run(patch)

        if not check.ok:
            # CI red: never open a branch, never auto-commit — route to human
            # review flagged fail_to_review (D52/D18).
            enriched = replace(
                env,
                status=CandidateStatus.IN_REVIEW,
                payload={
                    **env.payload,
                    "schema_edit_review": {
                        "pr_opened": False,
                        "reason": "fail_to_review",
                        "lint_ok": check.lint_ok,
                        "explain_ok": check.explain_ok,
                        "detail": check.reason,
                    },
                },
            )
            return StageResult(envelope=enriched, control="route_inbox")

        spec = PullRequestSpec(
            branch=_branch_name(env),
            title=f"[schema-edit] {patch.edit_kind}: {patch.statement}"[:200],
            body=(
                "Automated schema-edit PR proposed by the learning loop (D53/D18).\n\n"
                f"Candidate: {env.candidate_id}\n"
                f"Source session: {env.source_session}\n"
                f"Rationale: {env.extractor_rationale}\n\n"
                "This PR is NOT auto-merged — a human review + MERGE is the gate."
            ),
            path=_catalog_path(patch),
            patch=patch.proposed_yaml,
        )
        pr = await self.git_client.open_pull_request(spec)

        enriched = replace(
            env,
            status=CandidateStatus.IN_REVIEW,
            payload={
                **env.payload,
                "schema_edit_review": {
                    "pr_opened": True,
                    "pr_url": pr.url,
                    "pr_number": pr.number,
                    "branch": pr.branch,
                    "lint_ok": check.lint_ok,
                    "explain_ok": check.explain_ok,
                },
            },
        )
        return StageResult(envelope=enriched, control="route_inbox")
