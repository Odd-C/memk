"""Journal: the raw capture layer, chunked and hash-anchored.

Every eligible turn is stored verbatim so that an extracted memory can always be
traced back to the words it came from. Two properties make that trace trustworthy:

1. **Chunking is deterministic and self-describing.** Long turns are split into
   fixed-size chunks; each chunk records its index, the total count, and a hash of
   the *whole* turn. A link to any single chunk can therefore be expanded to the
   complete turn.
2. **Reconstruction is verified, not assumed.** The expanded turn is hashed and
   compared against the recorded hash. If it does not match, the caller is told the
   content is incomplete instead of receiving a fragment labelled as the original.

The previous arrangement linked one chunk and called it provenance. This is the
correction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import scope as scope_mod
from .util import normalise_space, sha256_text, utc_now

#: Characters per chunk. Large enough that most turns are a single chunk, small
#: enough that a very long paste cannot become one oversized row.
CHUNK_CHARS = 2000

#: Roles that may serve as admissible evidence for an extracted memory.
EVIDENCE_ROLES = ("user", "tool")


@dataclass(frozen=True)
class CaptureResult:
    session_id: str
    turn: int
    role: str
    origin_hash: str
    chunk_count: int
    journal_ids: list[int] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Reconstruction:
    complete: bool
    content: str
    role: str
    turn: int
    session_id: str
    chunk_ids: list[int]
    expected_chunks: int
    found_chunks: int
    hash_matches: bool
    truncated: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "role": self.role,
            "turn": self.turn,
            "session_id": self.session_id,
            "chunk_ids": self.chunk_ids,
            "expected_chunks": self.expected_chunks,
            "found_chunks": self.found_chunks,
            "hash_matches": self.hash_matches,
            "truncated": self.truncated,
            "note": self.note,
        }


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def capture_turn(
    conn: sqlite3.Connection,
    *,
    scope: scope_mod.Scope,
    session_id: str,
    turn: int,
    role: str,
    content: str,
    min_chars: int = 1,
) -> CaptureResult:
    """Store one turn, chunked. Idempotent: a repeated capture is a no-op."""

    text = content or ""
    if len(normalise_space(text)) < min_chars or not text.strip():
        return CaptureResult(session_id, turn, role, "", 0, skipped=True, reason="below_min_length")

    origin_hash = sha256_text(text)
    chunks = chunk_text(text)
    chunk_count = len(chunks)
    scope_id = scope.id()
    created = utc_now()
    ids: list[int] = []

    for index, chunk in enumerate(chunks):
        existing = conn.execute(
            """
            SELECT id FROM journal
            WHERE scope_id=? AND session_id=? AND turn=? AND role=?
              AND chunk_index=? AND origin_hash=?
            """,
            (scope_id, session_id, turn, role, index, origin_hash),
        ).fetchone()
        if existing is not None:
            ids.append(int(existing["id"]))
            continue
        cur = conn.execute(
            """
            INSERT INTO journal
                (scope_id, session_id, turn, role, chunk_index, chunk_count,
                 content, content_hash, origin_hash, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scope_id,
                session_id,
                turn,
                role,
                index,
                chunk_count,
                chunk,
                sha256_text(chunk),
                origin_hash,
                created,
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return CaptureResult(
        session_id=session_id,
        turn=turn,
        role=role,
        origin_hash=origin_hash,
        chunk_count=chunk_count,
        journal_ids=ids,
    )


def reconstruct(
    conn: sqlite3.Connection,
    journal_ids: Sequence[int],
    *,
    max_chars: int = 20000,
) -> Reconstruction | None:
    """Rebuild the full turn behind any chunk of it, with verification."""

    ids = [int(i) for i in journal_ids]
    if not ids:
        return None
    marks = ",".join("?" * len(ids))
    anchor = conn.execute(
        f"SELECT * FROM journal WHERE id IN ({marks}) ORDER BY id LIMIT 1", ids
    ).fetchone()
    if anchor is None:
        return None

    origin_hash = str(anchor["origin_hash"])
    rows = conn.execute(
        """
        SELECT id, content, chunk_index FROM journal
        WHERE scope_id=? AND session_id=? AND turn=? AND role=? AND origin_hash=?
        ORDER BY chunk_index
        """,
        (
            anchor["scope_id"],
            anchor["session_id"],
            anchor["turn"],
            anchor["role"],
            origin_hash,
        ),
    ).fetchall()

    expected = int(anchor["chunk_count"] or 1)
    joined = "".join(str(r["content"]) for r in rows)
    hash_matches = sha256_text(joined) == origin_hash
    found = len(rows)
    complete = hash_matches and found == expected

    note = ""
    if found != expected:
        note = f"expected {expected} chunks, found {found}"
    if not hash_matches:
        note = (note + "; " if note else "") + "content hash mismatch"

    truncated = len(joined) > max_chars
    content = joined[:max_chars]

    return Reconstruction(
        complete=complete,
        content=content,
        role=str(anchor["role"]),
        turn=int(anchor["turn"]),
        session_id=str(anchor["session_id"]),
        chunk_ids=[int(r["id"]) for r in rows],
        expected_chunks=expected,
        found_chunks=found,
        hash_matches=hash_matches,
        truncated=truncated,
        note=note,
    )


def roles_for(conn: sqlite3.Connection, journal_ids: Sequence[int]) -> set[str]:
    ids = [int(i) for i in journal_ids]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT DISTINCT LOWER(TRIM(role)) AS role FROM journal WHERE id IN ({marks})", ids
    ).fetchall()
    return {str(r["role"]) for r in rows if r["role"]}


def stats(conn: sqlite3.Connection, scope_ids: Sequence[str] | None = None) -> dict[str, Any]:
    if scope_ids:
        marks, params = scope_mod.placeholders(scope_ids)
        row = conn.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT session_id) AS s, MAX(created_at) AS last "
            f"FROM journal WHERE scope_id IN ({marks})",
            params,
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT session_id) AS s, MAX(created_at) AS last FROM journal"
        ).fetchone()
    return {
        "rows": int(row["n"] or 0),
        "sessions": int(row["s"] or 0),
        "last_capture": row["last"] or "",
    }
