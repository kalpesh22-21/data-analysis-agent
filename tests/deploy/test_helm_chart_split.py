"""The two charts have to survive being installed side by side — that is the whole split.

`data-agent` (online: runtime, agent UI, hydrator) and `data-agent-learning` (offline:
sweeper, consumer, scheduler, inbox, reviewer UI) ship the SAME image and are expected to
land in the SAME namespace. Everything that makes that safe is naming and reference
discipline inside the templates, and none of it is checked by `helm lint`:

  * two objects of one kind may not share a name (the second `helm install` fails, or —
    worse, when the collision is INSIDE one chart — one workload silently overwrites
    another and simply never runs);
  * a Deployment may only name a ConfigMap / Secret / ServiceAccount that ITS OWN chart
    renders, because a reference into the sibling release is a pod that never starts and
    a `CreateContainerConfigError` no probe can report;
  * `secrets.existingSecret` has to suppress the chart Secret in BOTH charts, or the
    recommended "one Secret, two releases" posture ships a second, empty Secret that
    shadows nothing and drifts silently.

The sibling files cover the per-workload wiring (`test_helm_inbox_ui.py`,
`test_helm_learning_scheduler.py`, `test_helm_hydrator.py`, `test_helm_tenant_claims.py`);
this one covers the seam between the charts.

The last two tests pin a defect this file first caught: the name helpers used to truncate
to 63 chars AFTER appending the component suffix, so a long-but-legal release name cut
away the one part of a name that distinguishes one object from another and collapsed
several objects onto a single name. Both `_helpers.tpl` files now budget
`63 - len(suffix) - 1` for the base before concatenating (see their `suffixedName`
helper); these tests hold that ordering in place.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_HELM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "helm"
_AGENT = _HELM_DIR / "data-agent"
_LEARNING = _HELM_DIR / "data-agent-learning"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


def _render(chart: Path, release: str, *sets: str, values: str | None = None) -> list[dict]:
    args = ["helm", "template", release, str(chart)]
    if values is not None:
        args += ["-f", str(chart / values)]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _names(docs: list[dict]) -> list[tuple[str, str]]:
    return [(d["kind"], d["metadata"]["name"]) for d in docs]


def _workloads(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d["kind"] == "Deployment"]


# --------------------------------------------------------------------------- #
# one namespace, two releases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("values", [None, "values-example.yaml"])
def test_the_two_charts_do_not_collide_in_one_namespace(values: str | None) -> None:
    """Same namespace, the documented release names. A shared (kind, name) is an
    install-time failure at best and a silently overwritten workload at worst."""
    agent = _render(_AGENT, "agent", values=values)
    learning = _render(_LEARNING, "learning", values=values)
    collisions = sorted(set(_names(agent)) & set(_names(learning)))
    assert not collisions, f"both charts render {collisions} into the same namespace"


def test_neither_charts_services_select_the_other_charts_pods() -> None:
    """The selector labels carry `app.kubernetes.io/name`, which differs per chart. If a
    refactor ever unified it, the agent UI Service would start load-balancing onto
    reviewer-UI pods — the same image, so the endpoints would look healthy while serving
    the wrong surface."""
    agent = _render(_AGENT, "agent")
    learning = _render(_LEARNING, "learning")
    for services, foreign in ((agent, learning), (learning, agent)):
        selectors = [
            d["spec"]["selector"] for d in services if d["kind"] == "Service"
        ]
        for pod_labels in [
            d["spec"]["template"]["metadata"]["labels"] for d in _workloads(foreign)
        ]:
            for selector in selectors:
                assert not all(
                    pod_labels.get(k) == v for k, v in selector.items()
                ), f"selector {selector} matches a sibling chart's pod {pod_labels}"


# --------------------------------------------------------------------------- #
# no dangling references
# --------------------------------------------------------------------------- #


def _dangling(docs: list[dict], existing_secret: str | None = None) -> list[tuple]:
    config_maps = {n for k, n in _names(docs) if k == "ConfigMap"}
    secrets = {n for k, n in _names(docs) if k == "Secret"}
    accounts = {n for k, n in _names(docs) if k == "ServiceAccount"} | {"default"}
    if existing_secret:
        secrets.add(existing_secret)

    problems: list[tuple] = []
    for workload in _workloads(docs):
        name = workload["metadata"]["name"]
        spec = workload["spec"]["template"]["spec"]
        if spec.get("serviceAccountName") not in accounts:
            problems.append((name, "serviceAccountName", spec.get("serviceAccountName")))
        for container in spec["containers"]:
            for source in container.get("envFrom", []):
                if "configMapRef" in source and source["configMapRef"]["name"] not in config_maps:
                    problems.append((name, "configMapRef", source["configMapRef"]["name"]))
                if "secretRef" in source and source["secretRef"]["name"] not in secrets:
                    problems.append((name, "secretRef", source["secretRef"]["name"]))
            for entry in container.get("env", []):
                ref = entry.get("valueFrom") or {}
                if "configMapKeyRef" in ref and ref["configMapKeyRef"]["name"] not in config_maps:
                    problems.append((name, "configMapKeyRef", ref["configMapKeyRef"]["name"]))
                if "secretKeyRef" in ref and ref["secretKeyRef"]["name"] not in secrets:
                    problems.append((name, "secretKeyRef", ref["secretKeyRef"]["name"]))
    return problems


@pytest.mark.parametrize("chart", [_AGENT, _LEARNING], ids=["data-agent", "data-agent-learning"])
@pytest.mark.parametrize("values", [None, "values-example.yaml"])
def test_every_workload_references_only_objects_its_own_chart_renders(
    chart: Path, values: str | None
) -> None:
    """A reference the chart does not create is a pod stuck in
    `CreateContainerConfigError` — invisible to `helm lint` and to every probe."""
    existing = "data-agent-secrets" if values else None
    docs = _render(chart, "t", values=values)
    assert not _dangling(docs, existing)


# --------------------------------------------------------------------------- #
# existingSecret
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("chart", [_AGENT, _LEARNING], ids=["data-agent", "data-agent-learning"])
def test_existing_secret_suppresses_the_chart_secret_in_both_charts(chart: Path) -> None:
    """The recommended production posture points BOTH releases at one out-of-band
    Secret. If either chart still rendered its own, the pods would `envFrom` a
    chart-managed Secret full of empty strings — and empty strings are not "unset": they
    reach the process and beat the code defaults."""
    docs = _render(chart, "t", "secrets.existingSecret=shared-secrets")
    assert not [n for k, n in _names(docs) if k == "Secret"], "chart Secret still rendered"
    referenced = {
        source["secretRef"]["name"]
        for workload in _workloads(docs)
        for container in workload["spec"]["template"]["spec"]["containers"]
        for source in container.get("envFrom", [])
        if "secretRef" in source
    }
    assert referenced == {"shared-secrets"}
    assert not _dangling(docs, "shared-secrets")


# --------------------------------------------------------------------------- #
# component toggles
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("chart", "flag"),
    [
        (_AGENT, "components.runtime.enabled"),
        (_AGENT, "components.ui.enabled"),
        (_AGENT, "components.hydrator.enabled"),
        (_LEARNING, "components.learningSweeper.enabled"),
        (_LEARNING, "components.learningConsumer.enabled"),
        (_LEARNING, "components.learningScheduler.enabled"),
        (_LEARNING, "components.inbox.enabled"),
        (_LEARNING, "components.inboxUi.enabled"),
    ],
)
def test_disabling_one_component_leaves_no_orphaned_ingress_pdb_or_hpa(
    chart: Path, flag: str
) -> None:
    """Optional add-ons must be gated on their TARGET, not just on their own flag. An
    Ingress routing to a Service that no longer exists is a 503 the chart happily
    installs; a PDB selecting nothing blocks node drains forever."""
    docs = _render(
        chart,
        "t",
        f"{flag}=false",
        "ingress.enabled=true",
        "components.runtime.autoscaling.enabled=true",
        "components.runtime.podDisruptionBudget.enabled=true",
        "components.ui.podDisruptionBudget.enabled=true",
        "components.learningConsumer.autoscaling.enabled=true",
        "components.inboxUi.podDisruptionBudget.enabled=true",
    )
    services = {n for k, n in _names(docs) if k == "Service"}
    deployments = {d["metadata"]["name"]: d for d in _workloads(docs)}

    for doc in docs:
        if doc["kind"] == "Ingress":
            backends = {
                p["backend"]["service"]["name"]
                for rule in doc["spec"]["rules"]
                for p in rule["http"]["paths"]
            }
            assert backends <= services, f"Ingress routes to a missing Service: {backends}"
        if doc["kind"] == "HorizontalPodAutoscaler":
            assert doc["spec"]["scaleTargetRef"]["name"] in deployments
        if doc["kind"] == "PodDisruptionBudget":
            selector = doc["spec"]["selector"]["matchLabels"]
            assert any(
                all(
                    d["spec"]["template"]["metadata"]["labels"].get(k) == v
                    for k, v in selector.items()
                )
                for d in deployments.values()
            ), f"PDB {doc['metadata']['name']} selects nothing"

    assert not _dangling(docs)


def test_turning_the_inbox_off_omits_the_derived_url_instead_of_naming_a_ghost() -> None:
    """`inbox.enabled=false` with the reviewer UI still on. The derivation must not emit
    a URL for a Service this render no longer creates: the proxy would resolve nothing
    and every reviewer action would 502 with no clue why. Omitting the key is the honest
    answer (the app then falls back to its own localhost default and fails loudly)."""
    docs = _render(_LEARNING, "t", "components.inbox.enabled=false")
    config = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert "INBOX_SERVICE_URL" not in config
    # ...but an operator pointing at an out-of-release inbox is still served.
    docs = _render(
        _LEARNING,
        "t",
        "components.inbox.enabled=false",
        "config.INBOX_SERVICE_URL=http://inbox.other.svc.cluster.local:8100",
    )
    config = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert config["INBOX_SERVICE_URL"] == "http://inbox.other.svc.cluster.local:8100"


# --------------------------------------------------------------------------- #
# name truncation must not eat the component suffix (fixed; pinned here)
# --------------------------------------------------------------------------- #

# 33 chars, well inside Helm's 53-char release-name limit, and the shape an operator
# actually types when releases are environment-scoped.
_LONG_RELEASE = "platform-analytics-staging-euwest1"


def test_component_names_stay_unique_under_a_long_release_name() -> None:
    """Three learning daemons sharing one Deployment name is not a rendering curiosity:
    two of the three simply never run, and nothing reports it."""
    for chart in (_AGENT, _LEARNING):
        rendered = _names(_render(chart, _LONG_RELEASE))
        duplicates = sorted({n for n in rendered if rendered.count(n) > 1})
        assert not duplicates, f"{chart.name}: {duplicates}"


def test_generated_object_names_fit_the_63_character_dns_limit() -> None:
    for chart in (_AGENT, _LEARNING):
        oversized = [
            (kind, name, len(name))
            for kind, name in _names(_render(chart, _LONG_RELEASE))
            if len(name) > 63
        ]
        assert not oversized, f"{chart.name}: {oversized}"
