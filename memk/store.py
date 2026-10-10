"""Store: the single source of truth for durable memory.

Every write goes through this module, which keeps the two lexical indexes in step
with the row and records evidence in the same transaction. A memory that exists
but is invisible to search is a bug with a long history, so the two are never
allowed to diverge by construction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import freshness as freshness_mod
from . import lexer, scope as scope_mod
from .util import new_id, normalise_space, parse_epoch, sha256_text, summary_of, utc_now

TARGETS = ("user", "memory", "project", "ops", "general")
#: Who asserted a fact. Only ``user`` and ``operator`` count as human-confirmed.
AUTHORITIES = ("user", "operator", "tool", "inference", "unknown")
HUMAN_AUTHORITIES = ("user", "operator")

#: Lifecycle values that recall must not return by default. Defined here because
#: every reader (retrieval, vectors, audit) has to agree on the same set, and a
#: second copy of this tuple elsewhere is how boundaries drift apart.
HIDDEN_LIFECYCLES = ("archived", "rejected", "candidate")


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope_id: str
    target: str
    content: str
    summary: str
    authority: str
    lifecycle: str
    freshness: str
    memory_type: str
    source: str
    importance: float
    confidence: float
    pinned: bool
    superseded_by: str
    created_at: str
    updated_at: str
    last_recalled: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryRecord":
        return cls(
            id=str(row["id"]),
            scope_id=str(row["scope_id"]),
            target=str(row["target"]),
            content=str(row["content"]),
            summary=str(row["summary"] or ""),
            authority=str(row["authority"] or "unknown"),
            lifecycle=str(row["lifecycle"] or "candidate"),
            freshness=str(row["freshness"] or ""),
            memory_type=str(row["memory_type"] or "factual"),
            source=str(row["source"] or "operator"),
            importance=float(row["importance"] or 0.0),
            confidence=float(row["confidence"] or 0.0),
            pinned=bool(row["pinned"]),
            superseded_by=str(row["superseded_by"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_recalled=int(row["last_recalled"] or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_id": self.scope_id,
            "target": self.target,
            "content": self.content,
            "summary": self.summary,
            "authority": self.authority,
            "lifecycle": self.lifecycle,
            "freshness": self.freshness or "untracked",
            "memory_type": self.memory_type,
            "source": self.source,
            "importance": self.importance,
            "confidence": self.confidence,
            "pinned": self.pinned,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_recalled": self.last_recalled,
        }


def _index(conn: sqlite3.Connection, memory_id: str, content: str, summary: str) -> None:
    """Keep both lexical indexes in step with a row."""

    for table in ("mem_fts", "mem_ngram"):
        conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (memory_id,))
    word_text = normalise_space(f"{content} {summary}")
    grams = " ".join(lexer.ngrams(f"{content} {summary}"))
    conn.execute(
        "INSERT INTO mem_fts (memory_id, content, summary) VALUES (?,?,?)",
        (memory_id, word_text, summary or ""),
    )
    conn.execute(
        "INSERT INTO mem_ngram (memory_id, gram) VALUES (?,?)",
        (memory_id, grams),
    )


def _unindex(conn: sqlite3.Connection, memory_id: str) -> None:
    for table in ("mem_fts", "mem_ngram"):
        conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (memory_id,))


def put_memory(
    conn: sqlite3.Connection,
    *,
    scope: scope_mod.Scope,
    target: str,
    content: str,
    authority: str = "unknown",
    memory_type: str = "factual",
    source: str = "operator",
    importance: float = 0.5,
    confidence: float = 0.5,
    freshness: str = "",
    lifecycle: str = "promoted",
    pinned: bool = False,
    scope_id: str | None = None,
) -> str:
    """Insert a memory and index it. Returns the new id."""

    if target not in TARGETS:
        raise StoreError(f"unknown target {target!r}")
    if authority not in AUTHORITIES:
        raise StoreError(f"unknown authority {authority!r}")
    text = (content or "").strip()
    if not text:
        raise StoreError("refusing to store an empty memory")

    resolved_scope = scope_id or scope_mod.writable_scope(scope, target)
    memory_id = new_id()
    stamp = utc_now()
    conn.execute(
        """
        INSERT INTO memories
            (id, scope_id, target, content, summary, authority, lifecycle, freshness,
             memory_type, source, importance, confidence, pinned, superseded_by,
             created_at, updated_at, last_recalled)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        """,
        (
            memory_id,
            resolved_scope,
            target,
            text,
            summary_of(text),
            authority,
            lifecycle,
            freshness_mod.normalise(freshness),
            memory_type,
            source,
            float(importance),
            float(confidence),
            1 if pinned else 0,
            "",
            stamp,
            stamp,
        ),
    )
    _index(conn, memory_id, text, summary_of(text))
    conn.commit()
    return memory_id


def get(conn: sqlite3.Connection, memory_id: str) -> MemoryRecord | None:
    row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    return MemoryRecord.from_row(row) if row else None


def find_duplicate(
    conn: sqlite3.Connection, scope: scope_mod.Scope, content: str
) -> MemoryRecord | None:
    """Exact-content duplicate in a readable scope, live rows only."""

    marks, params = scope_mod.placeholders(scope.readable())
    row = conn.execute(
        f"""
        SELECT * FROM memories
        WHERE scope_id IN ({marks}) AND content=?
          AND lifecycle NOT IN ('archived','rejected')
        ORDER BY updated_at DESC LIMIT 1
        """,
        [*params, content.strip()],
    ).fetchone()
    return MemoryRecord.from_row(row) if row else None


def record_evidence(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    journal_ids: Sequence[int],
    excerpt_chars: int = 400,
) -> int:
    """Attach evidence rows for a memory, verified against the journal."""

    from . import journal as journal_mod

    written = 0
    for journal_id in journal_ids:
        row = conn.execute("SELECT * FROM journal WHERE id=?", (int(journal_id),)).fetchone()
        if row is None:
            continue
        excerpt = str(row["content"])[:excerpt_chars]
        conn.execute(
            """
            INSERT OR REPLACE INTO evidence
                (memory_id, journal_id, role, excerpt, excerpt_hash, complete, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                memory_id,
                int(journal_id),
                str(row["role"]).lower(),
                excerpt,
                sha256_text(str(row["content"])),
                1 if int(row["chunk_count"] or 1) == 1 else 0,
                utc_now(),
            ),
        )
        written += 1
    conn.commit()
    return written


