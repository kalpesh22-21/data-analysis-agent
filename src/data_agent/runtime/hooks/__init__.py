"""Lifecycle-hook seams (D72, docs/12-extensibility.md).

The chapter specifies H1–H17 across the request and learning paths. This package
is NOT that framework — it is the first NARROW instance of it, covering the two
answer-table conditions that have a real failure mode today. The rest of D72 stays
unbuilt until there is a concrete need, rather than shipping seventeen speculative
seams.

Everything here ships DORMANT: the default registry is empty, so every hook point
is a no-op and the runtime behaves byte-identically to having no hooks at all.
Nothing is wired in `app.py`. Activating one means registering a callable — a
deliberate act, not a config flag someone flips by accident.
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
