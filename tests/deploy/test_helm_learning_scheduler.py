"""Phase-3 PART F — the learning-scheduler Helm Deployment renders (and is gated).

`helm template` must render the `learning-scheduler` Deployment when
`components.learningScheduler.enabled=true` and OMIT it when false. The daemon is the
single promotion writer, so the render is pinned to `replicas: 1` + `Recreate` + the
correct entrypoint (a >1 replica would double-promote / double-land).

The scheduler moved to the `data-agent-learning` chart in the two-chart split, so this
points there — and the last test holds the OTHER half of that split: the data-agent
chart must no longer render ANY learning workload. A split that leaves a working copy
behind in the old chart is a split that silently runs two promotion writers.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HELM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "helm"
_CHART = _HELM_DIR / "data-agent-learning"
_AGENT_CHART = _HELM_DIR / "data-agent"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


def _template(chart: Path, *sets: str) -> str:
    args = ["helm", "template", "t", str(chart)]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(
        args, capture_output=True, text=True, check=True
    ).stdout


def test_renders_when_enabled() -> None:
    out = _template(_CHART, "components.learningScheduler.enabled=true")
    assert "learning-scheduler" in out
    assert "scripts/run_learning_scheduler.py" in out
    # Single-writer invariant: fixed 1 replica + Recreate.
    assert "replicas: 1" in out
    assert "type: Recreate" in out


def test_omitted_when_disabled() -> None:
    out = _template(_CHART, "components.learningScheduler.enabled=false")
    assert "learning-scheduler" not in out
    assert "run_learning_scheduler.py" not in out


def test_the_agent_chart_no_longer_ships_a_learning_plane() -> None:
    """The other half of the split. `--set` cannot resurrect what was deleted, so
    render the data-agent chart with its OWN defaults and assert every learning/inbox
    entrypoint is gone. A leftover template here would mean an operator upgrading the
    data-agent release keeps running a second promotion writer beside the one the
    learning release now owns — two writers racing the same candidate store, which is
    exactly the invariant `replicas: 1` above exists to protect."""
    out = _template(_AGENT_CHART)
    for entrypoint in (
        "run_learning_scheduler.py",
        "run_learning_sweeper.py",
        "run_learning_consumer.py",
        "run_inbox_service.py",
    ):
        assert entrypoint not in out, f"{entrypoint} still renders from the data-agent chart"
    # And the agent UI must not be handed a route to a review inbox it no longer owns.
    assert "INBOX_SERVICE_URL" not in out
    assert "REVIEW_INBOX_ENABLED" not in out
