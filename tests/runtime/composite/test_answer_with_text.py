from __future__ import annotations

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.answer_with_text import AnswerWithTextTool, clean_evidence


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s", jwt="jwt", column_scope=frozenset())


def test_clean_evidence_keeps_unique_non_blank_tool_names() -> None:
    assert clean_evidence([" runQuery ", "", "runQuery", 3, "runBlueprint"]) == (
        "runQuery",
        "runBlueprint",
    )
    assert clean_evidence("runQuery") == ()


async def test_answer_with_text_reports_answer_and_evidence_presence() -> None:
    result = await AnswerWithTextTool().run(
        {"answer": "There are 12.", "evidence": ["runQuery"]}, _credentials()
    )

    assert result.status == "ok"
    assert result.provenance == frozenset()
    assert result.result_preview is not None
    assert result.result_preview.preview_rows == [[True, True]]
