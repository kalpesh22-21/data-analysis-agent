"""The umbrella exists to make ONE config error impossible, so that is what this pins.

Eighteen keys must be IDENTICAL in the online (`data-agent`) and offline
(`data-agent-learning`) planes: the Couchbase session keyspace the sweeper scans, the
Neo4j graph a blueprint lands in, the embedding model the vectors are compared with,
the MCP and token-service endpoints, the OTLP endpoint, and the warehouse tenant
triple. Every divergence is SILENT — row policies filter rather than error, a sweeper
pointed at the wrong scope simply finds nothing forever, vectors from two models
compare as noise. Two separate releases made keeping them equal a discipline, and the
discipline failed in production (a Couchbase connect failure in the sweeper, from a
scope set on one side only).

`data-agent-platform` installs both charts as subcharts of ONE release so those keys
are written ONCE, in `global.config`. That only works because of an edit inside BOTH
subcharts: their `configmap.yaml` now merges `global.config` OVER their own `config`,
and GLOBAL WINS. The precedence is forced, not stylistic — each subchart ships a
default for nearly all 18 in its own values.yaml, so a local-wins merge would make
`global.config` a knob helm accepts and nothing reads. Half of this file is that
precedence, from three directions: over a shipped default, over an explicit
per-subchart override, and — the case sprig's `merge` gets wrong on its own, because
mergo treats "" as EMPTY and backfills it from the source map — over a non-blank local
with a DELIBERATELY BLANK global.

The other half is what the umbrella must NOT break:

  * standalone installs of either chart. `.Values.global` is absent there, and the
    nil-safe merge must reduce to exactly the pre-change template. That is checked by
    reconstructing the pre-change `configmap.yaml` and byte-comparing the renders,
    across a scenario matrix — not by eyeballing one default render.
  * the blank-TENANT_* carve-out, which now has to survive the merge path. An omitted
    claim is not a blank one: the process falls through to the LOCAL DEV SEED and the
    readiness gates stay dark.
  * the three per-plane DERIVED urls (RUNTIME_URL, INBOX_SERVICE_URL,
    LEARNING_REDIS_URL), which are resolved by helpers below the range and must keep
    naming their own release's Services.

And one trap the umbrella CREATES. Both subcharts now share a `.Release.Name`, and
both fullname helpers collapse `<release>-<chart>` to just `<release>` when the
release name already contains the chart name — so a release called
`data-agent-learning-prod` gives BOTH planes the same fullname, and their ConfigMap /
ServiceAccount / test Pod render under identical names. Helm does not object; the API
server keeps whichever applied last, and one plane silently mounts the other plane's
env. The umbrella refuses to render that, and the guard COMPUTES both fullnames rather
than string-matching, because the same comparison also catches a second class the
substring test would miss: at a release name near Helm's 53-character maximum, both
fullnames truncate to the same 63 characters.

63 is not the budget that matters, though, and believing it was is how this file
shipped with a hole in it. Only the ServiceAccount is named with the bare fullname;
every other object is `<fullname truncated to 62-len(suffix)>-<suffix>`, so the shared
"test-connection" Pod is built on a base of just 47 characters. Two fullnames still
distinct at 63 can be identical at 47 — and were, from a 35-character release name
upwards, with the guard silent and the first duplicate object arriving nine characters
before it fired. A collision test pinned to a handful of hand-picked release names
cannot find that; the sweep below is parametrised over LENGTH instead, and asserts the
only property that actually holds — every length either refuses to render or renders
no two objects of one kind under one name.

Skipped when the `helm` binary is unavailable (a dev box without it).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.deploy.test_helm_chart_split import _dangling, _names, _workloads

_HELM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "helm"
_AGENT = _HELM_DIR / "data-agent"
_LEARNING = _HELM_DIR / "data-agent-learning"
_PLATFORM = _HELM_DIR / "data-agent-platform"

# The keys the umbrella exists for. Duplicated here ON PURPOSE rather than read out of
# the umbrella's values.yaml: a test that derives its expectations from the file under
# test cannot notice a key being dropped from it.
_SHARED_KEYS = (
    "CATALOG_FIXTURE_PATH",
    "COUCHBASE_BUCKET",
    "COUCHBASE_CONNECTION_STRING",
    "COUCHBASE_RESULTS_COLLECTION",
    "COUCHBASE_SCOPE",
    "COUCHBASE_SESSIONS_COLLECTION",
    "COUCHBASE_USERNAME",
    "EMBEDDING_API_URL",
    "EMBEDDING_MODEL",
    "MCP_URL",
    "NEO4J_DATABASE",
    "NEO4J_URL",
    "NEO4J_USERNAME",
    "OTLP_ENDPOINT",
    "TENANT_CLIENT_CODE",
    "TENANT_JTI",
    "TENANT_PROC_CENTER",
    "TOKEN_SERVICE_URL",
)

_TENANT_KEYS = ("TENANT_CLIENT_CODE", "TENANT_PROC_CENTER", "TENANT_JTI")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Requires the 'helm' binary."
)


@pytest.fixture(scope="session", autouse=True)
def _umbrella_dependencies_are_current() -> None:
    """Repackage the `file://` subcharts before rendering anything.

    `charts/*.tgz` is a BUILD ARTIFACT and is gitignored, so on a fresh clone it is
    absent and `helm template` refuses outright. Worse, when it is present it WINS over
    the subchart directory beside it — so a stale tarball would have this file testing
    yesterday's subchart while reporting on today's. Rebuilding here makes the suite
    correct by construction instead of dependent on whether someone remembered."""
    subprocess.run(
        ["helm", "dependency", "update", str(_PLATFORM)],
        capture_output=True,
        text=True,
        check=True,
    )


def _render(chart: Path, release: str, *sets: str, values: str | None = None) -> str:
    args = ["helm", "template", release, str(chart)]
    if values is not None:
        args += ["-f", str(chart / values)]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def _docs(chart: Path, release: str, *sets: str, values: str | None = None) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(_render(chart, release, *sets, values=values)) if doc]


def _config_maps(docs: list[dict]) -> dict[str, dict[str, str]]:
    """Every ConfigMap in the render, by name. The umbrella renders two — one per
    plane — and most assertions below only mean something when applied to BOTH."""
    return {d["metadata"]["name"]: (d.get("data") or {}) for d in docs if d["kind"] == "ConfigMap"}


def _both_planes(docs: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    maps = _config_maps(docs)
    assert len(maps) == 2, f"expected one ConfigMap per plane, got {sorted(maps)}"
    online = next(v for k, v in maps.items() if "learning" not in k)
    offline = next(v for k, v in maps.items() if "learning" in k)
    return online, offline


# --------------------------------------------------------------------------- #
# the version pins
# --------------------------------------------------------------------------- #


def test_the_dependency_pins_match_the_subchart_versions() -> None:
    """The pins are exact, so they go stale the moment a subchart is bumped.

    `helm dependency update` fails loudly on a mismatch, which is the design — but it
    fails on whoever runs it NEXT, possibly in CI on an unrelated change, possibly at
    install time on someone's cluster. Catching it in the suite attributes the failure
    to the commit that caused it. (No helm binary needed; this is YAML arithmetic.)"""
    pins = {
        dep["name"]: dep["version"]
        for dep in yaml.safe_load((_PLATFORM / "Chart.yaml").read_text())["dependencies"]
    }
    for chart in (_AGENT, _LEARNING):
        actual = str(yaml.safe_load((chart / "Chart.yaml").read_text())["version"])
        assert pins[chart.name] == actual, (
            f"{chart.name} is at {actual} but data-agent-platform pins {pins[chart.name]}; "
            "bump the pin and re-run `helm dependency update deploy/helm/data-agent-platform`"
        )


# --------------------------------------------------------------------------- #
# global.config reaches both planes, and wins
# --------------------------------------------------------------------------- #


def test_every_shared_key_lands_identically_in_both_planes() -> None:
    """The whole product of the umbrella, at its defaults: 18 keys, one value each,
    two ConfigMaps. A key that reaches only one plane is the drift class returning
    through the front door."""
    online, offline = _both_planes(_docs(_PLATFORM, "platform"))
    disagreements = {
        key: (online.get(key, "<absent>"), offline.get(key, "<absent>"))
        for key in _SHARED_KEYS
        if online.get(key, "<absent>") != offline.get(key, "<absent>")
    }
    assert not disagreements, f"the two planes disagree on {disagreements}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # The consolidated-bucket layout — the shape that actually broke in production.
        ("COUCHBASE_SCOPE", "sessions"),
        ("COUCHBASE_BUCKET", "pcm_iwant"),
        # A blueprint landed in another database is invisible to the agent's recall.
        ("NEO4J_DATABASE", "prod"),
        # Vectors written by one model and searched by another compare as noise.
        ("EMBEDDING_MODEL", "text-embedding-3-large"),
        ("MCP_URL", "https://mcp.internal/prefix/mcp"),
        ("TENANT_CLIENT_CODE", "CLIENT_Z"),
    ],
)
def test_a_key_set_once_in_global_config_reaches_both_planes(key: str, value: str) -> None:
    """Set it ONCE. Both ConfigMaps must carry it — including over the non-empty
    default each subchart's own values.yaml ships for it, which is the whole reason
    global has to win."""
    online, offline = _both_planes(_docs(_PLATFORM, "platform", f"global.config.{key}={value}"))
    assert online[key] == value
    assert offline[key] == value


def test_global_config_beats_an_explicit_per_subchart_override() -> None:
    """Not just the shipped default — an override someone TYPED under `data-agent:`.

    This is the sharp edge of the design and it is deliberate: for these 18 keys the
    per-subchart block is overruled, because a per-plane value for a must-match key is
    exactly the drift the umbrella removes. Anything softer (local-wins, or
    first-one-set-wins) reintroduces it silently."""
    online, offline = _both_planes(
        _docs(
            _PLATFORM,
            "platform",
            "global.config.COUCHBASE_SCOPE=global_wins",
            "data-agent.config.COUCHBASE_SCOPE=local_agent",
            "data-agent-learning.config.COUCHBASE_SCOPE=local_learning",
        )
    )
    assert online["COUCHBASE_SCOPE"] == "global_wins"
    assert offline["COUCHBASE_SCOPE"] == "global_wins"


def test_a_deliberately_blank_global_value_also_wins() -> None:
    """Sprig `merge` alone gets this WRONG, which is why the templates do not stop there.

    `merge` delegates to mergo, and mergo treats "" as EMPTY: a blank value in the
    destination map is BACKFILLED from the source. So `global.config.TENANT_JTI: ""`
    against a subchart that sets it non-blank would lose — half a precedence rule, and
    the half that fails is the one that re-mints a tenant nobody chose. Both configmap
    templates re-assert every global key over the merge result to close it."""
    online, offline = _both_planes(
        _docs(
            _PLATFORM,
            "platform",
            "global.config.TENANT_CLIENT_CODE=",
            "data-agent.config.TENANT_CLIENT_CODE=SNEAKY",
            "data-agent-learning.config.TENANT_CLIENT_CODE=SNEAKIER",
        )
    )
    assert online["TENANT_CLIENT_CODE"] == ""
    assert offline["TENANT_CLIENT_CODE"] == ""


def test_the_tenant_triple_still_renders_when_blank_through_the_merge_path() -> None:
    """The carve-out has to survive the new code path.

    Blank values are omitted from the ConfigMap so the process falls through to its
    settings-model default — and for these three that default is the LOCAL DEV SEED.
    An omitted claim is therefore NOT an unset one: it silently re-mints CLIENT_A /
    PC01 / TESTJTI001, matches zero rows under the row policies, and the golden
    replay's grain check passes on `0 == 0`. Present-and-blank is what trips the
    readiness gates instead."""
    online, offline = _both_planes(_docs(_PLATFORM, "platform"))
    for plane, config in (("online", online), ("offline", offline)):
        for key in _TENANT_KEYS:
            assert key in config, f"{plane}: {key} was omitted; the dev seed wins"
            assert config[key] == ""


def test_per_plane_keys_are_refused_in_global_config() -> None:
    """A global value for a DERIVED key would be silently discarded — the range in each
    configmap excludes those keys and the helper below it reads `.Values.config`.

    INBOX_SERVICE_URL is worse than a no-op: the ONLINE plane does not exclude it, so a
    global one would render the reviewer inbox's address into the agent UI's ConfigMap.
    It does nothing today (that pod leaves REVIEW_INBOX_ENABLED unset, so its inbox
    routes 404) but it is half of the second door into the reviewer surface, installed
    without anyone deciding to."""
    for key in ("RUNTIME_URL", "INBOX_SERVICE_URL", "LEARNING_REDIS_URL"):
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            _render(_PLATFORM, "platform", f"global.config.{key}=http://somewhere:8000")
        assert "IS NOT A SHARED KEY" in excinfo.value.stderr, excinfo.value.stderr


# --------------------------------------------------------------------------- #
# the shared Secret
# --------------------------------------------------------------------------- #


def _envfrom_secret_names(docs: list[dict]) -> set[str]:
    return {
        source["secretRef"]["name"]
        for workload in _workloads(docs)
        for container in workload["spec"]["template"]["spec"]["containers"]
        for source in container.get("envFrom") or []
        if "secretRef" in source
    }


def test_one_global_secret_serves_both_planes() -> None:
    """The posture the README always recommended, made the default instead of a
    discipline. COUCHBASE_PASSWORD / NEO4J_PASSWORD / TOKEN_ISSUER_API_KEY /
    EMBEDDING_API_KEY authenticate the SAME identity in both planes; two
    hand-maintained Secrets drift, and the drift has no symptom."""
    docs = _docs(_PLATFORM, "platform", "global.secrets.existingSecret=data-agent-secrets")
    assert _envfrom_secret_names(docs) == {"data-agent-secrets"}
    # Still no chart-managed Secret anywhere — the umbrella renders none either.
    assert not [n for k, n in _names(docs) if k == "Secret"]


def test_a_per_subchart_secret_still_overrides_the_global_one() -> None:
    """LOCAL beats GLOBAL here, the opposite of `config`, and the asymmetry is the
    point: `secrets.existingSecret` ships EMPTY in both subcharts, so a local value can
    only exist because someone typed it — deferring to it costs the global nothing and
    leaves an escape hatch for a plane whose credentials genuinely differ."""
    docs = _docs(
        _PLATFORM,
        "platform",
        "global.secrets.existingSecret=shared",
        "data-agent-learning.secrets.existingSecret=learning-only",
    )
    assert _envfrom_secret_names(docs) == {"shared", "learning-only"}


def test_without_a_global_secret_each_plane_keeps_its_conventional_name() -> None:
    """Supported, and the thing `global.secrets.existingSecret` exists to avoid: two
    Secrets to create out-of-band and keep in sync by hand."""
    docs = _docs(_PLATFORM, "platform")
    assert _envfrom_secret_names(docs) == {
        "platform-data-agent-secret",
        "platform-data-agent-learning-secret",
    }


# --------------------------------------------------------------------------- #
# the derived per-plane urls
# --------------------------------------------------------------------------- #


def test_the_derived_urls_still_name_their_own_planes_services() -> None:
    """One release does not merge the two planes' service discovery. Each derivation
    still resolves through its own subchart's helper, and the names it produces must be
    Services THIS render actually creates — a derived URL pointing at nothing is a
    daemon that fails on DNS with the chart looking perfectly healthy."""
    docs = _docs(_PLATFORM, "platform")
    online, offline = _both_planes(docs)
    services = {n for k, n in _names(docs) if k == "Service"}

    assert online["RUNTIME_URL"] == "http://platform-data-agent-runtime:8000"
    assert offline["INBOX_SERVICE_URL"] == "http://platform-data-agent-learning-inbox:8100"
    assert offline["LEARNING_REDIS_URL"] == "redis://platform-data-agent-learning-redis:6379/0"

    for url in (online["RUNTIME_URL"], offline["INBOX_SERVICE_URL"], offline["LEARNING_REDIS_URL"]):
        host = url.split("//", 1)[1].split(":", 1)[0]
        assert host in services, f"{url} names no Service this release renders"

    # RUNTIME_URL stays the ONLINE plane's alone, and INBOX_SERVICE_URL the OFFLINE
    # plane's. One release makes the inbox Service name derivable from .Release.Name
    # for the first time; handing it to the agent UI would open a second door into the
    # reviewer surface, published on the agent UI's hostname.
    assert "RUNTIME_URL" not in offline
    assert "INBOX_SERVICE_URL" not in online
    assert "REVIEW_INBOX_ENABLED" not in online


# --------------------------------------------------------------------------- #
# the release-name collision guard
# --------------------------------------------------------------------------- #

# 53 characters — Helm's own maximum release-name length, and short enough to be a
# legal name. `<release>-data-agent` and `<release>-data-agent-learning` BOTH truncate
# to the same 63 characters here, so this collides without containing either chart
# name: the case a substring guard would wave through.
_TRUNCATING_RELEASE = "a" * 53


@pytest.mark.parametrize(
    "release",
    [
        "data-agent-learning",
        "data-agent-learning-prod",
        "eu-data-agent-learning",
        _TRUNCATING_RELEASE,
    ],
    ids=["exact", "suffixed", "prefixed", "truncation-collision"],
)
def test_the_collision_guard_refuses_a_release_name_that_collides(release: str) -> None:
    """Both planes' ConfigMap / ServiceAccount / test Pod would render under ONE name.
    Helm renders both happily and the API server keeps the last apply, so one plane
    silently mounts the other's env — the learning daemons reading the online plane's
    config, or the reverse, with no error anywhere."""
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _render(_PLATFORM, release)
    assert "RELEASE NAME COLLISION" in excinfo.value.stderr, excinfo.value.stderr
    # The message has to be actionable: it names a working alternative.
    assert "helm upgrade --install platform" in excinfo.value.stderr


@pytest.mark.parametrize(
    "release",
    ["platform", "data-agent-platform", "prod", "acme-data-agent-prod", "dap-eu-west-1"],
)
def test_the_collision_guard_stays_quiet_for_a_normal_release_name(release: str) -> None:
    """Including `acme-data-agent-prod`, which CONTAINS "data-agent" and is perfectly
    safe: only the online plane's fullname collapses, the offline plane's does not, and
    the two names stay distinct. A guard that fired here would be unusable."""
    docs = _docs(_PLATFORM, release)
    assert _config_maps(docs), f"{release} rendered nothing"


def test_the_collision_guard_accepts_a_fullname_override_as_the_escape_hatch() -> None:
    """A release name is not always the operator's to change (argocd, a naming policy).
    The guard computes fullnames the same way the subcharts do, `fullnameOverride`
    included, so the documented escape hatch actually works rather than being advice
    the guard then ignores."""
    docs = _docs(_PLATFORM, "data-agent-learning-prod", "data-agent.fullnameOverride=online-plane")
    assert _config_maps(docs)


def test_the_collision_guard_does_not_fire_when_only_one_plane_is_enabled() -> None:
    """Nothing can collide with a plane that is not rendered. Failing here would block
    the legitimate `enabled=false` install on a name that is only a problem for two."""
    docs = _docs(_PLATFORM, "data-agent-learning-prod", "data-agent.enabled=false")
    assert _config_maps(docs)


# --------------------------------------------------------------------------- #
# one release, no name collisions, no dangling references
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "release", ["platform", "prod", "platform-analytics-staging-euwest1", "acme-data-agent-prod"]
)
def test_no_two_objects_of_one_kind_share_a_name_under_the_umbrella(release: str) -> None:
    """`test_helm_chart_split.py` proves this for two SEPARATE releases, where a
    collision at least fails the second `helm install`. Under one release helm renders
    both objects into a single manifest and the API server keeps whichever applied
    last — the collision becomes silent, so it has to be checked here too, across the
    release-name SHAPES that stress the fullname rule (the collapse branch, in
    particular). Length is the other axis and is swept separately below; picking names
    by shape alone is exactly how the 47-character truncation went unnoticed."""
    rendered = _names(_docs(_PLATFORM, release))
    duplicates = sorted({n for n in rendered if rendered.count(n) > 1})
    assert not duplicates, f"{release}: {duplicates}"
    oversized = [(k, n, len(n)) for k, n in rendered if len(n) > 63]
    assert not oversized, f"{release}: {oversized}"


# Helm's own limit on a release name, and the top of the sweep below. 30 is the bottom
# because the first real collision lands at 35 and a sweep that starts above it would
# never observe the guard staying quiet.
_MAX_RELEASE_LEN = 53
_MIN_SWEEP_LEN = 30

# The length at which the two planes' names FIRST collide, with a release name that
# contains neither chart name: `<release>-data-agent` (46 chars) and
# `<release>-data-agent-learning` truncated to the "test-connection" base budget of
# `62 - len("test-connection")` = 47 are the same string. Anything longer collides too.
_FIRST_COLLIDING_LEN = 35


@pytest.mark.parametrize("length", range(_MIN_SWEEP_LEN, _MAX_RELEASE_LEN + 1))
def test_at_every_release_name_length_the_render_is_refused_or_collision_free(
    length: int,
) -> None:
    """The property that actually matters, swept over the one axis that breaks it.

    Object names are NOT budgeted at 63. `suffixedName` truncates the fullname to
    `62 - len(suffix)` so the suffix survives, which means the online and offline
    fullnames must stay distinct within 47 characters (the shared "test-connection"
    Pod), not 63. A guard comparing at 63 passed every hand-picked release name in this
    file and still let `<release>-data-agent-test-connection` render TWICE from a
    35-character release name — one Pod per plane, same name, second apply wins.

    So this asserts the invariant rather than a list of names: at EVERY legal length,
    helm either refuses (with the collision guard, not some unrelated error) or emits a
    manifest in which no two objects of one kind share a name. A length that renders
    something broken fails here; a length that renders nothing at all cannot hide,
    because the refusal has to be the collision guard and the boundary test below pins
    where refusals may start."""
    release = "x" * length
    result = subprocess.run(
        ["helm", "template", release, str(_PLATFORM)], capture_output=True, text=True
    )
    if result.returncode != 0:
        assert "RELEASE NAME COLLISION" in result.stderr, (
            f"len={length}: render failed for a reason other than the collision guard:\n"
            f"{result.stderr}"
        )
        return

    rendered = _names([doc for doc in yaml.safe_load_all(result.stdout) if doc])
    duplicates = sorted({n for n in rendered if rendered.count(n) > 1})
    assert not duplicates, (
        f"len={length}: rendered SILENTLY yet two objects share a name: {duplicates}"
    )
    oversized = [(k, n, len(n)) for k, n in rendered if len(n) > 63]
    assert not oversized, f"len={length}: {oversized}"


def test_the_guard_fires_at_exactly_the_length_that_first_collides() -> None:
    """Both halves of the boundary, so neither can drift into uselessness.

    A guard that fired one character early would be merely annoying; a guard that fired
    one character late is the bug this replaced — and a guard that "fixed" the hole by
    refusing everything long would pass the sweep above completely vacuously. Realistic
    names either side of the line, not `xxxx`: a region suffix is all that separates
    them.

    The unsafe one is unsafe on the evidence, not the arithmetic — before the fix it
    rendered clean and shipped two helm-test Pods called
    `platform-analytics-staging-euwest-1-data-agent-test-connection`."""
    safe = "platform-analytics-staging-euwest1"
    unsafe = "platform-analytics-staging-euwest-1"
    assert len(safe) == _FIRST_COLLIDING_LEN - 1
    assert len(unsafe) == _FIRST_COLLIDING_LEN

    assert _config_maps(_docs(_PLATFORM, safe)), f"{safe} must still install"

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _render(_PLATFORM, unsafe)
    assert "RELEASE NAME COLLISION" in excinfo.value.stderr, excinfo.value.stderr
    # The message must name the object that collides, not just assert that one does —
    # the Pod below is what `helm test` would have created twice.
    assert f"{unsafe}-data-agent-test-connection" in excinfo.value.stderr, excinfo.value.stderr
    # And the rule of thumb it offers has to be the real boundary, not a stale number:
    # the template derives it from the suffix budget, this pins it to observed behaviour.
    assert f"under {_FIRST_COLLIDING_LEN} characters" in excinfo.value.stderr, excinfo.value.stderr


def test_every_umbrella_workload_references_only_objects_the_release_renders() -> None:
    """The same discipline the split test enforces per chart, applied to the combined
    manifest — reusing its checker so the two cannot drift apart.

    The out-of-band envFrom Secrets are the sanctioned exception (nothing renders one,
    by design); every other dangling reference is a pod stuck in
    CreateContainerConfigError or Pending forever."""
    docs = _docs(_PLATFORM, "platform", "global.secrets.existingSecret=data-agent-secrets")
    assert not _dangling(docs, "data-agent-secrets")


# --------------------------------------------------------------------------- #
# plane toggles
# --------------------------------------------------------------------------- #

_ONLINE_COMPONENTS = {"runtime", "ui", "hydrator"}
_OFFLINE_COMPONENTS = {
    "learning-sweeper",
    "learning-consumer",
    "learning-scheduler",
    "inbox",
    "inbox-ui",
    "redis",
}


def _components(docs: list[dict]) -> set[str]:
    return {
        (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component")
        for d in _workloads(docs)
    }


@pytest.mark.parametrize(
    ("flag", "expected", "gone"),
    [
        ("data-agent.enabled=false", _OFFLINE_COMPONENTS, _ONLINE_COMPONENTS),
        ("data-agent-learning.enabled=false", _ONLINE_COMPONENTS, _OFFLINE_COMPONENTS),
    ],
    ids=["online-off", "offline-off"],
)
def test_disabling_a_plane_renders_only_the_other(
    flag: str, expected: set[str], gone: set[str]
) -> None:
    """The `condition:` on each dependency is what keeps the two lifecycles separate
    under one release — you must still be able to stop the entire learning loop without
    touching the agent serving traffic. A plane switched off must leave NOTHING behind:
    its ConfigMap goes too, or the next `helm upgrade` re-adopts a config for workloads
    that no longer exist."""
    docs = _docs(_PLATFORM, "platform", flag)
    assert _components(docs) == expected
    assert not _components(docs) & gone
    assert len(_config_maps(docs)) == 1
    # The surviving plane must not have been left naming the departed one's objects.
    # `<fullname>-secret` is derived from the ConfigMap that DID render: both names go
    # through the same `suffixedName` helper and "config"/"secret" are the same length,
    # so swapping the suffix reproduces it exactly, truncation included.
    remaining = next(iter(_config_maps(docs)))
    assert not _dangling(docs, f"{remaining.removesuffix('config')}secret")


# --------------------------------------------------------------------------- #
# standalone installs must be untouched by the global-awareness edit
# --------------------------------------------------------------------------- #

# The exact lines the global merge added to BOTH configmap templates, and the one line
# it changed. Removing the first and reverting the second reconstructs the PRE-CHANGE
# template — which is the only way to assert "this edit is inert" rather than merely
# "this render looks plausible".
_MERGE_PROLOGUE = """{{- $globalConfig := ((.Values.global).config) | default dict }}
{{- $config := merge (deepCopy $globalConfig) (.Values.config | default dict) }}
{{- range $key, $val := $globalConfig }}
{{- $_ := set $config $key $val }}
{{- end }}
"""
_MERGED_RANGE = "{{- range $key, $val := $config }}"
_ORIGINAL_RANGE = "{{- range $key, $val := .Values.config }}"

# Standalone scenarios worth proving inert. A single default render would not exercise
# the blank-omission rule, the TENANT_* carve-out, the derived-URL fallbacks or the
# truncation path — all of which the merge sits directly upstream of.
_STANDALONE_SCENARIOS: dict[str, tuple[tuple[str, ...], ...]] = {
    "data-agent": (
        (),
        ("--set", "secrets.existingSecret=shared-secrets"),
        ("--set", "config.TENANT_CLIENT_CODE=CLIENT_A", "--set", "config.TENANT_JTI=J1"),
        ("--set", "components.runtime.enabled=false"),
        ("--set", "components.runtime.enabled=false", "--set", "config.RUNTIME_URL=http://x:8000"),
        ("--set", "ingress.enabled=true", "--set", "uiIngress.enabled=true"),
    ),
    "data-agent-learning": (
        (),
        ("--set", "secrets.existingSecret=shared-secrets"),
        ("--set", "config.TENANT_CLIENT_CODE=CLIENT_A", "--set", "config.TENANT_JTI=J1"),
        ("--set", "components.inbox.enabled=false"),
        ("--set", "redis.enabled=false"),
        ("--set", "config.LEARNING_REDIS_URL=redis://ext:6379/2"),
    ),
}


def _pre_change_copy(chart: Path, destination: Path) -> Path:
    """The chart as it was before the global-awareness edit, byte-for-byte otherwise."""
    copy = destination / chart.name
    shutil.copytree(chart, copy)
    template = copy / "templates" / "configmap.yaml"
    text = template.read_text()
    assert _MERGE_PROLOGUE in text, f"{chart.name}: the merge prologue moved; update this test"
    assert text.count(_MERGED_RANGE) == 1, f"{chart.name}: the merged range moved; update this test"
    template.write_text(
        text.replace(_MERGE_PROLOGUE, "").replace(_MERGED_RANGE, _ORIGINAL_RANGE)
    )
    return copy


@pytest.mark.parametrize("chart", [_AGENT, _LEARNING], ids=lambda c: c.name)
def test_standalone_renders_are_byte_identical_without_a_global(chart: Path, tmp_path: Path) -> None:
    """Two separate releases stay a first-class install path, so the edit that makes
    these charts umbrella-aware must be provably inert without an umbrella.

    `.Values.global` is absent standalone; the parenthesised access yields nil,
    `default dict` makes it empty, and both the merge and the re-assert reduce to
    `.Values.config` unchanged. "Reduce to unchanged" is exactly the kind of claim that
    is true until sprig's `merge` reorders a map or coerces a type, so this compares the
    RENDERED BYTES against the pre-change template rather than trusting the reasoning."""
    baseline = _pre_change_copy(chart, tmp_path)
    for scenario in _STANDALONE_SCENARIOS[chart.name]:
        for release in ("t", "platform-analytics-staging-euwest1"):
            before = subprocess.run(
                ["helm", "template", release, str(baseline), *scenario],
                capture_output=True, text=True, check=True,
            ).stdout
            after = subprocess.run(
                ["helm", "template", release, str(chart), *scenario],
                capture_output=True, text=True, check=True,
            ).stdout
            assert after == before, (
                f"{chart.name} {release} {' '.join(scenario)}: the global merge changed a "
                "STANDALONE render"
            )


@pytest.mark.parametrize("chart", [_AGENT, _LEARNING], ids=lambda c: c.name)
def test_the_example_values_render_identically_too(chart: Path, tmp_path: Path) -> None:
    """`values-example.yaml` is the shape an operator copies, and it sets far more of
    the surface than any `--set` above. Rendered through the pre-change template it must
    still come out byte-for-byte the same."""
    baseline = _pre_change_copy(chart, tmp_path)
    # The example file is copied along with the chart, so point each render at its own.
    before = subprocess.run(
        ["helm", "template", "t", str(baseline), "-f", str(baseline / "values-example.yaml")],
        capture_output=True, text=True, check=True,
    ).stdout
    after = subprocess.run(
        ["helm", "template", "t", str(chart), "-f", str(chart / "values-example.yaml")],
        capture_output=True, text=True, check=True,
    ).stdout
    assert after == before
