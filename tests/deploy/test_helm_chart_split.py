"""The two charts have to survive being installed side by side — that is the whole split.

`data-agent` (online: runtime, agent UI, hydrator) and `data-agent-learning` (offline:
sweeper, consumer, scheduler, inbox, reviewer UI) ship the SAME image and are expected to
land in the SAME namespace. Everything that makes that safe is naming and reference
discipline inside the templates, and none of it is checked by `helm lint`:

  * two objects of one kind may not share a name (the second `helm install` fails, or —
    worse, when the collision is INSIDE one chart — one workload silently overwrites
    another and simply never runs);
  * a Deployment may only name a ConfigMap / ServiceAccount / PVC that ITS OWN chart
    renders, because a reference into the sibling release is a pod that never starts and
    a `CreateContainerConfigError` no probe can report;
  * the envFrom Secret is the ONE SANCTIONED EXCEPTION to that rule, and it is an
    exception by design (see below) — so it gets its own, stricter check instead: its
    name must resolve to `secrets.existingSecret` when set and to `<fullname>-secret`
    when not, in BOTH charts, and neither chart may render a Secret of its own.

NEITHER CHART RENDERS A SECRET ANY MORE. Secret material never passes through values, a
rendered manifest or `helm get values`; the Secret is created out-of-band (vault /
external-secrets / sealed-secrets) and the pods mount it wholesale via envFrom. That
makes a dangling secretRef the EXPECTED steady state of `helm template`, which is why the
generic reference check below has to whitelist exactly one name and keep refusing every
other kind of dangling reference. The failure mode this trades into is real and worth
naming: if the out-of-band Secret does not exist when the pods start, they wedge in
`CreateContainerConfigError` rather than failing loudly.

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


def _conventional_secret_name(docs: list[dict]) -> str:
    """`<fullname>-secret`, derived from the ConfigMap the chart DOES render.

    Both charts build these names through their `suffixedName` helper, which budgets
    `63 - len(suffix) - 1` for the base — and "config" and "secret" are the same length,
    so swapping the suffix reproduces the Secret name exactly, truncation included. That
    is what makes this derivation valid under a long release name too."""
    config_map = next(n for k, n in _names(docs) if k == "ConfigMap")
    assert config_map.endswith("-config"), config_map
    return f"{config_map.removesuffix('config')}secret"


def _envfrom_secret_names(docs: list[dict]) -> set[str]:
    return {
        source["secretRef"]["name"]
        for workload in _workloads(docs)
        for container in workload["spec"]["template"]["spec"]["containers"]
        for source in container.get("envFrom") or []
        if "secretRef" in source
    }


def _dangling(docs: list[dict], sanctioned_secret: str | None = None) -> list[tuple]:
    """Every reference a workload makes, checked against what this render creates.

    `sanctioned_secret` is the ONE name allowed to be absent from the render: the
    out-of-band envFrom Secret neither chart renders any more. Pass it explicitly so the
    exception stays a decision at each call site rather than a hole in the checker.
    """
    config_maps = {n for k, n in _names(docs) if k == "ConfigMap"}
    secrets = {n for k, n in _names(docs) if k == "Secret"}
    accounts = {n for k, n in _names(docs) if k == "ServiceAccount"} | {"default"}
    claims = {n for k, n in _names(docs) if k == "PersistentVolumeClaim"}
    if sanctioned_secret:
        secrets.add(sanctioned_secret)

    problems: list[tuple] = []
    for workload in _workloads(docs):
        name = workload["metadata"]["name"]
        spec = workload["spec"]["template"]["spec"]
        if spec.get("serviceAccountName") not in accounts:
            problems.append((name, "serviceAccountName", spec.get("serviceAccountName")))
        # A pod naming a PVC no template rendered never schedules: it sits Pending on
        # `persistentvolumeclaim not found`, which no probe and no rollout status explains.
        for volume in spec.get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim is not None and claim not in claims:
                problems.append((name, "persistentVolumeClaim", claim))
        for container in spec["containers"]:
            # `or []`, not a default: several templates render a bare `env:` with nothing
            # under it (an unconditional key wrapping a `with` block), which parses as
            # None. Kubernetes treats that as absent, so it is not a defect to assert on
            # here — but it does mean `.get(k, [])` is not enough.
            for source in container.get("envFrom") or []:
                if "configMapRef" in source and source["configMapRef"]["name"] not in config_maps:
                    problems.append((name, "configMapRef", source["configMapRef"]["name"]))
                if "secretRef" in source and source["secretRef"]["name"] not in secrets:
                    problems.append((name, "secretRef", source["secretRef"]["name"]))
            for entry in container.get("env") or []:
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
    `CreateContainerConfigError` (envFrom/ServiceAccount) or Pending forever (PVC) —
    invisible to `helm lint` and to every probe.

    The out-of-band envFrom Secret is the single sanctioned exception and is whitelisted
    BY NAME, not by kind: a secretRef to anything other than the resolved Secret name is
    still a failure here, and `test_neither_chart_renders_a_secret_...` pins what that
    name is allowed to be."""
    docs = _render(chart, "t", values=values)
    # values-example.yaml points both charts at one out-of-band `data-agent-secrets`.
    sanctioned = "data-agent-secrets" if values else _conventional_secret_name(docs)
    assert _envfrom_secret_names(docs) == {sanctioned}
    assert not _dangling(docs, sanctioned)


