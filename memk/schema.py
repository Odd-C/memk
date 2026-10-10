"""Schema and connection discipline for the memk truth store.

One database, one connection factory, one set of rules. Everything that touches
SQLite in this project goes through :func:`connect` so the connection flags, the
journal-mode gate and the busy timeout cannot drift apart.

Design note: the lexical index keeps two views of the same text -- a token table
for word-boundary languages and an n-gram table for languages that do not use
spaces. Lexical recall for CJK must not depend on an embedding model being
present, which is what the previous arrangement got wrong.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

#: Version of the on-disk shape. Additive changes (new tables, new indexes) are
#: applied by re-running the idempotent DDL below, so they do not need a
#: migration step. Destructive changes would, and none exist yet.
SCHEMA_VERSION = 2

#: Milliseconds a writer may wait for a lock before raising. Long enough for a
#: healthy writer to finish, short enough that a turn never blocks indefinitely.
BUSY_TIMEOUT_MS = 8000


def wal_is_vulnerable(version: tuple[int, ...] | None = None) -> bool:
    """Report whether this SQLite build carries the WAL-reset defect.

    Derived from the SQLite release history: the defect was introduced with the
    WAL2/reset rework in 3.44 and fixed in the 3.44.6, 3.50.7 and 3.51.3 point
    releases. Everything strictly between those points is treated as affected.

    This is a deliberate re-derivation, not a copy: the rule we want is "only
    trust a journal-mode switch on a build that is known to handle it".
    """

    info = tuple(version) if version is not None else tuple(sqlite3.sqlite_version_info)
    if info < (3, 44, 0):
        return False
    fixed_points = ((3, 44, 6), (3, 50, 7), (3, 51, 3))
    for point in fixed_points:
        if info[0] == point[0] and info[1] == point[1] and info >= point:
            return False
    if info >= (3, 51, 3):
        return False
    return True


def apply_journal_mode(conn: sqlite3.Connection) -> str:
    """Set a safe journal mode and return the mode actually in effect.

    Asymmetric on purpose:

    * a build without the defect gets WAL, which is what we want;
    * a build with the defect never flips a non-WAL database into WAL, because
      that is what re-arms the defect. An already-WAL database stays WAL and is
      never downgraded while it might have other openers.
    """

    if not wal_is_vulnerable():
        conn.execute("PRAGMA journal_mode=WAL")
        return "wal"
    current = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if current == "wal":
        logger.warning(
            "sqlite %s is affected by the WAL-reset defect; keeping the existing WAL "
            "database (no live downgrade). Switch it to DELETE while the store is closed.",
            sqlite3.sqlite_version,
        )
        return "wal"
    return current


DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- Raw capture: one row per chunk of one turn. Never recalled directly; it is
-- the evidence layer behind an extracted memory.
CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    turn            INTEGER NOT NULL,
    role            TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    chunk_count     INTEGER NOT NULL DEFAULT 1,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    origin_hash     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (scope_id, session_id, turn, role, chunk_index, origin_hash)
);
CREATE INDEX IF NOT EXISTS journal_lookup
    ON journal (scope_id, session_id, turn, role, origin_hash);

-- Durable memory. `authority` and `freshness` are first-class: ranking and the
-- admission gate both read them, so they cannot live in free-form prose.
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    scope_id        TEXT NOT NULL,
    target          TEXT NOT NULL,
    content         TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    authority       TEXT NOT NULL DEFAULT 'unknown',
    lifecycle       TEXT NOT NULL DEFAULT 'candidate',
    freshness       TEXT NOT NULL DEFAULT '',
    memory_type     TEXT NOT NULL DEFAULT 'factual',
    source          TEXT NOT NULL DEFAULT 'operator',
    importance      REAL NOT NULL DEFAULT 0.5,
    confidence      REAL NOT NULL DEFAULT 0.5,
    pinned          INTEGER NOT NULL DEFAULT 0,
    superseded_by   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_recalled   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS memories_scope ON memories (scope_id, target, lifecycle);
CREATE INDEX IF NOT EXISTS memories_dedup ON memories (scope_id, content);

-- Evidence: why a memory is believed. One row per supporting journal turn.
CREATE TABLE IF NOT EXISTS evidence (
    memory_id       TEXT NOT NULL,
    journal_id      INTEGER NOT NULL,
    role            TEXT NOT NULL,
    excerpt         TEXT NOT NULL,
    excerpt_hash    TEXT NOT NULL,
    complete        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (memory_id, journal_id)
);

-- Word-boundary lexical index.
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
    memory_id UNINDEXED,
    content,
    summary,
    tokenize = 'unicode61'
);

-- N-gram lexical index for languages without word breaks. Populated by the
-- lexer; queried only when the query itself contains such characters.
CREATE VIRTUAL TABLE IF NOT EXISTS mem_ngram USING fts5(
    memory_id UNINDEXED,
    gram,
    tokenize = 'unicode61'
);

-- Optional semantic channel. A row exists only when an embedder was configured
-- and named: ``embedder`` records which model produced the vector, so a vector of
-- unknown provenance cannot silently participate in ranking. The vector is kept
-- as packed float32 to stay dependency-free.
CREATE TABLE IF NOT EXISTS vector_records (
    memory_id   TEXT PRIMARY KEY,
    embedder    TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vector_records_embedder ON vector_records (embedder);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the truth store with the project's flags applied."""

    path = str(db_path)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    # Order matters: the mode first, then the durability and wait settings.
    apply_journal_mode(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialise(conn: sqlite3.Connection) -> None:
    """Create or extend the schema, then record the version.

    Re-running the DDL is how additive changes land: every statement is
    ``IF NOT EXISTS``, so an existing store gains the new tables without losing
    anything. A destructive change would need a real migration, and there is none.
    """

    conn.executescript(DDL)
    current = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if current is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(current["value"]) != SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
        )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    return {str(r["name"]) for r in rows}


def open_store(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    initialise(conn)
    return conn


def summarise_tables(conn: sqlite3.Connection, names: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
        except sqlite3.Error:
            out[name] = -1
    return out
