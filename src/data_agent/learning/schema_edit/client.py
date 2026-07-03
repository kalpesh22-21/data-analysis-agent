"""Injected seams for the schema-edit PR bot (S8, D53/D18).

Two injected collaborators, both scripted in tests (NO real GitHub, NO real
network, NO catalog write):

  * `GitPullRequestClient` — authors a branch + YAML-patch PR. The real
    GitHub/git wiring is DELIBERATELY DEFERRED; this slice builds only the seam.
  * `SchemaEditChecks` — runs the CI gate (schema lint + `explainQuery` dry-run)
    that CI would run on the PR. Injected so a test drives a deterministic
    pass/fail without a schema engine.

The default `SchemaEditChecks` (`AllPassChecks`) passes; there is NO default git
client (a missing client is a wiring error the stage surfaces loudly, never a
silent no-op that could look like an auto-commit).
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
