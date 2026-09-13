"""The local identifier fixture matches the requested dynamic schema and returns no real PII."""
import httpx
from scripts.mock_capability_api import app

from data_agent.runtime.capabilities.client import HttpCapabilityClient
from data_agent.runtime.capabilities.resolution import CapabilityResolutionRegistry


async def test_identifier_is_discoverable_and_translates_the_requested_parameters():
    client = HttpCapabilityClient(base_url="http://mock/v1/capabilities", transport=httpx.ASGITransport(app=app))
    cards = await client.search("Show me the SSN of every employee named Smith", ("data_widget",), end_user_jwt="mock-user")
    assert cards[0].tool_name == "get_employee_personal_identifier"
    assert cards[0].presentation.title == "Personal identifiers"
    definition = await client.get_definition(cards[0].tool_name, end_user_jwt="mock-user")
    schema = definition.tool_schema(CapabilityResolutionRegistry.load("config/capability-value-resolution.yaml"))
    properties = schema["parameters"]["properties"]
    assert set(properties) == {"employees", "department", "position", "work_location", "answer", "serves_intent"}
    for name in ("employees", "department", "position", "work_location"):
        assert properties[name]["type"] == "array"
        assert properties[name]["default"] == ["all"]
    assert "employee names, not employee codes" in properties["employees"]["description"]
    assert "position_title_position_info" in properties["position"]["description"]
    assert "work_location_description" in properties["work_location"]["description"]
    assert "never present the nearest related option" in properties["answer"]["description"]
    assert schema["parameters"]["additionalProperties"] is False


async def test_identifier_mock_hydrates_references_without_fabricating_identifier_values():
    client = HttpCapabilityClient(base_url="http://mock/v1/capabilities", transport=httpx.ASGITransport(app=app))
    card = await client.hydrate("get_employee_personal_identifier", query="Show identifiers for Smith",
                                raw_arguments={"employees": ["Smith"], "department": ["Sales"]},
                                end_user_jwt="mock-user", forward_end_user=True)
    assert card["arguments"]["employees"] == [{"eecode": "SMITH", "description": "Smith", "entity_type": "employee"}]
    assert card["arguments"]["filters"]["department"] == {"identifier": ["Sales"]}
    assert "ssn" not in card["arguments"]
    assert card["metadata"]["preamble_url"] == "ember:PersonalIdentifierCard"


async def test_adjacent_probe_fixtures_do_not_claim_direct_deposit_or_pay_stub_coverage():
    client = HttpCapabilityClient(base_url="http://mock/v1/capabilities", transport=httpx.ASGITransport(app=app))
    banking = await client.get_definition("navigate_to_banking_center", end_user_jwt="mock-user")
    payroll = await client.get_definition("get_payroll_totals", end_user_jwt="mock-user")
    assert "does not edit an employee's direct deposit" in banking.description
    assert "Does not show or link to individual pay stubs" in payroll.description


async def test_mock_requires_authorization_and_exposes_the_separate_healthy_article_fixture():
    async with httpx.AsyncClient(base_url="http://mock", transport=httpx.ASGITransport(app=app)) as client:
        response = await client.get("/v1/capabilities/tools/get_employee_personal_identifier")
        assert response.status_code == 401
        response = await client.post("/probe-help-center/search", json={"query": "mobile clock in"}, headers={"Authorization": "Bearer mock-user"})
        article = response.json()["documents"][0]
        response = await client.get(f"/probe-help-center/documents/{article['id']}", headers={"Authorization": "Bearer mock-user"})
        assert "Synthetic acceptance guide" in response.json()["content"]
        assert (await client.post("/help-center/search", json={"query": "mobile clock in"})).status_code == 404
