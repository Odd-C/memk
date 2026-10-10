"""Compaction: relieve pressure on the injection view without losing anchors.

The view is rendered under a budget, so "the file is too long" is not the problem
here -- the problem is *what occupies the budget*. Two remedies, both reversible:

* **demote** rows that were never recalled and are old: they keep existing, they just
  stop outranking things that are actually used;
* **shorten** rows that are long, by cutting at a clause boundary.

Shortening is where a compaction job usually loses information, so it fails closed.
Every anchor in the original -- URLs, file paths, versions, dates, environment
variable names, long identifiers, percentages -- must survive the cut. If even one
does not, that row is left alone and the refusal is reported. A compaction that cannot
say what it preserved is a data-loss bug with good intentions.

Nothing here deletes. Demotion is a number, shortening is validated text, and a pinned
row is exempt from both.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import scope as scope_mod, store, view
from .util import normalise_space, parse_epoch, utc_now

ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"https?://[^\s)>\]]+")),
    ("path", re.compile(r"(?:/[\w.\-]+){2,}/?|~/.{2,}|[A-Za-z]:\\[^\s]+")),
    ("version", re.compile(r"\bv?\d+\.\d+(?:\.\d+)+\b")),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("env_var", re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")),
    ("long_id", re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)),
    ("percent", re.compile(r"\b\d+(?:\.\d+)?%")),
)

DEFAULT_MAX_CHARS_PER_ROW = 400
DEFAULT_STALE_DAYS = 180
DEFAULT_DEMOTE_DELTA = 0.05
DEFAULT_IMPORTANCE_FLOOR = 0.30


@dataclass(frozen=True)
class ShortenResult:
    changed: bool
    text: str
    dropped_anchors: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.dropped_anchors


def anchors(text: str) -> dict[str, list[str]]:
    """Every anchor in the text, grouped by kind."""

    found: dict[str, list[str]] = {}
    for kind, pattern in ANCHOR_PATTERNS:
        hits = pattern.findall(text or "")
        if hits:
            found[kind] = sorted({str(h) for h in hits})
    return found


def shorten(text: str, max_chars: int) -> ShortenResult:
    """Cut at a clause boundary, then prove nothing load-bearing was lost."""

    body = normalise_space(text)
    if len(body) <= max_chars:
        return ShortenResult(False, body)

    window = body[:max_chars]
    cut = -1
    for boundary in ("。", "；", "！", "？", ". ", "; ", "，", ", "):
        index = window.rfind(boundary)
        if index > max_chars // 3:
            cut = index + len(boundary)
            break
    candidate = (window[:cut] if cut > 0 else window).rstrip() + "…"

    original_anchors = {item for hits in anchors(body).values() for item in hits}
    kept = candidate
    dropped = tuple(sorted(item for item in original_anchors if item not in kept))
    return ShortenResult(True, candidate, dropped)


def pressure(
    conn: sqlite3.Connection, scope: scope_mod.Scope, *, budget: int = view.DEFAULT_BUDGET
) -> dict[str, Any]:
    rendered = view.render(conn, scope, budget=budget)
    return {
        "budget": budget,
        "char_count": rendered.char_count,
        "rendered": len(rendered.rendered_ids),
        "skipped": len(rendered.skipped_ids),
        "over_budget": rendered.char_count > budget,
        "unaided_overflow": len(rendered.skipped_ids) > 0,
    }


def plan(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    *,
    max_chars_per_row: int = DEFAULT_MAX_CHARS_PER_ROW,
    stale_days: int = DEFAULT_STALE_DAYS,
    demote_delta: float = DEFAULT_DEMOTE_DELTA,
    importance_floor: float = DEFAULT_IMPORTANCE_FLOOR,
) -> dict[str, Any]:
    """Propose shortenings and demotions. Reads only; nothing is written."""

    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"""
        SELECT * FROM memories
        WHERE scope_id IN ({marks}) AND lifecycle NOT IN ('archived','rejected','candidate')
        ORDER BY importance DESC
        """,
        params,
    ).fetchall()

    cutoff = parse_epoch(utc_now()) - stale_days * 86400
    # Named distinctly from the `shorten()` helper on purpose: a local called
    # `shorten` would shadow the function and turn the next call into a TypeError.
    shorten_items: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    demote: list[dict[str, Any]] = []
    exempt: list[str] = []

    for row in rows:
        memory_id = str(row["id"])
        if int(row["pinned"] or 0):
            exempt.append(memory_id)
            continue
        text = str(row["content"])
        if len(text) > max_chars_per_row:
            result = shorten(text=text, max_chars=max_chars_per_row)
            if result.safe:
                shorten_items.append(
                    {
                        "id": memory_id,
                        "before_chars": len(normalise_space(text)),
                        "after_chars": len(result.text),
                        "text": result.text,
                        "anchors_kept": sorted(
                            {item for hits in anchors(text).values() for item in hits}
                        ),
                    }
                )
            else:
                refused.append(
                    {
                        "id": memory_id,
                        "reason": "anchor_loss",
                        "dropped": list(result.dropped_anchors),
                    }
                )

        recalled = int(row["last_recalled"] or 0)
        importance = float(row["importance"] or 0.0)
        updated = parse_epoch(str(row["updated_at"]))
        if recalled == 0 and updated and updated < cutoff and importance > importance_floor:
            demote.append(
                {
                    "id": memory_id,
                    "from": importance,
                    "to": round(max(importance_floor, importance - demote_delta), 4),
                    "reason": "never_recalled_and_old",
                }
            )

    return {
        "considered": len(rows),
        "shorten": shorten_items,
        "refused": refused,
        "demote": demote,
        "pinned_exempt": exempt,
        "parameters": {
            "max_chars_per_row": max_chars_per_row,
            "stale_days": stale_days,
            "demote_delta": demote_delta,
            "importance_floor": importance_floor,
        },
    }


def apply(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    plan_data: dict[str, Any] | None = None,
    *,
    apply: bool = False,
    backup_before: bool = True,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """Carry out a plan. Shortenings re-verify their anchors before writing."""

    plan_data = plan_data or plan(conn, scope)
    backed_up = ""
    if apply and backup_before and backup_dir:
        from . import backup as backup_mod

        entry = backup_mod.create(conn, backup_dir)
        backed_up = entry.path

    changed: list[dict[str, Any]] = []
    revalidated_refusals: list[dict[str, Any]] = []

    for item in plan_data.get("shorten") or []:
        record = store.get(conn, item["id"])
        if record is None:
            continue
        result = shorten(record.content, len(item["text"]))
        if not result.safe:
            # The row changed since the plan was made, or the plan was not produced by
            # shorten(). Either way: refuse rather than write unverified text.
            revalidated_refusals.append({"id": item["id"], "dropped": list(result.dropped_anchors)})
            continue
        if apply:
            conn.execute(
                "UPDATE memories SET content=?, summary=?, updated_at=? WHERE id=?",
                (item["text"], item["text"][:220], utc_now(), item["id"]),
            )
            row = conn.execute(
                "SELECT content, summary FROM memories WHERE id=?", (item["id"],)
            ).fetchone()
            store._index(conn, item["id"], str(row["content"]), str(row["summary"]))
            changed.append({"id": item["id"], "action": "shortened"})

    for item in plan_data.get("demote") or []:
        if apply:
            conn.execute(
                "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                (float(item["to"]), utc_now(), item["id"]),
            )
            changed.append({"id": item["id"], "action": "demoted", "to": item["to"]})

    if apply:
        conn.commit()

    return {
        "applied": bool(apply),
        "changed": changed,
        "refused_on_revalidation": revalidated_refusals,
        "planned_shorten": len(plan_data.get("shorten") or []),
        "planned_demote": len(plan_data.get("demote") or []),
        "backup": backed_up,
    }


def describe(conn: sqlite3.Connection, scope: scope_mod.Scope, **kwargs: Any) -> dict[str, Any]:
    plan_data = plan(conn, scope, **kwargs)
    return {
        "considered": plan_data["considered"],
        "can_shorten": len(plan_data["shorten"]),
        "refused_anchor_loss": len(plan_data["refused"]),
        "can_demote": len(plan_data["demote"]),
        "pinned_exempt": len(plan_data["pinned_exempt"]),
    }
