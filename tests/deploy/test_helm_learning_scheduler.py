"""Phase-3 PART F — the learning-scheduler Helm Deployment renders (and is gated).

`helm template` must render the new `learning-scheduler` Deployment when
`components.learningScheduler.enabled=true` and OMIT it when false. The daemon is the
single promotion writer, so the render is pinned to `replicas: 1` + `Recreate` + the
correct entrypoint (a >1 replica would double-promote / double-land).

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "data-agent"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


def _template(*sets: str) -> str:
    args = ["helm", "template", "t", str(_CHART)]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(
        args, capture_output=True, text=True, check=True
    ).stdout


def test_renders_when_enabled() -> None:
    out = _template("components.learningScheduler.enabled=true")
    assert "learning-scheduler" in out
    assert "scripts/run_learning_scheduler.py" in out
    # Single-writer invariant: fixed 1 replica + Recreate.
    assert "replicas: 1" in out
    assert "type: Recreate" in out


def test_omitted_when_disabled() -> None:
    out = _template("components.learningScheduler.enabled=false")
    assert "learning-scheduler" not in out
    assert "run_learning_scheduler.py" not in out
