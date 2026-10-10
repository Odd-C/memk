"""Self test: exercises the real paths and reports evidence.

This is a program, not a mock: it opens a database, writes memory, runs retrieval,
and asserts the properties the kernel claims. Each check reports the value it
observed, so a failure is diagnosable without a debugger.

Run: ``python -m memk.selftest``          (temporary database, removed afterwards)
     ``python -m memk.selftest --keep``   (leaves the database for inspection)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

from . import (
    admission,
    backup as backup_mod,
    compact as compact_mod,
    dedupe as dedupe_mod,
    embed as embed_mod,
    freshness,
    golden as golden_mod,
    journal,
    lexer,
    packet,
    recall,
    scope as scope_mod,
    session as session_mod,
    store,
    timeline,
    vectors,
    view,
)
from .schema import open_store, schema_version, table_names
from .util import sha256_text

CHECKS: list[tuple[str, Callable[..., dict[str, Any]]]] = []


def check(name: str):
    def decorate(fn: Callable[..., dict[str, Any]]):
        CHECKS.append((name, fn))
        return fn

    return decorate


def _scope(user: str = "alice") -> scope_mod.Scope:
    return scope_mod.Scope(platform="cli", workspace="default", agent="assistant", user=user)


@check("schema: version and tables")
def t_schema(conn, ctx):
    names = table_names(conn)
    expected = {"memories", "journal", "evidence", "meta", "mem_fts", "mem_ngram"}
    return {
        "schema_version": schema_version(conn),
        "missing": sorted(expected - names),
        "present": sorted(expected & names),
    }


@check("journal: chunking, reconstruction, tamper detection")
def t_journal(conn, ctx):
    scope = _scope()
    body = "用户要求整理部署流程。" + ("细节内容" * 700) + "结束时确认周五发布。"
    captured = journal.capture_turn(
        conn, scope=scope, session_id="s-long", turn=1, role="user", content=body
    )
    rebuilt = journal.reconstruct(conn, captured.journal_ids)
    assert rebuilt is not None, "reconstruction returned nothing"
    assert rebuilt.complete, f"expected complete reconstruction: {rebuilt.note}"
    assert rebuilt.content == body, "reconstructed text differs from the original"
    assert captured.chunk_count > 1, "long turn was not chunked"

    # Introduce a single-character corruption in one chunk and re-check.
    target = captured.journal_ids[-1]
    row = conn.execute("SELECT content FROM journal WHERE id=?", (target,)).fetchone()
    conn.execute(
        "UPDATE journal SET content=? WHERE id=?", (str(row["content"]) + "x", target)
    )
    conn.commit()
    tampered = journal.reconstruct(conn, captured.journal_ids)
    assert tampered is not None
    assert not tampered.complete, "tampered content still reported as complete"
    assert "hash mismatch" in tampered.note.lower(), tampered.note
    return {
        "chunks": captured.chunk_count,
        "complete_before_tamper": rebuilt.complete,
        "complete_after_tamper": tampered.complete,
        "note": tampered.note,
    }


@check("admission: assistant-only evidence is refused")
def t_admission_refuse(conn, ctx):
    scope = _scope()
    turn = journal.capture_turn(
        conn, scope=scope, session_id="s-a", turn=1, role="assistant",
        content="我推断用户可能更倾向于在周五发布，这一点尚未得到确认。",
    )
    candidate = admission.propose_from_turn(
        role="assistant", content="用户偏好周五发布（未经确认）", journal_ids=turn.journal_ids
    )
    decision = admission.admit(conn, scope=scope, candidate=candidate)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE content LIKE '%未经确认%'"
    ).fetchone()["n"]
    assert not decision.admitted, f"assistant-only evidence was admitted: {decision.reason}"
    assert decision.reason == "assistant_only_evidence", decision.reason
    assert count == 0, "refused candidate still wrote a memory"
    return {"reason": decision.reason, "risk": decision.risk, "rows_written": int(count)}


@check("admission: user turn is admitted with evidence")
def t_admission_accept(conn, ctx):
    scope = _scope()
    text = "用户拍板：生产发布固定在每周五下午，回滚窗口为两小时。"
    turn = journal.capture_turn(
        conn, scope=scope, session_id="s-b", turn=1, role="user", content=text
    )
    candidate = admission.propose_from_turn(
        role="user", content=text, journal_ids=turn.journal_ids, target="memory"
    )
    decision = admission.admit(conn, scope=scope, candidate=candidate)
    assert decision.admitted, f"user turn was refused: {decision.reason}"
    roles = store.evidence_roles(conn, decision.memory_id)
    evidence = store.evidence_for(conn, decision.memory_id)
    assert roles == {"user"}, roles
    assert evidence and evidence[0]["excerpt"].startswith("用户拍板"), evidence
    return {
        "memory_id": decision.memory_id,
        "reason": decision.reason,
        "evidence_roles": sorted(roles),
        "excerpt_chars": len(evidence[0]["excerpt"]),
    }


@check("recall: two-character Chinese query matches (lexical, no embedder)")
def t_cjk_recall(conn, ctx):
    scope = _scope()
    store.put_memory(
        conn, scope=scope, target="project",
        content="生产环境部署固定在每周五下午进行，回滚窗口两小时。",
        authority="operator", source="operator", importance=0.7,
    )
    store.put_memory(
        conn, scope=scope, target="project",
        content="备份策略为每日增量、每周全量，保留三十份。",
        authority="operator", source="operator", importance=0.6,
    )
    before = int(conn.execute("SELECT COUNT(*) AS n FROM mem_ngram").fetchone()["n"])
    hit = recall.search(conn, scope, "部署", limit=5, mark_recalled=False)
    hit2 = recall.search(conn, scope, "备份策略", limit=5, mark_recalled=False)
    assert hit, "two-character Chinese query returned nothing"
    assert "部署" in hit[0].content, hit[0].content
    assert hit2, "two-character Chinese query returned nothing"
    return {
        "ngram_rows": before,
        "query": "部署",
        "top": hit[0].content[:40],
        "channels": hit[0].channels,
        "matched_terms": hit[0].matched_terms[:6],
        "second_query_top": hit2[0].content[:30],
    }


@check("recall: english query uses the word index")
def t_word_recall(conn, ctx):
    scope = _scope()
    store.put_memory(
        conn, scope=scope, target="ops", content="The deployment pipeline runs on GitHub Actions.",
        authority="operator", source="operator", importance=0.6,
    )
    hits = recall.search(conn, scope, "deployment", limit=5, mark_recalled=False)
    assert hits, "english query returned nothing"
    return {"top": hits[0].content[:50], "channels": hits[0].channels}


@check("recall: candidates are hidden until promoted")
def t_candidate_hidden(conn, ctx):
    scope = _scope()
    memory_id = store.put_memory(
        conn, scope=scope, target="project", content="候选提案：也许应该改用周二发布。",
        authority="inference", source="extraction", importance=0.6, lifecycle="candidate",
    )
    default_hits = recall.search(conn, scope, "周二发布", limit=5, mark_recalled=False)
    audit_hits = recall.search(
        conn, scope, "周二发布", limit=5, mark_recalled=False, include_candidates=True
    )
    assert memory_id not in [h.memory_id for h in default_hits], "candidate leaked into recall"
    assert memory_id in [h.memory_id for h in audit_hits], "candidate invisible to audit view"
    return {
        "hidden_by_default": memory_id not in [h.memory_id for h in default_hits],
        "visible_to_audit": memory_id in [h.memory_id for h in audit_hits],
    }


@check("scope: another user's memory is not readable")
def t_scope_isolation(conn, ctx):
    alice = _scope("alice")
    bob = _scope("bob")
    store.put_memory(
        conn, scope=bob, target="memory", content="Bob 的私有内容：不接受周五发布。",
        authority="user", source="operator", importance=0.8,
    )
    alice_hits = recall.search(conn, alice, "私有内容", limit=5, mark_recalled=False)
    bob_hits = recall.search(conn, bob, "私有内容", limit=5, mark_recalled=False)
    assert not alice_hits, "cross-scope leak: alice saw bob's memory"
    assert bob_hits, "bob could not read his own memory"
    return {
        "alice_sees": len(alice_hits),
        "bob_sees": len(bob_hits),
        "alice_readable_scopes": alice.readable(),
    }


@check("freshness: bulk change refused, supersession keeps history")
def t_freshness(conn, ctx):
    scope = _scope()
    old_id = store.put_memory(
        conn, scope=scope, target="memory", content="发布窗口是每周四。",
        authority="user", source="operator", importance=0.6, freshness="current",
    )
    other_id = store.put_memory(
        conn, scope=scope, target="memory", content="回滚窗口是一小时。",
        authority="user", source="operator", importance=0.6, freshness="current",
    )
    refused = False
    try:
        store.set_freshness(conn, [old_id, other_id], freshness.NEEDS_CHECK)
    except freshness.FreshnessError as exc:
        refused = True
        message = str(exc)
    assert refused, "bulk freshness change was allowed"

    new_id = store.put_memory(
        conn, scope=scope, target="memory", content="发布窗口改为每周五。",
        authority="user", source="operator", importance=0.6, freshness="current",
    )
    store.supersede(conn, new_id=new_id, old_id=old_id)
    old = store.get(conn, old_id)
    assert old is not None and old.freshness == freshness.HISTORICAL, old
    assert old.superseded_by == new_id, old.superseded_by

    hits = recall.search(conn, scope, "发布窗口", limit=5, mark_recalled=False)
    ids = [h.memory_id for h in hits]
    assert new_id in ids, "new version not recallable"
    assert old_id in ids, "superseded version was dropped instead of ranked down"
    assert ids.index(new_id) < ids.index(old_id), f"new version should rank first: {ids}"
    old_hit = next(h for h in hits if h.memory_id == old_id)
    return {
        "bulk_refused": refused,
        "bulk_message": message[:70],
        "old_freshness": old.freshness,
        "pointer_set": old.superseded_by == new_id,
        "penalty_applied": old_hit.explanation["freshness_penalty"],
        "order": ["new", "old"] if ids.index(new_id) < ids.index(old_id) else ["old", "new"],
    }


@check("packet: structure, intent and handling rule")
def t_packet(conn, ctx):
    scope = _scope()
    store.put_memory(
        conn, scope=scope, target="memory",
        content="用户偏好结构化回答，先给结论再给依据。",
        authority="user", source="operator", importance=0.8, memory_type="preference",
    )
    built = packet.build_from_search(conn, scope, "用户偏好什么样的回答", limit=5)
    payload = built.as_dict()
    rendered = built.render()
    assert payload["packet_version"] == 1
    assert payload["items"], "packet had no items"
    item = payload["items"][0]
    for field in ("id", "authority", "lifecycle", "freshness", "retrieval_confidence"):
        assert field in item, f"packet item missing {field}"
    assert "not user input" in rendered and "inspect" in rendered
    assert json.dumps(payload, ensure_ascii=False)

    current = packet.build_from_search(conn, scope, "现在的发布窗口是什么", limit=5)
    return {
        "intent": payload["intent"],
        "item_count": payload["item_count"],
        "item_authority": item["authority"],
        "requires_live_check_current_query": current.as_dict()["requires_live_check"],
        "rendered_chars": len(rendered),
    }


@check("timeline: range parsed, chronology gated by explicit wording")
def t_timeline(conn, ctx):
    explicit = timeline.parse("2026年6月以后我们做了什么")
    implicit = timeline.parse("最近的部署状态")
    window = timeline.parse("最近三个月做了什么")
    assert explicit is not None and explicit.kind == "month_from" and explicit.explicit
    # A recency phrase with no range and no chronology wording must not open the
    # date-bounded path at all; either no intent or a non-explicit one is correct.
    assert implicit is None or not implicit.explicit, "recency question opened a chronology"
    assert window is not None and window.kind == "recent_window" and window.explicit

    scope = _scope()
    store.put_memory(
        conn, scope=scope, target="project", content="六月完成迁移到新存储。",
        authority="operator", source="operator", importance=0.5, memory_type="episodic",
    )
    store.put_memory(
        conn, scope=scope, target="project", content="七月上线了告警规则。",
        authority="operator", source="operator", importance=0.5, memory_type="episodic",
    )
    hits = recall.search(conn, scope, "2026年6月以后我们做了什么", limit=5, mark_recalled=False)
    timeline_hits = [h for h in hits if h.explanation.get("timeline_match")]
    assert timeline_hits, "explicit chronology request returned no timeline rows"
    return {
        "explicit_kind": explicit.kind,
        "implicit_parsed": implicit is not None,
        "implicit_explicit_flag": bool(implicit.explicit) if implicit else False,
        "window_kind": window.kind,
        "timeline_rows": len(timeline_hits),
        "no_topic_overlap_needed": timeline_hits[0].coverage,
    }


@check("view: budget respected, pinned survives, hand edit adopted")
def t_view(conn, ctx):
    scope = _scope("viewer")
    for index in range(40):
        store.put_memory(
            conn, scope=scope, target="memory",
            content=f"常规事实 {index}：" + ("描述内容" * 30),
            authority="operator", source="operator", importance=0.5,
        )
    pinned_id = store.put_memory(
        conn, scope=scope, target="memory", content="安全红线：凭据绝不写入记忆。",
        authority="user", source="operator", importance=0.1, pinned=True,
    )
    # The profile target belongs in the view too: a view that rendered only
    # target=memory would drop the profile from every prompt.
    profile_id = store.put_memory(
        conn, scope=scope, target="user", content="画像示例条目：用于验证画像目标会被注入。",
        authority="user", source="operator", importance=0.4,
    )
    # A project-scoped row is recall-only by design and must not be injected.
    project_id = store.put_memory(
        conn, scope=scope, target="project", content="项目细节：本行不应进入注入视图。",
        authority="operator", source="operator", importance=0.9,
    )
    rendered = view.render(conn, scope, budget=1500)
    assert rendered.char_count <= 1500 + 200, rendered.char_count
    assert pinned_id in rendered.rendered_ids, "pinned row was cut by the budget"
    assert profile_id in rendered.rendered_ids, "the profile target was not injected"
    assert project_id not in rendered.rendered_ids, "a recall-only target was injected"
    assert rendered.skipped_ids, "expected some rows to be skipped at this budget"

    path = Path(ctx["dir"]) / "MEMORY.md"
    first = view.sync(conn, scope, path)
    assert path.exists() and "安全红线" in path.read_text(encoding="utf-8")

    # A hand edit must be detected and adopted rather than overwritten.
    path.write_text(path.read_text(encoding="utf-8") + "\n手工补充的事实：每周五发布。\n", encoding="utf-8")
    second = view.sync(conn, scope, path)
    adopted_rows = [r for r in store.recent(conn, scope, 50) if "手工补充" in r.content]
    assert second.hand_edit_detected, "hand edit was not detected"
    assert adopted_rows, "hand edit was not written back into the store"
    assert adopted_rows[0].authority == "operator", adopted_rows[0].authority
    return {
        "char_count": rendered.char_count,
        "rendered": len(rendered.rendered_ids),
        "skipped": len(rendered.skipped_ids),
        "pinned_kept": pinned_id in rendered.rendered_ids,
        "profile_injected": profile_id in rendered.rendered_ids,
        "project_excluded": project_id not in rendered.rendered_ids,
        "view_targets": list(view.VIEW_TARGETS),
        "hand_edit_detected": second.hand_edit_detected,
        "adopted": len(adopted_rows),
        "fingerprint": first.fingerprint[:12],
    }


@check("index: lexer produces grams and words for mixed text")
def t_lexer(conn, ctx):
    info = lexer.describe_indexes("部署 deploy 周五 Friday")
    assert info["has_cjk"], info
    # Two separate CJK runs of two characters each produce exactly two grams.
    assert info["ngram_count"] >= 2, info
    assert "deploy" in info["word_tokens"], info
    coverage, matched = lexer.gram_coverage("部署流程", "生产环境部署流程已固定")
    assert coverage > 0 and matched, (coverage, matched)
    return {"has_cjk": info["has_cjk"], "ngram_count": info["ngram_count"],
            "word_tokens": info["word_tokens"], "coverage": round(coverage, 3)}


@check("audit: no promoted memory lacks human or tool evidence")
def t_audit(conn, ctx):
    scope = _scope()
    # Plant one unbacked inference: the audit must find it.
    planted = store.put_memory(
        conn, scope=scope, target="project",
        content="未经验证的推断：也许应该改用周二发布。",
        authority="inference", source="extraction", importance=0.5, lifecycle="promoted",
    )
    report = admission.audit(conn, scope)
    stats = store.stats(conn, scope)
    flagged_ids = {row["id"] for row in report["flagged"]}
    assert planted in flagged_ids, f"audit missed an unbacked inference: {report['flagged']}"
    assert flagged_ids == {planted}, f"audit flagged legitimate rows: {flagged_ids}"
    assert json.dumps(report) and json.dumps(stats)
    return {
        "checked": report["checked"],
        "authority_counts": report["authority_counts"],
        "flagged": sorted(flagged_ids),
        "planted_found": planted in flagged_ids,
        "rows": stats["rows"],
        "index": stats["index"],
    }


@check("tuning: recalled memory heats up, unused memory cools down")
def t_tune(conn, ctx):
    scope = _scope("tuner")
    hot = store.put_memory(
        conn, scope=scope, target="memory", content="常被问到的部署窗口。",
        authority="user", source="operator", importance=0.5,
    )
    cold = store.put_memory(
        conn, scope=scope, target="memory", content="很久没被问到的旧流程。",
        authority="user", source="operator", importance=0.5,
    )
    conn.execute("UPDATE memories SET last_recalled=? WHERE id=?", (__import__("time").time() - 3600, hot))
    conn.execute("UPDATE memories SET last_recalled=? WHERE id=?", (__import__("time").time() - 90 * 86400, cold))
    conn.commit()
    preview = recall.tune(conn, scope)
    assert any(row["id"] == hot for row in preview["heated"]), preview
    assert any(row["id"] == cold for row in preview["cooled"]), preview
    recall.tune(conn, scope, apply=True)
    after_hot = store.get(conn, hot).importance
    after_cold = store.get(conn, cold).importance
    assert after_hot > 0.5 and after_cold < 0.5, (after_hot, after_cold)
    return {"hot": after_hot, "cold": after_cold, "applied": preview["applied"]}


@check("capture: identical turn is not duplicated")
def t_idempotent(conn, ctx):
    scope = _scope("idem")
    first = journal.capture_turn(
        conn, scope=scope, session_id="s-idem", turn=7, role="user", content="同一条内容，重复采集。"
    )
    second = journal.capture_turn(
        conn, scope=scope, session_id="s-idem", turn=7, role="user", content="同一条内容，重复采集。"
    )
    assert first.journal_ids == second.journal_ids, (first.journal_ids, second.journal_ids)
    total = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
    return {"ids": first.journal_ids, "total_journal_rows": int(total)}


@check("freshness: states, penalties and recallability agree")
def t_freshness_states(conn, ctx):
    table = {
        "": ("untracked", 0.0, True),
        "current": ("current", 0.0, True),
        "historical": ("historical", 0.35, True),
        "needs_check": ("needs_check", 0.18, True),
        "archived": ("archived", 0.0, False),
    }
    observed = {}
    for raw, (state, penalty, recallable) in table.items():
        described = freshness.describe(raw)
        assert described["state"] == state, (raw, described)
        assert abs(described["penalty"] - penalty) < 1e-9, (raw, described)
        assert described["recallable"] is recallable, (raw, described)
        observed[raw or "untracked"] = described
    return observed


@check("schema: vulnerable WAL builds are gated, fixed ones are not")
def t_wal_gate(conn, ctx):
    from .schema import wal_is_vulnerable

    cases = [
        ((3, 43, 0), False, "before the WAL reset rework"),
        ((3, 50, 4), True, "shipped in our venv, inside an unfixed range"),
        ((3, 50, 7), False, "point fix"),
        ((3, 51, 3), False, "later fix"),
        ((3, 52, 0), False, "after all fixes"),
    ]
    observed = {}
    for version, expected, why in cases:
        actual = wal_is_vulnerable(version)
        assert actual is expected, f"{version}: expected {expected} ({why}), got {actual}"
        observed[".".join(map(str, version))] = {"vulnerable": actual, "why": why}
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return {"cases": observed, "journal_mode_in_effect": str(mode)}


@check("embed: absent by default, and nothing is substituted for it")
def t_embed_absent(conn, ctx):
    embedder, status = embed_mod.resolve(None)
    assert embedder is None, "an embedder appeared without being configured"
    assert status.reason == "not_configured", status.reason

    # Same for a spec that names a provider but supplies no credential: the channel
    # must go missing, not fall back to something weaker.
    missing, missing_status = embed_mod.resolve(
        {"provider": "openai", "model": "text-embedding-3-small", "api_key_env": "MEMK_TEST_ABSENT_KEY"}
    )
    assert missing is None, "a credentialed embedder appeared without a credential"
    assert missing_status.reason == "missing_api_key", missing_status.reason

    unknown, unknown_status = embed_mod.resolve({"provider": "nonesuch"})
    assert unknown is None and unknown_status.reason.startswith("unknown_provider"), unknown_status

    scope = _scope("no-embedder")
    store.put_memory(
        conn, scope=scope, target="memory", content="没有嵌入通道时，词法通道必须自己站得住。",
        authority="operator", source="operator", importance=0.6,
    )
    hits = recall.search(conn, scope, "词法通道", limit=5, mark_recalled=False)
    assert hits, "lexical recall failed with no embedder configured"
    assert "vector" not in hits[0].channels, hits[0].channels
    return {
        "default_reason": status.reason,
        "missing_key_reason": missing_status.reason,
        "unknown_provider_reason": unknown_status.reason,
        "lexical_hits_without_embedder": len(hits),
        "channels": hits[0].channels,
    }


@check("embed: a configured channel names itself and joins the fusion")
def t_embed_present(conn, ctx):
    memory = session_mod.Memory(
        ctx["db"], _scope("vector-user"), embedder_spec={"provider": "test", "dimensions": 64}
    )
    try:
        assert memory.embedder is not None, memory.embedder_status.as_dict()
        memory_id = memory.remember("部署窗口固定在每周五下午，回滚窗口两小时。", target="project")
        stored = vectors.stored(memory.conn, memory_id)
        assert stored is not None, "no vector was written for an embedder-enabled store"
        name, vector = stored
        assert name == memory.embedder.name and len(vector) == 64, (name, len(vector))

        hits = memory.recall("每周五部署", limit=5)
        assert hits, "no hits with the semantic channel enabled"
        top = hits[0]
        assert "vector" in top.channels, top.channels
        assert top.explanation["embedder"] == memory.embedder.name, top.explanation
        assert top.explanation["vector_score"] > 0.0, top.explanation
        stats = vectors.stats(memory.conn)
        return {
            "embedder": memory.embedder.name,
            "stored_dimensions": len(vector),
            "top_channels": top.channels,
            "vector_score": top.explanation["vector_score"],
            "coverage": round(stats["coverage"], 3),
        }
    finally:
        memory.close()


@check("embed: vectors from another embedder are never mixed in")
def t_embed_isolation(conn, ctx):
    scope = _scope("mixed-embedder")
    memory_id = store.put_memory(
        conn, scope=scope, target="memory", content="这条记忆只由另一个模型建过向量。",
        authority="operator", source="operator", importance=0.6,
    )
    other = embed_mod.DeterministicTestEmbedder(dimensions=32)
    vectors.put_vector(
        conn, memory_id=memory_id, embedder_name="other-model:32",
        vector=other.embed(["这条记忆只由另一个模型建过向量。"])[0],
    )
    mine = embed_mod.DeterministicTestEmbedder(dimensions=32)
    found = vectors.search(conn, scope, mine, "另一个模型", pool=10)
    hits_with_mine = [mid for mid, _ in found if mid == memory_id]
    # Now search as the embedder that actually produced the vector.
    class NamedOther(embed_mod.DeterministicTestEmbedder):
        def __init__(self) -> None:
            super().__init__(dimensions=32)
            self.name = "other-model:32"

    found_other = vectors.search(conn, scope, NamedOther(), "另一个模型", pool=10)
    hits_with_other = [mid for mid, _ in found_other if mid == memory_id]
    assert not hits_with_mine, "a foreign embedder's vector participated in ranking"
    assert hits_with_other, "the producing embedder could not see its own vector"
    return {
        "foreign_embedder_matches": len(hits_with_mine),
        "producing_embedder_matches": len(hits_with_other),
        "stored": sorted(vectors.stats(conn)["by_embedder"]),
    }


@check("session: one turn end to end, hooks included")
def t_session(conn, ctx):
    memory = session_mod.Memory(ctx["db"], _scope("session-user"))
    try:
        first = memory.on_turn_start(
            session_id="sess-1",
            turn=1,
            role="user",
            content="用户拍板：发布固定在周五下午，回滚窗口两小时。",
            query="发布窗口是什么",
        )
        assert first["ingest"]["captured"], first
        assert first["ingest"]["admitted"], first["ingest"]["decision"]
        assert first["packet"]["item_count"] >= 1, first["packet"]

        staged = memory.on_pre_compress(
            session_id="sess-1", turn=2, role="assistant", content="准备压缩前的原始措辞。"
        )
        assert staged["staged"] and staged["journal_ids"], staged

        closed = memory.on_session_end(session_id="sess-1")
        assert closed["index"].get("skipped") == "not_configured", closed

        health = memory.health()
        assert health["healthy"], health["problems"]
        return {
            "admitted": first["ingest"]["admitted"],
            "reason": first["ingest"]["decision"]["reason"],
            "packet_items": first["packet"]["item_count"],
            "staged_chunks": staged["chunk_count"],
            "session_end": closed["index"],
            "healthy": health["healthy"],
            "schema_version": health["schema_version"],
        }
    finally:
        memory.close()


@check("schema: additive change keeps existing data")
def t_schema_additive(conn, ctx):
    from .schema import open_store as reopen

    before = store.stats(conn, _scope())["rows"]
    again = reopen(ctx["db"])
    try:
        after = store.stats(again, _scope())["rows"]
        version = schema_version(again)
        tables = sorted(table_names(again))
    finally:
        again.close()
    assert after == before, f"re-opening the store changed the row count: {before} -> {after}"
    assert "vector_records" in tables, tables
    return {"rows_before": before, "rows_after": after, "schema_version": version, "tables": tables}


@check("backup: a snapshot is verified by restoring it, not by existing")
def t_backup(conn, ctx):
    directory = Path(ctx["dir"]) / "backups"
    entry = backup_mod.create(conn, directory)
    assert Path(entry.path).exists(), entry.as_dict()
    assert entry.counts.get("memories", 0) > 0, entry.counts

    report = backup_mod.verify(entry.path)
    assert report["ok"], report["problems"]
    assert report["observed"]["integrity_check"].lower() == "ok", report["observed"]

    # Corrupt one byte: verification must refuse it, not report success.
    raw = bytearray(Path(entry.path).read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    Path(entry.path).write_bytes(bytes(raw))
    broken = backup_mod.verify(entry.path)
    assert not broken["ok"], "a corrupted snapshot passed verification"
    assert any("checksum" in problem or "integrity" in problem for problem in broken["problems"]), broken

    # A refusal must also block a restore that would have overwritten a store.
    refused = backup_mod.restore(entry.path, Path(ctx["dir"]) / "restored.sqlite3")
    assert not refused["restored"] and refused["reason"] == "verification_failed", refused

    # Now take a good snapshot and prove the round trip.
    fresh = backup_mod.create(conn, directory, kind="daily")
    before = store.stats(conn, _scope())["rows"]
    restored_path = Path(ctx["dir"]) / "restored.sqlite3"
    outcome = backup_mod.restore(fresh.path, restored_path, overwrite=True)
    assert outcome["restored"], outcome
    restored = open_store(restored_path)
    try:
        after = store.stats(restored, _scope())["rows"]
        integrity = str(restored.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        restored.close()
    assert after == before and integrity.lower() == "ok", (before, after, integrity)
    return {
        "snapshot_kind": entry.kind,
        "verified_ok": report["ok"],
        "corruption_detected": not broken["ok"],
        "corruption_problems": broken["problems"][:2],
        "restore_refused_when_broken": not refused["restored"],
        "rows_before": before,
        "rows_after_restore": after,
        "restored_integrity": integrity,
    }


@check("dedupe: losers are archived, pinned rows never lose, prefix pairs only reported")
def t_dedupe(conn, ctx):
    scope = _scope("dupe-user")
    # Long enough to clear the dedupe scanner's prefix floor (24 characters), which
    # exists so a short row is not treated as a subset of an unrelated longer one.
    text = "用户拍板：生产发布固定在每周五下午执行，回滚窗口为两小时，超时立即人工介入。"
    first = store.put_memory(
        conn, scope=scope, target="memory", content=text,
        authority="user", source="operator", importance=0.6,
    )
    second = store.put_memory(
        conn, scope=scope, target="memory", content=text,
        authority="user", source="operator", importance=0.6,
    )
    container = store.put_memory(
        conn, scope=scope, target="memory",
        content=text + " 另外：发布前必须冻结分支，回滚演练每月一次。",
        authority="user", source="operator", importance=0.6,
    )
    # A prefix pair: same opening, different tails, neither containing the other.
    # Each half may carry something the other does not, so this must never resolve
    # itself -- it is reporting only.
    prefix_left = store.put_memory(
        conn, scope=scope, target="ops",
        content="上线流程第一步：先拉取全部代码并运行完整的测试套件，然后进入人工确认环节再发布。",
        authority="operator", source="operator", importance=0.5,
    )
    prefix_right = store.put_memory(
        conn, scope=scope, target="ops",
        content="上线流程第一步：先拉取全部代码并运行完整的测试套件，然后直接自动发布到生产环境。",
        authority="operator", source="operator", importance=0.5,
    )
    # A pinned pair where one contains the other: the pinned row is the shorter one,
    # so the automatic verdict would archive a pinned row. It must be refused instead.
    pinned_short = store.put_memory(
        conn, scope=scope, target="memory", content="安全红线：凭据只能保存位置指针，绝不写入记忆正文。",
        authority="user", source="operator", importance=0.2, pinned=True,
    )
    pinned_long = store.put_memory(
        conn, scope=scope, target="memory",
        content="安全红线：凭据只能保存位置指针，绝不写入记忆正文，也不写入日志与备份文件。",
        authority="user", source="operator", importance=0.2, pinned=True,
    )

    plan = dedupe_mod.scan(conn, scope)
    auto_kinds = {pair["kind"] for pair in plan["auto"]}
    manual_kinds = {pair["kind"] for pair in plan["manual"]}
    assert "exact" in auto_kinds, plan
    assert "substring" in auto_kinds, plan
    assert "prefix" in manual_kinds, plan

    result = dedupe_mod.apply(conn, scope, plan, apply=False)
    assert result["archived"] == [] and not result["applied"], result
    assert result["to_archive"], result
    assert len(result["to_archive"]) == len(set(result["to_archive"])), result["to_archive"]
    live_after_preview = len(store.rows_for_view(conn, scope, limit=50))

    applied = dedupe_mod.apply(conn, scope, plan, apply=True)
    assert applied["applied"] and applied["archived"], applied
    assert applied["archived"] == applied["to_archive"], applied

    for memory_id in applied["archived"]:
        record = store.get(conn, memory_id)
        assert record is not None, f"dedupe deleted a row instead of archiving: {memory_id}"
        assert record.lifecycle == "archived", record.lifecycle
    for pinned in (pinned_short, pinned_long):
        record = store.get(conn, pinned)
        assert record is not None and record.lifecycle != "archived", (
            "a pinned row was archived by dedupe"
        )
    survivors = {record.id for record in store.rows_for_view(conn, scope, limit=50)}
    # The containing row says everything the other two say, so it is the survivor and
    # both shorter copies go. One live row per fact is the contract; which id wins is
    # the scanner's decision, and it must be the informative one.
    assert container in survivors, "the row that contains the others should have survived"
    assert first not in survivors and second not in survivors, survivors
    assert {first, second} <= set(applied["archived"]), applied["archived"]
    return {
        "auto_pairs": len(plan["auto"]),
        "manual_pairs": len(plan["manual"]),
        "auto_kinds": sorted(auto_kinds),
        "manual_kinds": sorted(manual_kinds),
        "archived": len(applied["archived"]),
        "live_before_apply": live_after_preview,
        "pinned_survived": True,
        "dry_run_wrote_nothing": result["archived"] == [],
    }


@check("compact: anchors are preserved, or the row is refused")
def t_compact(conn, ctx):
    scope = _scope("compact-user")
    # Every anchor sits in the head of the row, inside the cut window, so this row is
    # safe to shorten -- the anchors must survive the cut.
    safe_text = (
        "部署流程：文档 https://example.invalid/runbook，脚本 /opt/deploy/run.sh，"
        "版本 v2.4.1，窗口 ROLLBACK_WINDOW_MINUTES=120。"
    ) + "（补充说明文字，用于把正文撑长到需要裁剪的长度。）" * 6
    unsafe_text = (
        "另一条很长的说明，前面的内容都很普通。" * 20
        + "关键入口是 https://example.invalid/secret-runbook，千万别丢。"
    )
    safe_id = store.put_memory(
        conn, scope=scope, target="ops", content=safe_text,
        authority="operator", source="operator", importance=0.5,
    )
    unsafe_id = store.put_memory(
        conn, scope=scope, target="ops", content=unsafe_text,
        authority="operator", source="operator", importance=0.5,
    )

    plan = compact_mod.plan(conn, scope, max_chars_per_row=120, stale_days=0)
    planned_ids = {item["id"] for item in plan["shorten"]}
    refused_ids = {item["id"] for item in plan["refused"]}
    assert safe_id in planned_ids, plan["refused"]
    assert unsafe_id in refused_ids, plan["shorten"]
    refusal = next(item for item in plan["refused"] if item["id"] == unsafe_id)
    assert "anchor_loss" == refusal["reason"] and refusal["dropped"], refusal

    outcome = compact_mod.apply(
        conn, scope, plan, apply=True, backup_before=True, backup_dir=str(Path(ctx["dir"]) / "backups")
    )
    assert outcome["backup"], "compaction applied without taking a snapshot first"
    assert not outcome["refused_on_revalidation"], outcome

    shortened = store.get(conn, safe_id)
    assert shortened is not None and len(shortened.content) < len(safe_text), shortened
    for anchor in ("https://example.invalid/runbook", "/opt/deploy/run.sh", "v2.4.1", "ROLLBACK_WINDOW_MINUTES"):
        assert anchor in shortened.content, f"anchor lost during shortening: {anchor}"
    untouched = store.get(conn, unsafe_id)
    assert untouched is not None and untouched.content == unsafe_text, "a refused row was modified"
    return {
        "planned_shorten": len(plan["shorten"]),
        "refused_anchor_loss": len(plan["refused"]),
        "planned_demote": len(plan["demote"]),
        "before_chars": len(safe_text),
        "after_chars": len(shortened.content),
        "anchors_kept": sorted(
            {a for hits in compact_mod.anchors(safe_text).values() for a in hits}
        )[:6],
        "refused_row_untouched": untouched.content == unsafe_text,
        "backup_taken": bool(outcome["backup"]),
    }


@check("golden: hit rates, MRR and the negative half of the test")
def t_golden(conn, ctx):
    scope = _scope("golden-user")
    keeper = store.put_memory(
        conn, scope=scope, target="project", content="发布固定在周五下午执行，回滚窗口两小时。",
        authority="user", source="operator", importance=0.8, memory_type="decision",
    )
    store.put_memory(
        conn, scope=scope, target="project", content="备份策略为每日增量、每周全量，保留三十份。",
        authority="operator", source="operator", importance=0.6,
    )
    cases = [
        golden_mod.Case(query="发布窗口", expect_ids=(keeper,)),
        golden_mod.Case(query="备份策略", expect_tokens=("每日增量",)),
        golden_mod.Case(query="不存在的主题词", expect_tokens=("绝不可能出现的词",)),
        golden_mod.Case(query="发布窗口", forbid_ids=(keeper,)),
    ]
    report = golden_mod.run(conn, scope, cases, limit=10)
    assert report["cases"] == 4, report
    assert report["hit_at"]["@1"] >= 2, report["hit_at"]
    assert report["mrr"] > 0.3, report["mrr"]
    assert len(report["misses"]) == 1, report["misses"]
    assert report["misses"][0]["diagnosis"], report["misses"]
    assert len(report["forbidden_violations"]) == 1, report["forbidden_violations"]
    assert golden_mod.load(None), "the example case file did not load"
    return {
        "hit_at": report["hit_at"],
        "hit_rate": report["hit_rate"],
        "mrr": report["mrr"],
        "misses": len(report["misses"]),
        "miss_diagnosis": report["misses"][0]["diagnosis"],
        "forbidden_violations": len(report["forbidden_violations"]),
    }


def run(keep: bool = False, db_dir: str | None = None) -> tuple[int, list[dict[str, Any]]]:
    if db_dir:
        db = Path(db_dir) / "memory.sqlite3"
    else:
        db = Path(tempfile.mkdtemp(prefix="memk-selftest-")) / "memory.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_store(db)
    ctx = {"dir": str(db.parent), "db": str(db)}
    results: list[dict[str, Any]] = []
    failures = 0
    for name, fn in CHECKS:
        record: dict[str, Any] = {"check": name}
        try:
            evidence = fn(conn, ctx) or {}
            record.update({"ok": True, "evidence": evidence})
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - the runner reports, never hides
            failures += 1
            record.update(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc().splitlines()[-6:],
                }
            )
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        results.append(record)
    conn.close()
    if not keep:
        import shutil

        shutil.rmtree(db.parent, ignore_errors=True)
    else:
        print(f"\n  database kept at {db}")
    return failures, results


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    keep = "--keep" in args
    print("memk self test")
    failures, results = run(keep=keep)
    passed = sum(1 for r in results if r["ok"])
    print(f"\n  {passed}/{len(results)} checks passed")
    if failures:
        print(json.dumps([r for r in results if not r["ok"]], ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
