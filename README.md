# memk

**English** · [简体中文](README.zh-CN.md)

**A memory kernel for agents: every durable fact is a claim with an authority, a freshness state, and provenance you can re-verify.**

No runtime dependencies — SQLite and the standard library, nothing else. No embedding model, no vector database, no service.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-success.svg)](#install)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](ROADMAP.md)

**Contents**
[What it is](#what-it-is) ·
[Why it exists](#why-it-exists) ·
[The model](#the-model-a-memory-is-a-claim) ·
[Install](#install) ·
[Quick start](#quick-start) ·
[How it works](#how-it-works) ·
[CLI](#cli-reference) ·
[Python API](#python-api) ·
[Semantic channel](#the-optional-semantic-channel) ·
[Operations](#operations) ·
[Verification](#verification) ·
[Layout](#project-layout) ·
[Scope and gaps](#scope-and-known-gaps) ·
[Docs](#documentation) ·
[Contributing](#contributing) ·
[Licence](#licence)

---

## What it is

`memk` is an embedded memory store for agent hosts. It captures turns into a
chunked journal, extracts candidate facts, runs each candidate past a
fail-closed admission gate, and answers questions with a budgeted packet that
carries its own provenance.

It is a **library and a CLI**, not a service. There is no HTTP surface, no
account system, and no framework coupling — the adapter to any particular agent
host lives outside the core.

## Why it exists

Agent memory usually fails in one of two ways: it recalls something that was
never true, or it forgets something that was. Both come from the same omission —
memory treated as text rather than as a claim with a standing, a source, and an
expiry.

## The model: a memory is a claim

Every durable fact is stored as a claim, which makes four questions answerable at
the moment of use:

| Question | Mechanism |
|---|---|
| **Who said it?** | `authority` is a first-class column: a user statement, an operator write, a tool result, or an inference. An inference is never presented as a confirmed fact. |
| **Is it still true?** | `freshness` is one of four states with a fixed ranking cost. A superseded fact stays recallable and ranks down instead of disappearing. |
| **Where is the original?** | Every admitted fact carries evidence rows pointing into a chunked journal, and a quote is only reported complete when the rebuild passes a hash check. |
| **Can it become a fact by itself?** | No. Automatic promotion requires non-assistant evidence; every failure of evidence denies, with a reason code. |

A memory is not "written" — it is **proposed, evidenced, admitted, and then
current**:

```
turn ──capture──► journal row (role, chunk, hash)
        │
        └──extract──► candidate  ──admission gate──► memory (authority, freshness)
                          │                              │
                          │ fails gate                   │ superseded
                          ▼                              ▼
                     stays candidate               historical + pointer
```

The gate reads **roles, never content**, so a provenance decision cannot leak the
conversation into a decision log.

## Install

**Requirements:** Python 3.10+ and SQLite (bundled with CPython). No other runtime
dependency, by design.

```bash
git clone https://github.com/Odd-C/memk.git && cd memk
python3 -m memk.selftest          # 27 checks, end to end; no install step required
```

Or install the console script:

```bash
pip install .
memk --help
```

> **Windows:** CPython on Windows ships no IANA time-zone database, so the
> timezone-aware paths in `timeline.py` raise `ZoneInfoNotFoundError` unless
> `tzdata` is present. Install the pure-data package to make the timeline, recall
> and packet paths work:
>
> ```bash
> pip install tzdata
> ```
>
> Without it, `python -m memk.selftest` reports 16 PASS / 11 FAIL on an otherwise
> clean Windows interpreter. This is a known portability gap, not a data problem.

## Quick start

Commands take the database from `--db`, falling back to `$MEMK_DB`, then
`./memk.sqlite3`. The examples below use `python3 -m memk.cli`, which is
equivalent to the `memk` console script installed by `pip install .`.

```bash
export DB=demo.sqlite3

python3 -m memk.cli --db $DB init

# An operator write with authority by construction, pinned so it never leaves the view.
python3 -m memk.cli --db $DB store --target memory \
  --content "Credentials are never written to memory; store a pointer instead." --pinned

python3 -m memk.cli --db $DB store --target project \
  --content "部署流水线每周五执行，采用蓝绿切换。"
```

Recall works on Chinese without any model — the CJK n-gram index is native:

```bash
$ python3 -m memk.cli --db $DB recall 部署
# semantic channel absent: not_configured
  0.01966 [gram] 部署流水线每周五执行，采用蓝绿切换。
```

Now the gate. An assistant's inference does not pass; the user's own words do:

```bash
$ python3 -m memk.cli --db $DB propose --role assistant \
    --content "I infer the user prefers Tuesday releases." --apply
{
  "candidate": { "authority": "inference", "confidence": 0.35, ... },
  "decision": {
    "action": "keep",
    "reason": "assistant_only_evidence",
    "risk": "high",
    "evidence_roles": ["assistant"]
  }
}
$ echo $?
1

$ python3 -m memk.cli --db $DB propose --role user \
    --content "Confirmed: backups are daily incremental, weekly full, thirty retained." \
    --target project --apply
```

Only `propose --apply` with non-assistant evidence (or `--operator`) promotes the
candidate; everything else stays a candidate with a reason code.

Read the result, then check the store's health:

```bash
python3 -m memk.cli --db $DB ask "when is the release window"   # answer-shaped read
python3 -m memk.cli --db $DB packet "deployment"                # the raw payload
python3 -m memk.cli --db $DB explain "deployment"               # why ranking did what it did
python3 -m memk.cli --db $DB doctor                             # read-only health check
```

## How it works

```
   injection     view render      ← projection of the store, pinned-aware, budgeted
   ─────────────────────────────────────────────────────────────────────────
   retrieval     lexical + ngram + vector, fused          ← scope & lifecycle in SQL
   ─────────────────────────────────────────────────────────────────────────
   truth         memories / evidence / candidates / journal      (SQLite)
   ─────────────────────────────────────────────────────────────────────────
   capture       journal: chunked, role-tagged, hashed
```

Four properties hold across all four layers:

- **The store is the only truth.** The injected view is rendered from it and
  records what it rendered, so a difference between the two is attributable
  rather than mysterious.
- **Nothing is deleted automatically.** Supersession marks history; archival is an
  explicit operator act.
- **Ties fail closed.** Missing tables, missing evidence, unreadable provenance,
  assistant-only provenance and unknown roles all deny promotion.
- **Boundaries are enforced in SQL.** Scope and lifecycle filters run before
  ranking, not after.

Freshness states and their ranking costs:

| State | Penalty | Meaning |
|---|---|---|
| absent | none | default; the safest state |
| `current` | none | confirmed, or the user decided it |
| `needs_check` | small | awaiting verification; **never bulk-applied** |
| `historical` | large | superseded, still recallable for audit |

Full reasoning, layer by layer, is in [`DESIGN.md`](DESIGN.md).

## CLI reference

Every subcommand is usable without an agent host, which is the point: the kernel
has to be inspectable and drivable by a human before anything is allowed to
depend on it.

**Write path** — nothing reaches the store except through one of these.

| Command | What it does |
|---|---|
| `init` | create the database |
| `capture` | store a raw turn in the journal, no extraction |
| `propose` | capture a turn, propose a memory, run the gate |
| `store` | write a memory directly, with operator authority |
| `turn` | one host turn: capture → extract → gate → offer a packet |
| `supersede` | replace a memory, keeping history |

**Read path**

| Command | What it does |
|---|---|
| `recall` | search memory |
| `ask` | answer-shaped read: recall and render the packet |
| `packet` | render the packet for a query |
| `explain` | why a query returned what it did |
| `inspect` | show a memory with its verbatim evidence |

**Inspection** — all read-only.

| Command | What it does |
|---|---|
| `doctor` | health check |
| `stats` | counts across layers |
| `audit` | evidence audit |
| `view` | inspect or write the injection view |
| `lex` | show how text is indexed |
| `timeline` | show the intent parsed from a question |
| `freshness` | show the states and their penalties |
| `selftest` | run the built-in self test |

**Maintenance**

| Command | What it does |
|---|---|
| `promote` | re-run the gate over candidates |
| `tune` | heat recalled memory, cool unused memory |
| `compact` | relieve injection-view pressure |
| `dedupe` | find and archive duplicate rows |
| `backup` | `create` · `list` · `verify` · `restore` · `prune` · `status` |
| `golden` | `run` · `example` |

**Conventions**

- Scope flags `--user`, `--agent`, `--platform`, `--workspace` apply to any
  command that reads or writes.
- Mutation commands default to a **dry run**: `propose`, `promote`, `tune`,
  `compact` and `dedupe` write nothing until you pass `--apply`. `view` writes
  only with `--write`.
- The semantic channel is opt-in per command via `--embedder` (see below).

## Python API

The seam a host talks to is `memk.open_memory`, which returns a `Memory` bound to
one database and one reader scope.

```python
from memk import open_memory

with open_memory("memory.sqlite3", user="alice") as mem:
    result = mem.ingest(
        session_id="s1", turn=1, role="user",
        content="Deploys are fixed to Friday afternoon, rollback window two hours.",
    )
    print(result.admitted, result.decision["reason"])
    # True human_authored_with_provenance

    mem.remember(
        "Credentials are never written to memory; store a pointer instead.",
        target="memory", pinned=True,
    )

    print(mem.packet("Friday afternoon", limit=3, budget_chars=1200).render())
```

The host hooks are deliberately small and named after what the host knows. None
of them can write a durable memory without passing the gate, which is the property
that makes it safe to wire into a host's main loop:

```python
mem.on_turn_start(session_id="s1", turn=2, role="user",
                  content="...", query="what changed this week")
mem.on_pre_compress(session_id="s1", turn=3, role="user", content="...")  # stage, do not extract
mem.on_session_end(session_id="s1")                                       # backfill vectors
```

Useful attributes and methods:

| Member | Purpose |
|---|---|
| `ingest(...)` | capture a turn and run extraction + the gate |
| `remember(...)` | an explicit operator write, authoritative by construction |
| `recall(query)` / `packet(query)` / `explain(query)` | ranked hits, rendered packet, ranking breakdown |
| `inspect(memory_id)` | the memory plus its verbatim, hash-verified quotes |
| `supersede(new_id=, old_id=)` / `forget(memory_id)` | explicit lifecycle acts |
| `health()` | the same report `doctor` prints |

Lower-level modules (`journal`, `admission`, `recall`, `packet`, `store`) are
importable directly when you want to own the sequence yourself; see the module
docstring in [`memk/__init__.py`](memk/__init__.py) for the manual pipeline.

## The optional semantic channel

The channel is **off unless asked for**, and asking is a per-command decision.
The whole difference is measurable:

```bash
$ python3 -m memk.cli --db $DB recall "每周五部署"
# semantic channel absent: not_configured
  0.01966 [gram] 部署流水线每周五执行，采用蓝绿切换。

$ python3 -m memk.cli --db $DB embed reindex --embedder test --embedder-dimensions 64
{ "embedder": "test-double:char-ngram:64", "dimensions": 64, "indexed": 2, ... }

$ python3 -m memk.cli --db $DB recall "每周五部署" --embedder test --embedder-dimensions 64
  0.03724 [gram,vector] 部署流水线每周五执行，采用蓝绿切换。
  0.01787 [vector] Credentials are never written to memory; store a pointer instead.
```

Three rules make it safe to have an optional channel at all:

1. An unavailable embedder means the channel is **absent**, never substituted —
   `resolve()` returns `None` plus a reason, and the CLI prints that reason.
2. Every stored vector records the embedder that produced it, and a search only
   reads vectors from the embedder in use — so two models never mix.
3. The channel that produced a hit is **named in the result** (`[gram,vector]`),
   so a suspiciously good match can be traced to its source.

Implementations: an OpenAI-compatible endpoint (standard library only), a local
`sentence-transformers` model loaded offline, and a deterministic test double that
announces itself as a test double in its own name.

## Operations

The ops layer lives **inside the package**, not in a folder of scripts beside it,
because the kernel owns its schema — a script that guesses at the schema is bug
waiting for a schema change.

```bash
# snapshot, then prove it by restoring it
memk backup create
memk backup verify <snapshot>          # integrity_check + row counts vs the manifest
memk backup restore <snapshot> --to /tmp/restored.sqlite3
memk backup status                     # window contents, with "copies is not days" stated

memk dedupe                            # report
memk dedupe --apply                    # archive the automatic losers (never deletes)

memk compact                           # plan
memk compact --apply                   # snapshot first, then apply

memk golden run --file cases.json      # hit@1/3/10, MRR, miss diagnosis, forbidden hits
```

Two behaviours are the difference between a useful ops layer and a dangerous one:

- **verification restores.** `backup verify` opens the snapshot, runs
  `PRAGMA integrity_check`, and compares the checksum and row counts against the
  manifest written beside it. A corrupted snapshot is refused, and the same
  refusal blocks a restore from clobbering a live store.
- **compaction fails closed.** If shortening a row would drop an anchor — a URL, a
  path, a version, a date, an environment variable name, a long id — the row is
  left exactly as it was, and the refusal names the anchors it would have lost.

Observed on a live run:

```
backup    ok=true  integrity=ok  counts={'memories': 3, ...}  problems=[]
dedupe    live=3   auto_mergeable=1  needs_review=0
compact   view 29 chars / budget 5600 · planned_shorten=1 · refused_anchor_loss=0
golden    cases=4  hit@1=3  hit@3=3  MRR=0.75  misses=1 (no_candidates)  forbidden=0
restore   restored=true  source_rows=3  restored_rows=3  consistent=true
```

Schedules, cron wiring, output conventions and the restore drill are in
[`RUNBOOK.md`](RUNBOOK.md).

## Verification

The project's claim is that each failure mode below was **observed running**
before it was designed out — and every one is pinned by a named check, so the
answer is reproducible rather than asserted.

```bash
python3 -m memk.selftest
```

| # | Failure mode | Pinned by |
|---|---|---|
| 1 | CJK lexical recall collapsed whole phrases into one token, so common two-character words matched nothing | `recall: two-character Chinese query matches (lexical, no embedder)` |
| 2 | A missing embedding key silently downgraded semantic recall to string matching | `embed: absent by default, and nothing is substituted for it` |
| 3 | The injected file and the store were written independently and drifted apart | `view: budget respected, pinned survives, hand edit adopted` |
| 4 | An extracted fact carried a journal link but no verifiable excerpt | `journal: chunking, reconstruction, tamper detection` |
| 5 | An assistant's inference could rank as a user-confirmed fact | `audit: no promoted memory lacks human or tool evidence` |
| 6 | Bulk freshness marking pushed many rows down together and degraded recall | `freshness: bulk change refused, supersession keeps history` |
| 7 | A stalled lock could freeze the caller | enforced in `schema.connect`; no dedicated check |
| 8 | WAL was armed on a build with the reset defect | `schema: vulnerable WAL builds are gated, fixed ones are not` |

The full answer for each — what was actually wrong and what was built instead — is
in [`DESIGN.md`](DESIGN.md).

<details>
<summary>All 27 self-test checks</summary>

```
schema: version and tables
schema: vulnerable WAL builds are gated, fixed ones are not
schema: additive change keeps existing data
journal: chunking, reconstruction, tamper detection
admission: assistant-only evidence is refused
admission: user turn is admitted with evidence
recall: two-character Chinese query matches (lexical, no embedder)
recall: english query uses the word index
recall: candidates are hidden until promoted
scope: another user's memory is not readable
freshness: bulk change refused, supersession keeps history
freshness: states, penalties and recallability agree
packet: structure, intent and handling rule
timeline: range parsed, chronology gated by explicit wording
view: budget respected, pinned survives, hand edit adopted
index: lexer produces grams and words for mixed text
audit: no promoted memory lacks human or tool evidence
tuning: recalled memory heats up, unused memory cools down
capture: identical turn is not duplicated
embed: absent by default, and nothing is substituted for it
embed: a configured channel names itself and joins the fusion
embed: vectors from another embedder are never mixed in
session: one turn end to end, hooks included
backup: a snapshot is verified by restoring it, not by existing
dedupe: losers are archived, pinned rows never lose, prefix pairs only reported
compact: anchors are preserved, or the row is refused
golden: hit rates, MRR and the negative half of the test
```

</details>

## Project layout

```
memk/
  schema.py      one connection factory: journal-mode gate, busy timeout, schema.
                 Two lexical indexes: words and CJK n-grams.
  journal.py     chunked capture with per-turn hashes; reconstruction that
                 verifies before it claims completeness.
  store.py       the truth store: memory CRUD, evidence, lifecycle, freshness,
                 index maintenance in the same transaction.
  admission.py   candidate extraction, the fail-closed evidence gate, the
                 promotion sweep, the evidence audit.
  recall.py      three channels (word, n-gram, timeline), rank fusion, freshness
                 and authority corrections, reweighting.
  timeline.py    deterministic Chinese time-range parsing, with range and
                 chronology kept as separate signals.
  packet.py      the structured payload handed to a model, including the
                 handling rule.
  view.py        the injection projection: budget, pinned exemption, hand-edit
                 detection, render-set tracking.
  scope.py       scope composition, including the agent level that reads see and
                 writes never target implicitly.
  freshness.py   four states, their ranking costs, the supersession plan, and the
                 bulk-change refusal.
  embed.py       the optional embedding channel: a self-declaring interface, an
                 OpenAI-compatible client, a local offline model, and a test
                 double that announces itself.
  vectors.py     the vector companion in SQLite: packed float32, provenance per
                 vector, no mixing across models.
  session.py     the seam a host talks to: ingest, hooks, packet, health.
  backup.py      consistent snapshots, a rolling window plus monthly permanents,
                 and verification that restores rather than checks for a file.
  dedupe.py      exact / substring / prefix-similar detection. The first two
                 resolve themselves; the third is reported and never resolved.
  compact.py     view pressure relief: demote what was never used, shorten what
                 is long, and refuse to shorten a row whose anchors would be lost.
  golden.py      the golden recall set: hit@k, MRR, miss diagnosis, and
                 forbidden-id violations.
  lexer.py       the text splitter behind both lexical indexes.
  selftest.py    the 27 checks above.
  cli.py         everything above, drivable by a human. Mutation commands
                 default to a dry run.
DESIGN.md        why the design is the way it is.
RUNBOOK.md       schedules, output conventions, the restore drill, and what to do
                 when the doctor complains.
ROADMAP.md       what is done, what is next, and what is known to be missing.
```

## Scope and known gaps

**Deliberately out of scope:**

- no vector dependency is bundled — vectors live in SQLite and the channel is
  optional, so lexical recall has to be good on its own terms first;
- no account system, HTTP surface or remote API;
- no agent-framework coupling: the kernel is importable and CLI-driven, and host
  adapters belong outside the core;
- no auto-deletion: supersession and archival only.

**Known gaps, stated rather than discovered later:**

- destructive schema changes have no migration path; additive ones apply through
  idempotent DDL, and the self test asserts a reopen keeps every row;
- the render set is recorded **per scope, not per consumer** — two hosts on one
  scope can archive each other's rows, so one host per scope for now;
- the semantic channel is brute force: every query reads all vectors in scope;
- extraction is rule-based (speaker role plus a length floor); a model-driven
  extractor belongs **behind** the gate, not in front of it;
- on Windows without `tzdata`, the timeline path raises (see [Install](#install)).

Forward plans — typed relations, the evaluation work, the cold-start latency
budget — are in [`ROADMAP.md`](ROADMAP.md).

## Documentation

| File | Contents |
|---|---|
| [`DESIGN.md`](DESIGN.md) | Why the design is the way it is: layers, lifecycle, and each failure mode with the answer it produced. |
| [`RUNBOOK.md`](RUNBOOK.md) | Schedules, output conventions, the restore drill, and what to do when the doctor complains. |
| [`ROADMAP.md`](ROADMAP.md) | What is done, what is next, and what is known to be missing. |
| [`LICENSE`](LICENSE) | MIT. |

## Contributing

The house rules are the ones the code already follows, and a change that breaks
one of them is the change that gets pushed back:

1. **No new runtime dependency.** SQLite and the standard library. An optional
   capability plugs in behind an interface and must be able to be absent.
2. **Fail closed.** A check that cannot decide denies. A skipped check is never a
   pass.
3. **No silent substitution.** Every optional channel names itself, in the config
   and in the result.
4. **Nothing is deleted automatically.**
5. **Every fix gets a check.** Add it to `selftest.py` with the failure mode in
   the name: `python3 -m memk.selftest` must be green before a change is done.

## Licence

MIT — see [`LICENSE`](LICENSE). No third-party code is included: the package is
self-contained, and `DESIGN.md` documents the reasoning behind each decision.
