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

The executor (`executor.py`) + the `RunBlueprintTool` (`tool.py`) land in Slice B.
"""

from __future__ import annotations

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
from .verify import VerifyOutcome, verify_result
from .when import WhenClauseError, evaluate_when, validate_when

__all__ = [
    "AskUser",
    "Blueprint",
    "BlueprintParseError",
    "Node",
    "OmitSlot",
    "ResultGrain",
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
