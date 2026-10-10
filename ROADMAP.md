# Roadmap

Ordered by what unblocks what, not by what is interesting. Each item names the
design note it comes from, so the next step is never a guess.

## v0.2 — the embedding channel (done)

The kernel is deliberately lexical-first. This step adds a *second* channel without
letting it become the only one:

- ✅ an `Embedder` interface with one method and one property: `embed(texts)` and a
  `name` that identifies the model and its dimensions, because a silent
  substitution is the failure this project exists to remove;
- ✅ vectors stored in SQLite beside the rows (brute force over a few 10k rows is
  microseconds), no server, no optional heavy dependency in the hot path;
- ✅ a fusion channel with a configurable weight, absent unless requested;
- ✅ offline model loading only: the flags are set before the import and the
  constructor is told not to reach the network;
- ✅ per-vector provenance, so two models can never be mixed in one ranking.

Design basis: embedder selection criteria plus measured CPU latency, so the choice
stays defensible rather than assumed.

## v0.3 — the host adapter (done)

The kernel is usable by a host without the host owning the schema:

- ✅ a small hook surface: turn start (capture + offer a packet), pre-compression
  (stage the turn before context is rewritten), session end (backfill vectors);
- ✅ the adapter cannot write memory directly: it goes through `admission`;
- ✅ no host-specific names in the core, so the same shape can be reused elsewhere.

Still open in this area: the hooks are plain method calls, not a registered plugin
interface. Wiring them into a specific host is the next concrete step.

## v0.4 — the ops layer (done)

The ops layer lives inside the package rather than beside it, because the kernel owns
the schema:

- ✅ backup with a rolling window plus permanent monthly snapshots, and verification
  that restores the snapshot instead of checking that a file exists;
- ✅ compaction of the injection view with anchor validation and fail-closed refusal;
- ✅ dedupe (exact, substring, prefix-similar) with the human-confirm path intact and
  archival instead of deletion;
- ✅ the golden recall set, reporting hit\@k and MRR plus a miss diagnosis and
  forbidden-id violations.

Not carried over: the remote mirror. It belongs to a deployment, not to the kernel;
the snapshot directory is local and its location is the caller's choice. See
`RUNBOOK.md` for the wiring.

Design basis: two rules carry over — backups must be isolated per deployment, and
compaction must never be a silent no-op.

## v0.5 — typed relations (from the graph notes)

- typed edges between memories (supersedes, contradicts, derived-from,
  same-entity), written by the same admission path;
- two-hop expansion in recall, ranked separately from direct hits;
- contradiction detection at admission time, which is where it is cheap.

Design basis: typed edges are what contradiction detection needs; a flat list of
memories cannot express "these two disagree".

## Evaluation work, alongside everything

- golden set with negative samples, near-synonym rewrites, mixed-language
  queries, and cross-scope isolation cases;
- the four-ability score (durability, findability, currency, use) reported as
  numbers with the evidence behind each;
- a cold-start latency budget and a capacity curve at 1k / 10k / 100k rows.

Design basis: measure recall with hit rates and negative samples, not with a
pass/fail count.

## Known gaps, stated plainly

- **No migration story yet.** Schema version is recorded; no upgrade path is
  implemented for *destructive* changes. Additive changes are applied by re-running
  the idempotent DDL, and the self test asserts that an existing store keeps its rows
  when reopened.
- **The extraction step is rule-based.** `propose_from_turn` uses the speaker role
  and a length floor. A model-driven extractor belongs behind the same gate, not
  in front of it.
- **The render set is per scope, not per consumer.** Two hosts reading one scope
  would share a render record and could archive each other's rows. Documented
  here so the adapter work has to answer it.
- **The vector channel is brute force.** Correct and dependency-free at this scale,
  but it reads every vector for the scope on every query. A cap or an index is
  needed before the tens-of-thousands range.
- **Timeline scoring is deliberately weak.** A chronology hit gets a floor, not a
  high score, because a date match is not topical relevance. Revisit only with
  measurements.
