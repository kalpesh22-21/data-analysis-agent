from __future__ import annotations

from typing import TYPE_CHECKING, Any

from data_agent.runtime.dispatch.tool_dispatcher import (
    ToolObserver,
    ToolResult,
    _build_preview,
    _default_observer,
)
from data_agent.runtime.dispatch.tool_envelope import RuntimeToolBase
from data_agent.runtime.model.reranker_client import RerankerClient, RerankerError
from data_agent.runtime.observability.redaction import tool_span_args

from .client import HelpCenterClient, HelpCenterError

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

    from data_agent.runtime.auth.credentials import RuntimeCredentials
    from data_agent.runtime.loop.agent_loop import TurnContext

INVALID_ARGS = "HELP_CENTER_INVALID_ARGS"
UNAVAILABLE = "HELP_CENTER_UNAVAILABLE"
INTERNAL_ERROR = "HELP_CENTER_INTERNAL_ERROR"


def _result(tool_name: str, value: dict[str, Any]) -> ToolResult:
    return ToolResult(
        status="ok",
        tool_name=tool_name,
        error_code=None,
        retryable=None,
        user_message=None,
        provenance=frozenset(),
        result_preview=_build_preview(value, 20, 4_000),
        result_full=value,
    )


class _HelpCenterTool(RuntimeToolBase):
    _INTERNAL_ERROR_CODE = INTERNAL_ERROR
    _INTERNAL_ERROR_MESSAGE = "Help Center is temporarily unavailable."
    _GUARDED_EXCEPTIONS = (HelpCenterError,)

    def __init__(
        self,
        *,
        client: HelpCenterClient,
        observer: ToolObserver = _default_observer,
        tracer: Tracer | None = None,
        disable_redaction: bool = False,
    ) -> None:
        super().__init__(observer=observer, tracer=tracer, disable_redaction=disable_redaction)
        self._client = client

    def _span_args(self, model_args: dict[str, Any]) -> dict[str, Any]:
        return tool_span_args(self.tool_name, model_args, disable_redaction=self._disable_redaction)

    def _on_guarded_exception(self, exc: Exception, model_args: dict[str, Any]) -> ToolResult:
        return self._error(UNAVAILABLE, "Help Center is temporarily unavailable.", retryable=True)


class SearchHelpCenterTool(_HelpCenterTool):
    tool_name = "searchHelpCenter"

    def __init__(
        self,
        *,
        client: HelpCenterClient,
        reranker: RerankerClient,
        candidate_limit: int = 25,
        top_k: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(client=client, **kwargs)
        self._reranker = reranker
        self._candidate_limit = candidate_limit
        self._top_k = top_k

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: TurnContext | None
    ) -> ToolResult:
        query = model_args.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._error(INVALID_ARGS, "'query' must be non-empty text.", retryable=True)
        hits = (
            await self._client.search(query.strip(), self._candidate_limit, jwt=credentials.jwt)
        )[: self._candidate_limit]
        reranked = False
        ranked_hits = [(hit.score, hit) for hit in hits]
        if hits:
            try:
                scores = await self._reranker.rerank(query, [hit.snippet for hit in hits])
                ranked_hits = sorted(
                    zip(scores, hits, strict=True),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                reranked = True
            except RerankerError:
                pass
        selected = ranked_hits[: self._top_k]
        return _result(
            self.tool_name,
            {
                "documents": [
                    {
                        "id": hit.id,
                        "search_score": hit.score,
                        "rerank_score": score if reranked else None,
                        "snippet": hit.snippet,
                    }
                    for score, hit in selected
                ],
                "count": len(selected),
                "reranked": reranked,
            },
        )


class GetHelpCenterDocumentTool(_HelpCenterTool):
    tool_name = "getHelpCenterDocument"

    async def _execute(
        self, model_args: dict[str, Any], credentials: RuntimeCredentials, turn: TurnContext | None
    ) -> ToolResult:
        article_id = model_args.get("id")
        if not isinstance(article_id, str) or not article_id.strip():
            return self._error(INVALID_ARGS, "'id' must be non-empty text.", retryable=True)
        document = await self._client.get_document(article_id.strip(), jwt=credentials.jwt)
        if document is None:
            return _result(self.tool_name, {"found": False, "id": article_id.strip()})
        return _result(
            self.tool_name, {"found": True, "id": document.id, "content": document.content}
        )


__all__ = ["GetHelpCenterDocumentTool", "SearchHelpCenterTool"]
