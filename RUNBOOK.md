# Runbook

Operating a `memk` store. Commands assume the database is in `$MEMK_DB` (or pass
`--db`).

## Daily, weekly, monthly

| When | Command | Why that order |
|---|---|---|
| daily, 03:00 | `memk backup create` | first, so any later job has something to fall back to |
| daily, 03:05 | `memk view --write ~/.memk/MEMORY.md` | after the backup; a bad render stays recoverable |
| weekly | `memk tune --apply` | heat what is recalled, cool what is not |
| weekly, after tuning | `memk promote --apply` | so fresh extractions reach the view in the same run |
| weekly | `memk compact --apply` | snapshot first (automatic), then shorten and demote |
| weekly | `memk dedupe --apply` | after compaction, so pairs are judged on final text |
| monthly, 1st | `memk golden run --file cases.json` | the regression gate |
| monthly, 1st | `memk backup prune --keep-daily 30` | keeps monthly snapshots by default |
| on demand | `memk doctor`, `memk audit` | read-only inspection |

Windows note: `backup status` reports **copies**, not days. "30 copies" is only 30
days if the job runs once a day; a render or a test that also snapshots consumes the
window faster. Check the oldest entry's date before trusting the window.

## Cron wiring

The scheduler used here treats the `script` field as a path and does not expand
arguments, so each job is a one-line wrapper. Argument-free wrappers also mean the
schedule is readable in the crontab itself.

```bash
#!/usr/bin/env bash
# ~/.memk/cron/backup.sh
set -euo pipefail
exec python3 -m memk.cli --db "$MEMK_DB" backup create >/dev/null
```

| Job | Wrapper body |
|---|---|
| backup | `memk --db "$MEMK_DB" backup create >/dev/null` |
| view | `memk --db "$MEMK_DB" view --write "$MEMK_VIEW"` |
| tune | `memk --db "$MEMK_DB" tune --apply` |
| promote | `memk --db "$MEMK_DB" promote --apply` |
| compact | `memk --db "$MEMK_DB" compact --apply` |
| dedupe | `memk --db "$MEMK_DB" dedupe --apply` |
| golden | `memk --db "$MEMK_DB" golden run --file "$MEMK_CASES"` |
| prune | `memk --db "$MEMK_DB" backup prune --keep-daily 30` |

## Output conventions

A job that says nothing when there is nothing to say is a job that can be wired to an
alert channel. The contract:

| Outcome | stdout | exit |
|---|---|---|
| nothing to do | empty | 0 |
| something worth reporting | summary | 0 |
| a check failed (doctor, backup verify, golden) | the failure | 1 |
| a real error | error on stderr | 1 |

A silent job must never mean "the error was swallowed". `backup create` fails loudly
if the snapshot cannot be written; it never reports success for a file it did not
verify.

## Restore drill

Do this on a schedule, not the first time it is needed.

```bash
latest=$(memk backup list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["path"])')
memk backup verify "$latest"                      # integrity, checksum, row counts
memk backup restore "$latest" --to /tmp/drill.sqlite3
python3 - <<'PY'
import sqlite3
for path in ("/tmp/drill.sqlite3",):
    conn = sqlite3.connect(path)
    print(path, conn.execute("select count(*) from memories").fetchone()[0], "memories")
PY
```

A restore that is refused (`reason=verification_failed`) means the snapshot is bad:
investigate before deleting anything, and take a fresh one from the live store.

## When the doctor complains

| Finding | What it means | What to do |
|---|---|---|
| `lexical index drift` | index rows and memory rows disagree | rows were written outside `store.put_memory`; re-index or restore from a snapshot |
| `n-gram index drift` | same, for the CJK index | as above |
| `injection view over budget` | the view exceeds its character budget (can only happen with pinned rows) | unpin something, or lower the pinned set |
| `promoted rows without non-assistant evidence` | a memory is live without user or tool evidence | `memk audit` to list them, then demote to `candidate` or archive |
| `vector coverage …` | some live rows have no vector for the active embedder | `memk embed reindex` |

## Changing the embedder

Switching models is a re-index, never a mix:

```bash
memk embed status --embedder sentence-transformers      # shows what is stored per model
memk embed reindex --embedder sentence-transformers --embedder-model BAAI/bge-small-zh-v1.5
```

Vectors produced by the previous model stay in the table under their own name and are
simply not read by the new one. Delete them only when you are sure no rollback is
wanted.

## Gaps to keep in mind

- no migration path for **destructive** schema changes; additive ones apply
  themselves, and the self test checks that a reopen keeps every row
- the render set is recorded per scope, not per consumer, so two hosts on one scope
  can archive each other's rows — one host per scope until that changes
- the vector channel reads every vector in scope on every query
