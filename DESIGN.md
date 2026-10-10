# memk — design and reuse map

`memk` is a self-authored memory kernel for agents. It is written from our own
design notes, not derived from any other memory project's code. This document
records exactly which parts of our earlier architecture are reused, which are
rebuilt, and why.

## What "reuse" means here

We had a complete *architecture* before, and a borrowed *engine* underneath it.
The architecture is ours: layered model, tiering rules, admission doctrine,
freshness semantics, evaluation method, ops layer. The engine was never ours.

So the kernel reuses the first and replaces the second.

| Asset | Status | Where it came from |
|---|---|---|
| 7-layer architecture model | **reused as the structural spec** | our own `architecture-deep-dive` notes |
| hot/cold tiering (injection layer, heat/cool, pinned exemption) | **reused as behaviour** | our own tiering design |
| four-ability scoring (durability / findability / currency / use) | **reused as the acceptance frame** | our own assessment model |
| five-state audit (implemented → configured → reachable → populated → effective) | **reused as the review method** | our own audit methodology |
| provenance grading + fail-closed admission | **reused as the core invariant** | our own write protocol |
| freshness three-state + penalty table | **reused, values kept** | our own freshness work |
| ops layer (backup, compaction, dedup, tuning, golden set, health) | **reused as-is** | 2686 lines we wrote |
| agent-facing contracts | **reused as-is** | our own three contracts |
| the engine (capture, store, retrieval, vectors) | **rebuilt from scratch** | this repository |
| upstream patches | **not used** | dropped entirely |

## What the old engine got wrong (and the kernel fixes natively)

These are not hypotheticals; each was measured and recorded in our own notes.
They are the kernel's requirements.

| # | Defect we recorded | Kernel's native answer |
|---|---|---|
| 1 | **CJK lexical recall was broken.** The FTS tokenizer treated a whole Chinese run as one token, so `火山引擎` matched nothing while `ComfyUI` matched. Chinese recall depended entirely on the vector channel. | Store a CJK n-gram shadow column and index it, so Chinese text is lexically searchable without an embedding model. |
| 2 | **The embedder degraded silently.** A missing API key fell back to a 256-dimension hash embedder, so semantic recall silently became string matching. | The semantic channel is opt-in and self-declaring: no configuration means no channel, an unusable configuration means no channel with a stated reason, and every stored vector records which model produced it so two models can never be mixed in one ranking. |
| 3 | **Two sources of truth.** The injected file and the store were written independently and did not agree (16 rows vs 61 rows, almost no overlap). | The store is the only truth. The injected view is a rendered projection with a recorded render set. |
| 4 | **Auto-extracted memory carried a link but no evidence.** Provenance was a journal id with no excerpt and no hash, so "quote the original" was unverifiable. | Every admitted memory stores evidence rows with excerpt and content hash, verified on read. |
| 5 | **A research/planning layer could outrank a confirmed fact.** Ranking used a single importance number with no authority notion. | Authority is a first-class field; `inference` and unknown never rank as user-confirmed. |
| 6 | **Bulk freshness marking degraded recall.** Marking many rows "needs check" at once pushed ranking down and had to be rolled back. | Freshness is per-fact and the bulk path is absent by construction; the CLI refuses batch mutation without an explicit per-row flag. |
| 7 | **A stalled lock could freeze the agent.** Unbounded file locking plus a shared connection meant one slow holder hung the event loop. | Every lock has a deadline; the request path never takes the writer connection. |
| 8 | **WAL mode was armed unconditionally** on a build with a known reset defect. | A single connection factory gates the journal mode and never live-downgrades. |

## Design

### What the optional semantic channel changed

Two design notes fed this, and both were ours: the embedder selection chain
(rejecting a remote embedder for a private assistant, rejecting a hosted model that
would move memory text off the machine, and settling on a local model) and the CPU
latency measurements that made the choice defensible.

The implementation keeps three of those conclusions as hard rules:

- **absence is a state, not a fallback** — `resolve()` returns `None` plus a reason
  rather than a weaker model, and the CLI prints that reason;
- **a vector belongs to a model** — `vector_records.embedder` is part of the search
  predicate, so switching models means re-indexing, never mixing distances;
- **the channel names itself in the result** — a hit records the embedder and the
  vector score, so a suspiciously good match can be traced to its source.

The interface has three implementations: an OpenAI-compatible endpoint (standard
library only), a local sentence-transformers model loaded offline, and a
deterministic test double that announces itself as a test double in its own name.

### Layers (kept from the old model, now owned end to end)

```
   injection     view render      ← projection of the store, pinned-aware, budgeted
   ─────────────────────────────────────────────────────────────────────────
   retrieval     lexical + ngram + vector, fused          ← scope & lifecycle in SQL
   ─────────────────────────────────────────────────────────────────────────
   truth         memories / evidence / candidates / journal      (SQLite)
   ─────────────────────────────────────────────────────────────────────────
   capture       journal: chunked, role-tagged, hashed
```

### The lifecycle, stated as a rule

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
explicitly. Everything else stays a candidate with a reason code.

### Freshness

| State | Penalty | Meaning |
|---|---|---|
| absent | none | default; the safest state |
| `current` | none | confirmed, or the user decided it |
| `needs_check` | small | awaiting verification; **never bulk-applied** |
| `historical` | large | superseded, still recallable for audit |

### Tiering

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

## What is intentionally not built

- **No vector backend bundled.** The interface exists and the store works
  lexically first; a vector companion is optional, pluggable, and must name
  itself. This follows from defect #2: an optional dependency must not be able to
  silently become the whole retrieval story.
- **No multi-user server.** Scope is per-user and enforced in SQL, but there is
  no account system, no HTTP surface, no remote API.
- **No auto-deletion.** Supersession and archival only; hard delete requires an
  explicit operator command with a backup.
- **No agent framework coupling.** The kernel is importable and CLI-driven; the
  adapter to any particular agent host lives outside the core.
