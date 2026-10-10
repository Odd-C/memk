"""The optional vector companion.

Stored beside the rows, in SQLite, as packed float32. Brute force over a few tens of
thousands of rows is fine at this scale, and avoiding a separate service keeps the
kernel's deployment story to "one file".

What matters more than the storage is the **provenance of a vector**: each row
records the embedder that produced it, and a search only looks at vectors from the
embedder the caller is currently using. A store re-indexed with a different model
therefore cannot return a mixture of incomparable distances, and a real model can
never be compared against a test double.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from . import scope as scope_mod
from .embed import Embedder, cosine, pack, unpack
from .store import HIDDEN_LIFECYCLES
from .util import utc_now


def put_vector(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    embedder_name: str,
    vector: Sequence[float],
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO vector_records (memory_id, embedder, dimensions, vector, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(memory_id) DO UPDATE SET
            embedder=excluded.embedder,
            dimensions=excluded.dimensions,
            vector=excluded.vector,
            created_at=excluded.created_at
        """,
        (memory_id, embedder_name, len(vector), pack(vector), utc_now()),
    )
    if commit:
        conn.commit()


def delete_vector(conn: sqlite3.Connection, memory_id: str) -> None:
    conn.execute("DELETE FROM vector_records WHERE memory_id=?", (memory_id,))


def stored(conn: sqlite3.Connection, memory_id: str) -> tuple[str, list[float]] | None:
    row = conn.execute(
        "SELECT embedder, vector FROM vector_records WHERE memory_id=?", (memory_id,)
    ).fetchone()
    if row is None:
        return None
    return str(row["embedder"]), unpack(row["vector"])


def search(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    embedder: Embedder,
    query: str,
    *,
    pool: int = 24,
    targets: Sequence[str] | None = None,
) -> list[tuple[str, float]]:
    """Nearest memories by cosine, restricted to this embedder and this scope."""

    text = str(query or "").strip()
    if not text:
        return []
    vectors = embedder.embed([text])
    if not vectors:
        return []
    query_vector = vectors[0]

    marks, params = scope_mod.placeholders(scope.readable())
    hidden = ",".join("?" * len(HIDDEN_LIFECYCLES))
    target_clause = ""
    target_params: list[str] = []
    if targets:
        target_clause = f"AND m.target IN ({','.join('?' * len(targets))})"
        target_params = list(targets)

    rows = conn.execute(
        f"""
        SELECT v.memory_id, v.vector, v.dimensions
        FROM vector_records v
        JOIN memories m ON m.id = v.memory_id
        WHERE v.embedder = ? AND m.scope_id IN ({marks}) AND m.lifecycle NOT IN ({hidden})
        {target_clause}
        """,
        [embedder.name, *params, *HIDDEN_LIFECYCLES, *target_params],
    ).fetchall()

    scored: list[tuple[str, float]] = []
    for row in rows:
        if int(row["dimensions"]) != len(query_vector):
            # A dimension mismatch means the stored vector came from a different
            # build of the same model name. Skip rather than truncate or pad.
            continue
        score = cosine(query_vector, unpack(row["vector"]))
        if score > 0.0:
            scored.append((str(row["memory_id"]), score))
    scored.sort(key=lambda item: -item[1])
    return scored[:pool]


def reindex(
    conn: sqlite3.Connection,
    embedder: Embedder,
    *,
    batch_size: int = 32,
    scope: scope_mod.Scope | None = None,
    force: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    """Embed memories that have no vector for this embedder.

    Only the active embedder's rows are considered, so switching models means
    re-indexing rather than mixing: existing rows for the old model stay until the
    caller deletes them.
    """

    clause = ""
    params: list[Any] = []
    if scope is not None:
        marks, scope_params = scope_mod.placeholders(scope.readable())
        clause = f"AND m.scope_id IN ({marks})"
        params.extend(scope_params)

    stale = "" if force else "AND (v.memory_id IS NULL OR v.embedder <> ?)"
    if not force:
        params.insert(0, embedder.name)

    rows = conn.execute(
        f"""
        SELECT m.id, m.content, m.summary
        FROM memories m
        LEFT JOIN vector_records v ON v.memory_id = m.id
        WHERE m.lifecycle NOT IN ('archived','rejected') {stale} {clause}
        ORDER BY m.updated_at DESC
        {f'LIMIT {int(limit)}' if limit else ''}
        """,
        params,
    ).fetchall()

    done = 0
    failures: list[dict[str, str]] = []
    batch: list[sqlite3.Row] = list(rows)
    for start in range(0, len(batch), batch_size):
        chunk = batch[start : start + batch_size]
        texts = [f"{row['content']} {row['summary']}".strip() for row in chunk]
        try:
            vectors = embedder.embed(texts)
        except Exception as exc:  # noqa: BLE001 - report per batch, keep going
            failures.append({"batch_start": str(start), "error": f"{type(exc).__name__}"})
            continue
        for row, vector in zip(chunk, vectors):
            put_vector(
                conn,
                memory_id=str(row["id"]),
                embedder_name=embedder.name,
                vector=vector,
                commit=False,
            )
            done += 1
        conn.commit()

    return {
        "embedder": embedder.name,
        "dimensions": embedder.dimensions,
        "candidates": len(batch),
        "indexed": done,
        "failures": failures,
    }


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    by_embedder = {
        str(row["embedder"]): {
            "vectors": int(row["n"]),
            "dimensions": int(row["dims"] or 0),
        }
        for row in conn.execute(
            """
            SELECT embedder, COUNT(*) AS n, MAX(dimensions) AS dims
            FROM vector_records GROUP BY embedder
            """
        ).fetchall()
    }
    rows = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
    live = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE lifecycle NOT IN ('archived','rejected')"
        ).fetchone()["n"]
    )
    return {
        "by_embedder": by_embedder,
        "live_memories": live,
        "all_memories": rows,
        "coverage": (sum(v["vectors"] for v in by_embedder.values()) / live) if live else 0.0,
    }
