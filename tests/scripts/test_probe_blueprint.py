"""The diagnostic runs the executor, preserving failures and pauses in its report."""

import json
from dataclasses import asdict

import pytest
import yaml
from scripts import probe_blueprint as probe
from tests.runtime.blueprint.test_bound_slot_regression import PROVENANCE, detail
from tests.runtime.blueprint.test_executor import _ok_result, _rq

from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher, ToolResult
from data_agent.runtime.mcp.fake_client import FakeMCPClient


@pytest.mark.parametrize("mode", ["completed", "paused", "failed"])
async def test_diagnostic_uses_bound_template_and_reports_resolution_outcomes(
    tmp_path, monkeypatch, mode
):
    raw = asdict(detail())
    raw["uses"] = sorted(raw["uses"])
    path = tmp_path / "blueprint.yaml"
    path.write_text(yaml.safe_dump(raw))
    args = probe.parser().parse_args(
        [
            "--blueprint",
            str(path),
            "--mcp",
            "http://test/mcp",
            "--bindings",
            '{"employee":"A4I8","period":"2026-08-08"}',
        ]
    )
    export = json.loads(probe.Path("tests/fixtures/catalog_export.json").read_text())

    async def fetch(self, **kwargs):
        return export

    monkeypatch.setattr(probe.HttpCatalogClient, "fetch_export", fetch)
    monkeypatch.setattr(probe, "RealMCPClient", lambda url: FakeMCPClient())
    results = [
        _ok_result(PROVENANCE, _rq(["employee_code"], [["A4I8"]])),
        _ok_result(PROVENANCE, _rq(["pay_period_end_date"], [["2026-08-08"]])),
        _ok_result(PROVENANCE, _rq(["register_type", "type_code", "amount"], [["R", "REG", 12]])),
        _ok_result(PROVENANCE, _rq(["__bp_n", "__bp_d"], [[1, 1]])),
    ]
    if mode == "paused":
        results = [_ok_result(PROVENANCE, _rq(["employee_code"], []))]
    elif mode == "failed":
        results = [
            ToolResult(
                status="error",
                tool_name="runQuery",
                error_code="API_PROVENANCE_INVALID",
                retryable=False,
                user_message="Actual API error",
                provenance=None,
                result_preview=None,
                result_full=None,
            )
        ]

    async def dispatch(self, name, args, creds, **kwargs):
        assert creds.jwt == "test-token"
        return results.pop(0)

    monkeypatch.setattr(ToolDispatcher, "dispatch", dispatch)
    report = {"calls": []}
    code = await probe.diagnose(args, "test-token", report)
    assert report["status"] == mode
    assert code == {"completed": 0, "paused": 2, "failed": 1}[mode]
    assert "WHERE" in report["calls"][0]["arguments"]["sql"]
    if mode == "completed":
        assert "GROUP BY register_type, type_code" in report["calls"][2]["arguments"]["sql"]
        assert report["row_count"] == 1
    elif mode == "failed":
        assert report["message"] == "Actual API error"
        assert report["calls"][0]["error_code"] == "API_PROVENANCE_INVALID"
    else:
        assert report["reason"] == "blueprint_slot"
    assert "test-token" not in json.dumps(report)


def test_invalid_blueprint_report_does_not_echo_token(tmp_path, monkeypatch, capsys):
    path = tmp_path / "blueprint.yaml"
    path.write_text("[]")
    monkeypatch.setattr(
        "sys.argv",
        [
            "probe",
            "--blueprint",
            str(path),
            "--mcp",
            "http://test/mcp",
            "--token",
            "secret-credential",
        ],
    )
    assert probe.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "blueprint_validation"
    assert "secret-credential" not in json.dumps(report)
