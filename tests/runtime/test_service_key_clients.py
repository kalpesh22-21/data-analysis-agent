"""Service-key auth mode for the MCP export clients (singleton-hydrator redesign).

`HttpCatalogClient`/`HttpCorpusClient` gain an optional `service_key`: when set, a fetch
sends `X-Service-Key: <key>` INSTEAD of the `Authorization: Bearer` + `X-Session-Id`
pair; when unset, the per-request-JWT behavior is unchanged. Asserted at the
`_auth_headers` branch (the single seam both clients route every fetch through).
"""

from __future__ import annotations

from data_agent.runtime.catalog.export_client import HttpCatalogClient, build_catalog_cache
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.retrieval.corpus_client import HttpCorpusClient


def test_catalog_client_service_key_sends_x_service_key_only() -> None:
    client = HttpCatalogClient("http://mcp/catalog", service_key="svc-123")
    headers = client._auth_headers(jwt="user-jwt", session_id="sess-1")
    assert headers == {"X-Service-Key": "svc-123"}
    # The user creds never reach the wire when a service key is configured.
    assert "Authorization" not in headers
    assert "X-Session-Id" not in headers


def test_catalog_client_without_service_key_sends_bearer_pair() -> None:
    client = HttpCatalogClient("http://mcp/catalog")
    headers = client._auth_headers(jwt="user-jwt", session_id="sess-1")
    assert headers == {"Authorization": "Bearer user-jwt", "X-Session-Id": "sess-1"}


def test_corpus_client_service_key_sends_x_service_key_only() -> None:
    client = HttpCorpusClient("http://mcp", service_key="svc-abc")
    headers = client._auth_headers(jwt="user-jwt", session_id="sess-2")
    assert headers == {"X-Service-Key": "svc-abc"}
    assert "Authorization" not in headers
    assert "X-Session-Id" not in headers


def test_corpus_client_without_service_key_sends_bearer_pair() -> None:
    client = HttpCorpusClient("http://mcp")
    headers = client._auth_headers(jwt="user-jwt", session_id="sess-2")
    assert headers == {"Authorization": "Bearer user-jwt", "X-Session-Id": "sess-2"}


def test_empty_service_key_falls_back_to_jwt_mode() -> None:
    # An empty string is normalized to None (no key) → per-request JWT mode.
    cat = HttpCatalogClient("http://mcp/catalog", service_key="")
    assert cat._auth_headers(jwt="j", session_id="s") == {
        "Authorization": "Bearer j",
        "X-Session-Id": "s",
    }
    corp = HttpCorpusClient("http://mcp", service_key="")
    assert corp._auth_headers(jwt="j", session_id="s") == {
        "Authorization": "Bearer j",
        "X-Session-Id": "s",
    }


def test_build_catalog_cache_threads_service_key_into_http_client() -> None:
    settings = RuntimeSettings(_env_file=None, mcp_service_key="svc-cfg")
    cache = build_catalog_cache(settings)
    client = cache._client
    assert isinstance(client, HttpCatalogClient)
    assert client._auth_headers(jwt="j", session_id="s") == {"X-Service-Key": "svc-cfg"}


def test_build_catalog_cache_default_jwt_without_service_key() -> None:
    # Without a configured service key the catalog cache keeps per-request-JWT mode.
    settings = RuntimeSettings(_env_file=None)
    cache = build_catalog_cache(settings)
    client = cache._client
    assert isinstance(client, HttpCatalogClient)
    assert "Authorization" in client._auth_headers(jwt="j", session_id="s")
