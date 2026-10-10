# memk

[English](README.md) · **简体中文**

**面向 agent 的记忆内核：每一条持久化事实都是一份 claim，带着它的 authority、freshness，和可复验的来源。**

零运行时依赖 —— 只用 SQLite 和标准库，别无其他。没有 embedding 模型，没有向量数据库，没有服务。

**MIT 许可** · **Python 3.10+** · **零运行时依赖** · **Alpha**

*刻意用纯文本：徽章图片会在每次打开页面时向第三方服务器发起请求，而本项目不产生任何对外调用。*

**目录**
[这是什么](#这是什么) ·
[为什么存在](#为什么存在) ·
[核心模型](#核心模型记忆是一份-claim) ·
[安装](#安装) ·
[快速上手](#快速上手) ·
[工作原理](#工作原理) ·
[CLI 参考](#cli-参考) ·
[Python API](#python-api) ·
[语义通道](#可选的语义通道) ·
[运维](#运维) ·
[验证](#验证) ·
[项目结构](#项目结构) ·
[范围与已知缺口](#范围与已知缺口) ·
[文档](#文档) ·
[参与贡献](#参与贡献) ·
[许可](#许可)

---

## 这是什么

`memk` 是给 agent host 用的嵌入式记忆存储。它把每一轮对话捕获进分块日志（journal），
从中抽取候选事实，让每个候选通过一道 fail-closed 的准入闸门，最后用带预算的 packet
回答问题 —— 而 packet 自带来源信息。

它是**一个库加一个 CLI**，不是一个服务。没有 HTTP 接口，没有账号体系，也不与任何
agent 框架耦合：对接具体 host 的适配器活在核之外。

## 为什么存在

agent 记忆通常栽在两种方式上：要么回想起从没成立过的事，要么忘掉了曾经成立的事。
两者同源 —— 把记忆当成**文本**，而不是当成一份带着立场、来源和有效期的 **claim**。

## 核心模型：记忆是一份 claim

每一条持久化事实都以 claim 的形式存储，于是四个问题在使用的那一刻都可回答：

| 问题 | 机制 |
|---|---|
| **谁说的？** | `authority` 是一等列：用户陈述、运维写入、工具结果，或推断。推断永远不会被当成已确认事实呈现。 |
| **现在还成立吗？** | `freshness` 是四种状态之一，各自带固定的排序代价。被取代的事实仍可召回，只是排序下沉，而不是消失。 |
| **原文在哪？** | 每条准入的事实都带 evidence 行，指向分块日志；只有重建通过哈希校验，引文才会被报告为完整。 |
| **它能自己变成事实吗？** | 不能。自动晋升要求存在非 assistant 的证据；任何证据缺口都会拒绝，并给出 reason code。 |

记忆不是被「写入」的 —— 它是**被提议、被举证、被准入，然后进入当前态**：

```
turn ──capture──► journal row (role, chunk, hash)
        │
        └──extract──► candidate  ──admission gate──► memory (authority, freshness)
                          │                              │
                          │ fails gate                   │ superseded
                          ▼                              ▼
                     stays candidate               historical + pointer
```

闸门**只读角色，从不读内容**，因此来源判定不可能把对话内容泄漏进决策日志。

## 安装

**环境要求：** Python 3.10+ 和 SQLite（CPython 自带）。按设计，没有其他运行时依赖。

```bash
git clone https://github.com/Odd-C/memk.git && cd memk
python3 -m memk.selftest          # 27 checks, end to end; no install step required
```

或安装 console script：

```bash
pip install .
memk --help
```

> **Windows：** Windows 版 CPython 不自带 IANA 时区库，因此除非存在 `tzdata`，
> `timeline.py` 中涉及时区的路径会抛 `ZoneInfoNotFoundError`。装上这个纯数据包即可
> 让 timeline、recall 和 packet 路径正常工作：
>
> ```bash
> pip install tzdata
> ```
>
> 不装的话，一个干净的 Windows 解释器跑 `python -m memk.selftest` 会得到
> 16 PASS / 11 FAIL。这是已知的可移植性缺口，不是数据问题。

## 快速上手

命令从 `--db` 取数据库路径，没有则回退到 `$MEMK_DB`，再回退到 `./memk.sqlite3`。
下面统一用 `python3 -m memk.cli`，它等价于 `pip install .` 装出来的 `memk` console script。

```bash
export DB=demo.sqlite3

python3 -m memk.cli --db $DB init

# An operator write with authority by construction, pinned so it never leaves the view.
python3 -m memk.cli --db $DB store --target memory \
  --content "Credentials are never written to memory; store a pointer instead." --pinned

python3 -m memk.cli --db $DB store --target project \
  --content "部署流水线每周五执行，采用蓝绿切换。"
```

召回中文不需要任何模型 —— CJK n-gram 索引是原生的：

```bash
$ python3 -m memk.cli --db $DB recall 部署
# semantic channel absent: not_configured
  0.01966 [gram] 部署流水线每周五执行，采用蓝绿切换。
```

现在看闸门。assistant 的推断过不去；用户自己的话可以：

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

只有 `propose --apply` 带有非 assistant 证据（或 `--operator`）时才会晋升候选；
其余情况都停在候选态，带一个 reason code。

读结果，然后检查存储健康度：

```bash
python3 -m memk.cli --db $DB ask "when is the release window"   # answer-shaped read
python3 -m memk.cli --db $DB packet "deployment"                # the raw payload
python3 -m memk.cli --db $DB explain "deployment"               # why ranking did what it did
python3 -m memk.cli --db $DB doctor                             # read-only health check
```

## 工作原理

```
   injection     view render      ← projection of the store, pinned-aware, budgeted
   ─────────────────────────────────────────────────────────────────────────
   retrieval     lexical + ngram + vector, fused          ← scope & lifecycle in SQL
   ─────────────────────────────────────────────────────────────────────────
   truth         memories / evidence / candidates / journal      (SQLite)
   ─────────────────────────────────────────────────────────────────────────
   capture       journal: chunked, role-tagged, hashed
```

四条性质贯穿全部四层：

- **存储是唯一的真相。** 注入视图由它渲染而来，并记录自己渲染了什么，所以两者的
  差异是可归因的，而不是神秘的。
- **没有任何东西被自动删除。** 取代只标记历史；归档是明确的运维动作。
- **边界情况一律 fail closed。** 表缺失、证据缺失、来源不可读、只有 assistant 来源、
  角色未知 —— 全部拒绝晋升。
- **边界在 SQL 里执行。** scope 和 lifecycle 过滤在排序之前发生，而不是之后。

freshness 的各状态及其排序代价：

| 状态 | 代价 | 含义 |
|---|---|---|
| 缺省 | 无 | 默认值；最安全的状态 |
| `current` | 无 | 已确认，或用户已拍定 |
| `needs_check` | 小 | 待验证；**永不批量施加** |
| `historical` | 大 | 已被取代，仍可召回以供审计 |

逐层的完整设计理由见 [`DESIGN.md`](DESIGN.md)。

## CLI 参考

每个子命令都不依赖 agent host —— 这正是重点：在任何东西被允许依赖它之前，内核必须
可被人检视、可被人驱动。

**写入路径** —— 不经过这里，进不了存储。

| 命令 | 作用 |
|---|---|
| `init` | 创建数据库 |
| `capture` | 把原始一轮存入 journal，不做抽取 |
| `propose` | 捕获一轮、提议一条记忆、跑闸门 |
| `store` | 直接写入一条记忆，带运维权限 |
| `turn` | 一个 host 轮次：捕获 → 抽取 → 闸门 → 提供 packet |
| `supersede` | 替换一条记忆，保留历史 |

**读取路径**

| 命令 | 作用 |
|---|---|
| `recall` | 检索记忆 |
| `ask` | 答案形态的读取：召回并渲染 packet |
| `packet` | 为某个查询渲染 packet |
| `explain` | 说明某个查询为何返回这些结果 |
| `inspect` | 显示一条记忆及其逐字证据 |

**检查** —— 全部只读。

| 命令 | 作用 |
|---|---|
| `doctor` | 健康检查 |
| `stats` | 各层计数 |
| `audit` | 证据审计 |
| `view` | 检视或写出注入视图 |
| `lex` | 显示文本是如何被索引的 |
| `timeline` | 显示从问题中解析出的意图 |
| `freshness` | 显示各状态及其代价 |
| `selftest` | 运行内置自检 |

**维护**

| 命令 | 作用 |
|---|---|
| `promote` | 对候选重跑闸门 |
| `tune` | 被召回的记忆升温，未被使用的降温 |
| `compact` | 缓解注入视图压力 |
| `dedupe` | 找出并归档重复行 |
| `backup` | `create` · `list` · `verify` · `restore` · `prune` · `status` |
| `golden` | `run` · `example` |

**约定**

- scope 相关参数 `--user`、`--agent`、`--platform`、`--workspace` 适用于任何
  读或写的命令。
- 变更类命令默认是 **dry run**：`propose`、`promote`、`tune`、`compact`、`dedupe`
  在你不传 `--apply` 之前什么都不写；`view` 只在传 `--write` 时才写。
- 语义通道按命令逐个开启，通过 `--embedder`（见下文）。

## Python API

host 对接的接缝是 `memk.open_memory`，它返回一个绑定到单个数据库与单个读 scope 的
`Memory` 对象。

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

host hook 刻意做得很小，并按 host 自己知道的东西命名。它们中的任何一个都无法绕过
闸门写入持久记忆 —— 正是这条性质，让它可以被安全地接进 host 的主循环：

```python
mem.on_turn_start(session_id="s1", turn=2, role="user",
                  content="...", query="what changed this week")
mem.on_pre_compress(session_id="s1", turn=3, role="user", content="...")  # stage, do not extract
mem.on_session_end(session_id="s1")                                       # backfill vectors
```

常用属性与方法：

| 成员 | 用途 |
|---|---|
| `ingest(...)` | 捕获一轮，并执行抽取 + 闸门 |
| `remember(...)` | 明确的运维写入，天然具备权限 |
| `recall(query)` / `packet(query)` / `explain(query)` | 排序后的命中、渲染好的 packet、排序分解 |
| `inspect(memory_id)` | 该条记忆及其逐字、经过哈希校验的引文 |
| `supersede(new_id=, old_id=)` / `forget(memory_id)` | 明确的生命周期动作 |
| `health()` | 与 `doctor` 打印内容相同的报告 |

当你希望自己掌控调用顺序时，也可以直接导入更底层的模块（`journal`、`admission`、
`recall`、`packet`、`store`）；手工流水线见 [`memk/__init__.py`](memk/__init__.py)
的模块 docstring。

## 可选的语义通道

该通道**默认关闭，除非你主动要求**，而且要求是逐个命令的决策。差异是可量化的：

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

三条规则让「可选通道」这件事本身是安全的：

1. embedder 不可用意味着通道**缺席**，而绝不被替代 —— `resolve()` 返回 `None` 加
   一个原因，CLI 会把该原因打印出来。
2. 每个存储的向量都记录了产出它的 embedder，检索只读当前使用中的那个 embedder 的
   向量 —— 因此两个模型永远不会混在一起。
3. 产生命中的通道会**在结果中具名**（`[gram,vector]`），于是一个好得可疑的匹配可以
   被追溯到来源。

实现有三个：一个 OpenAI 兼容端点（只用标准库）、一个离线加载的本地
`sentence-transformers` 模型，以及一个确定性的 test double —— 它在自己的名字里就
声明自己是 test double。

## 运维

运维层活在**包内**，而不是包旁边的一堆脚本里，因为内核自己拥有 schema —— 一个靠猜
schema 的脚本，离一次 schema 变更出错只差一步。

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

有两个行为值得单独点出，因为它们正是「有用的运维层」与「危险的运维层」的分界：

- **验证方式是恢复。** `backup verify` 会打开快照、跑 `PRAGMA integrity_check`、
  并把校验和与行数与写在旁边的 manifest 对比。损坏的快照会被拒绝，而同一条拒绝也
  会阻止一次 restore 覆写活库。
- **压缩 fail closed。** 如果缩短一行会丢掉锚点 —— URL、路径、版本号、日期、环境
  变量名、长 id —— 该行就保持原样，拒绝理由会列出它本会丢掉的锚点。

实跑观测到的输出：

```
backup    ok=true  integrity=ok  counts={'memories': 3, ...}  problems=[]
dedupe    live=3   auto_mergeable=1  needs_review=0
compact   view 29 chars / budget 5600 · planned_shorten=1 · refused_anchor_loss=0
golden    cases=4  hit@1=3  hit@3=3  MRR=0.75  misses=1 (no_candidates)  forbidden=0
restore   restored=true  source_rows=3  restored_rows=3  consistent=true
```

排期、cron 接线、输出约定与恢复演练见 [`RUNBOOK.md`](RUNBOOK.md)。

## 验证

本项目的说法是：下面每一种失败模式都是**先被观察到真实发生**，然后才被设计掉的 ——
而且每一种都有具名检查钉住，所以答案是**可复现**的，而不是断言出来的。

```bash
python3 -m memk.selftest
```

| # | 失败模式 | 由哪项检查钉住 |
|---|---|---|
| 1 | CJK 词法召回把整段短语塌缩成一个 token，常见双字词一个都匹配不到 | `recall: two-character Chinese query matches (lexical, no embedder)` |
| 2 | 缺少 embedding key 时静默降级为字符串匹配，语义召回被悄悄换掉 | `embed: absent by default, and nothing is substituted for it` |
| 3 | 注入文件与存储被各自独立写入，彼此逐渐漂移 | `view: budget respected, pinned survives, hand edit adopted` |
| 4 | 抽取出的事实带着 journal 链接，却没有可验证的原文片段 | `journal: chunking, reconstruction, tamper detection` |
| 5 | assistant 的推断可能被排序成用户已确认的事实 | `audit: no promoted memory lacks human or tool evidence` |
| 6 | 批量标记 freshness 会把大量行一起压下去，召回质量下降 | `freshness: bulk change refused, supersession keeps history` |
| 7 | 卡住的锁可能冻住调用方 | 由 `schema.connect` 保证；无独立检查项 |
| 8 | 在带 reset 缺陷的构建上启用了 WAL | `schema: vulnerable WAL builds are gated, fixed ones are not` |

每一项的完整答案 —— 当时到底哪里坏了、改成了什么 —— 见 [`DESIGN.md`](DESIGN.md)。

<details>
<summary>全部 27 项自检</summary>

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

## 项目结构

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

模块说明保持英文原文，与代码中的模块 docstring 一一对应，便于对照阅读。

## 范围与已知缺口

**刻意不做：**

- 不捆绑向量依赖 —— 向量存在 SQLite 里，通道是可选的，因此词法召回必须先靠自己站住；
- 没有账号体系、HTTP 接口或远程 API；
- 不与 agent 框架耦合：内核可导入、可 CLI 驱动，host 适配器属于核之外；
- 不自动删除：只做取代与归档。

**已知缺口，明写出来而不是等别人后来发现：**

- 破坏性 schema 变更没有迁移路径；增量变更通过幂等 DDL 生效，自检断言重开库后每一行
  还在；
- 渲染集合是**按 scope 记录，而不是按消费者记录** —— 同一 scope 上的两个 host 可能
  互相归档对方的行，所以目前约定一个 scope 一个 host；
- 语义通道是暴力扫描：每次查询都会读取 scope 内全部向量；
- 抽取是基于规则的（说话角色 + 长度下限）；由模型驱动的抽取器应该待在闸门**之后**，
  而不是之前；
- Windows 上若没有 `tzdata`，timeline 路径会抛异常（见[安装](#安装)）。

后续计划 —— 类型化关系、评测工作、冷启动延迟预算 —— 见 [`ROADMAP.md`](ROADMAP.md)。

## 文档

| 文件 | 内容 |
|---|---|
| [`DESIGN.md`](DESIGN.md) | 设计为何是现在这样：分层、生命周期，以及每种失败模式及其产出的答案。 |
| [`RUNBOOK.md`](RUNBOOK.md) | 排期、输出约定、恢复演练，以及 doctor 报警时该怎么办。 |
| [`ROADMAP.md`](ROADMAP.md) | 已完成什么、下一步是什么、已知缺什么。 |
| [`LICENSE`](LICENSE) | MIT。 |

## 参与贡献

这里的规矩就是代码已经在遵守的那几条；破坏其中任何一条的改动，就是会被挡回去的改动：

1. **不引入新的运行时依赖。** 只用 SQLite 和标准库。可选能力插在接口之后，并且必须
   允许它缺席。
2. **Fail closed。** 判不了的检查就拒绝。被跳过的检查永远不算通过。
3. **不允许静默替代。** 每个可选通道都要在配置里、也在结果里具名。
4. **没有任何东西被自动删除。**
5. **每个修复都配一个检查。** 加进 `selftest.py`，并把失败模式写进它的名字里：
   改动完成前，`python3 -m memk.selftest` 必须是全绿。

## 许可

MIT —— 见 [`LICENSE`](LICENSE)。不包含任何第三方代码：本包自成一体，每一个决策背后
的理由都记录在 `DESIGN.md` 中。
