"""Lifecycle-hook seams (D72, docs/12-extensibility.md).

A narrow first instance of the chapter's H1-H17, covering the two answer-table
conditions with a real failure mode today. Everything ships DORMANT: the default
registry is empty, nothing is wired in `app.py`, and the runtime behaves identically to
having no hooks at all until a callable is deliberately registered.
"""

from data_agent.runtime.hooks.answer_table import (
    AnswerTableEvent,
    AnswerTableHooks,
    EphemeralDesignationHook,
    UnresolvedDesignationHook,
)

__all__ = [
    "AnswerTableEvent",
    "AnswerTableHooks",
    "EphemeralDesignationHook",
    "UnresolvedDesignationHook",
]
