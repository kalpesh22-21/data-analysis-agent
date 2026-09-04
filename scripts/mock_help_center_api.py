from __future__ import annotations

import logging
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from data_agent.http_daemon import run_http_daemon


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(ge=1, le=100)


DOCUMENTS = {
    "paf-position-change": (
        "# Change an employee's position with a Personnel Action Form\n\n"
        "Open Personnel Action Forms and select Add PAF. Choose the employee, select "
        "Position Change as the action type, enter the effective date, and choose the new "
        "position seat. Review any related department, supervisor, pay-class, and rate "
        "changes before saving the PAF as a draft or submitting it for approval. Access to "
        "the available fields depends on the user's permission profile."
    ),
    "position-management-overview": (
        "# Position Management overview\n\n"
        "Position Management ties job attributes to positions instead of individual "
        "employees. Use it to maintain positions, position families, management levels, "
        "and one-to-one position seats across the organization."
    ),
    "performance-management": (
        "# Performance Management\n\n"
        "Performance Management supports goals and reviews. Administrators can create, "
        "schedule, and assign reviews, then track goals and competencies."
    ),
    "password-expiration": (
        "# Employee password expiration\n\n"
        "Password expiration settings are maintained from User Access and Security. The "
        "available controls depend on the organization's security configuration and the "
        "administrator's permission profile."
    ),
    "time-off-request": (
        "# Requesting time off\n\n"
        "Employees can request available time off from Employee Self-Service by opening "
        "Time-Off Requests, selecting a policy and dates, and submitting the request."
    ),
    "expense-reports": (
        "# Expense reports\n\n"
        "Expense Management lets employees create reports, attach receipts, and submit "
        "expenses through the configured approval workflow."
    ),
}


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


app = FastAPI(title="Mock Help Center API")
_logger = logging.getLogger(__name__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/help-center/search")
async def search(request: SearchRequest) -> dict[str, list[dict[str, object]]]:
    query_terms = _terms(request.query)
    ranked: list[tuple[float, str, str]] = []
    for article_id, content in DOCUMENTS.items():
        overlap = len(query_terms & _terms(content))
        score = overlap / max(len(query_terms), 1)
        if score:
            ranked.append((score, article_id, content[:400]))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return {
        "documents": [
            {"id": article_id, "score": score, "snippet": snippet}
            for score, article_id, snippet in ranked[: request.limit]
        ]
    }


@app.get("/v1/help-center/documents/{article_id}")
async def get_document(article_id: str) -> dict[str, str]:
    content = DOCUMENTS.get(article_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"id": article_id, "content": content}


if __name__ == "__main__":
    raise SystemExit(
        run_http_daemon(
            lambda: app,
            host="127.0.0.1",
            port=18005,
            logger=_logger,
            process="mock-help-center",
            log_level="info",
        )
    )