def evidence_roles(conn: sqlite3.Connection, memory_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT LOWER(TRIM(role)) AS role FROM evidence WHERE memory_id=?", (memory_id,)
    ).fetchall()
    return {str(r["role"]) for r in rows if r["role"]}


def evidence_for(conn: sqlite3.Connection, memory_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM evidence WHERE memory_id=? ORDER BY journal_id", (memory_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def set_lifecycle(conn: sqlite3.Connection, memory_ids: Iterable[str], lifecycle: str) -> int:
    ids = [str(i) for i in memory_ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE memories SET lifecycle=?, updated_at=? WHERE id IN ({marks})",
        [lifecycle, utc_now(), *ids],
    )
    conn.commit()
    return int(cur.rowcount or 0)


def set_freshness(
    conn: sqlite3.Connection,
    memory_ids: Sequence[str],
    state: str,
    *,
    allow_bulk: bool = False,
    superseded_by: str = "",
) -> int:
    """Change freshness for up to one row, unless bulk is explicitly allowed."""

    ids = [str(i) for i in memory_ids]
    freshness_mod.guard_bulk_change(ids, state, allow=allow_bulk)
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE memories SET freshness=?, superseded_by=?, updated_at=? WHERE id IN ({marks})",
        [freshness_mod.normalise(state), superseded_by, utc_now(), *ids],
    )
    conn.commit()
    return int(cur.rowcount or 0)


def supersede(conn: sqlite3.Connection, *, new_id: str, old_id: str) -> None:
    old = get(conn, old_id)
    if old is None:
        raise StoreError(f"no such memory {old_id!r}")
    transition = freshness_mod.plan_supersede(new_id, old_id, old.freshness)
    set_freshness(
        conn,
        [transition.memory_id],
        transition.current,
        superseded_by=transition.superseded_by,
    )