# --------------------------------------------------------------------------- #
# the out-of-band Secret
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("chart", [_AGENT, _LEARNING], ids=["data-agent", "data-agent-learning"])
@pytest.mark.parametrize("existing", [None, "shared-secrets"], ids=["default", "existingSecret"])
def test_neither_chart_renders_a_secret_and_every_workload_points_at_the_out_of_band_one(
    chart: Path, existing: str | None
) -> None:
    """No chart-managed Secret, in either chart, in either configuration.

    A chart that rendered one would ship a Secret full of EMPTY STRINGS — and an empty
    string is not "unset": it reaches the process through envFrom and beats the code
    default, so the app runs with a blank password instead of falling back. It would also
    put secret material into `helm get values` and every rendered manifest. The pods must
    still name a Secret, and the name is the whole contract with whatever creates it
    out-of-band: `secrets.existingSecret` when set, `<fullname>-secret` when not."""
    sets = [f"secrets.existingSecret={existing}"] if existing else []
    docs = _render(chart, "t", *sets)

    assert not [n for k, n in _names(docs) if k == "Secret"], (
        "a chart-managed Secret is back; the charts render none by design"
    )
    expected = existing or _conventional_secret_name(docs)
    assert _envfrom_secret_names(docs) == {expected}
    # Every workload that mounts config must mount the Secret too — a daemon that got
    # only the ConfigMap starts fine and fails later, at its first credentialed call.
    # (The in-chart Redis is the deliberate exception: a third-party image is given
    # neither the ConfigMap nor the Secret.)
    for workload in _workloads(docs):
        for container in workload["spec"]["template"]["spec"]["containers"]:
            kinds = {k for source in container.get("envFrom") or [] for k in source}
            assert kinds in ({"configMapRef", "secretRef"}, set()), (
                f"{workload['metadata']['name']}/{container['name']} mounts {kinds}"
            )
    assert not _dangling(docs, expected)


# --------------------------------------------------------------------------- #
# component toggles
# --------------------------------------------------------------------------- #


# Everything optional, ON. The two charts spell their Ingresses differently and the
# names are load-bearing: in data-agent `ingress` is the RUNTIME API ingress and
# `uiIngress` the agent UI's, while data-agent-learning has only `uiIngress` (the
# reviewer surface). Setting a key a chart does not read is silently accepted by helm,
# so listing them per chart is what keeps this test from going vacuous.
_ALL_ADDONS: dict[str, tuple[str, ...]] = {
    "data-agent": (
        "ingress.enabled=true",
        "uiIngress.enabled=true",
        "components.runtime.autoscaling.enabled=true",
        "components.runtime.podDisruptionBudget.enabled=true",
        "components.ui.podDisruptionBudget.enabled=true",
    ),
    "data-agent-learning": (
        "uiIngress.enabled=true",
        "components.learningConsumer.autoscaling.enabled=true",
        "components.inbox.podDisruptionBudget.enabled=true",
        "components.inboxUi.podDisruptionBudget.enabled=true",
        # Persistence on, so the redis PVC exists and CAN be orphaned by the toggle.
        "redis.persistence.enabled=true",
    ),
}

