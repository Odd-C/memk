"""Dedupe: one live row per fact.

Recall quality degrades quietly when the same fact exists twice in different
wordings: the two versions compete, the ranking looks unstable, and the injection
budget is spent twice on one thing.

The pass is deliberately conservative, and the asymmetry is the point:

* **exact duplicates** (same normalised text) are decided automatically: newest wins;
* **substring pairs** are decided automatically: the longer one wins, because the
  shorter one carries strictly less;
* **prefix-similar pairs that each hold unique content** are *reported only*, never
  resolved. A machine cannot tell which half of a fact matters.

Nothing is deleted. The loser is archived -- unreachable by recall, still present for
audit -- and a pinned row never loses to an unpinned one. If both rows are pinned, the
pair is reported instead of touched.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from . import scope as scope_mod, store
from .util import normalise_space, parse_epoch

DEFAULT_MIN_PREFIX = 24
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class DuplicatePair:
    keep: str
    drop: str
    kind: str  # exact | substring | prefix
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"keep": self.keep, "drop": self.drop, "kind": self.kind, "detail": self.detail}


def _key(text: str) -> str:
    return _WS.sub("", str(text or "")).casefold()


def _live(conn: sqlite3.Connection, scope: scope_mod.Scope) -> list[store.MemoryRecord]:
    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"""
        SELECT * FROM memories
        WHERE scope_id IN ({marks}) AND lifecycle NOT IN ('archived','rejected','candidate')
        ORDER BY updated_at DESC
        """,
        params,
    ).fetchall()
    return [store.MemoryRecord.from_row(row) for row in rows]


def scan(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    *,
    min_prefix: int = DEFAULT_MIN_PREFIX,
) -> dict[str, Any]:
    """Find duplicate and near-duplicate pairs. Reads only."""

    records = _live(conn, scope)
    by_key: dict[str, list[store.MemoryRecord]] = {}
    for record in records:
        by_key.setdefault(_key(record.content), []).append(record)

    auto: list[DuplicatePair] = []
    manual: list[DuplicatePair] = []

    for group in by_key.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: (not r.pinned, -parse_epoch(r.updated_at)))
        keeper = ordered[0]
        for loser in ordered[1:]:
            if keeper.pinned and loser.pinned:
                manual.append(DuplicatePair(keeper.id, loser.id, "exact", "both_pinned"))
            else:
                auto.append(DuplicatePair(keeper.id, loser.id, "exact", "identical_text"))

    # Substring relations across different keys: the shorter text is contained in the
    # longer, so the longer strictly dominates.
    keys = list(by_key)
    for i, left_key in enumerate(keys):
        for right_key in keys[i + 1 :]:
            if not left_key or not right_key:
                continue
            shorter, longer = (left_key, right_key) if len(left_key) <= len(right_key) else (right_key, left_key)
            if len(shorter) < min_prefix:
                continue
            if shorter == longer or shorter not in longer:
                continue
            short_group, long_group = by_key[shorter], by_key[longer]
            keeper = sorted(long_group, key=lambda r: (not r.pinned, -parse_epoch(r.updated_at)))[0]
            for loser in sorted(short_group, key=lambda r: (not r.pinned, -parse_epoch(r.updated_at))):
                if keeper.pinned and loser.pinned:
                    manual.append(DuplicatePair(keeper.id, loser.id, "substring", "both_pinned"))
                else:
                    auto.append(DuplicatePair(keeper.id, loser.id, "substring", "shorter_is_subset"))

    # Same opening, different tails: each may carry something the other does not.
    seen_pairs: set[tuple[str, str]] = set()
    for i, left in enumerate(records):
        for right in records[i + 1 :]:
            left_key, right_key = _key(left.content), _key(right.content)
            if not left_key or not right_key or left_key == right_key:
                continue
            if left_key in right_key or right_key in left_key:
                continue
            prefix = 0
            limit = min(len(left_key), len(right_key))
            while prefix < limit and left_key[prefix] == right_key[prefix]:
                prefix += 1
            if prefix < min_prefix:
                continue
            key = tuple(sorted((left.id, right.id)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            manual.append(
                DuplicatePair(left.id, right.id, "prefix", f"shared_prefix={prefix}_chars_review_only")
            )

    return {
        "live_rows": len(records),
        "auto": [pair.as_dict() for pair in auto],
        "manual": [pair.as_dict() for pair in manual],
        "note": "auto pairs are safe to archive; manual pairs need a human decision",
    }


def apply(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    plan: dict[str, Any] | None = None,
    *,
    min_prefix: int = DEFAULT_MIN_PREFIX,
    apply: bool = False,
) -> dict[str, Any]:
    """Archive the loser of every automatically-decided pair."""

    plan = plan or scan(conn, scope, min_prefix=min_prefix)
    pairs = plan.get("auto") or []
    # One row can be the loser in several pairs (it duplicates one row and is a
    # subset of another), so the decision list is de-duplicated: counting the same row
    # twice would overstate the work and hide which rows actually changed.
    to_archive: list[str] = []
    skipped: list[dict[str, str]] = []

    for pair in pairs:
        keeper = store.get(conn, pair["keep"])
        loser = store.get(conn, pair["drop"])
        if keeper is None or loser is None:
            skipped.append({"drop": pair["drop"], "reason": "row_missing"})
            continue
        if loser.pinned and not keeper.pinned:
            # A pinned row is never the loser, whatever its age.
            skipped.append({"drop": loser.id, "reason": "loser_is_pinned"})
            continue
        if loser.lifecycle == "archived":
            continue
        if loser.id not in to_archive:
            to_archive.append(loser.id)

    archived: list[str] = []
    if apply and to_archive:
        store.set_lifecycle(conn, to_archive, "archived")
        archived = list(to_archive)

    return {
        "candidates": len(pairs),
        # What the decision is, independent of whether anything was written.
        "to_archive": to_archive,
        # What was actually written: empty on a dry run, by construction.
        "archived": archived,
        "skipped": skipped,
        "applied": bool(apply),
        "manual_review": len(plan.get("manual") or []),
    }


def describe(conn: sqlite3.Connection, scope: scope_mod.Scope) -> dict[str, Any]:
    plan = scan(conn, scope)
    return {
        "live_rows": plan["live_rows"],
        "auto_mergeable": len(plan["auto"]),
        "needs_review": len(plan["manual"]),
        "samples": (plan["auto"] + plan["manual"])[:5],
    }


def normalise(text: str) -> str:
    """Exposed so callers can key rows the same way the scanner does."""

    return normalise_space(text).casefold()


def ids_from(plan: dict[str, Any], field: str = "auto") -> Sequence[str]:
    return [pair["drop"] for pair in plan.get(field) or []]
