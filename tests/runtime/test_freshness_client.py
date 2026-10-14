"""Runtime-owned lookup: tenant binding, complete coverage, and optional failures."""

import asyncio
from email.parser import BytesParser
from email.policy import default
from unittest.mock import Mock

import httpx
import pytest

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.provenance.freshness import FreshnessClient
from tests.runtime.dispatch.test_tool_dispatcher import _credentials


@pytest.fixture
def verifier(monkeypatch):
    mock = Mock(return_value={"proc_center": "PC01", "clientcode": "CLIENT_A"})
    monkeypatch.setattr("data_agent.runtime.provenance.freshness.verify_jwt_claims", mock)
    return mock


def client(handler, **settings):
    return FreshnessClient(
        RuntimeSettings(
            _env_file=None, data_as_of_clickhouse_url="http://clickhouse:8123", **settings
        ),
        transport=httpx.MockTransport(handler),
    )


async def test_bound_tenants_complete_coverage_and_minimum(verifier):
    def handler(request):
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        body = {
            part.get_param("name", header="content-disposition"): [part.get_content()]
            for part in message.iter_parts()
        }
        assert not request.url.query
        assert body["param_pc"] == ["PC01"]
        assert body["param_client"] == ["CLIENT_A"]
        assert body["param_table_0"] == ["employee"]
        assert body["param_table_1"] == ["payroll"]
        assert body["readonly"] == ["1"]
        sql = body["query"][0]
        assert "etl_product_refresh_status FINAL" in sql
        assert "max(last_sync_date)" in sql
        assert "{pc:String}" in sql and "CLIENT_A" not in sql
        return httpx.Response(
            200, text='["employee","1791871200000"]\n["payroll","1791783000000"]\n'
        )

    result = await client(handler).lookup(
        "runQuery",
        {"sql": "SELECT count(*) FROM employee JOIN payroll USING (id)"},
        _credentials(),
        None,
    )
    assert result == "2026-10-12T05:30:00.000Z"
    assert verifier.call_args.args == (_credentials().jwt,)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT count(*) FROM employee", ["employee"]),
        ("WITH e AS (SELECT * FROM employee) SELECT * FROM e", ["employee"]),
        ("SELECT * FROM employee WHERE id IN (SELECT id FROM payroll)", ["employee", "payroll"]),
        ("SELECT * FROM dbpcm_scratch.employee", None),
        ("SELECT * FROM numbers(10)", None),
        ("SELECT 1", None),
    ],
)
def test_source_table_detection(sql, expected):
    assert client(lambda _: None)._tables("runQuery", {"sql": sql}, None) == expected


def test_provenance_union_and_sample():
    c = client(lambda _: None)
    assert c._tables(
        "runQuery", {"sql": "SELECT * FROM employee"}, {("dbpcm_warehouse.payroll", "id")}
    ) == ["employee", "payroll"]
    assert c._tables("sampleRows", {"database": "dbpcm_warehouse", "table": "employee"}, None) == [
        "employee"
    ]
    assert c._tables("sampleRows", {"database": "other", "table": "employee"}, None) is None


@pytest.mark.parametrize(
    "status,body",
    [
        (404, "Unknown table"),
        (500, "Unknown database"),
        (403, "Denied"),
        (200, ""),
        (200, '["payroll",1791871200000]'),
        (200, '["employee","not a date"]'),
        (200, "malformed"),
    ],
)
async def test_missing_table_incomplete_or_bad_response_is_unknown(verifier, status, body):
    c = client(lambda _: httpx.Response(status, text=body))
    assert (
        await c.lookup("runQuery", {"sql": "SELECT * FROM employee"}, _credentials(), None) is None
    )


async def test_any_unmatched_source_omits_date(verifier):
    c = client(lambda _: httpx.Response(200, text='["employee",1791871200000]'))
    assert (
        await c.lookup(
            "explainQuery",
            {"sql": "SELECT * FROM employee JOIN payroll USING (id)"},
            _credentials(),
            None,
        )
        is None
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"data_as_of_enabled": False},
        {"data_as_of_clickhouse_url": ""},
    ],
)
async def test_disabled_skips_verification_and_http(verifier, settings):
    handler = Mock()
    config = {"data_as_of_clickhouse_url": "http://clickhouse:8123", **settings}
    c = FreshnessClient(
        RuntimeSettings(_env_file=None, **config), transport=httpx.MockTransport(handler)
    )
    assert (
        await c.lookup("runQuery", {"sql": "SELECT * FROM employee"}, _credentials(), None) is None
    )
    verifier.assert_not_called()
    handler.assert_not_called()


@pytest.mark.parametrize(
    "claims", [{}, {"proc_center": "PC01"}, {"proc_center": [], "clientcode": "A"}]
)
async def test_missing_tenant_never_queries(verifier, claims):
    verifier.return_value = claims
    handler = Mock()
    assert (
        await client(handler).lookup(
            "runQuery", {"sql": "SELECT * FROM employee"}, _credentials(), None
        )
        is None
    )
    handler.assert_not_called()


async def test_invalid_jwt_never_queries(verifier):
    verifier.side_effect = ValueError("invalid signature")
    handler = Mock()
    assert (
        await client(handler).lookup(
            "runQuery", {"sql": "SELECT * FROM employee"}, _credentials(), None
        )
        is None
    )
    handler.assert_not_called()


async def test_timeout_is_optional(verifier):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    c = client(handler, data_as_of_timeout_seconds=0.02)
    assert (
        await c.lookup("runQuery", {"sql": "SELECT * FROM employee"}, _credentials(), None) is None
    )