# The literal the redisUrl helper falls back to when there is no in-chart Redis and no
# explicit `config.LEARNING_REDIS_URL`. It is a guess about the cluster, not a
# configuration — but it must be what gets rendered, because the alternative (naming the
# Service this render no longer creates) is a transport that resolves to nothing.
_EXTERNAL_REDIS_FALLBACK = "redis://redis:6379/0"

# The `app.kubernetes.io/component` label each toggle owns — the label is how the orphan
# sweep in the test below finds objects the flag should have removed.
_COMPONENT_OF_FLAG = {
    "components.runtime.enabled": "runtime",
    "components.ui.enabled": "ui",
    "components.hydrator.enabled": "hydrator",
    "components.learningSweeper.enabled": "learning-sweeper",
    "components.learningConsumer.enabled": "learning-consumer",
    "components.learningScheduler.enabled": "learning-scheduler",
    "components.inbox.enabled": "inbox",
    "components.inboxUi.enabled": "inbox-ui",
    "redis.enabled": "redis",
}


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
        (_LEARNING, "redis.enabled"),
    ],
)
def test_disabling_one_component_leaves_no_orphaned_ingress_pdb_or_hpa(
    chart: Path, flag: str
) -> None:
    """Optional add-ons must be gated on their TARGET, not just on their own flag. An
    Ingress routing to a Service that no longer exists is a 503 the chart happily
    installs; a PDB selecting nothing blocks node drains forever; a PVC left behind by a
    component that is gone is a volume nobody mounts and everybody pays for."""
    docs = _render(chart, "t", f"{flag}=false", *_ALL_ADDONS[chart.name])
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

    # Nothing may be left behind that belongs to the component just switched off.
    component = _COMPONENT_OF_FLAG[flag]
    orphans = sorted(
        (d["kind"], d["metadata"]["name"])
        for d in docs
        if ((d.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/component")
        == component
    )
    assert not orphans, f"{flag}=false still renders {orphans}"

    if flag == "redis.enabled":
        # The transport must not keep naming a Service this render no longer creates:
        # every daemon would resolve nothing and the sweeper's enqueue would fail on a
        # DNS error, with the chart looking perfectly healthy.
        config = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
        assert config["LEARNING_REDIS_URL"] == _EXTERNAL_REDIS_FALLBACK

    assert not _dangling(docs, _conventional_secret_name(docs))


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


def test_the_conventional_secret_name_stays_predictable_under_a_long_release_name() -> None:
    """`<fullname>-secret` is now a NAME CONTRACT with something outside the chart.

    Nothing renders this Secret any more — a human, a vault policy or an
    ExternalSecret creates it, using a name they worked out from the release name. So
    truncation here is not cosmetic: if the rendered reference silently loses or moves
    its `-secret` suffix, the operator creates one name and the pods mount another, and
    the only symptom is every pod wedged in CreateContainerConfigError.

    The suffix must therefore survive truncation intact (same rule as the component
    names above), and the name must stay inside the 63-char DNS limit so it is creatable
    at all."""
    for chart in (_AGENT, _LEARNING):
        docs = _render(chart, _LONG_RELEASE)
        referenced = _envfrom_secret_names(docs)
        assert len(referenced) == 1, f"{chart.name}: workloads disagree on the Secret name"
        name = referenced.pop()
        assert name.endswith("-secret"), f"{chart.name}: truncation ate the suffix: {name}"
        assert len(name) <= 63, f"{chart.name}: {name} is {len(name)} chars"
        assert name.startswith(_LONG_RELEASE[:20]), f"{chart.name}: {name}"


def test_generated_object_names_fit_the_63_character_dns_limit() -> None:
    for chart in (_AGENT, _LEARNING):
        oversized = [
            (kind, name, len(name))
            for kind, name in _names(_render(chart, _LONG_RELEASE))
            if len(name) > 63
        ]
        assert not oversized, f"{chart.name}: {oversized}"
