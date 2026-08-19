"""No rendered workload may enter through the uvicorn CLI (ISSUES.md C3).

The defect never lived in the application: uvicorn re-raises the SIGTERM it captured onto
the `SIG_DFL` it restores after `serve()` returns, so a *clean* shutdown killed the pod by
signal at 143 and every rollout was reported as a container crash. `run_http_daemon` fixes
it by owning `serve()`, and a workload only gets that by being launched from a repo
script. So the chart's `command:` IS the fix, and it is the one place a future edit can
silently undo it — `helm template` renders fine either way and no probe would notice.

Both assertions are deliberately about the whole render rather than about the three
workloads that were wrong: the next HTTP workload someone adds is the one at risk.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_HELM = _REPO / "deploy" / "helm"
_CHARTS = (_HELM / "data-agent", _HELM / "data-agent-learning")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


def _containers(chart: Path) -> list[tuple[str, dict]]:
    """Every container in every workload the chart renders with its defaults, named by
    `<kind>/<container>` so a failure says which one."""
    rendered = subprocess.run(
        ["helm", "template", "t", str(chart)], capture_output=True, text=True, check=True
    ).stdout
    found = []
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            found.append((f"{doc['kind']}/{container['name']}", container))
    assert found, f"{chart.name} rendered no workloads — the guard would be vacuous"
    return found


@pytest.mark.parametrize("chart", _CHARTS, ids=lambda c: c.name)
def test_no_workload_is_launched_by_the_uvicorn_cli(chart: Path) -> None:
    offenders = [
        name
        for name, container in _containers(chart)
        if container.get("command", [None])[0] == "uvicorn"
    ]
    assert offenders == [], (
        f"{offenders} enter through the uvicorn CLI, where no repo code brackets "
        "`serve()` — a clean SIGTERM shutdown exits 143 and the rollout reads as a "
        "crash. Point the command at a `python scripts/run_*.py` launcher (C3)."
    )


@pytest.mark.parametrize("chart", _CHARTS, ids=lambda c: c.name)
def test_every_launcher_a_workload_names_actually_exists(chart: Path) -> None:
    """A `command:` naming a script the image does not carry is `CrashLoopBackOff` with a
    `No such file or directory` — invisible to `helm lint` and to `helm template`. The
    Dockerfile copies `scripts/` wholesale, so existing in the repo is existing in the
    image."""
    missing = [
        (name, container["command"][1])
        for name, container in _containers(chart)
        if container.get("command", [None])[0] == "python"
        and not (_REPO / container["command"][1]).is_file()
    ]
    assert missing == [], f"these commands name a script that is not in the repo: {missing}"
