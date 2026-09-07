from __future__ import annotations

import logging
import re
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from data_agent.http_daemon import run_http_daemon

Kind = Literal["navigation", "data_widget"]

TOOLS: dict[str, dict[str, Any]] = {
    "navigate_to_position_management": {
        "name": "navigate_to_position_management",
        "version": "1",
        "kind": "navigation",
        "description": "Open the Position Management page.",
        "summary": "Open Position Management.",
        "parameters": [],
        "metadata": {
            "preamble_url": "ember:GenericButton",
            "arguments": {
                "links": [
                    {
                        "clRedirect": "web.php/positionmanagement/index",
                        "webPage": "Position Management",
                        "description": "Open Position Management.",
                    }
                ]
            },
        },
        "questions": ["Take me to position management", "Where is position management"],
        "actions": ["Manage positions", "Open position management"],
        "data_points": ["Position information", "Seat creation history"],
    },
    "navigate_to_manage_position_seats": {
        "name": "navigate_to_manage_position_seats",
        "version": "1",
        "kind": "navigation",
        "description": "Open the Manage Position Seats page.",
        "summary": "Open Manage Position Seats.",
        "parameters": [],
        "metadata": {
            "preamble_url": "ember:GenericButton",
            "arguments": {
                "links": [
                    {
                        "clRedirect": "web.php/positionseats/index",
                        "webPage": "Manage Position Seats",
                        "description": "Open Manage Position Seats.",
                    }
                ]
            },
        },
        "questions": ["Take me to position seats", "Where can I update position seats"],
        "actions": ["Manage position seats", "Create a position seat"],
        "data_points": ["Position seats", "Seat creation history"],
    },
    "submit_paf_transaction": {
        "name": "submit_paf_transaction",
        "version": "1",
        "kind": "data_widget",
        "description": "Create a saved Personnel Action Form draft for an employee.",
        "summary": "Create a saved PAF draft.",
        "parameters": [
            {
                "name": "employees",
                "description": "The employee or employees.",
                "type": "employee",
                "collection": True,
                "enum": None,
                "enumDescriptions": None,
                "enumDisplayNames": None,
                "default": "all",
                "resolution": {"strategy": "torch", "entity_type": "employee"},
            },
            {
                "name": "department",
                "description": "The destination department.",
                "type": "department",
                "collection": False,
                "enum": None,
                "enumDescriptions": None,
                "enumDisplayNames": None,
                "default": None,
                "resolution": {
                    "strategy": "resolve_values",
                    "semantic_type": "department",
                },
            },
        ],
        "metadata": {"preamble_url": "ember:PafCard"},
        "questions": ["How do I change an employee position", "How do I promote an employee"],
        "actions": ["Change employee position", "Promote employee", "Move employee department"],
        "data_points": [],
    },
    "show_employee_profile": {
        "name": "show_employee_profile",
        "version": "1",
        "kind": "data_widget",
        "description": "Display an employee profile widget for one employee.",
        "summary": "Display an employee profile widget.",
        "parameters": [
            {
                "name": "employees",
                "description": "The employee or employees.",
                "type": "employee",
                "collection": True,
                "enum": None,
                "enumDescriptions": None,
                "enumDisplayNames": None,
                "default": "all",
                "resolution": {"strategy": "torch", "entity_type": "employee"},
            }
        ],
        "metadata": {"preamble_url": "ember:EmployeeCard"},
        "questions": ["Show me an employee profile", "What is an employee's current position"],
        "actions": ["Open employee profile"],
        "data_points": ["Employee position", "Employee department", "Primary supervisor"],
    },
}


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    kinds: list[Kind]
    limit: int = Field(ge=1, le=10)
    category_limits: dict[str, int]


class HydrateRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    raw_arguments: dict[str, Any]


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _score(query: str, values: list[str]) -> float:
    terms = _terms(query)
    return max((len(terms & _terms(value)) / max(len(terms), 1) for value in values), default=0)


app = FastAPI(title="Mock Capability API")
_logger = logging.getLogger(__name__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/capabilities/search")
async def search(request: SearchRequest) -> dict[str, list[dict[str, Any]]]:
    ranked = []
    for tool in TOOLS.values():
        if tool["kind"] not in request.kinds:
            continue
        category_scores = {
            category: _score(request.query, tool[category])
            for category in ("questions", "actions", "data_points")
        }
        score = max(category_scores.values())
        if score:
            ranked.append((score, tool, category_scores))
    ranked.sort(key=lambda item: (-item[0], item[1]["name"]))
    cards = []
    for _, tool, scores in ranked[: request.limit]:
        cards.append(
            {
                "id": f"capability:{tool['name']}",
                "tool_name": tool["name"],
                "kind": tool["kind"],
                "summary": tool["summary"],
                "matched_questions": tool["questions"][:5] if scores["questions"] else [],
                "matched_actions": tool["actions"][:5] if scores["actions"] else [],
                "matched_data_points": tool["data_points"][:5] if scores["data_points"] else [],
            }
        )
    return {"cards": cards}


@app.get("/v1/capabilities/tools/{tool_name}")
async def get_tool(tool_name: str) -> dict[str, Any]:
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    return {
        key: tool[key]
        for key in ("name", "version", "kind", "description", "parameters", "metadata")
    }


@app.post("/v1/capabilities/tools/{tool_name}/hydrate")
async def hydrate_tool(
    tool_name: str,
    request: HydrateRequest,
    end_user_auth: str | None = Header(default=None, alias="X-End-User-Authorization"),
) -> dict[str, Any]:
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool not found")
    entity_params = {
        parameter["name"]
        for parameter in tool["parameters"]
        if parameter["collection"] and parameter["type"] not in {"string", "date", "dateRange"}
    }
    known_params = {parameter["name"] for parameter in tool["parameters"]}
    unknown_params = request.raw_arguments.keys() - known_params
    if unknown_params:
        raise HTTPException(status_code=422, detail=f"Unknown parameters: {sorted(unknown_params)}")
    if entity_params & request.raw_arguments.keys() and not end_user_auth:
        raise HTTPException(status_code=401, detail="End-user authorization required")

    arguments: dict[str, Any] = {}
    resolved_entities: dict[str, list[str]] = {}
    for name, value in request.raw_arguments.items():
        if name in entity_params:
            values = value if isinstance(value, list) else [value]
            arguments[name] = [
                {
                    "eecode": re.sub(r"[^A-Z0-9]", "", str(item).upper())[:12] or "MOCK",
                    "description": str(item),
                    "entity_type": "employee",
                }
                for item in values
            ]
            resolved_entities.setdefault("employee", []).extend(str(item) for item in values)
        else:
            arguments.setdefault("filters", {})[name] = {"identifier": value}
    arguments["has_unresolved_entities"] = False
    return {
        "name": tool_name,
        "arguments": arguments,
        "metadata": {**tool["metadata"], "ui_parameters": tool["parameters"]},
        "parameters": tool["parameters"],
        "next_best_tools": [],
        "are_best_tools_suggestion": False,
        "resolved_entities": resolved_entities,
        "additional_arguments": {},
    }


if __name__ == "__main__":
    raise SystemExit(
        run_http_daemon(
            lambda: app,
            host="127.0.0.1",
            port=18006,
            logger=_logger,
            process="mock-capability",
            log_level="info",
        )
    )
