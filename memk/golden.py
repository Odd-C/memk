"""Golden recall set: measure retrieval with the real path, not a probe.

The question this answers is not "is the row still in the database" but "does the
question find it". Each case runs through the full retrieval path and reports where
the expected row landed -- or that it did not.

What it reports, and why each number is there:

* **hit@k** for k in 1, 3, 10 -- because "recalled at position 7" and "recalled
  first" are different failures;
* **MRR** -- one number that punishes burying an answer;
* **misses with the reason** -- a miss caused by a candidate state, a scope boundary
  or a timeline gate is a different bug from a miss caused by ranking;
* **forbidden ids** -- the negative half of the test, which is what catches a filter
  that stopped filtering.

Cases are data (JSON), not code, so the set can grow without touching the kernel. The
bundled example set is a *shape*, not a benchmark: expected tokens have to exist in the
store being measured, or every case misses and the report says nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import recall, scope as scope_mod
from .embed import Embedder
from .recall import Hit

DEFAULT_KS = (1, 3, 10)


@dataclass(frozen=True)
class Case:
    query: str
    expect_ids: tuple[str, ...] = ()
    expect_tokens: tuple[str, ...] = ()
    forbid_ids: tuple[str, ...] = ()
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Case":
        return cls(
            query=str(raw.get("query") or "").strip(),
            expect_ids=tuple(str(v) for v in (raw.get("expect_ids") or [])),
            expect_tokens=tuple(str(v) for v in (raw.get("expect_tokens") or raw.get("tokens") or [])),
            forbid_ids=tuple(str(v) for v in (raw.get("forbid_ids") or [])),
            note=str(raw.get("note") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "expect_ids": list(self.expect_ids),
            "expect_tokens": list(self.expect_tokens),
            "forbid_ids": list(self.forbid_ids),
            "note": self.note,
        }


EXAMPLE_CASES: list[dict[str, Any]] = [
    {
        "query": "example: a durable fact question",
        "expect_tokens": ["replace-with-a-token-that-exists-in-your-store"],
        "note": "shape example only",
    },
    {
        "query": "example: a decision question",
        "expect_tokens": ["another-token"],
        "note": "shape example only",
    },
]


def load(path: str | Path | None = None) -> list[Case]:
    if path is None:
        return [Case.from_dict(raw) for raw in EXAMPLE_CASES]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("golden set must be a JSON list of cases")
    return [Case.from_dict(raw) for raw in data if isinstance(raw, dict)]


def _first_hit(case: Case, hits: Sequence[Hit]) -> tuple[int, str]:
    """Rank (1-based) of the first result that satisfies the case, else (0, reason)."""

    for index, hit in enumerate(hits, start=1):
        if case.expect_ids and hit.memory_id in case.expect_ids:
            return index, "id"
        if case.expect_tokens:
            haystack = f"{hit.content} {hit.summary}".casefold()
            if any(token.casefold() in haystack for token in case.expect_tokens):
                return index, "token"
    if not case.expect_ids and not case.expect_tokens:
        return 1, "unchecked"
    return 0, "not_found"


def _why_missing(conn, scope: scope_mod.Scope, case: Case, hits: Sequence[Hit]) -> str:
    """Distinguish reasons a case can miss, because they are different bugs."""

    if not hits:
        return "no_candidates"
    for hit in hits:
        if hit.explanation.get("timeline_match"):
            return "timeline_channel_suppressed_topical"
    if any("vector" not in hit.channels for hit in hits):
        return "lexical_only"
    return "ranked_below_limit"


def run(
    conn: Any,
    scope: scope_mod.Scope,
    cases: Sequence[Case],
    *,
    ks: Sequence[int] = DEFAULT_KS,
    limit: int = 10,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Run every case through the real retrieval path and summarise."""

    per_case: list[dict[str, Any]] = []
    reciprocal: list[float] = []
    hits_at: dict[int, int] = {k: 0 for k in ks}
    graded = 0

    for case in cases:
        results = recall.search(
            conn, scope, case.query, limit=limit, mark_recalled=False, embedder=embedder
        )
        rank, matched_by = _first_hit(case, results)
        forbidden_hits = [
            hit.memory_id for hit in results if hit.memory_id in set(case.forbid_ids)
        ]
        graded += 1
        reciprocal.append(1.0 / rank if rank else 0.0)
        for k in ks:
            if rank and rank <= k:
                hits_at[k] += 1
        entry = {
            "query": case.query,
            "rank": rank,
            "matched_by": matched_by,
            "returned": len(results),
            "top_id": results[0].memory_id if results else "",
            "top_channels": results[0].channels if results else [],
            "forbidden_hits": forbidden_hits,
            "note": case.note,
        }
        if not rank:
            entry["diagnosis"] = _why_missing(conn, scope, case, results)
        per_case.append(entry)

    total = max(1, graded)
    return {
        "cases": graded,
        "limit": limit,
        "hit_at": {f"@{k}": hits_at[k] for k in ks},
        "hit_rate": {f"@{k}": round(hits_at[k] / total, 4) for k in ks},
        "mrr": round(sum(reciprocal) / total, 4),
        "misses": [entry for entry in per_case if not entry["rank"]],
        "forbidden_violations": [
            entry for entry in per_case if entry["forbidden_hits"]
        ],
        "details": per_case,
        "embedder": embedder.name if embedder is not None else "",
    }


def write_example(path: str | Path) -> str:
    target = Path(path)
    target.write_text(
        json.dumps(EXAMPLE_CASES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return str(target)
