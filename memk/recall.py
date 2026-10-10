"""Recall: candidate generation from independent channels, then fusion.

Three channels, each earning its place:

* **word** - the token index, for queries written with spaces;
* **gram** - the two-character index, for queries written without them;
* **timeline** - a date-bounded chronology, opened only when the query explicitly
  asks for one.

Fusion is rank-based (reciprocal rank), so channels with incomparable scores can
be combined without pretending their numbers are comparable. Afterwards two
corrections apply, both of them explainable: a freshness penalty and an authority
factor. A hit always carries the reason it was chosen.

Scope and lifecycle are enforced in SQL, before fusion. A boundary that is applied
after ranking is a preference, not a boundary.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import freshness as freshness_mod
from . import lexer, scope as scope_mod, timeline as timeline_mod, vectors
from .embed import Embedder
from .store import HIDDEN_LIFECYCLES
from .util import now_epoch, parse_epoch

#: Reciprocal-rank constant. 60 is the value from the original RRF paper.
RRF_K = 60
CHANNEL_WEIGHTS = {"word": 1.0, "gram": 1.1, "timeline": 0.9, "vector": 1.0}
AUTHORITY_FACTOR = {
    "user": 1.0,
    "operator": 1.0,
    "tool": 0.95,
    "inference": 0.7,
    "unknown": 0.85,
}



@dataclass
class Hit:
    memory_id: str
    target: str
    content: str
    summary: str
    authority: str
    lifecycle: str
    freshness: str
    memory_type: str
    source: str
    importance: float
    score: float
    channels: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    coverage: float = 0.0
    explanation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.memory_id,
            "target": self.target,
            "content": self.content,
            "summary": self.summary,
            "authority": self.authority,
            "lifecycle": self.lifecycle,
            "freshness": self.freshness or "untracked",
            "memory_type": self.memory_type,
            "source": self.source,
            "importance": self.importance,
            "score": round(self.score, 6),
            "channels": self.channels,
            "matched_terms": self.matched_terms[:12],
            "coverage": round(self.coverage, 4),
            "explanation": self.explanation,
        }


def _scope_clause(scope: scope_mod.Scope) -> tuple[str, list[str]]:
    marks, params = scope_mod.placeholders(scope.readable())
    return f"m.scope_id IN ({marks})", params


def _hidden_clause(include_candidates: bool = False) -> tuple[str, list[str]]:
    """Lifecycle filter, applied in SQL so the boundary is not a ranking concern.

    The audit path may ask for candidates explicitly; it still never asks for
    archived rows, because an archived row is not a memory under any reading.
    """

    hidden = tuple(c for c in HIDDEN_LIFECYCLES if not (include_candidates and c == "candidate"))
    marks = ",".join("?" * len(hidden))
    return f"AND m.lifecycle NOT IN ({marks})", list(hidden)


def _channel_word(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    query: str,
    pool: int,
    targets: Sequence[str] | None,
    include_candidates: bool = False,
) -> list[tuple[str, float, list[str]]]:
    tokens = lexer.word_tokens(query)
    if not tokens:
        return []
    expression = " OR ".join(f'"{t}"' for t in tokens)
    clause, params = _scope_clause(scope)
    target_clause, target_params = _target_clause(targets)
    hidden_clause, hidden_params = _hidden_clause(include_candidates)
    sql = f"""
        SELECT m.id, bm25(mem_fts) AS rank_score
        FROM mem_fts
        JOIN memories m ON m.id = mem_fts.memory_id
        WHERE mem_fts MATCH ? AND {clause} {hidden_clause} {target_clause}
        ORDER BY rank_score LIMIT ?
    """
    rows = conn.execute(
        sql, [expression, *params, *hidden_params, *target_params, pool]
    ).fetchall()
    return [(str(r["id"]), float(r["rank_score"]), tokens) for r in rows]


def _channel_gram(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    query: str,
    pool: int,
    targets: Sequence[str] | None,
    include_candidates: bool = False,
) -> list[tuple[str, float, list[str]]]:
    grams = lexer.ngrams(query)
    if not grams:
        return []
    expression = " OR ".join(f'"{g}"' for g in grams)
    clause, params = _scope_clause(scope)
    target_clause, target_params = _target_clause(targets)
    hidden_clause, hidden_params = _hidden_clause(include_candidates)
    sql = f"""
        SELECT m.id, bm25(mem_ngram) AS rank_score
        FROM mem_ngram
        JOIN memories m ON m.id = mem_ngram.memory_id
        WHERE mem_ngram MATCH ? AND {clause} {hidden_clause} {target_clause}
        ORDER BY rank_score LIMIT ?
    """
    rows = conn.execute(
        sql, [expression, *params, *hidden_params, *target_params, pool]
    ).fetchall()
    return [(str(r["id"]), float(r["rank_score"]), grams) for r in rows]


def _target_clause(targets: Sequence[str] | None) -> tuple[str, list[str]]:
    if not targets:
        return "", []
    marks = ",".join("?" * len(targets))
    return f"AND m.target IN ({marks})", list(targets)


def _channel_timeline(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    intent: timeline_mod.TimelineIntent,
    pool: int,
    include_candidates: bool = False,
) -> list[str]:
    """Date-bounded chronology over work and operational memory."""

    clause, params = _scope_clause(scope)
    hidden_clause, hidden_params = _hidden_clause(include_candidates)
    sql = f"""
        SELECT m.id
        FROM memories m
        WHERE {clause} {hidden_clause}
          AND m.target IN ('project','ops')
          AND COALESCE(m.updated_at, m.created_at) >= ?
          AND (? IS NULL OR COALESCE(m.updated_at, m.created_at) < ?)
        ORDER BY
          CASE LOWER(COALESCE(m.memory_type,''))
            WHEN 'episodic' THEN 0 WHEN 'summary' THEN 1
            WHEN 'decision' THEN 2 WHEN 'project' THEN 3 ELSE 4 END,
          CASE m.target WHEN 'project' THEN 0 WHEN 'ops' THEN 1 ELSE 2 END,
          m.updated_at DESC
        LIMIT ?
    """
    rows = conn.execute(
        sql,
        [
            *params,
            *hidden_params,
            intent.start,
            intent.end,
            intent.end,
            pool,
        ],
    ).fetchall()
    return [str(r["id"]) for r in rows]


def _load(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, sqlite3.Row]:
    items = list({str(i) for i in ids})
    if not items:
        return {}
    marks = ",".join("?" * len(items))
    rows = conn.execute(f"SELECT * FROM memories WHERE id IN ({marks})", items).fetchall()
    return {str(r["id"]): r for r in rows}


def search(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    query: str,
    *,
    limit: int = 8,
    targets: Sequence[str] | None = None,
    include_candidates: bool = False,
    mark_recalled: bool = True,
    now: str | None = None,
    embedder: Embedder | None = None,
) -> list[Hit]:
    """Return ranked hits with their explanation."""

    text = str(query or "").strip()
    if not text:
        return []
    pool = max(limit * 4, 24)

    intent = timeline_mod.parse(text)
    channels: dict[str, list[tuple[str, float, list[str]]]] = {
        "word": _channel_word(conn, scope, text, pool, targets, include_candidates),
        "gram": _channel_gram(conn, scope, text, pool, targets, include_candidates),
    }
    vector_scores: dict[str, float] = {}
    if embedder is not None:
        # The semantic channel exists only when an embedder was configured and
        # named. When it is absent no substitution happens: the lexical channels
        # above simply carry the query on their own.
        vector_hits = vectors.search(conn, scope, embedder, text, pool=pool, targets=targets)
        channels["vector"] = [(memory_id, score, []) for memory_id, score in vector_hits]
        vector_scores = {memory_id: score for memory_id, score in vector_hits}
    timeline_ids: list[str] = []
    if intent is not None and intent.explicit:
        timeline_ids = _channel_timeline(conn, scope, intent, pool, include_candidates)

    # Reciprocal-rank contribution per channel.
    contributions: dict[str, dict[str, float]] = {}
    matched: dict[str, list[str]] = {}
    members: dict[str, list[str]] = {}
    for name, rows in channels.items():
        for rank, (memory_id, _rank_score, terms) in enumerate(rows, start=1):
            contributions.setdefault(memory_id, {})[name] = CHANNEL_WEIGHTS[name] * (
                1.0 / (RRF_K + rank)
            )
            matched.setdefault(memory_id, [])
            for term in terms:
                if term not in matched[memory_id]:
                    matched[memory_id].append(term)
            members.setdefault(memory_id, []).append(name)
    for rank, memory_id in enumerate(timeline_ids, start=1):
        contributions.setdefault(memory_id, {})["timeline"] = CHANNEL_WEIGHTS["timeline"] * (
            1.0 / (RRF_K + rank)
        )
        members.setdefault(memory_id, []).append("timeline")

    if not contributions:
        return []

    rows = _load(conn, contributions)
    hits: list[Hit] = []
    for memory_id, per_channel in contributions.items():
        row = rows.get(memory_id)
        if row is None:
            continue
        lifecycle = str(row["lifecycle"])
        if not include_candidates and lifecycle in HIDDEN_LIFECYCLES:
            continue

        fused = sum(per_channel.values())
        freshness_state = str(row["freshness"] or "")
        if include_candidates and lifecycle == "candidate":
            # Candidates are visible only to auditing; they rank below real memory.
            fused *= 0.5
        penalty = freshness_mod.penalty(freshness_state)
        authority = str(row["authority"] or "unknown")
        factor = AUTHORITY_FACTOR.get(authority, 0.8)
        importance_boost = 1.0 + 0.15 * float(row["importance"] or 0.0)

        document = f"{row['content']} {row['summary']}"
        coverage, gram_hits = lexer.gram_coverage(text, document)
        score = fused * (1.0 - penalty) * factor * importance_boost
        if "timeline" in per_channel:
            # A chronology hit must survive fusion even with no topical overlap,
            # but must not outrank a strong topical match.
            score = max(score, min(0.02, 0.012 + 0.004 * importance_boost))

        hit = Hit(
            memory_id=memory_id,
            target=str(row["target"]),
            content=str(row["content"]),
            summary=str(row["summary"] or ""),
            authority=authority,
            lifecycle=lifecycle,
            freshness=freshness_state,
            memory_type=str(row["memory_type"] or "factual"),
            source=str(row["source"] or ""),
            importance=float(row["importance"] or 0.0),
            score=score,
            channels=sorted(per_channel),
            matched_terms=gram_hits or matched.get(memory_id, []),
            coverage=coverage,
            explanation={
                "rrf": {k: round(v, 6) for k, v in per_channel.items()},
                "freshness_penalty": penalty,
                "authority_factor": factor,
                "importance_boost": round(importance_boost, 4),
                "timeline_kind": intent.kind if intent else "",
                "timeline_match": "timeline" in per_channel,
                "superseded_by": str(row["superseded_by"] or ""),
                "vector_score": round(vector_scores.get(memory_id, 0.0), 6),
                "embedder": embedder.name if (embedder is not None) else "",
            },
        )
        hits.append(hit)

    hits.sort(key=lambda h: (-h.score, -h.importance, h.memory_id))
    selected = hits[:limit]

    if mark_recalled and selected:
        marks = ",".join("?" * len(selected))
        conn.execute(
            f"UPDATE memories SET last_recalled=? WHERE id IN ({marks})",
            [now_epoch() if now is None else parse_epoch(now), *[h.memory_id for h in selected]],
        )
        conn.commit()
    return selected


def explain(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    query: str,
    *,
    limit: int = 5,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Why a query returned what it did, including the intent it was read as."""

    intent = timeline_mod.describe(query)
    hits = search(conn, scope, query, limit=limit, mark_recalled=False, embedder=embedder)
    return {
        "query": query,
        "intent": intent,
        "returned": len(hits),
        "hits": [h.as_dict() for h in hits],
        "channels_consulted": [
            "word" if lexer.word_tokens(query) else None,
            "gram" if lexer.ngrams(query) else None,
            "vector" if embedder is not None else None,
            "timeline" if (intent.get("explicit") and intent.get("intent")) else None,
        ],
        "embedder": embedder.name if embedder is not None else "",
    }


