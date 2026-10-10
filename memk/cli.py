"""Command line interface.

Every subcommand is usable without an agent host, which is the point: the kernel
has to be inspectable and drivable by a human before anything is allowed to depend
on it. Mutation commands default to a dry run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import admission, embed, freshness, journal, lexer, packet, recall, scope as scope_mod, store, timeline, vectors, view
from .schema import open_store, schema_version


def _scope(args: argparse.Namespace) -> scope_mod.Scope:
    return scope_mod.Scope(
        platform=args.platform,
        workspace=args.workspace,
        agent=args.agent,
        user=args.user or "",
    )


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _db_path(args: argparse.Namespace) -> Path:
    raw = args.db or os.environ.get("MEMK_DB") or "memk.sqlite3"
    return Path(raw).expanduser()


def _embedder_spec(args: argparse.Namespace) -> dict[str, Any] | None:
    """Build an embedder spec from the CLI, or ``None`` when not requested.

    Returning ``None`` is meaningful: it means the semantic channel is not part of
    this invocation, not that it silently fell back to something else.
    """

    provider = str(getattr(args, "embedder", "") or "").strip()
    if not provider or provider == "none":
        return None
    return {
        "provider": provider,
        "model": getattr(args, "embedder_model", "") or "",
        "base_url": getattr(args, "embedder_base", "") or "",
        "api_key_env": getattr(args, "embedder_key_env", "") or "",
        "dimensions": int(getattr(args, "embedder_dimensions", 0) or 0),
    }


def _resolve_embedder(args: argparse.Namespace):
    spec = _embedder_spec(args)
    if spec is None:
        return None, embed.EmbedderStatus(False, "", 0, "not_configured")
    return embed.resolve(spec)


def _memory(args: argparse.Namespace):
    """Open a session against the same database, scope and embedder."""

    from .session import Memory

    return Memory(
        _db_path(args),
        _scope(args),
        embedder_spec=_embedder_spec(args),
    )


def cmd_init(args: argparse.Namespace) -> int:
    path = _db_path(args)
    conn = open_store(path)
    conn.close()
    print(f"initialised {path} (schema v{schema_version(open_store(path))})")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    result = journal.capture_turn(
        conn,
        scope=_scope(args),
        session_id=args.session,
        turn=args.turn,
        role=args.role,
        content=args.content,
    )
    conn.close()
    if result.skipped:
        print(f"skipped: {result.reason}")
        return 0
    print(
        f"captured {result.chunk_count} chunk(s) role={result.role} "
        f"journal_ids={result.journal_ids}"
    )
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    captured = journal.capture_turn(
        conn,
        scope=_scope(args),
        session_id=args.session,
        turn=args.turn,
        role=args.role,
        content=args.content,
    )
    candidate = admission.propose_from_turn(
        role=args.role, content=args.content, journal_ids=captured.journal_ids, target=args.target
    )
    if candidate is None:
        print("no candidate: content below the extraction floor")
        return 0
    decision = admission.admit(
        conn, scope=_scope(args), candidate=candidate, operator=args.operator, promote=args.apply
    )
    print(json.dumps({"candidate": candidate.as_dict(), "decision": decision.as_dict()},
                     ensure_ascii=False, indent=2))
    conn.close()
    return 0 if decision.admitted or not args.apply else 1


def cmd_store(args: argparse.Namespace) -> int:
    # Writes go through the session so an active embedder also indexes the row; a
    # memory that exists but is invisible to one channel is the drift this avoids.
    memory = _memory(args)
    if args.candidate:
        memory_id = store.put_memory(
            memory.conn,
            scope=memory.scope,
            target=args.target,
            content=args.content,
            authority=args.authority,
            memory_type=args.memory_type,
            source="cli",
            importance=args.importance,
            confidence=args.confidence,
            freshness=args.freshness,
            lifecycle="candidate",
            pinned=args.pinned,
        )
    else:
        memory_id = memory.remember(
            args.content,
            target=args.target,
            authority=args.authority,
            memory_type=args.memory_type,
            importance=args.importance,
            confidence=args.confidence,
            freshness=args.freshness,
            pinned=args.pinned,
        )
    print(memory_id)
    memory.close()
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    embedder, status = _resolve_embedder(args)
    hits = recall.search(
        conn,
        _scope(args),
        args.query,
        limit=args.limit,
        include_candidates=args.candidates,
        embedder=embedder,
    )
    payload = [h.as_dict() for h in hits]
    if args.json:
        _emit({"embedder": status.as_dict(), "hits": payload}, True)
    else:
        if embedder is None:
            print(f"# semantic channel absent: {status.reason}")
        if not hits:
            print(f"no hits for {args.query!r}")
        for h in hits:
            flag = "*" if h.explanation.get("timeline_match") else " "
            print(f"{flag} {h.score:.5f} [{','.join(h.channels)}] {h.content[:70]}")
    conn.close()
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    embedder, _status = _resolve_embedder(args)
    _emit(
        recall.explain(conn, _scope(args), args.query, limit=args.limit, embedder=embedder), True
    )
    conn.close()
    return 0


def cmd_packet(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    embedder, _status = _resolve_embedder(args)
    hits = recall.search(
        conn, _scope(args), args.query, limit=args.limit, embedder=embedder, mark_recalled=False
    )
    print(packet.build(args.query, hits).render())
    conn.close()
    return 0


def cmd_turn(args: argparse.Namespace) -> int:
    """One host turn: capture, extract, run the gate, and offer a packet."""

    memory = _memory(args)
    result = memory.on_turn_start(
        session_id=args.session,
        turn=args.turn,
        role=args.role,
        content=args.content,
        query=args.query or "",
        extract=not args.no_extract,
    )
    if args.json:
        _emit(result, True)
    else:
        ingest = result["ingest"]
        print(
            f"captured={ingest['captured']} chunks={ingest['chunk_count']} "
            f"admitted={ingest['admitted']} reason={ingest['decision'].get('reason', '-')}"
        )
        if "packet" in result:
            print(json.dumps(result["packet"], ensure_ascii=False, indent=2))
    memory.close()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer-shaped read: recall, then render the packet."""

    memory = _memory(args)
    if args.explain:
        _emit(memory.explain(args.query, limit=args.limit), True)
    else:
        print(memory.packet(args.query, limit=args.limit, budget_chars=args.budget).render())
    memory.close()
    return 0


