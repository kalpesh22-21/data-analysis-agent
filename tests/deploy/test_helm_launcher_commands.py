"""The `command:` of every rendered workload is pinned here, exactly.

A container's entrypoint is the one part of a chart that `helm lint` and `helm template`
both accept in any shape and no probe can report on: a wrong module path is a
`CrashLoopBackOff` with an ImportError, a wrong flag is a pod listening on the wrong
interface. So the launch line is asserted verbatim, per workload, and the workload SET is
asserted too — a newly added Deployment fails this file until someone decides, in
writing, how it starts.

THE C3 HISTORY, KEPT VISIBLE ON PURPOSE. This file used to assert the opposite of what it
asserts now: that NO workload enters through the uvicorn CLI. The defect behind that rule
was real and is not fixed — uvicorn re-raises the SIGTERM it captured onto the `SIG_DFL`
it restores after `serve()` returns, so a CLEAN shutdown kills the pod by signal and the
container exits 143. Kubernetes reports that rollout as a crash. `run_http_daemon` (used
by `scripts/run_*.py`) owns `serve()` and chains the re-raise so the same shutdown exits
0.

The charts have deliberately moved the HTTP workloads back onto the uvicorn CLI, trading
that cosmetic-but-noisy 143 for a standard, image-independent entrypoint. The trade is
the point of this comment: if rollouts start reading as crashes, this is why, and the
fix is to point these four commands back at their `scripts/run_*.py` launchers (which
still exist and still serve the identical apps).

  runtime    uvicorn data_agent.runtime.app:create_app --factory  ← scripts/run_runtime_api.py
  ui         uvicorn ui.server:app                                ← scripts/run_ui_bff.py
  inbox      uvicorn data_agent.learning.inbox.service:create_inbox_app --factory
                                                                  ← scripts/run_inbox_service.py
  inbox-ui   uvicorn ui.server:app                                ← scripts/run_ui_bff.py

The three NON-HTTP learning daemons (sweeper, consumer, scheduler) are not affected by
C3 at all — they own their own loops — and still launch from their repo scripts.

The in-chart Redis is exempt from both rules: it runs a third-party image whose command
is `redis-server`, and neither a repo script nor a uvicorn target could apply to it.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_HELM = _REPO / "deploy" / "helm"
_AGENT = _HELM / "data-agent"
_LEARNING = _HELM / "data-agent-learning"
_CHARTS = (_AGENT, _LEARNING)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


# The exact launch line per workload, keyed by `<kind>/<container>` as `_containers`
# reports it. Ports are the chart defaults for the component's `service.port`.
_EXPECTED_COMMANDS: dict[str, dict[str, list[str]]] = {
    "data-agent": {
        # HTTP — uvicorn CLI (see the C3 note in the module docstring).
        "Deployment/runtime": [
            "uvicorn",
            "data_agent.runtime.app:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
        "Deployment/ui": [
            "uvicorn",
            "ui.server:app",
            "--host",
            "0.0.0.0",
            "--port",
            "3000",
        ],
        # Non-HTTP daemon — owns its own loop, launched from the repo script.
        "Deployment/hydrator": ["python", "scripts/run_hydrator.py"],
    },
    "data-agent-learning": {
        # HTTP — uvicorn CLI.
        "Deployment/inbox": [
            "uvicorn",
            "data_agent.learning.inbox.service:create_inbox_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8100",
        ],
        "Deployment/inbox-ui": [
            "uvicorn",
            "ui.server:app",
            "--host",
            "0.0.0.0",
            "--port",
            "3000",
        ],
        # Non-HTTP daemons — the three that C3 never applied to.
        "Deployment/learning-sweeper": ["python", "scripts/run_learning_sweeper.py"],
        "Deployment/learning-consumer": ["python", "scripts/run_learning_consumer.py"],
        "Deployment/learning-scheduler": ["python", "scripts/run_learning_scheduler.py"],
        # Third-party image, exempt from both rules.
        "Deployment/redis": ["redis-server"],
    },
}


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
def test_every_workload_launches_with_its_pinned_command(chart: Path) -> None:
    """Exact match, per workload. Reviewing a diff here is the point: a changed launch
    line is a deployment behaviour change, not a formatting one."""
    expected = _EXPECTED_COMMANDS[chart.name]
    actual = {name: container.get("command") for name, container in _containers(chart)}

    assert sorted(actual) == sorted(expected), (
        f"{chart.name} renders a different set of workloads than this file pins. A NEW "
        "workload must declare here how it starts (uvicorn CLI for HTTP surfaces, "
        "`python scripts/run_*.py` for daemons) — see the C3 note in the docstring."
    )
    for name in sorted(expected):
        assert actual[name] == expected[name], f"{chart.name} {name}: unexpected command"


@pytest.mark.parametrize("chart", _CHARTS, ids=lambda c: c.name)
def test_the_non_http_daemons_still_enter_through_their_repo_launchers(chart: Path) -> None:
    """The narrow half of the C3 rule that still stands. The sweeper/consumer/scheduler
    have no `serve()` to bracket, so there is no reason to ever move them onto a CLI —
    and this asserts it independently of the table above, which someone could edit."""
    daemons = {
        name: container.get("command")
        for name, container in _containers(chart)
        if name.startswith("Deployment/learning-")
    }
    if chart is _LEARNING:
        assert len(daemons) == 3, f"expected 3 learning daemons, got {sorted(daemons)}"
    for name, command in daemons.items():
        assert command[0] == "python" and command[1].startswith("scripts/run_"), (
            f"{name} no longer enters through a repo launcher: {command}"
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


def _module_file(module: str) -> Path | None:
    """Resolve a dotted module to its file the way the IMAGE will: `src/` is installed on
    the path, and the uvicorn CLI prepends its `--app-dir` default (`.` = the Dockerfile's
    /app WORKDIR), which is what makes the root-level `ui` package importable."""
    parts = module.split(".")
    for root in (_REPO / "src", _REPO):
        candidate = root.joinpath(*parts).with_suffix(".py")
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.parametrize("chart", _CHARTS, ids=lambda c: c.name)
def test_every_uvicorn_target_names_a_module_and_attribute_that_exist(chart: Path) -> None:
    """The uvicorn-CLI counterpart of the launcher-exists check above: `module:attr` is a
    STRING resolved at container start, so a rename in `src/` leaves a chart that renders,
    lints and installs, and whose pod dies on an ImportError/AttributeError. Checked
    statically (AST) rather than by importing — importing these modules pulls in the whole
    app and its optional deps, which would make this guard slow and flaky."""
    problems: list[str] = []
    for name, container in _containers(chart):
        command = container.get("command") or []
        if command[:1] != ["uvicorn"]:
            continue
        target = command[1]
        assert ":" in target, f"{name}: uvicorn target {target!r} names no attribute"
        module, _, attribute = target.partition(":")
        path = _module_file(module)
        if path is None:
            problems.append(f"{name}: no module {module!r} in the repo")
            continue
        tree = ast.parse(path.read_text())
        defined = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        if attribute not in defined:
            problems.append(f"{name}: {module} defines no top-level {attribute!r}")

        # `--factory` is not decoration: uvicorn CALLS the target when it is present and
        # SERVES it when it is not. Pointing it at a factory without the flag serves a
        # function object (every request 500s); the reverse calls an ASGI app instance.
        is_factory_flagged = "--factory" in command
        looks_like_factory = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == attribute
            for node in tree.body
        )
        if is_factory_flagged != looks_like_factory:
            problems.append(
                f"{name}: {target} is {'a function' if looks_like_factory else 'an object'} "
                f"but --factory is {'set' if is_factory_flagged else 'absent'}"
            )
    assert problems == [], f"{chart.name}: {problems}"
