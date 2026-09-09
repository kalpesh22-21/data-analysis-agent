from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "data-agent"
_CONFIG_KEYS = {
    "HELP_CENTER_ENABLED",
    "HELP_CENTER_SEARCH_URL",
    "HELP_CENTER_DOCUMENTS_URL",
    "HELP_CENTER_TIMEOUT_SECONDS",
    "HELP_CENTER_SEARCH_CANDIDATE_LIMIT",
    "HELP_CENTER_SEARCH_TOP_K",
    "CAPABILITY_TOOLS_ENABLED",
    "CAPABILITY_PREFETCH_ENABLED",
    "CAPABILITY_API_URL",
    "CAPABILITY_TIMEOUT_SECONDS",
    "CAPABILITY_RESOLUTION_PATH",
}


@pytest.mark.parametrize("filename", ["values.yaml", "values-example.yaml"])
def test_values_expose_help_center_and_capability_settings(filename: str) -> None:
    values = yaml.safe_load((_CHART / filename).read_text())
    assert _CONFIG_KEYS <= values["config"].keys()
    assert values["config"]["HELP_CENTER_ENABLED"] == "false"
    assert values["config"]["CAPABILITY_TOOLS_ENABLED"] == "false"
    assert values["config"]["CAPABILITY_PREFETCH_ENABLED"] == "false"
    assert "CAPABILITY_API_KEY" not in values["config"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="Requires the 'helm' binary.")
def test_example_values_render_into_configmap_without_capability_secret() -> None:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "t",
            str(_CHART),
            "-f",
            str(_CHART / "values-example.yaml"),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    config_map = next(
        doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == "ConfigMap"
    )
    assert _CONFIG_KEYS <= config_map["data"].keys()
    assert "CAPABILITY_API_KEY" not in config_map["data"]
