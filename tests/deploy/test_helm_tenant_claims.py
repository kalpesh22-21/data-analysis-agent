"""The chart must not mint a tenant nobody chose.

`TENANT_CLIENT_CODE` / `TENANT_PROC_CENTER` / `TENANT_JTI` decide which warehouse
tenant the offline golden replay runs as. Their `runtime/config.py` defaults are the
LOCAL DEV SEED (CLIENT_A / PC01 / TESTJTI001), and the chart used to repeat those
values — so a deployment that simply forgot to configure a tenant still minted one.
Row policies FILTER rather than error, so that replay matches zero rows, the grain
teeth read `0 == 0`, and every promotion passes its verification gate against
nobody's data. The readiness gates added for this only fire on a BLANK claim, which
nobody would ever set explicitly.

Two things therefore have to hold together, and only one of them is the values file:

  1. the chart defaults the three keys to "", and
  2. the ConfigMap RENDERS them anyway. The template omits blank values by design,
     and an omitted env var is not a blank one — the process would fall through to
     the dev-seed code default and the gate would stay dark. Blanking the values
     without this second half is a fix that changes nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "data-agent"
_TENANT_KEYS = ("TENANT_CLIENT_CODE", "TENANT_PROC_CENTER", "TENANT_JTI")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Requires the 'helm' binary.")


def _config_map(*values_files: Path) -> dict[str, str]:
    args = ["helm", "template", "t", str(_CHART)]
    for path in values_files:
        args += ["-f", str(path)]
    rendered = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "ConfigMap":
            return doc.get("data") or {}
    raise AssertionError("the chart rendered no ConfigMap")


def test_the_chart_ships_no_tenant_and_says_so_out_loud() -> None:
    """Present but blank — NOT absent. An absent key is indistinguishable, to the
    process, from a key nobody configured, and pydantic then supplies the dev seed."""
    data = _config_map()
    for key in _TENANT_KEYS:
        assert key in data, (
            f"{key} is missing from the rendered ConfigMap. A blank value omitted from "
            "the ConfigMap leaves the env var UNSET, so the process falls back to the "
            "dev-seed default in runtime/config.py and the readiness gate never fires — "
            "the exact silent-green this blanking exists to prevent."
        )
        assert data[key] == ""


def test_a_configured_deployment_still_gets_its_tenant() -> None:
    """The blank default must not be a wall: values-example.yaml is the contract, and
    setting the three keys must reach the ConfigMap unchanged."""
    data = _config_map(_CHART / "values-example.yaml")
    assert data["TENANT_CLIENT_CODE"] == "ACME"
    assert data["TENANT_PROC_CENTER"] == "PC42"
    assert data["TENANT_JTI"] == "svc-data-agent"


def test_blank_omission_still_applies_to_everything_else() -> None:
    """The always-render carve-out is for the tenant claims ONLY. Other blanks stay
    omitted so their code defaults keep working (that IS the right rule elsewhere)."""
    data = _config_map()
    assert "CATALOG_API_URL" not in data  # blank in values.yaml, deliberately omitted
    assert "OTLP_ENDPOINT" not in data


def test_the_blank_default_reaches_the_settings_model_as_blank() -> None:
    """Closes the loop the ConfigMap opens: an empty env var must OVERRIDE the dev-seed
    field default, not be discarded as 'unset' by pydantic-settings."""
    import os

    from data_agent.runtime.config import RuntimeSettings

    previous = {k: os.environ.get(k) for k in _TENANT_KEYS}
    try:
        for key in _TENANT_KEYS:
            os.environ[key] = ""
        settings = RuntimeSettings(_env_file=None)
        assert settings.tenant_client_code == ""
        assert settings.tenant_proc_center == ""
        assert settings.tenant_jti == ""
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
