"""runtime/blueprint — the `runBlueprint` fast-path package.

  - `models`   — the typed parse layer (`Blueprint`, `SlotSpec`, `Node`, `WhenClause`,
                 `ResultGrain`) over the additive full-DAG JSON stored on the `:Blueprint`
                 node and carried by `BlueprintDetail`.
  - `template` — sqlglot-AST typed-literal slot binding (D10-safe), the `resolveValues`
                 `sql_builder` discipline realized for authored `{slot}` templates.
  - `slots`    — the D49 deterministic per-type slot resolvers: pure code, NO LLM, where a
                 multi-match, no-match or fuzzy result raises an `AskUser` signal rather than
                 guessing.
  - `when`     — the declarative `when`-clause evaluator plus the entity-agnostic leakage
                 validator (D59) used at load time.
  - `verify`   — the D56 deterministic grain-integrity + signature assertion, as a pure
                 function.
  - `executor` — `BlueprintExecutor`: the DAG walk (fetch, resolve + bind slots, dispatch
                 through the runQuery choke point, D56 verify gate), returning a typed
                 `ExecOutcome` union.
  - `tool`     — `RunBlueprintTool`: the model-facing `RuntimeTool` wrapper (span, redaction,
                 crash guard, and the pausing-tool seam).
"""

from __future__ import annotations

from .executor import (
    BlueprintExecutor,
    ExecCompleted,
    ExecFailed,
    ExecOutcome,
    ExecPaused,
)
from .models import (
    Blueprint,
    BlueprintParseError,
    Node,
    ResultGrain,
    SlotSpec,
    WhenClause,
)
from .slots import AskUser, OmitSlot, SlotBinding, resolve_slot
from .template import TemplateBindError, bind_template
from .tool import RunBlueprintTool
from .verify import VerifyOutcome, verify_result
from .when import WhenClauseError, evaluate_when, validate_when

__all__ = [
    "AskUser",
    "Blueprint",
    "BlueprintExecutor",
    "BlueprintParseError",
    "ExecCompleted",
    "ExecFailed",
    "ExecOutcome",
    "ExecPaused",
    "Node",
    "OmitSlot",
    "ResultGrain",
    "RunBlueprintTool",
    "SlotBinding",
    "SlotSpec",
    "TemplateBindError",
    "VerifyOutcome",
    "WhenClause",
    "WhenClauseError",
    "bind_template",
    "evaluate_when",
    "resolve_slot",
    "validate_when",
    "verify_result",
]
