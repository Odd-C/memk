"""Admission: how a proposal becomes a memory.

Extraction always produces a *candidate*. A candidate becomes a memory only when
the evidence supports it, and the rule is asymmetric on purpose: the burden is on
promotion, and every failure of evidence denies.

The gate reads **roles, never content**. A provenance check that has to read the
conversation to decide is a provenance check that can leak the conversation into a
decision log, so it does not get to read at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import freshness as freshness_mod
from . import journal as journal_mod
from . import scope as scope_mod
from . import store
from .util import normalise_space, summary_of

#: Confidence below this keeps a candidate a candidate.
MIN_CONFIDENCE = 0.55
#: Importance below this keeps a candidate a candidate.
MIN_IMPORTANCE = 0.45

PROMOTE = "promote"
KEEP = "keep"
REJECT = "reject"


@dataclass(frozen=True)
class Candidate:
    """A proposed memory, before admission."""

    content: str
    target: str
    authority: str
    memory_type: str
    source: str
    confidence: float
    importance: float
    journal_ids: list[int] = field(default_factory=list)
    freshness: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "target": self.target,
            "authority": self.authority,
            "memory_type": self.memory_type,
            "source": self.source,
            "confidence": self.confidence,
            "importance": self.importance,
            "journal_ids": list(self.journal_ids),
            "freshness": self.freshness or "untracked",
        }


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    risk: str = "low"
    evidence_roles: tuple[str, ...] = ()
    memory_id: str = ""

    @property
    def admitted(self) -> bool:
        return self.action == PROMOTE

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "risk": self.risk,
            "evidence_roles": list(self.evidence_roles),
            "memory_id": self.memory_id,
        }


def propose_from_turn(
    *,
    role: str,
    content: str,
    journal_ids: Sequence[int],
    min_chars: int = 12,
    target: str = "project",
) -> Candidate | None:
    """Turn a captured turn into a candidate.

    The authority follows the speaker: a user turn is the user's own statement; an
    assistant turn is an inference, and is labelled as one. Nothing here decides
    admission -- that is the gate's job.
    """

    text = normalise_space(content)
    if len(text) < min_chars:
        return None
    lowered = str(role or "").strip().lower()
    if lowered == "user":
        authority, confidence, importance, memory_type = "user", 0.85, 0.7, "factual"
    elif lowered == "tool":
        authority, confidence, importance, memory_type = "tool", 0.7, 0.5, "tool_trace"
    else:
        authority, confidence, importance, memory_type = "inference", 0.35, 0.35, "summary"
    return Candidate(
        content=summary_of(text, 600),
        target=target,
        authority=authority,
        memory_type=memory_type,
        source="extraction",
        confidence=confidence,
        importance=importance,
        journal_ids=[int(i) for i in journal_ids],
        freshness="" if lowered in ("user", "tool") else freshness_mod.NEEDS_CHECK,
    )


def _evidence_roles(conn: sqlite3.Connection, candidate: Candidate) -> set[str]:
    """Roles reachable through the candidate's journal links, without reading text."""

    roles = journal_mod.roles_for(conn, candidate.journal_ids)
    if roles:
        return roles
    tables = {"journal"}
    if not tables.issubset(_tables(conn)):
        return set()
    return set()


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(r["name"]) for r in rows}


def classify(conn: sqlite3.Connection, candidate: Candidate, *, operator: bool = False) -> Decision:
    """Decide what to do with a candidate. Denies by default."""

    tables = _tables(conn)
    if not {"journal", "memories", "evidence"}.issubset(tables):
        return Decision(KEEP, "store_tables_unavailable", risk="high")

    roles = _evidence_roles(conn, candidate)
    role_tuple = tuple(sorted(roles))

    if operator:
        # An explicit operator action carries its own authority; recorded as such.
        return Decision(PROMOTE, "operator_decision", risk="low", evidence_roles=role_tuple)

    if candidate.authority in store.HUMAN_AUTHORITIES:
        if not roles:
            return Decision(KEEP, "missing_provenance", risk="medium", evidence_roles=role_tuple)
        return Decision(PROMOTE, "human_authored_with_provenance", evidence_roles=role_tuple)

    if not roles:
        return Decision(KEEP, "missing_provenance", risk="high", evidence_roles=role_tuple)
    if roles <= {"assistant"}:
        return Decision(KEEP, "assistant_only_evidence", risk="high", evidence_roles=role_tuple)
    if not (roles & set(journal_mod.EVIDENCE_ROLES)):
        return Decision(KEEP, "unsupported_evidence_roles", risk="high", evidence_roles=role_tuple)

    if candidate.confidence < MIN_CONFIDENCE:
        return Decision(KEEP, "insufficient_confidence", risk="medium", evidence_roles=role_tuple)
    if candidate.importance < MIN_IMPORTANCE:
        return Decision(KEEP, "below_importance_floor", risk="low", evidence_roles=role_tuple)

    return Decision(PROMOTE, "non_assistant_evidence", evidence_roles=role_tuple)


