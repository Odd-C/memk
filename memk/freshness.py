"""Freshness: is this still true, or is it history?

Four states, three of them explicit. A missing value is a state, and the safest
one, so "untracked" is not a gap to be filled by a maintenance sweep.

The penalty table is small on purpose: ranking effects are hard to reason about,
so each state's effect is fixed, documented, and asserted in the self test.
Bulk marking is refused rather than discouraged, because a batch sweep is exactly
how this went wrong before.
"""

from __future__ import annotations

from dataclasses import dataclass

CURRENT = "current"
NEEDS_CHECK = "needs_check"
HISTORICAL = "historical"
ARCHIVED = "archived"

#: Ranking penalty per state. ``None`` means "no marker", which costs nothing.
PENALTY: dict[str, float] = {
    "": 0.0,
    CURRENT: 0.0,
    NEEDS_CHECK: 0.18,
    HISTORICAL: 0.35,
    ARCHIVED: 0.0,  # archived rows are filtered before ranking, not penalised
}


class FreshnessError(ValueError):
    """Raised when a requested freshness change would be unsafe."""


@dataclass(frozen=True)
class Transition:
    memory_id: str
    previous: str
    current: str
    superseded_by: str = ""

    @property
    def changed(self) -> bool:
        return self.previous != self.current


def normalise(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in ("", "untracked", "none", "unknown"):
        return ""
    if text in ("stale", "expired", "needs_live_check", "needs_check"):
        return NEEDS_CHECK
    if text in ("superseded", "historical", "old"):
        return HISTORICAL
    if text == ARCHIVED:
        # Archival is normally expressed as a lifecycle, which recall filters in SQL.
        # If a row carries it as a freshness state instead, it must still read as
        # not-recallable, so the two representations agree.
        return ARCHIVED
    if text in ("active", "current", "verified", "promoted"):
        return CURRENT
    return ""


def penalty(value: str | None) -> float:
    return PENALTY.get(normalise(value), 0.0)


def is_recallable(value: str | None) -> bool:
    """Archived rows are out of scope for recall; everything else is in."""

    return normalise(value) != ARCHIVED


def plan_supersede(new_id: str, old_id: str, old_freshness: str | None) -> Transition:
    """The standard replacement: new row current, old row historical with a pointer.

    The old row is deliberately *not* archived. Superseded facts stay recallable at
    a penalty, because "what was true before" is a legitimate question and an
    archived row cannot answer it.
    """

    return Transition(
        memory_id=old_id,
        previous=normalise(old_freshness),
        current=HISTORICAL,
        superseded_by=new_id,
    )


def guard_bulk_change(memory_ids: list[str], state: str, *, allow: bool = False) -> None:
    """Refuse a multi-row change to a lossy state unless it is explicitly allowed.

    Marking many rows at once as ``needs_check`` pushed recall quality down and had
    to be rolled back. The rule is now enforced by the kernel instead of by a note
    in a document: one row at a time, or an explicit override.
    """

    target = normalise(state)
    if target in ("", CURRENT):
        return
    if len(memory_ids) > 1 and not allow:
        raise FreshnessError(
            f"refusing to mark {len(memory_ids)} rows as {target or 'unset'}: "
            "apply freshness one row at a time, or pass allow_bulk=True deliberately"
        )


def describe(value: str | None) -> dict[str, object]:
    state = normalise(value)
    return {
        "state": state or "untracked",
        "penalty": penalty(value),
        "recallable": is_recallable(value),
        "needs_verification": state == NEEDS_CHECK,
    }
