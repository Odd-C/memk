# memk

A self-authored memory kernel for agents. **No runtime dependencies** — SQLite and
the standard library, nothing else. No embedding model, no vector database, no
service. Everything below runs on a laptop with the Python that ships with it.

```bash
python3 -m memk.selftest          # 23 checks, end to end, on a temporary database
```

**MIT** · **Python 3.10+** · **zero dependencies** · **status: alpha**

---

## Why this exists

Agent memory usually fails in one of two ways: it recalls something that was never
true, or it forgets something that was. Both are caused by the same omission —
memory treated as text rather than as a claim with a standing, a source, and an
expiry.

`memk` treats every durable fact as a claim, and makes four questions answerable
at the moment of use:

| Question | Mechanism |
|---|---|
| **Who said it?** | `authority` is a first-class column: a user statement, an operator write, a tool result, or an inference. An inference is never presented as a confirmed fact. |
| **Is it still true?** | `freshness` is one of four states with a fixed ranking cost. A superseded fact stays recallable and ranks down instead of disappearing. |
| **Where is the original?** | Every admitted fact carries evidence rows pointing into a chunked journal, and a quote is only reported complete when the rebuild passes a hash check. |
| **Can it become a fact by itself?** | No. Automatic promotion requires non-assistant evidence; every failure of evidence denies, with a reason code. |

## Correctness properties, measured not asserted

The kernel was written against eight defects we measured in a previous
arrangement (full list and provenance in `DESIGN.md`). Each is now a self test:

| Defect we had | Check in this repo |
|---|---|
| A two-character Chinese query matched nothing, because the word index collapsed a whole phrase into one token | `recall: two-character Chinese query matches (lexical, no embedder)` |
| A missing embedding key silently downgraded semantic recall to string matching | No embedder is bundled at all; lexical recall for CJK is native |
| An extracted fact had a journal link but no verifiable quote | `journal: chunking, reconstruction, tamper detection` |
| An assistant's inference could be promoted as a user fact | `admission: assistant-only evidence is refused` |
| Two sources of truth drifted apart | `view: budget respected, pinned survives, hand edit adopted` (the file is a projection; the store is the truth) |
| Bulk freshness marking degraded recall | `freshness: bulk change refused, supersession keeps history` |
| A stalled lock could freeze the caller | every lock has a deadline; connection flags are applied in one factory |
| WAL was armed on a build with the reset defect | `schema.py` gates the journal mode and never live-downgrades |

## Quick start

```bash
DB=/tmp/demo.sqlite3

python3 -m memk.cli --db $DB init

# A pinned safety rule: exempt from the injection budget and from reweighting.
python3 -m memk.cli --db $DB store --target memory \
  --content "Credentials are never written to memory; store a pointer instead." --pinned

python3 -m memk.cli --db $DB store --target project \
  --content "Deployment runs every Friday afternoon; rollback window is two hours."

# Chinese, two characters, no model involved:
python3 -m memk.cli --db $DB recall 部署

# An inference does not pass the gate:
python3 -m memk.cli --db $DB propose --role assistant \
  --content "I infer the user prefers Tuesday releases." --apply     # exit 1, kept as candidate

# The user's own words do:
python3 -m memk.cli --db $DB propose --role user \
  --content "Confirmed: backups are daily incremental, weekly full, thirty retained." \
  --target project --apply

python3 -m memk.cli --db $DB doctor
python3 -m memk.cli --db $DB packet "what is the current release window"
```

## One turn, and the optional semantic channel

The whole loop is one call, and every option is explicit:

```bash
# capture -> extract -> admission gate -> packet for the question
python3 -m memk.cli --db $DB turn --role user \
  --content "User: releases are fixed to Friday afternoon." \
  --query "when is the release window"

# answer-shaped read
python3 -m memk.cli --db $DB ask "when is the release window"

# is a semantic channel configured? (it is absent unless you say otherwise)
python3 -m memk.cli --db $DB embed status
```

The semantic channel is off unless asked for. Asking for it is a per-command
decision, and the difference is measurable:

```
$ memk recall "每周五部署"
# semantic channel absent: not_configured
  0.01966 [gram] 部署流水线每周五执行，采用蓝绿切换。

$ memk recall "每周五部署" --embedder test --embedder-dimensions 64
  0.03752 [gram,vector] 部署流水线每周五执行，采用蓝绿切换。
     vector_score: 0.201262
```

Three rules make it safe to have an optional channel at all:

1. an unavailable embedder means the channel is **absent**, never substituted;
2. every stored vector records the embedder that produced it, and a search only
   reads vectors from the embedder in use — so two models never mix;
3. the channel that produced a hit is named in the result.

## What is in the box

| Module | Responsibility |
|---|---|
| `schema.py` | One connection factory: journal-mode gate, busy timeout, schema. Two lexical indexes: words and CJK n-grams. |
| `journal.py` | Chunked capture with per-turn hashes; reconstruction that verifies before it claims completeness. |
| `store.py` | The truth store: memory CRUD, evidence, lifecycle, freshness, index maintenance in the same transaction. |
| `admission.py` | Candidate extraction, the fail-closed evidence gate, the promotion sweep, the evidence audit. |
| `recall.py` | Three channels (word, n-gram, timeline), rank fusion, freshness and authority corrections, reweighting. |
| `timeline.py` | Deterministic Chinese time-range parsing, with range and chronology kept as separate signals. |
| `packet.py` | The structured payload handed to a model, including the handling rule. |
| `view.py` | The injection projection: budget, pinned exemption, hand-edit detection, render-set tracking. |
| `scope.py` | Scope composition, including the agent level that reads see and writes never target implicitly. |
| `freshness.py` | Four states, their ranking costs, the supersession plan, and the bulk-change refusal. |
| `embed.py` | The optional embedding channel: a self-declaring interface, an OpenAI-compatible client, a local offline model, and a test double that announces itself. |
| `vectors.py` | The vector companion in SQLite: packed float32, provenance per vector, no mixing across models. |
| `session.py` | The seam a host talks to: ingest, hooks (`on_turn_start`, `on_pre_compress`, `on_session_end`), packet, health. |
| `cli.py` | Everything above, drivable by a human. Mutation commands default to a dry run. |

## Invariants

- **The store is the only truth.** The injected view is rendered from it and records
  what it rendered, so a difference between the two is attributable rather than
  mysterious.
- **Nothing is deleted automatically.** Supersession marks history; archival is an
  explicit operator act.
- **Ties fail closed.** Missing tables, missing evidence, unreadable provenance,
  assistant-only provenance and unknown roles all deny promotion.
- **Boundaries are enforced in SQL.** Scope and lifecycle filters run before
  ranking, not after.
- **Every optional capability must name itself.** A channel that cannot run is
  absent, not silently substituted.

## Deliberately out of scope

- No vector dependency is bundled: vectors live in SQLite and the channel is
  optional, so lexical recall has to be good on its own terms first.
- No account system, HTTP surface or remote API.
- No agent-framework coupling: the kernel is importable and CLI-driven. Host
  adapters belong outside the core.

## Licence

MIT. See `LICENSE`. `DESIGN.md` records which parts of an earlier architecture were
reused (the design) and which were rebuilt (the engine), so the provenance of every
idea here is checkable.
