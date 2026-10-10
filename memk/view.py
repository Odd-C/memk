"""View: the injection projection.

The store is the truth; this module renders a *view* of it for the prompt. Two
properties keep the projection honest:

* the set of rows that was rendered is recorded, so a later difference between the
  file and the store is attributable;
* a hand edit is detected by hashing the file, and is written back into the store
  rather than being silently overwritten.

Pinned rows are exempt from the budget cut. A safety rule that can be pushed out of
the prompt by a long week of unrelated notes is not pinned.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import scope as scope_mod
from .util import sha256_text, utc_now
from .store import MemoryRecord, rows_for_view

#: Character budget for the injected view. Kept below the prompt-side cap so the
#: view can never be the reason a turn overflows.
DEFAULT_BUDGET = 5600
#: Targets the injection layer renders. ``memory`` carries the rules and durable
#: facts; ``user`` carries the profile. Everything else is recall-only.
VIEW_TARGETS = ("memory", "user")
META_PREFIX = "view_render"


@dataclass
class RenderResult:
    scope_id: str
    text: str
    rendered_ids: list[str] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)
    char_count: int = 0
    fingerprint: str = ""
    budget: int = DEFAULT_BUDGET


@dataclass
class SyncResult:
    changed: bool
    hand_edit_detected: bool
    adopted: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    previous_fingerprint: str = ""
    fingerprint: str = ""


def _line(record: MemoryRecord) -> str:
    marker = "★ " if record.pinned else ""
    return f"{marker}{record.content.strip()}"


def render(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    *,
    targets: Sequence[str] = VIEW_TARGETS,
    budget: int = DEFAULT_BUDGET,
) -> RenderResult:
    """Render the view, pinned-first, stopping at the budget."""

    records = rows_for_view(conn, scope, targets=targets, limit=400)
    lines: list[str] = []
    rendered: list[str] = []
    skipped: list[str] = []
    used = 0
    for record in records:
        line = _line(record)
        if record.pinned:
            lines.append(line)
            rendered.append(record.id)
            used += len(line)
            continue
        if used + len(line) > budget:
            skipped.append(record.id)
            continue
        lines.append(line)
        rendered.append(record.id)
        used += len(line)

    text = "\n".join(lines)
    if text:
        text += "\n"
    return RenderResult(
        scope_id=scope.id(),
        text=text,
        rendered_ids=rendered,
        skipped_ids=skipped,
        char_count=len(text),
        fingerprint=sha256_text(text),
        budget=budget,
    )


def _meta_key(scope_id: str, suffix: str) -> str:
    return f"{META_PREFIX}:{scope_id}:{suffix}"


def load_state(conn: sqlite3.Connection, scope_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (_meta_key(scope_id, "state"),)
    ).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return {}


def save_state(conn: sqlite3.Connection, scope_id: str, state: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_meta_key(scope_id, "state"), json.dumps(state, ensure_ascii=False, sort_keys=True)),
    )
    conn.commit()


def sync(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    path: str | Path,
    *,
    adopt_hand_edits: bool = True,
    dry_run: bool = False,
) -> SyncResult:
    """Write the rendered view to *path*, detecting hand edits and disappearances."""

    file_path = Path(path)
    previous_text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    state = load_state(conn, scope.id())
    previous_fingerprint = str(state.get("fingerprint") or "")
    rendered_before = set(state.get("rendered_ids") or [])

    result = render(conn, scope)
    current_fingerprint = result.fingerprint

    # A file that no longer matches what we last rendered was edited by hand.
    hand_edit = bool(previous_text) and sha256_text(previous_text) != previous_fingerprint
    adopted: list[str] = []

    if hand_edit and adopt_hand_edits and previous_text:
        from . import store

        existing = {
            r.content.strip()
            for r in rows_for_view(conn, scope, targets=VIEW_TARGETS, limit=400)
        }
        for line in previous_text.splitlines():
            text = line.strip().lstrip("★").strip()
            if not text or text in existing:
                continue
            store.put_memory(
                conn,
                scope=scope,
                target="memory",
                content=text,
                authority="operator",
                memory_type="factual",
                source="view-sync",
                importance=0.55,
                confidence=0.6,
                lifecycle="promoted",
            )
            adopted.append(text[:60])

    # Rows that were rendered last time and are absent now are gone from the store;
    # rows that were never rendered are not deletions, they were over budget.
    rendered_now = set(result.rendered_ids)
    disappeared = sorted(rendered_before - rendered_now)

    archived: list[str] = []
    if disappeared and not dry_run:
        from . import store

        store.set_lifecycle(conn, disappeared, "archived")
        archived = disappeared

    if not dry_run:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(result.text, encoding="utf-8")
        save_state(
            conn,
            scope.id(),
            {
                "fingerprint": current_fingerprint,
                "rendered_ids": result.rendered_ids,
                "skipped_ids": result.skipped_ids,
                "char_count": result.char_count,
                "updated_at": utc_now(),
            },
        )

    return SyncResult(
        changed=hand_edit or previous_fingerprint != current_fingerprint,
        hand_edit_detected=hand_edit,
        adopted=adopted,
        archived=archived,
        previous_fingerprint=previous_fingerprint,
        fingerprint=current_fingerprint,
    )


def describe(conn: sqlite3.Connection, scope: scope_mod.Scope) -> dict[str, Any]:
    result = render(conn, scope)
    state = load_state(conn, scope.id())
    return {
        "scope_id": scope.id(),
        "char_count": result.char_count,
        "budget": result.budget,
        "rendered": len(result.rendered_ids),
        "skipped": len(result.skipped_ids),
        "fingerprint": result.fingerprint,
        "last_sync": state.get("updated_at", ""),
        "over_budget": result.char_count > result.budget,
    }