def cmd_embed_status(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    embedder, status = _resolve_embedder(args)
    payload = {
        "status": status.as_dict(),
        "stored": vectors.stats(conn)["by_embedder"],
        "active": embedder.name if embedder is not None else "",
    }
    _emit(payload, True)
    conn.close()
    return 0


def cmd_embed_reindex(args: argparse.Namespace) -> int:
    memory = _memory(args)
    if memory.embedder is None:
        _emit({"skipped": memory.embedder_status.reason}, True)
        memory.close()
        return 1
    _emit(memory.reindex(force=args.force), True)
    memory.close()
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    if args.write:
        result = view.sync(conn, _scope(args), args.write, dry_run=args.dry_run)
        _emit(
            {
                "written": args.write,
                "changed": result.changed,
                "hand_edit_detected": result.hand_edit_detected,
                "adopted": result.adopted,
                "archived": result.archived,
                "fingerprint": result.fingerprint[:16],
                "dry_run": args.dry_run,
            },
            args.json,
        )
    else:
        _emit(view.describe(conn, _scope(args)), args.json)
    conn.close()
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    _emit(admission.promote_candidates(conn, _scope(args), apply=args.apply), args.json)
    conn.close()
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    _emit(recall.tune(conn, _scope(args), stale_days=args.stale_days, apply=args.apply), args.json)
    conn.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    _emit(admission.audit(conn, _scope(args)), args.json)
    conn.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    payload = {
        "memory": store.stats(conn, _scope(args)),
        "journal": journal.stats(conn, _scope(args).readable()),
        "view": view.describe(conn, _scope(args)),
    }
    _emit(payload, args.json)
    conn.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    scope = _scope(args)
    problems: list[str] = []

    memories = store.stats(conn, scope)
    view_state = view.describe(conn, scope)
    audit = admission.audit(conn, scope)

    if memories["index"]["word_rows"] != memories["all_rows"]:
        problems.append(
            f"lexical index drift: {memories['all_rows']} rows vs "
            f"{memories['index']['word_rows']} indexed"
        )
    if memories["index"]["ngram_rows"] != memories["all_rows"]:
        problems.append(
            f"n-gram index drift: {memories['all_rows']} rows vs "
            f"{memories['index']['ngram_rows']} indexed"
        )
    if view_state["over_budget"]:
        problems.append(f"view over budget: {view_state['char_count']} > {view_state['budget']}")
    if audit["flagged"]:
        problems.append(f"{len(audit['flagged'])} promoted row(s) lack non-assistant evidence")

    payload = {
        "database": str(_db_path(args)),
        "schema_version": schema_version(conn),
        "embedder": _resolve_embedder(args)[1].as_dict(),
        "vectors": vectors.stats(conn),
        "memory": memories,
        "view": view_state,
        "journal": journal.stats(conn, scope.readable()),
        "audit": {"checked": audit["checked"], "flagged": len(audit["flagged"])},
        "problems": problems,
        "healthy": not problems,
    }
    _emit(payload, args.json)
    conn.close()
    return 1 if problems else 0


def cmd_supersede(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    store.supersede(conn, new_id=args.new, old_id=args.old)
    old = store.get(conn, args.old)
    _emit(old.as_dict() if old else {}, args.json)
    conn.close()
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    conn = open_store(_db_path(args))
    record = store.get(conn, args.memory_id)
    if record is None:
        print(f"no such memory: {args.memory_id}", file=sys.stderr)
        return 2
    evidence = store.evidence_for(conn, args.memory_id)
    quotes = []
    for row in evidence:
        rebuilt = journal.reconstruct(conn, [int(row["journal_id"])])
        quotes.append(
            {
                "journal_id": row["journal_id"],
                "role": row["role"],
                # What the evidence table stored at admission time.
                "stored_excerpt": row["excerpt"][:120],
                # What the journal returns now, rebuilt and hash-verified.
                "reconstruction": rebuilt.as_dict() if rebuilt else None,
                "content": (rebuilt.content[: args.chars] if rebuilt else ""),
            }
        )
    _emit({"memory": record.as_dict(), "quotes": quotes}, True)
    conn.close()
    return 0


def cmd_lex(args: argparse.Namespace) -> int:
    _emit({"text": args.text, **lexer.describe_indexes(args.text)}, True)
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    _emit(timeline.describe(args.query), True)
    return 0


def cmd_show_freshness(args: argparse.Namespace) -> int:
    states = ["", freshness.CURRENT, freshness.NEEDS_CHECK, freshness.HISTORICAL]
    _emit({s or "untracked": freshness.describe(s) for s in states}, True)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from . import selftest

    return selftest.main(["--keep"] if args.keep else [])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memk", description=__doc__)
    parser.add_argument("--db", help="database path (default: $MEMK_DB or ./memk.sqlite3)")
    parser.add_argument("--user", default="", help="user segment of the scope")
    parser.add_argument("--agent", default="default", help="agent segment of the scope")
    parser.add_argument("--platform", default="cli", help="platform segment of the scope")
    parser.add_argument("--workspace", default="default", help="workspace segment of the scope")

    # The semantic channel is opt-in per command. ``none`` is the default, and there
    # is no hidden fallback: an unconfigured command runs lexically.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--embedder",
        default="none",
        choices=["none", "test", "openai", "sentence-transformers"],
        help="semantic channel to use (default: none)",
    )
    common.add_argument("--embedder-model", default="", help="model name for the embedder")
    common.add_argument("--embedder-base", default="", help="base URL for an OpenAI-compatible embedder")
    common.add_argument(
        "--embedder-key-env", default="", help="environment variable holding the embedder API key"
    )
    common.add_argument("--embedder-dimensions", type=int, default=0)

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    capture = sub.add_parser("capture", help="store a raw turn in the journal")
    capture.add_argument("--session", default="cli")
    capture.add_argument("--turn", type=int, default=1)
    capture.add_argument("--role", default="user", choices=["user", "assistant", "tool"])
    capture.add_argument("--content", required=True)
    capture.set_defaults(func=cmd_capture)

    propose = sub.add_parser("propose", help="capture a turn, propose a memory, run the gate")
    propose.add_argument("--session", default="cli")
    propose.add_argument("--turn", type=int, default=1)
    propose.add_argument("--role", default="user", choices=["user", "assistant", "tool"])
    propose.add_argument("--content", required=True)
    propose.add_argument("--target", default="project")
    propose.add_argument("--operator", action="store_true", help="treat as an operator decision")
    propose.add_argument("--apply", action="store_true", help="write it if the gate passes")
    propose.set_defaults(func=cmd_propose)

    put = sub.add_parser(
        "store",
        help="write a memory directly (operator authority)",
        parents=[common],
    )
    put.add_argument("--target", default="memory", choices=list(store.TARGETS))
    put.add_argument("--content", required=True)
    put.add_argument("--authority", default="operator", choices=list(store.AUTHORITIES))
    put.add_argument("--memory-type", default="factual")
    put.add_argument("--importance", type=float, default=0.6)
    put.add_argument("--confidence", type=float, default=0.6)
    put.add_argument("--freshness", default="")
    put.add_argument("--pinned", action="store_true")
    put.add_argument("--candidate", action="store_true", help="store as a candidate, not a memory")
    put.set_defaults(func=cmd_store)

    search = sub.add_parser("recall", help="search memory", parents=[common])
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--candidates", action="store_true", help="include candidates (audit)")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_recall)

    explain = sub.add_parser("explain", help="why a query returned what it did", parents=[common])
    explain.add_argument("query")
    explain.add_argument("--limit", type=int, default=5)
    explain.set_defaults(func=cmd_explain)

    pkt = sub.add_parser("packet", help="render the packet for a query", parents=[common])
    pkt.add_argument("query")
    pkt.add_argument("--limit", type=int, default=5)
    pkt.set_defaults(func=cmd_packet)

    turn = sub.add_parser(
        "turn",
        help="run one host turn: capture, extract, gate, and offer a packet",
        parents=[common],
    )
    turn.add_argument("--session", default="cli")
    turn.add_argument("--turn", type=int, default=1)
    turn.add_argument("--role", default="user", choices=["user", "assistant", "tool"])
    turn.add_argument("--content", required=True)
    turn.add_argument("--query", default="", help="the question to recall for; defaults to content")
    turn.add_argument("--no-extract", action="store_true", help="stage the turn only")
    turn.add_argument("--json", action="store_true")
    turn.set_defaults(func=cmd_turn)

    ask = sub.add_parser(
        "ask",
        help="answer-shaped read: recall and render the packet",
        parents=[common],
    )
    ask.add_argument("query")
    ask.add_argument("--limit", type=int, default=5)
    ask.add_argument("--budget", type=int, default=1600, help="packet character budget")
    ask.add_argument("--explain", action="store_true", help="show the ranking breakdown instead")
    ask.set_defaults(func=cmd_ask)

    embed_group = sub.add_parser("embed", help="semantic channel status and indexing", parents=[common])
    embed_sub = embed_group.add_subparsers(dest="embed_command", required=True)
    embed_sub.add_parser(
        "status", help="what the channel currently is", parents=[common]
    ).set_defaults(
        func=cmd_embed_status
    )
    reindex = embed_sub.add_parser(
        "reindex", help="embed memories missing a vector", parents=[common]
    )
    reindex.add_argument("--force", action="store_true", help="re-embed everything")
    reindex.set_defaults(func=cmd_embed_reindex)

    render = sub.add_parser("view", help="inspect or write the injection view")
    render.add_argument("--write", help="path to write the rendered view to")
    render.add_argument("--dry-run", action="store_true")
    render.add_argument("--json", action="store_true")
    render.set_defaults(func=cmd_view)

    promote = sub.add_parser("promote", help="re-run the gate over candidates")
    promote.add_argument("--apply", action="store_true")
    promote.add_argument("--json", action="store_true")
    promote.set_defaults(func=cmd_promote)

    tune = sub.add_parser("tune", help="heat recalled memory, cool unused memory")
    tune.add_argument("--stale-days", type=int, default=60)
    tune.add_argument("--apply", action="store_true")
    tune.add_argument("--json", action="store_true")
    tune.set_defaults(func=cmd_tune)

    audit = sub.add_parser("audit", help="evidence audit")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)

    stats = sub.add_parser("stats", help="counts across layers")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    doctor = sub.add_parser("doctor", help="read-only health check")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    supersede = sub.add_parser("supersede", help="replace a memory, keeping history")
    supersede.add_argument("--new", required=True)
    supersede.add_argument("--old", required=True)
    supersede.add_argument("--json", action="store_true")
    supersede.set_defaults(func=cmd_supersede)

    inspect = sub.add_parser("inspect", help="show a memory with its verbatim evidence")
    inspect.add_argument("memory_id")
    inspect.add_argument("--chars", type=int, default=400, help="characters of quote to show")
    inspect.set_defaults(func=cmd_inspect)

    lex = sub.add_parser("lex", help="show how text is indexed")
    lex.add_argument("text")
    lex.set_defaults(func=cmd_lex)

    tl = sub.add_parser("timeline", help="show the intent parsed from a question")
    tl.add_argument("query")
    tl.set_defaults(func=cmd_timeline)

    fr = sub.add_parser("freshness", help="show the freshness states and penalties")
    fr.set_defaults(func=cmd_show_freshness)

    selftest = sub.add_parser("selftest", help="run the built-in self test")
    selftest.add_argument("--keep", action="store_true")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
