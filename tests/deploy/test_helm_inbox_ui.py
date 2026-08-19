"""The reviewer UI is the agent UI with two env vars — so those env vars ARE the split.

`ui/server.py` is ONE FastAPI app carrying both surfaces: the agent chat shell (`GET /`,
`/api/turn*`, …) and the reviewer inbox (`GET /inbox`, `/api/inbox/*`). The two-chart
split deploys that one app TWICE with no application change, and the only things that
make the second copy a reviewer surface are:

  1. `REVIEW_INBOX_ENABLED=1` — without it every inbox route 404s and the deployment
     is an agent UI wearing a reviewer's name;
  2. `INBOX_SERVICE_URL` pointing at THIS release's inbox Service — the derivation is
     only correct because the inbox is rendered by the same chart (the data-agent chart
     deliberately lost this helper: it cannot know the learning release's name).

Nothing in the app enforces either one, so if a template edit drops them the failure is
a 404 page or a proxy pointed at `http://localhost:8100` inside the wrong pod. Hence
these assertions live here rather than in the app suite.

The last test pins the fence: this chart's Ingress publishes the inbox paths ONLY. The
chat routes exist on the pod and cannot be compiled out, so the path list is the entire
mechanism keeping the learning release from opening a second door into the agent.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "data-agent-learning"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


def _docs(*sets: str) -> list[dict]:
    args = ["helm", "template", "t", str(_CHART)]
    for s in sets:
        args += ["--set", s]
    rendered = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _by_kind_component(docs: list[dict], kind: str, component: str) -> dict:
    for doc in docs:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        if doc.get("kind") == kind and labels.get("app.kubernetes.io/component") == component:
            return doc
    raise AssertionError(f"no {kind} for component {component!r} in the render")


def test_inbox_ui_runs_the_same_app_with_the_reviewer_flag_on() -> None:
    docs = _docs()
    container = _by_kind_component(docs, "Deployment", "inbox-ui")["spec"]["template"]["spec"][
        "containers"
    ][0]
    # Same entrypoint as the agent UI — the split is configuration, not code. The
    # launcher, not the uvicorn CLI: a clean SIGTERM shutdown has to exit 0 (ISSUES.md
    # C3), and `scripts/run_ui_bff.py` serves the identical `ui.server:app`.
    assert container["command"] == [
        "python",
        "scripts/run_ui_bff.py",
        "--host",
        "0.0.0.0",
        "--port",
        "3000",
    ]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["REVIEW_INBOX_ENABLED"] == "1"


def test_inbox_service_url_is_derived_from_this_releases_inbox_service() -> None:
    """The derived URL must name a Service this same render actually creates — a URL
    that resolves to nothing is a 502 on every reviewer action."""
    docs = _docs()
    config = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert config["INBOX_SERVICE_URL"] == "http://t-data-agent-learning-inbox:8100"
    service = _by_kind_component(docs, "Service", "inbox")
    assert service["metadata"]["name"] == "t-data-agent-learning-inbox"
    assert service["spec"]["ports"][0]["port"] == 8100


def test_an_explicit_inbox_service_url_wins() -> None:
    """The derivation is a default, not a wall: an inbox in another namespace/release
    has to be reachable."""
    docs = _docs("config.INBOX_SERVICE_URL=http://inbox.other.svc.cluster.local:8100")
    config = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert config["INBOX_SERVICE_URL"] == "http://inbox.other.svc.cluster.local:8100"


def test_the_reviewer_probe_is_the_inbox_page_not_the_chat_shell() -> None:
    """`GET /` answers 200 on this pod whether or not the inbox gate is on, so probing
    it would report READY for a pod whose reviewer surface is 404ing. `/inbox` is 200
    only when the flag reached the process."""
    container = _by_kind_component(_docs(), "Deployment", "inbox-ui")["spec"]["template"][
        "spec"
    ]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/inbox"
    assert container["livenessProbe"]["httpGet"]["path"] == "/inbox"


def test_the_inbox_service_probe_is_not_an_unauthenticated_http_get() -> None:
    """Every inbox route, `/inbox/health` included, sits behind `_require_reviewer`.
    The kubelet sends no `X-Reviewer-Token`, so an httpGet probe reads 401 (or 503 when
    the token is unset) and treats it as a FAILURE — the pod would never become Ready
    and the Service would have no endpoints. Putting the token in `httpHeaders` is not
    the alternative: that writes a live credential into the pod spec."""
    container = _by_kind_component(_docs(), "Deployment", "inbox")["spec"]["template"]["spec"][
        "containers"
    ][0]
    for probe in ("readinessProbe", "livenessProbe"):
        assert "httpGet" not in container[probe], (
            f"the inbox {probe} does an unauthenticated httpGet; every route requires "
            "X-Reviewer-Token, so this probe can only ever read 401/503 and the pod can "
            "never become Ready"
        )
        assert container[probe]["tcpSocket"]["port"] == "http"


def test_every_learning_key_in_the_chart_is_a_real_settings_field() -> None:
    """`LearningSettings` sets `extra="ignore"`, so a typo'd key in values.yaml is not an
    error anywhere: it is dropped, the shipped default silently applies, and the operator
    believes they turned a knob that does not exist. The entrypoints log the ignored
    names at startup, but nobody reads a log to find out that a deploy did nothing —
    catch it at render time instead.

    `LEARNING_ENABLED` is the known exception: it is the kill-switch, read fresh by
    `learning_enabled()` and deliberately absent from the model.
    """
    from data_agent.learning.config import LearningSettings

    known = {name.upper() for name in LearningSettings.model_fields} | {"LEARNING_ENABLED"}
    for values in (None, "values-example.yaml"):
        args = ["helm", "template", "t", str(_CHART)]
        if values:
            args += ["-f", str(_CHART / values)]
        rendered = subprocess.run(args, capture_output=True, text=True, check=True).stdout
        config = next(
            d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "ConfigMap"
        )["data"]
        unknown = sorted(k for k in config if k.startswith("LEARNING_") and k not in known)
        assert not unknown, (
            f"{values or 'values.yaml'} sets {unknown}, which LearningSettings ignores — "
            "the shipped default applies and the setting silently does nothing"
        )


def test_the_ingress_publishes_the_inbox_surface_only() -> None:
    """The chat routes are on this pod and cannot be removed; the path list is the
    fence. `/` here would publish the agent UI from the learning release."""
    docs = _docs("ingress.enabled=true")
    ingress = _by_kind_component(docs, "Ingress", "inbox-ui")
    paths = [p["path"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]]
    assert paths == ["/inbox", "/api/inbox"]
    # It must front the reviewer UI, never the token-guarded inbox service itself.
    backends = {
        p["backend"]["service"]["name"]
        for rule in ingress["spec"]["rules"]
        for p in rule["http"]["paths"]
    }
    assert backends == {"t-data-agent-learning-inbox-ui"}