def tune(
    conn: sqlite3.Connection,
    scope: scope_mod.Scope,
    *,
    stale_days: int = 60,
    apply: bool = False,
) -> dict[str, Any]:
    """Heat frequently recalled memory, cool memory that stopped being used.

    Pinned rows are exempt in both directions: a safety rule should not decay just
    because nobody asked about it.
    """

    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"SELECT id, importance, pinned, last_recalled FROM memories "
        f"WHERE scope_id IN ({marks}) AND lifecycle NOT IN ('archived','rejected')",
        params,
    ).fetchall()

    cutoff = now_epoch() - stale_days * 86400
    heated: list[dict[str, Any]] = []
    cooled: list[dict[str, Any]] = []
    for row in rows:
        if int(row["pinned"] or 0):
            continue
        memory_id = str(row["id"])
        importance = float(row["importance"] or 0.0)
        recalled = int(row["last_recalled"] or 0)
        if recalled > cutoff:
            delta = 0.08 if (now_epoch() - recalled) < 7 * 86400 else 0.03
            new_value = min(0.95, importance + delta)
            if new_value > importance:
                heated.append({"id": memory_id, "from": importance, "to": new_value})
                if apply:
                    conn.execute(
                        "UPDATE memories SET importance=? WHERE id=?", (new_value, memory_id)
                    )
        elif recalled > 0:
            new_value = max(0.30, importance - 0.03)
            if new_value < importance:
                cooled.append({"id": memory_id, "from": importance, "to": new_value})
                if apply:
                    conn.execute(
                        "UPDATE memories SET importance=? WHERE id=?", (new_value, memory_id)
                    )
    if apply:
        conn.commit()
    return {"heated": heated, "cooled": cooled, "applied": bool(apply), "considered": len(rows)}
