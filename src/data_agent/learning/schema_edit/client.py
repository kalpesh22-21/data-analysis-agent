"""Injected seams for the schema-edit PR bot (S8, D53/D18).

Two injected collaborators, both scripted in tests (no real GitHub, no network, no catalog
write): `GitPullRequestClient` authors a branch + YAML-patch PR, with the real git wiring
deliberately deferred, and `SchemaEditChecks` runs the CI gate. The default checks pass, but
there is NO default git client — a missing client is a wiring error the stage surfaces
loudly, never a silent no-op that could look like an auto-commit.
"""

from __future__ import annotations

from typing import Protocol

from .models import CheckResult, PullRequestResult, PullRequestSpec, SchemaEditPatch


class GitPullRequestClient(Protocol):
    """The PR-authoring seam. A real impl opens a branch + commits the YAML patch +
    opens a PR against the catalog repo; the human MERGE is the gate (D53). Must
    NEVER commit to the catalog directly (D18)."""

    async def open_pull_request(self, spec: PullRequestSpec) -> PullRequestResult: ...


class SchemaEditChecks(Protocol):
    """The CI gate the PR must pass: schema lint + `explainQuery` dry-run."""

    async def run(self, patch: SchemaEditPatch) -> CheckResult: ...


class AllPassChecks:
    """Default CI gate: passes lint + dry-run. Wire a real impl to actually lint /
    dry-run; this default keeps the stage runnable while that is deferred."""

    async def run(self, patch: SchemaEditPatch) -> CheckResult:
        return CheckResult(ok=True, lint_ok=True, explain_ok=True)