def admit(
    conn: sqlite3.Connection,
    *,
    scope: scope_mod.Scope,
    candidate: Candidate,
    operator: bool = False,
    promote: bool = True,
) -> Decision:
    """Run the gate and, if it passes, write the memory with its evidence."""

    decision = classify(conn, candidate, operator=operator)
    if not decision.admitted or not promote:
        return decision

    if not candidate.journal_ids:
        return Decision(KEEP, "missing_provenance", risk="high")

    memory_id = store.put_memory(
        conn,
        scope=scope,
        target=candidate.target,
        content=candidate.content,
        authority=candidate.authority,
        memory_type=candidate.memory_type,
        source=candidate.source,
        importance=candidate.importance,
        confidence=candidate.confidence,
        freshness=candidate.freshness,
        lifecycle="promoted",
    )
    store.record_evidence(conn, memory_id=memory_id, journal_ids=candidate.journal_ids)
    return Decision(
        PROMOTE,
        decision.reason,
        risk=decision.risk,
        evidence_roles=decision.evidence_roles,
        memory_id=memory_id,
    )


def audit(conn: sqlite3.Connection, scope: scope_mod.Scope) -> dict[str, Any]:
    """Report the authority mix of live memory, and flag inference-only rows."""

    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"""
        SELECT m.id, m.authority, m.lifecycle, m.importance,
               (SELECT COUNT(*) FROM evidence e WHERE e.memory_id = m.id
                  AND e.role IN ('user','tool')) AS supporting,
               (SELECT COUNT(*) FROM evidence e WHERE e.memory_id = m.id) AS total
        FROM memories m WHERE m.scope_id IN ({marks})
        """,
        params,
    ).fetchall()

    counts: dict[str, int] = {}
    flagged: list[dict[str, Any]] = []
    for row in rows:
        authority = str(row["authority"])
        counts[authority] = counts.get(authority, 0) + 1
        # Only non-human authority needs evidence. An operator or user write is
        # authoritative by construction and legitimately has no conversation behind
        # it; an inference that was promoted without user or tool evidence is the
        # thing this audit exists to find.
        if (
            str(row["lifecycle"]) == "promoted"
            and authority not in store.HUMAN_AUTHORITIES
            and int(row["supporting"] or 0) == 0
        ):
            flagged.append(
                {
                    "id": str(row["id"]),
                    "authority": authority,
                    "evidence_rows": int(row["total"] or 0),
                    "reason": "promoted_without_human_or_tool_evidence",
                }
            )
    return {"authority_counts": counts, "flagged": flagged, "checked": len(rows)}


def promote_candidates(
    conn: sqlite3.Connection, scope: scope_mod.Scope, *, apply: bool = False
) -> dict[str, Any]:
    """Re-run the gate over admitted-but-unpromoted rows.

    Rows are never written without their evidence, so promotion here only has to
    ask the gate again; the evidence is already attached.
    """

    marks, params = scope_mod.placeholders(scope.readable())
    rows = conn.execute(
        f"""
        SELECT m.*, (SELECT COUNT(*) FROM evidence e WHERE e.memory_id = m.id) AS evidence_rows,
               (SELECT COUNT(*) FROM evidence e WHERE e.memory_id = m.id
                  AND e.role IN ('user','tool')) AS supporting
        FROM memories m
        WHERE m.scope_id IN ({marks}) AND m.lifecycle = 'candidate'
        ORDER BY m.importance DESC
        """,
        params,
    ).fetchall()

    promoted: list[str] = []
    kept: list[dict[str, str]] = []
    for row in rows:
        memory_id = str(row["id"])
        authority = str(row["authority"])
        supporting = int(row["supporting"] or 0)
        evidence_rows = int(row["evidence_rows"] or 0)
        if evidence_rows == 0:
            kept.append({"id": memory_id, "reason": "missing_provenance"})
            continue
        if supporting == 0 and authority not in store.HUMAN_AUTHORITIES:
            kept.append({"id": memory_id, "reason": "assistant_only_evidence"})
            continue
        if float(row["confidence"] or 0.0) < MIN_CONFIDENCE:
            kept.append({"id": memory_id, "reason": "insufficient_confidence"})
            continue
        promoted.append(memory_id)

    if apply and promoted:
        store.set_lifecycle(conn, promoted, "promoted")

    return {"candidates": len(rows), "promoted": promoted, "kept": kept, "applied": bool(apply)}
