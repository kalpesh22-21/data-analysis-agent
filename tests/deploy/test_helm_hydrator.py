"""The singleton hydrator Helm Deployment renders (and is gated), and the runtime
readiness probe switched to `httpGet /ready` (singleton-hydrator redesign).

`helm template` must render the `hydrator` Deployment when
`components.hydrator.enabled=true` and OMIT it when false. The daemon is the single
graph seeder (owns the destructive nuke/rebuild), so the render is pinned to
`replicas: 1` + `Recreate` + the correct entrypoint. The runtime Deployment's readiness
probe must be the `/ready` httpGet, while liveness stays a TCP probe.

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
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def test_hydrator_renders_when_enabled() -> None:
    out = _template("components.hydrator.enabled=true")
    assert "hydrator" in out
    assert "scripts/run_hydrator.py" in out
    # Single-seeder invariant: fixed 1 replica + Recreate.
    assert "replicas: 1" in out
    assert "type: Recreate" in out


def test_hydrator_omitted_when_disabled() -> None:
    out = _template("components.hydrator.enabled=false")
    assert "run_hydrator.py" not in out


def test_runtime_readiness_probe_is_ready_httpget() -> None:
    out = _template()
    # The runtime's readiness probe is the /ready httpGet; liveness stays tcpSocket.
    assert "path: /ready" in out
    assert "tcpSocket" in out
