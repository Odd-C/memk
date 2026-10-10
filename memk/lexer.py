"""Lexing: two indexes, because not every language uses spaces.

The index that a search consults must match how the query is written. A word
index over space-separated text and an n-gram index over runs of CJK characters
are kept side by side; the query decides which one runs. This is the answer to
the defect we measured earlier: a single word index over Chinese text collapses a
whole phrase into one token, so common two-character words match nothing, and
recall silently depends on an embedding model being installed.

The n-gram width is two. Two is the shortest unit that distinguishes common
Chinese words ("部署", "备份", "记忆") without exploding the index, and a
two-character query still produces at least one gram.
"""

from __future__ import annotations

import re
from typing import Iterable

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RUN = re.compile(f"[{CJK}]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")
NGRAM_WIDTH = 2
#: Ceiling per memory so one long document cannot dominate the index.
MAX_GRAMS = 4096


def has_cjk(text: str) -> bool:
    return bool(_CJK_RUN.search(text or ""))


def word_tokens(text: str) -> list[str]:
    """Tokens for the word index: lower-cased alphanumeric runs."""

    return [t.lower() for t in _WORD.findall(text or "")]


def cjk_runs(text: str) -> list[str]:
    return _CJK_RUN.findall(text or "")


def ngrams(text: str, width: int = NGRAM_WIDTH) -> list[str]:
    """Ordered, de-duplicated character n-grams from every CJK run."""

    out: list[str] = []
    seen: set[str] = set()
    for run in cjk_runs(text):
        if len(run) < width:
            # A single character is its own gram so one-character queries work.
            if run and run not in seen:
                seen.add(run)
                out.append(run)
            continue
        for i in range(len(run) - width + 1):
            gram = run[i : i + width]
            if gram not in seen:
                seen.add(gram)
                out.append(gram)
                if len(out) >= MAX_GRAMS:
                    return out
    return out


def index_row(text: str) -> tuple[str, str]:
    """Return ``(word_text, gram_text)`` for one indexed document.

    The word column keeps the original text; the gram column keeps the n-grams
    space-joined, which is what the n-gram index tokenises.
    """

    return text or "", " ".join(ngrams(text or ""))


def gram_coverage(query: str, document: str) -> tuple[float, list[str]]:
    """Fraction of the query's n-grams present in *document*, plus the hits.

    Coverage, not bag-of-words similarity: we want "did the document contain what
    was asked about", and the matched gram list is reported so a ranked result can
    explain itself.
    """

    wanted = ngrams(query)
    if not wanted:
        return 0.0, []
    have = set(ngrams(document))
    matched = [g for g in wanted if g in have]
    return len(matched) / max(1, len(wanted)), matched


def match_expression(query: str, *, mode: str = "or") -> str:
    """Build an FTS expression for a query, scoped to the right index."""

    terms: list[str] = []
    if has_cjk(query):
        terms = ngrams(query)
    else:
        terms = word_tokens(query)
    if not terms:
        return ""
    quoted = [f'"{t}"' for t in terms]
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join(quoted)


def describe_indexes(text: str) -> dict[str, object]:
    return {
        "has_cjk": has_cjk(text),
        "word_tokens": word_tokens(text)[:20],
        "ngram_count": len(ngrams(text)),
        "ngram_sample": ngrams(text)[:10],
    }


def dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
