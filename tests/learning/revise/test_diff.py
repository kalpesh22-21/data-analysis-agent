"""The slot-level diff a reviewer reads before applying a proposal.

Its whole job is to make the apply decision readable, so the failures that matter are the ones
that make it MISDESCRIBE the operation — most of all the append/replace asymmetry, where the
destructive half of a replace is exactly the half a wrong diff would omit.
"""

from __future__ import annotations

from data_agent.learning.revise import diff_parameterization


def _entry(column: str, value: str, role: str, **extra):
    entry = {
        "locator": {"table": "payroll.payroll_fact", "column": column, "value": value},
        "role": role,
    }
    entry.update(extra)
    return entry


SLOT = _entry(
    "department",
    "0420",
    "slot",
    slot={
        "name": "department",
        "type": "entity",
        "binds_to": "payroll.payroll_fact.department",
        "required": True,
    },
)
INLINE = _entry("record_type", "EARNING", "inline", why="defines the metric earnings")


def _kinds(rows):
    return {(r.kind, r.locator) for r in rows}


def test_a_new_entry_reads_as_added() -> None:
    rows = diff_parameterization([SLOT], [SLOT, INLINE], replace=False)
    assert ("added", "payroll.payroll_fact.record_type") in _kinds(rows)


SLOTTED_RECORD_TYPE = _entry(
    "record_type",
    "EARNING",
    "slot",
    slot={"name": "record_type", "type": "entity",
          "binds_to": "payroll.payroll_fact.record_type", "required": True},
)


def test_a_re_roled_entry_reads_as_a_role_change_not_an_add() -> None:
    """The identity of an entry is its LOCATOR, not its position: `record_type = EARNING`
    changing from slot to inline is one entry changing, not one removed and one added. A diff
    that said otherwise would describe the single most common repair as a destructive one."""
    rows = diff_parameterization([SLOTTED_RECORD_TYPE], [INLINE], replace=True)
    assert [r.kind for r in rows] == ["role_changed"]
    assert "slot record_type" in rows[0].before
    assert "inline" in rows[0].after


def test_changing_an_existing_entry_under_append_is_a_conflict_not_a_change() -> None:
    """⚠ APPEND CANNOT CHANGE AN ENTRY — `_merged_parameterization(replace_all=False)`
    concatenates, so the merged list classifies one literal twice and the rewrite then fails
    deterministically (the second entry cannot find a literal the first already replaced).

    Labelling this `role_changed` promised an in-place update the apply cannot perform, on the
    single most common repair (slot → inline) arriving in the mode the prompt tells the model to
    PREFER. The reviewer would read a clean diff, apply, and get a decline the diff gave no sign
    of — the exact "misdescribes the operation" failure this module exists to avoid.
    """
    rows = diff_parameterization([SLOTTED_RECORD_TYPE], [INLINE], replace=False)
    assert [r.kind for r in rows] == ["conflict"]
    # Both sides are still shown: the reviewer has to see WHAT would have changed to decide
    # whether ticking `replace` is the right move.
    assert rows[0].before and rows[0].after


def test_a_retyped_slot_reads_as_a_slot_change() -> None:
    retyped = _entry(
        "department",
        "0420",
        "slot",
        slot={"name": "department", "type": "period",
              "binds_to": "payroll.payroll_fact.department", "required": True},
    )
    assert [r.kind for r in diff_parameterization([SLOT], [retyped], replace=True)] == [
        "slot_changed"
    ]
    assert [r.kind for r in diff_parameterization([SLOT], [retyped], replace=False)] == [
        "conflict"
    ]


def test_an_edited_why_is_a_change_never_unchanged() -> None:
    """Same role, same slot shape, different `why`. "Unchanged" is the one label a diff must
    never apply to a change — the `why` is the justification a reviewer is adjudicating."""
    edited = _entry("record_type", "EARNING", "inline", why="a different reason entirely")
    assert [r.kind for r in diff_parameterization([INLINE], [edited], replace=True)] == [
        "slot_changed"
    ]


def test_an_identical_entry_is_unchanged_in_either_mode() -> None:
    """A no-op proposal is not a conflict: appending a duplicate of something already there
    changes nothing to collide over, and the reviewer should not be warned about it."""
    for mode in (True, False):
        assert [r.kind for r in diff_parameterization([INLINE], [INLINE], replace=mode)] == [
            "unchanged"
        ]


def test_under_append_an_untouched_entry_is_unchanged_not_removed() -> None:
    """⚠ The asymmetry. Under append, an entry absent from the proposal is simply not
    mentioned by it — it survives."""
    rows = diff_parameterization([SLOT, INLINE], [INLINE], replace=False)
    by_locator = {r.locator: r for r in rows}
    assert by_locator["payroll.payroll_fact.department"].kind == "unchanged"
    assert by_locator["payroll.payroll_fact.department"].after


def test_under_replace_the_same_entry_is_removed() -> None:
    """⚠ The other half. Under replace the proposal IS the whole list, so an entry missing
    from it is destroyed — and that is precisely what a reviewer must see before clicking
    apply."""
    rows = diff_parameterization([SLOT, INLINE], [INLINE], replace=True)
    by_locator = {r.locator: r for r in rows}
    assert by_locator["payroll.payroll_fact.department"].kind == "removed"
    assert by_locator["payroll.payroll_fact.department"].after == ""
    assert by_locator["payroll.payroll_fact.department"].before


def test_unchanged_rows_are_included() -> None:
    """A reviewer deciding whether to apply needs to see that the entries they already trusted
    are still there. A deltas-only diff cannot say so."""
    rows = diff_parameterization([SLOT], [SLOT], replace=True)
    assert [r.kind for r in rows] == ["unchanged"]


def test_junk_in_either_list_is_skipped_rather_than_raising() -> None:
    """Both sides are rehydrated JSON from stores a human can write through directly."""
    rows = diff_parameterization(["nope", None, SLOT], ["also nope", INLINE], replace=False)
    assert _kinds(rows) == {
        ("added", "payroll.payroll_fact.record_type"),
        ("unchanged", "payroll.payroll_fact.department"),
    }


def test_an_entry_with_no_locator_still_gets_a_row() -> None:
    rows = diff_parameterization([], [{"role": "inline", "why": "w"}], replace=False)
    assert [r.kind for r in rows] == ["added"]
    assert rows[0].locator == "(no locator)"
