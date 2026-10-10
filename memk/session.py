"""Session layer: the kernel as one usable object.

Modules are easy to test and awkward to use. This layer is the seam a host talks
to: it ingests turns, runs the gate, answers questions, and stages raw turns before
a context rewrite. It owns no rules of its own -- every decision still happens in
the module that owns it -- so the seam stays thin enough to read in one sitting.

The host hooks are deliberately small and named after what the host knows:

* ``on_turn_start`` - a turn arrived: capture it, and offer a packet for the query;
* ``on_pre_compress`` - context is about to be rewritten: stage the turn so nothing
  is lost even if extraction never runs;
* ``on_session_end`` - the session closed: index what is missing a vector.

Nothing here can write a durable memory without passing the admission gate, which is
the property that makes it safe to wire into a host's main loop.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import admission, embed, journal, packet as packet_mod, recall, scope as scope_mod, store, vectors, view
from .embed import Embedder, EmbedderStatus
from .recall import Hit
from .schema import open_store, schema_version


@dataclass
class IngestResult:
    session_id: str
    turn: int
    role: str
    captured: bool
    chunk_count: int
    journal_ids: list[int] = field(default_factory=list)
    candidate: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)

    @property
    def admitted(self) -> bool:
        return self.decision.get("action") == admission.PROMOTE

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn": self.turn,
            "role": self.role,
            "captured": self.captured,
            "chunk_count": self.chunk_count,
            "journal_ids": self.journal_ids,
            "admitted": self.admitted,
            "candidate": self.candidate,
            "decision": self.decision,
        }


class Memory:
    """A memory kernel bound to one database and one reader scope."""

    def __init__(
        self,
        db_path: str | Path,
        scope: scope_mod.Scope | None = None,
        *,
        embedder_spec: dict[str, Any] | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.scope = scope or scope_mod.Scope()
        self.conn = open_store(self.db_path)
        if embedder is not None:
            self.embedder = embedder
            self.embedder_status = EmbedderStatus(
                True, embedder.name, embedder.dimensions, "injected"
            )
        else:
            self.embedder, self.embedder_status = embed.resolve(embedder_spec)

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ writing
    def ingest(
        self,
        *,
        session_id: str,
        turn: int,
        role: str,
        content: str,
        extract: bool = True,
        target: str = "project",
    ) -> IngestResult:
        """Capture a turn and, if asked, run the extraction and admission path."""

        captured = journal.capture_turn(
            self.conn,
            scope=self.scope,
            session_id=session_id,
            turn=turn,
            role=role,
            content=content,
        )
        result = IngestResult(
            session_id=session_id,
            turn=turn,
            role=role,
            captured=not captured.skipped,
            chunk_count=captured.chunk_count,
            journal_ids=list(captured.journal_ids),
        )
        if not extract or captured.skipped:
            return result

        candidate = admission.propose_from_turn(
            role=role, content=content, journal_ids=captured.journal_ids, target=target
        )
        if candidate is None:
            result.candidate = {"skipped": "below_extraction_floor"}
            return result
        result.candidate = candidate.as_dict()
        decision = admission.admit(self.conn, scope=self.scope, candidate=candidate)
        result.decision = decision.as_dict()
        if decision.admitted:
            self._index_vector(decision.memory_id)
        return result

    def remember(
        self,
        content: str,
        *,
        target: str = "memory",
        authority: str = "operator",
        memory_type: str = "factual",
        importance: float = 0.6,
        confidence: float = 0.6,
        freshness: str = "",
        pinned: bool = False,
    ) -> str:
        """An explicit operator write: authoritative by construction."""

        memory_id = store.put_memory(
            self.conn,
            scope=self.scope,
            target=target,
            content=content,
            authority=authority,
            memory_type=memory_type,
            source="session",
            importance=importance,
            confidence=confidence,
            freshness=freshness,
            pinned=pinned,
        )
        self._index_vector(memory_id)
        return memory_id

    def supersede(self, *, new_id: str, old_id: str) -> None:
        store.supersede(self.conn, new_id=new_id, old_id=old_id)

    def forget(self, memory_id: str) -> bool:
        vectors.delete_vector(self.conn, memory_id)
        return store.delete(self.conn, memory_id)

    # ------------------------------------------------------------------ reading
    def recall(self, query: str, *, limit: int = 8, targets: Sequence[str] | None = None) -> list[Hit]:
        hits = recall.search(
            self.conn, self.scope, query, limit=limit, targets=targets, embedder=self.embedder
        )
        return hits

    def packet(
        self, query: str, *, limit: int = 5, budget_chars: int = 1600
    ) -> packet_mod.Packet:
        hits = self.recall(query, limit=limit)
        return packet_mod.build(query, hits, budget_chars=budget_chars)

    def explain(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        return recall.explain(self.conn, self.scope, query, limit=limit, embedder=self.embedder)

    def inspect(self, memory_id: str, *, chars: int = 400) -> dict[str, Any]:
        record = store.get(self.conn, memory_id)
        if record is None:
            return {}
        quotes = []
        for row in store.evidence_for(self.conn, memory_id):
            rebuilt = journal.reconstruct(self.conn, [int(row["journal_id"])])
            quotes.append(
                {
                    "journal_id": row["journal_id"],
                    "role": row["role"],
                    "stored_excerpt": row["excerpt"][:120],
                    "complete": rebuilt.complete if rebuilt else False,
                    "content": rebuilt.content[:chars] if rebuilt else "",
                }
            )
        return {"memory": record.as_dict(), "quotes": quotes}

    # -------------------------------------------------------------------- hooks
    def on_turn_start(
        self,
        *,
        session_id: str,
        turn: int,
        role: str,
        content: str,
        query: str = "",
        extract: bool = True,
    ) -> dict[str, Any]:
        """Capture the turn and offer a packet for the query that came with it."""

        ingested = self.ingest(
            session_id=session_id, turn=turn, role=role, content=content, extract=extract
        )
        result: dict[str, Any] = {"ingest": ingested.as_dict()}
        question = query or content
        if question and question.strip():
            result["packet"] = self.packet(question).as_dict()
        return result

    def on_pre_compress(
        self, *, session_id: str, turn: int, role: str, content: str
    ) -> dict[str, Any]:
        """Stage the turn before a context rewrite; no extraction here.

        Compression is exactly when raw wording gets summarised away, so this hook
        persists the turn first and lets extraction happen later, out of band.
        """

        captured = journal.capture_turn(
            self.conn, scope=self.scope, session_id=session_id, turn=turn, role=role, content=content
        )
        return {
            "staged": not captured.skipped,
            "chunk_count": captured.chunk_count,
            "journal_ids": list(captured.journal_ids),
        }

    def on_session_end(self, *, session_id: str) -> dict[str, Any]:
        """Close out a session: backfill vectors for anything missing one."""

        result: dict[str, Any] = {"session_id": session_id}
        if self.embedder is None:
            result["index"] = {"skipped": self.embedder_status.reason}
        else:
            result["index"] = vectors.reindex(self.conn, self.embedder, scope=self.scope)
        return result

    # -------------------------------------------------------------------- upkeep
    def _index_vector(self, memory_id: str) -> None:
        if self.embedder is None or not memory_id:
            return
        record = store.get(self.conn, memory_id)
        if record is None:
            return
        try:
            vector = self.embedder.embed([f"{record.content} {record.summary}"])[0]
        except Exception:  # noqa: BLE001 - a failed index must not fail the write
            return
        vectors.put_vector(
            self.conn, memory_id=memory_id, embedder_name=self.embedder.name, vector=vector
        )

    def reindex(self, *, force: bool = False) -> dict[str, Any]:
        if self.embedder is None:
            return {"skipped": self.embedder_status.reason}
        return vectors.reindex(self.conn, self.embedder, scope=self.scope, force=force)

    def sync_view(self, path: str | Path, *, dry_run: bool = False) -> view.SyncResult:
        return view.sync(self.conn, self.scope, path, dry_run=dry_run)

    def promote_candidates(self, *, apply: bool = False) -> dict[str, Any]:
        return admission.promote_candidates(self.conn, self.scope, apply=apply)

    def tune(self, *, stale_days: int = 60, apply: bool = False) -> dict[str, Any]:
        return recall.tune(self.conn, self.scope, stale_days=stale_days, apply=apply)

    def health(self) -> dict[str, Any]:
        stats = store.stats(self.conn, self.scope)
        view_state = view.describe(self.conn, self.scope)
        audit = admission.audit(self.conn, self.scope)
        vector_state = vectors.stats(self.conn)
        problems: list[str] = []
        # Compared against every row in the database: the indexes are global, so a
        # scope-limited comparison would report drift that is not there.
        if stats["index"]["word_rows"] != stats["all_rows"]:
            problems.append(
                f"lexical index drift: {stats['all_rows']} rows vs {stats['index']['word_rows']} indexed"
            )
        if stats["index"]["ngram_rows"] != stats["all_rows"]:
            problems.append(
                f"n-gram index drift: {stats['all_rows']} rows vs {stats['index']['ngram_rows']} indexed"
            )
        if view_state["over_budget"]:
            problems.append("injection view over budget")
        if audit["flagged"]:
            problems.append("promoted rows without non-assistant evidence")
        if self.embedder is not None and vector_state["coverage"] < 1.0:
            problems.append(f"vector coverage {vector_state['coverage']:.0%} for {self.embedder.name}")
        return {
            "database": self.db_path,
            "schema_version": schema_version(self.conn),
            "scope": scope_mod.explain(self.scope),
            "embedder": self.embedder_status.as_dict(),
            "memory": stats,
            "journal": journal.stats(self.conn, self.scope.readable()),
            "vectors": vector_state,
            "view": view_state,
            "audit": {"checked": audit["checked"], "flagged": len(audit["flagged"])},
            "problems": problems,
            "healthy": not problems,
        }


def open_memory(
    db_path: str | Path,
    *,
    user: str = "",
    agent: str = "default",
    platform: str = "cli",
    workspace: str = "default",
    embedder_spec: dict[str, Any] | None = None,
) -> Memory:
    scope = scope_mod.Scope(platform=platform, workspace=workspace, agent=agent, user=user)
    return Memory(db_path, scope, embedder_spec=embedder_spec)
