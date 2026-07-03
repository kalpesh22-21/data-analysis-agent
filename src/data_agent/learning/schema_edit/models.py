"""schema-edit PR-bot value objects (S8, D53/D18).

The typed payloads the PR-authoring seam moves: the parsed `schema_edit` candidate
(`SchemaEditPatch`), the CI-checks outcome (`CheckResult`), the PR request the git
client is handed (`PullRequestSpec`), and the PR the client reports back
(`PullRequestResult`). A `schema_edit` NEVER auto-commits to the catalog (D18): the
bot only OPENS a branch + PR; the human MERGE is the real gate (D53).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SchemaEditPatch:
    """The parsed `schema_edit` payload (05 §schema_edit — Locked). Tolerant of the
    fixture shape (`edit_kind`/`target_catalog`/`proposed_yaml`) and the Locked
    field names (`edit_type`/`target`/`patch`)."""

    edit_kind: str
    target_catalog: str
    statement: str
    proposed_yaml: str
    risk: str = "medium"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SchemaEditPatch:
        target = payload.get("target") or {}
        target_catalog = (
            payload.get("target_catalog")
            or (target.get("database") if isinstance(target, dict) else None)
            or ""
        )
        return cls(
            edit_kind=payload.get("edit_kind") or payload.get("edit_type") or "",
            target_catalog=target_catalog,
            statement=payload.get("statement", ""),
            proposed_yaml=payload.get("proposed_yaml") or payload.get("patch") or "",
            risk=payload.get("risk", "medium"),
        )


@dataclass(frozen=True)
class CheckResult:
    """The CI gate outcome for a proposed patch: schema lint + `explainQuery`
    dry-run. `ok` iff BOTH passed. A failing gate does NOT drop the candidate — it
    still opens a PR (so the human sees a red PR) OR routes fail_to_review,
    depending on `open_pr_on_failed_ci`; either way it never auto-commits."""

    ok: bool
    lint_ok: bool
    explain_ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class PullRequestSpec:
    """What the injected git client is handed to author the PR."""

    branch: str
    title: str
    body: str
    path: str  # the catalog YAML file the patch targets
    patch: str  # the YAML patch content
    base: str = "main"


@dataclass(frozen=True)
class PullRequestResult:
    """What the git client reports back after opening the PR."""

    url: str
    number: int
    branch: str
