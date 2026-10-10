# Design

How `memk` is put together, and why each decision is what it is.

## Failure modes this design answers

Eight problems, each observed running before it was designed out. Every one has a
check in the self test, so the answer is reproducible rather than asserted.

| # | Failure mode | The answer | Self test |
|---|---|---|---|
| 1 | **CJK lexical recall was broken.** The word index treated a whole Chinese run as one token, so common two-character words matched nothing and Chinese recall depended entirely on the vector channel. | Store a CJK n-gram shadow index beside the word index, so Chinese text is lexically searchable with no embedding model involved. | `recall: two-character Chinese query matches (lexical, no embedder)` |
| 2 | **The embedder degraded silently.** A missing key fell back to a hash embedder, so semantic recall quietly became string matching. | The semantic channel is opt-in and self-declaring: no configuration means no channel, an unusable configuration means no channel with a stated reason, and every stored vector records the model that produced it. | `embed: absent by default, and nothing is substituted for it` |
| 3 | **Two sources of truth drifted apart.** The injected file and the store were written independently and barely agreed. | The store is the only truth; the injected view is a projection with a recorded render set. | `view: budget respected, pinned survives, hand edit adopted` |
| 4 | **Provenance was a link with no wording.** An extracted fact carried a log id, no excerpt and no checksum, so a quote could not be verified. | Every admitted memory stores evidence rows with an excerpt and a content hash, verified on read; `complete=true` only after the hash check passes. | `journal: chunking, reconstruction, tamper detection` |
| 5 | **An inference ranked as a fact.** Ranking used one importance number with no notion of who said it. | Authority is a first-class field; inference and unknown are never presented as user-confirmed. | `audit: no promoted memory lacks human or tool evidence` |
| 6 | **Bulk freshness marking degraded recall.** Marking many rows at once pushed their ranking down together. | Freshness is per-fact, the bulk path is refused by construction, and supersession keeps history instead of hiding it. | `freshness: bulk change refused, supersession keeps history` |
| 7 | **A stalled lock could freeze the process.** Unbounded file locking let one slow holder hang the caller. | There is no file lock in the kernel at all: the only wait is SQLite's busy timeout, set once in the single connection factory so a writer cannot queue indefinitely. | enforced in `schema.connect`; no dedicated check |
| 8 | **WAL was armed on a build with the reset defect.** The journal mode was switched unconditionally. | A single connection factory gates the journal mode and never live-downgrades an open database. | `schema: vulnerable WAL builds are gated, fixed ones are not` |

## The optional semantic channel

Three rules make an optional channel safe to have at all:

- **absence is a state, not a fallback** — `resolve()` returns `None` plus a reason
  rather than a weaker model, and the CLI prints that reason;
- **a vector belongs to a model** — `vector_records.embedder` is part of the search
  predicate, so switching models means re-indexing, never mixing distances;
- **the channel names itself in the result** — a hit records the embedder and the
  vector score, so a suspiciously good match can be traced to its source.

The interface has three implementations: an OpenAI-compatible endpoint (standard
library only), a local sentence-transformers model loaded offline, and a
deterministic test double that announces itself as a test double in its own name.

## Layers

```
   injection     view render      ← projection of the store, pinned-aware, budgeted
   ─────────────────────────────────────────────────────────────────────────
   retrieval     lexical + ngram + vector, fused          ← scope & lifecycle in SQL
   ─────────────────────────────────────────────────────────────────────────
   truth         memories / evidence / candidates / journal      (SQLite)
   ─────────────────────────────────────────────────────────────────────────
   capture       journal: chunked, role-tagged, hashed
```

## The lifecycle, stated as a rule

A memory is not "written"; it is **proposed, evidenced, admitted, and then
current**. Each transition has a gate:

```
turn ──capture──► journal row (role, chunk, hash)
        │
        └──extract──► candidate  ──admission gate──► memory (authority, freshness)
                          │                              │
                          │ fails gate                   │ superseded
                          ▼                              ▼
                     stays candidate               historical + pointer
```

The admission gate is the load-bearing part: a candidate becomes a memory only
when its evidence includes a **non-assistant** role, or the operator promotes it
explicitly. Everything else stays a candidate with a reason code. The gate reads
roles, never content, so a provenance check cannot leak the conversation into a
decision log.

## Freshness

| State | Penalty | Meaning |
|---|---|---|
| absent | none | default; the safest state |
| `current` | none | confirmed, or the user decided it |
| `needs_check` | small | awaiting verification; **never bulk-applied** |
| `historical` | large | superseded, still recallable for audit |

## Tiering

The injection view holds what is always relevant: safety rules, the user
profile, and recently-used facts. Everything else lives in the store and is
retrieved on demand. Recall frequency moves `importance`, which decides what
re-enters the view. Pinned entries are exempt from both the budget cut and the
reweighting.

Which targets that means in practice is `VIEW_TARGETS` in `view.py`: `memory`
carries the rules and durable facts, `user` carries the profile, and every other
target is recall-only. The first version rendered only `memory`, which quietly
dropped the profile from every prompt -- the design said "safety rules and the user
profile", and the code said otherwise. A self test now pins the pair down.

## Why the ops layer is inside the package

Backup, dedupe, compaction and the golden set could have been a folder of scripts
beside the library. They are modules instead, because the kernel owns its schema:
a script that guesses at the schema is one schema change away from being wrong, and
one that writes without a snapshot is one bad run away from a support incident.

Two behaviours in that layer are deliberate and worth knowing:

- **verification restores.** A snapshot is proven by opening it, running
  `PRAGMA integrity_check` and comparing checksum and row counts against the
  manifest. A refused snapshot also blocks a restore, so a bad backup cannot
  overwrite a live store.
- **compaction fails closed.** Shortening a row is refused outright if it would
  drop an anchor — a URL, a path, a version, a date, an environment variable name,
  a long id — and the refusal names the anchors it would have lost.

## What is intentionally not built

- **No vector backend bundled.** The interface exists and the store works
  lexically first; a vector companion is optional, pluggable, and must name
  itself. An optional dependency must not be able to silently become the whole
  retrieval story.
- **No multi-user server.** Scope is per-user and enforced in SQL, but there is
  no account system, no HTTP surface, no remote API.
- **No auto-deletion.** Supersession and archival only; hard delete requires an
  explicit operator command with a backup.
- **No agent framework coupling.** The kernel is importable and CLI-driven; the
  adapter to any particular agent host lives outside the core.
- **No on-disk migration for destructive changes.** Additive schema changes apply
  through idempotent DDL, and the self test asserts a reopen keeps every row.
