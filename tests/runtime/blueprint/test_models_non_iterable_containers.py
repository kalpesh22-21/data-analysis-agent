"""A non-iterable ARRAY property must be a `BlueprintParseError`, not a `TypeError`.

`Blueprint.parse` is the fail-loud READ side (§2.1): every malformed stored shape
raises `BlueprintParseError`, which is the ONE exception the corpus loader catches
(`_validate_blueprint_dag` → `CorpusLoadError`). A `TypeError` escapes that handler
and aborts `load_corpus` un-wrapped — and per the loader's own contract that does not
fail one blueprint, it bricks the corpus indefinitely: the hydration cache re-arms and
retries the same poisoned entry on every turn.

`slots=5` / `composes=5` / `uses_rules=5` did exactly that, via `list(slots or [])`
and the generator over `composes`. `uses_rules` even HAD an `isinstance(..., list)`
check — one line BELOW `tuple(uses_rules or ())`, so it was dead for the only input
that could reach it. The fix is ordering: all three are type-checked before anything
iterates them.

Reachable from a poisoned MCP export entry, which is the threat model the parse
layer's fail-loud posture exists for (defense-in-depth over the WRITE-side loader
validation, per this module's docstring).
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _validate_blueprint_dag,
)

_OK: dict[str, Any] = {
    "id": "bp-probe",
    "intent": "probe",
    "sql_template": "SELECT 1 AS n",
}


@pytest.mark.parametrize("field", ["slots", "composes", "uses_rules"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(5, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(object(), id="opaque-object"),
    ],
)
def test_a_non_iterable_array_property_raises_blueprint_parse_error(
    field: str, value: Any
) -> None:
    with pytest.raises(BlueprintParseError, match=f"'{field}' must be a"):
        Blueprint.parse(**_OK, **{field: value})


def test_the_uses_rules_check_now_runs_before_the_iteration_that_used_to_crash() -> None:
    """The specific ordering bug: the `isinstance(uses_rules, list)` check existed but
    sat AFTER `tuple(uses_rules or ())`, so a list-typed input reached it and a
    non-iterable never did. Both spellings of the fault now raise the same way."""
    with pytest.raises(BlueprintParseError, match="'uses_rules' must be a list"):
        Blueprint.parse(**_OK, uses_rules=5)  # was: TypeError, un-wrapped
    with pytest.raises(BlueprintParseError, match="'uses_rules' must be a list"):
        Blueprint.parse(**_OK, uses_rules={"rule.a": 1})  # was: reached the check


@pytest.mark.parametrize("field", ["slots", "composes"])
def test_a_poisoned_seed_fails_the_load_as_a_corpus_load_error(field: str) -> None:
    """End to end through the handler that matters: the loader catches
    `BlueprintParseError` only, so this is what keeps a poisoned entry from aborting
    the whole hydration."""
    seed = BlueprintSeed(
        id="bp-probe",
        intent="probe",
        slots_summary="",
        uses=[],
        sql_template="SELECT 1 AS n",
        **{field: 5},
    )
    with pytest.raises(CorpusLoadError, match="malformed DAG"):
        _validate_blueprint_dag(seed)


@pytest.mark.parametrize("empty", [None, [], ()])
def test_absent_and_empty_array_properties_are_still_accepted(empty: Any) -> None:
    """The containers are OPTIONAL — a single-node blueprint declares no slots, no
    composes and no rules. A `tuple` stays accepted for `slots`/`composes` (in-process
    callers pass them); `uses_rules` keeps its authored list-only rule."""
    blueprint = Blueprint.parse(**_OK, slots=empty, composes=empty)
    assert blueprint.slots == () and blueprint.composes == ()
    assert Blueprint.parse(**_OK, uses_rules=empty or None).uses_rules == ()


def test_a_wellformed_blueprint_still_parses() -> None:
    blueprint = Blueprint.parse(
        id="bp-probe",
        intent="probe",
        slots=[{"name": "department", "type": "string"}],
        uses_rules=["rule.active_employee"],
        sql_template="SELECT 1 AS n WHERE d = {department}",
    )
    assert [s.name for s in blueprint.slots] == ["department"]
    assert blueprint.uses_rules == ("rule.active_employee",)