def set_importance(conn: sqlite3.Connection, memory_id: str, value: float) -> float:
    """Clamp and store importance; returns the stored value."""

    clamped = max(0.0, min(0.95, float(value)))
    conn.execute(
        "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
        (clamped, utc_now(), memory_id),
    )
    conn.commit()
    return clamped


def mark_recalled(conn: sqlite3.Connection, memory_ids: Sequence[str], *, epoch: int) -> int:
    ids = [str(i) for i in memory_ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE memories SET last_recalled=? WHERE id IN ({marks})", [int(epoch), *ids]
    )
    conn.commit()
    return int(cur.rowcount or 0)


def delete(conn: sqlite3.Connection, memory_id: str) -> bool:
    """Hard delete. The CLI requires a backup before it will call this."""

    row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
    if row is None:
        return False
    _unindex(conn, memory_id)
    conn.execute("DELETE FROM evidence WHERE memory_id=?", (memory_id,))
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    return True


def rows_for_view(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    *,
    target: str = "memory",
    limit: int = 100,
) -> list[MemoryRecord]:
    """Live, non-archived rows eligible for the injection view."""

    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"""
        SELECT * FROM memories
        WHERE scope_id IN ({marks}) AND target=?
          AND lifecycle NOT IN ('archived','rejected','candidate')
        ORDER BY pinned DESC, importance DESC, updated_at DESC
        LIMIT ?
        """,
        [*params, target, limit],
    ).fetchall()
    return [MemoryRecord.from_row(r) for r in rows]


def stats(conn: sqlite3.Connection, scope: scope_mod.Scope) -> dict[str, Any]:
    marks, params = scope_mod.placeholders(scope.readable())
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN lifecycle='candidate' THEN 1 ELSE 0 END) AS candidates,
               SUM(CASE WHEN lifecycle='archived' THEN 1 ELSE 0 END) AS archived,
               SUM(CASE WHEN superseded_by<>'' THEN 1 ELSE 0 END) AS superseded,
               SUM(CASE WHEN pinned=1 THEN 1 ELSE 0 END) AS pinned
        FROM memories WHERE scope_id IN ({marks})
        """,
        params,
    ).fetchone()
    by_authority = {
        str(r["authority"]): int(r["n"])
        for r in conn.execute(
            f"SELECT authority, COUNT(*) AS n FROM memories WHERE scope_id IN ({marks}) "
            f"GROUP BY authority",
            params,
        ).fetchall()
    }
    fts = int(conn.execute("SELECT COUNT(*) AS n FROM mem_fts").fetchone()["n"])
    grams = int(conn.execute("SELECT COUNT(*) AS n FROM mem_ngram").fetchone()["n"])
    # The lexical indexes are global, so drift has to be measured against every row
    # in the database -- not against the caller's readable scopes, which would report
    # drift for any reader who legitimately cannot see all of them.
    all_rows = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
    latest = conn.execute(
        f"SELECT MAX(last_recalled) AS last FROM memories WHERE scope_id IN ({marks})", params
    ).fetchone()
    return {
        "rows": int(totals["n"] or 0),
        "all_rows": all_rows,
        "candidates": int(totals["candidates"] or 0),
        "archived": int(totals["archived"] or 0),
        "superseded": int(totals["superseded"] or 0),
        "pinned": int(totals["pinned"] or 0),
        "by_authority": by_authority,
        "index": {"word_rows": fts, "ngram_rows": grams},
        "last_recalled_epoch": int(latest["last"] or 0),
    }


def recent(conn: sqlite3.Connection, scope: scope_mod.Scope, limit: int = 10) -> list[MemoryRecord]:
    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"SELECT * FROM memories WHERE scope_id IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [MemoryRecord.from_row(r) for r in rows]


def touchable(conn: sqlite3.Connection, memory_id: str) -> int:
    row = conn.execute("SELECT last_recalled FROM memories WHERE id=?", (memory_id,)).fetchone()
    return parse_epoch("") if row is None else int(row["last_recalled"] or 0)
