"""runtime/blueprint — the `runBlueprint` fast-path package (runblueprint-design).

Slice A ships the STORAGE + PURE-FUNCTION foundations only — no execution engine,
no loop changes beyond the §6 owed items:

  - `models`   — the typed parse layer (`Blueprint`, `SlotSpec`, `Node`,
                 `WhenClause`, `ResultGrain`) over the additive full-DAG JSON
                 stored on the `:Blueprint` node (§1) / carried by `BlueprintDetail`.
  - `template` — F1 sqlglot-AST typed-literal slot binding (D10-safe; the
                 `resolveValues` `sql_builder` discipline, realized for authored
                 `{slot}` templates).
  - `slots`    — the D49 deterministic per-type slot resolvers (pure code, NO LLM;
                 multi-match / no-match / fuzzy → an `AskUser` signal, never a guess).
  - `when`     — the §2.6 declarative `when`-clause evaluator + the entity-agnostic
                 (leakage) validator (D59) used at load time.
  - `verify`   — the D56 deterministic grain-integrity + signature assertion as a
                 pure function (the "no silent path" teeth).

Slice B adds the execution engine + the tool:
  - `executor` — `BlueprintExecutor`: the single-node DAG walk (fetch → resolve +
                 bind slots → dispatch through the runQuery choke point → D56
                 verify gate), returning a typed `ExecOutcome` union.
  - `tool`     — `RunBlueprintTool`: the model-facing `RuntimeTool` wrapper
                 (span + redaction + B4 guard + the §2.5 pausing-tool seam).
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
