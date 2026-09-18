"""Bound read retries and rounds without new evidence; never synthesize data."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from data_agent.runtime.dispatch.tool_dispatcher import ToolResult

from .help_grounding import HELP_TOOLS
from .read_guard import IDEMPOTENT_READ_TOOLS

HELP_BREAKER_TEXT = (
    "Help Center is unavailable for the rest of this turn after repeated availability failures. "
    "Do not call searchHelpCenter or getHelpCenterDocument again. Preserve supported work; "
    "use complete articles already received if sufficient, or disclose that product guidance "
    "could not be verified. Finish through finalizeAnswer."
)
NO_PROGRESS_TEXT = (
    "I stopped because repeated attempts were no longer adding information. "
    "I could not complete the remaining work from the available evidence."
)
NO_PROGRESS_COACH = (
    "Recent rounds added no new evidence. Stop repeating reads or preparations. "
    "Use the successful results already received to finalize the supported parts, "
    "and disclose any unfinished parts. Do not treat failed attempts as proof that data is absent."
)


def safety_refusal(name: str, code: str, detail: str) -> ToolResult:
    return ToolResult(
        status="error",
        tool_name=name,
        error_code=code,
        retryable=False,
        user_message=detail,
        denial_detail=detail,
        provenance=frozenset(),
        result_preview=None,
        result_full=None,
    )


class LoopSafety:
    def __init__(
        self, *, read_limit: int, no_progress_limit: int, help_failure_limit: int, observer
    ):
        self.read_limit = read_limit
        self.no_progress_limit = no_progress_limit
        self.help_failure_limit = help_failure_limit
        self.observer = observer
        self.read_attempts: Counter[str] = Counter()
        self.read_denials: Counter[str] = Counter()
        self.help_failures = 0
        self.help_open = False
        self.stagnant_rounds = 0
        self._seen: set[str] = set()
        self._round_progress = False

    def begin_round(self):
        self._round_progress = False

    def check(self, name: str) -> ToolResult | None:
        if self.help_open and name in HELP_TOOLS:
            return safety_refusal(name, "HELP_CENTER_CIRCUIT_OPEN", HELP_BREAKER_TEXT)
        if name in IDEMPOTENT_READ_TOOLS and self.read_attempts[name] >= self.read_limit:
            self.read_denials[name] += 1
            stage = "coach" if self.read_denials[name] == 1 else "decline"
            self.observer(
                "loop_read_refetch_limit",
                {
                    "tool_name": name,
                    "limit": self.read_limit,
                    "attempts": self.read_attempts[name],
                    "phase": stage,
                },
            )
            return safety_refusal(
                name,
                "READ_REFETCH_LIMIT",
                f"Read allowance exhausted for {name} ({self.read_limit} executions per window). "
                + ("Reuse earlier results. " if stage == "coach" else "Further reads are declined. ")
                + "Changing arguments does not renew it; no result was fetched. "
                "Finalize supported parts and disclose any missing coverage.",
            )
        return None

    def record_attempt(self, name: str):
        if name in IDEMPOTENT_READ_TOOLS:
            self.read_attempts[name] += 1

    def observe_help(self, name: str, error_code: str | None, *, emit=True):
        if name not in HELP_TOOLS or self.help_open:
            return
        self.help_failures = (
            self.help_failures + 1 if error_code == "HELP_CENTER_UNAVAILABLE" else 0
        )
        if self.help_failures >= self.help_failure_limit:
            self.help_open = True
            if emit:
                self.observer(
                    "loop_help_center_circuit_opened",
                    {
                        "tool_name": name,
                        "failures": self.help_failures,
                        "limit": self.help_failure_limit,
                        "error_code": "HELP_CENTER_UNAVAILABLE",
                    },
                )

    def observe_result(self, name: str, args: dict[str, Any], result: ToolResult, *, reused=False):
        if reused or result.status != "ok" or result.error_code:
            return
        if name in {"answerWithTable", "answerWithText", "finalizeAnswer", "recordAssumptions"}:
            return
        payload = (
            result.result_preview.to_doc()
            if result.result_preview is not None
            else result.result_full
        )
        if payload is None:
            return
        # Distinct executions can establish distinct empty populations; discovery
        # and UI preparations must actually change their result, not just their args.
        identity = args if name in {"runQuery", "runBlueprint"} else None
        digest = hashlib.sha256(
            json.dumps(
                [name, identity, payload],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        if digest not in self._seen:
            self._seen.add(digest)
            self._round_progress = True

    def end_round(self) -> bool:
        self.stagnant_rounds = 0 if self._round_progress else self.stagnant_rounds + 1
        return self.stagnant_rounds >= self.no_progress_limit
