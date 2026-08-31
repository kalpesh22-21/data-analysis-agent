"""The slot-level diff a reviewer reads before applying a proposal.

Computed SERVER-SIDE and shipped as structured rows, for the same reason the card exists at all:
showing a reviewer two JSON blobs and asking them to spot the difference would reproduce the
original complaint inside the fix.

The diff is keyed on the LOCATOR — `table.column`, plus the value — because that is the identity
of a parameterization entry. Its position in the list is not: `_merged_parameterization` appends,
so an entry's index moves whenever anything is added, and an index-keyed diff would report every
subsequent entry as changed.

PURE. No I/O, no model, no store. It describes what WOULD happen if these entries were applied;
whether they actually can is `to_candidate`'s answer, and this deliberately does not try to
predict it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DiffKind = Literal[
    "added",
    "removed",
    "role_changed",
    "slot_changed",
    "unchanged",
    # ⚠ APPEND MODE ONLY: the proposal changes an entry that already exists, and appending
    # cannot change anything — it concatenates. See `diff_parameterization`.
    "conflict",
]


def _locator_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    """The identity of one entry: which literal, in which column, it is about."""
    locator = entry.get("locator") if isinstance(entry.get("locator"), dict) else {}
    return (
        str(locator.get("table") or ""),
        str(locator.get("column") or ""),
        str(locator.get("value") or ""),
    )


def _describe(entry: dict[str, Any]) -> str:
    """One entry as a single line a card can render without further interpretation."""
    table, column, value = _locator_key(entry)
    where = ".".join(p for p in (table, column) if p) or "(no locator)"
    role = str(entry.get("role") or "(no role)")
    slot = entry.get("slot") if isinstance(entry.get("slot"), dict) else None
    if role == "slot" and slot:
        return f"{where} = {value} → slot {slot.get('name')} ({slot.get('binds_to')})"
    if role == "rule":
        return f"{where} = {value} → rule {entry.get('rule_id')}"
    if role == "inline":
        why = entry.get("why")
        return f"{where} = {value} → inline" + (f" — {why}" if why else " — no reason given")
    return f"{where} = {value} → {role}"


def _slot_shape(entry: dict[str, Any]) -> tuple[Any, ...]:
    """The parts of a slot a reviewer would call a change.

    `required` is included and `optional_pattern` is not: the first changes whether a caller
    MUST supply the slot, the second is a rewrite detail with no reviewer-facing meaning.
    """
    slot = entry.get("slot") if isinstance(entry.get("slot"), dict) else {}
    return (slot.get("name"), slot.get("type"), slot.get("binds_to"), slot.get("required"))


@dataclass(frozen=True)
class EntryDiff:
    """One row of the before/after a reviewer is shown."""

    kind: DiffKind
    locator: str
    before: str = ""
    after: str = ""

    def to_doc(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "before": self.before,
            "after": self.after,
        }


def diff_parameterization(
    current: list[Any], proposed: list[Any], *, replace: bool
) -> tuple[EntryDiff, ...]:
    """What applying *proposed* to *current* would change.

    *replace* is the difference between "these are the whole list" and "these are additions",
    and it changes the answer completely — under append, an entry present in `current` and
    absent from `proposed` is simply untouched; under replace, it is REMOVED. Getting this
    backwards would show a reviewer a diff that omits the destructive half of the operation,
    which is the half they most need to see.

    Unchanged rows are included. A reviewer deciding whether to apply needs to see that the
    three entries they already trusted are still there, and a diff that shows only deltas
    cannot say so.

    ⚠ Under append, touching an EXISTING locator is a `conflict`, never a `role_changed` — the
    merge concatenates, so the apply would decline rather than update. See the branch below.
    """
    current_entries = [e for e in current if isinstance(e, dict)]
    proposed_entries = [e for e in proposed if isinstance(e, dict)]
    by_key = {_locator_key(e): e for e in current_entries}
    seen: set[tuple[str, str, str]] = set()
    rows: list[EntryDiff] = []

    for entry in proposed_entries:
        key = _locator_key(entry)
        seen.add(key)
        locator = ".".join(p for p in key[:2] if p) or "(no locator)"
        existing = by_key.get(key)
        if existing is None:
            rows.append(EntryDiff(kind="added", locator=locator, after=_describe(entry)))
            continue
        before, after = _describe(existing), _describe(entry)
        # WHAT changed first, HOW to label it second. `_describe` is a RENDERING, not an
        # identity — a slot retyped `entity` -> `period` renders identically — so its equality
        # is the LAST test, never the first.
        role_differs = existing.get("role") != entry.get("role")
        shape_differs = _slot_shape(existing) != _slot_shape(entry)
        if not (role_differs or shape_differs or before != after):
            kind: DiffKind = "unchanged"
        elif not replace:
            # ⚠ APPEND CANNOT CHANGE AN ENTRY, and any label naming a change would promise a
            # repair the apply cannot perform. `_merged_parameterization(replace_all=False)`
            # CONCATENATES, so a proposal touching an existing locator produces a list with
            # that literal classified twice — and the rewrite then fails deterministically,
            # because the second entry cannot find a literal the first already replaced.
            #
            # This is the single most common repair (slot -> inline) arriving in the mode the
            # prompt tells the model to PREFER, so the reviewer would read a clean diff, apply,
            # and get a decline the diff gave no sign of. That is exactly the
            # "misdescribes the operation" failure this module exists to avoid.
            kind = "conflict"
        elif role_differs:
            kind = "role_changed"
        else:
            # A slot reshaped, or a `why`/`rule_id` edited. Reported as a change rather than as
            # unchanged: "unchanged" is the one label a diff must never apply to a change, and
            # the `why` is the justification a reviewer is adjudicating.
            kind = "slot_changed"
        rows.append(EntryDiff(kind=kind, locator=locator, before=before, after=after))

    for entry in current_entries:
        key = _locator_key(entry)
        if key in seen:
            continue
        locator = ".".join(p for p in key[:2] if p) or "(no locator)"
        rows.append(
            EntryDiff(
                kind="removed" if replace else "unchanged",
                locator=locator,
                before=_describe(entry),
                after="" if replace else _describe(entry),
            )
        )
    return tuple(rows)
