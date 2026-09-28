# 双 AI 并行开发规约（WorkBuddy × glm）

> 2026-09-28 立。用户指令：「做一个共同开发规划……明确好各自负责的部分，你们俩同时推进。」
> 规则写在仓库里而不是聊天里，因为**规约要在两个 AI 都看得见的地方**才有约束力。
>
> 本文只管**协作机制**。要做什么、做到哪一步，看 `docs/DATA_SYSTEM_TRACK.md`（数据系统赛道）
> 与 `docs/DEV_PLAN.md`（唯一事实源）。

---

## 零、这份文件解决什么问题

并行开发的失败模式**不是「谁能力不够」，是四种具体的互相破坏**：

| # | 破坏方式 | 本项目已经发生过 |
|---|---|---|
| 1 | 同一文件两人同时写 → 后写者覆盖先写者 | **实测过**：同一条消息里对同一文件发两次编辑，其中一次被**静默丢弃**且工具仍返回成功 |
| 2 | `git add -A` 把对方未提交的在途改动一起提交 | 工作区长期躺着另一泳道的 10+ 个文件；`.gitignore` 漏掉 `data/raw/*/` 时一次 `git add -A` 就会把 383 MB 真数据入库 |
| 3 | `git stash` / `checkout --` / `reset --hard` 不可逆清掉对方的工作 | 更早的一次 `git prune --expire now` **铲掉过 22 个已推送提交** |
| 4 | 全量格式化 / 批量重构改写对方正在编辑的文件 | 当前 `ruff format` 欠债 **55 个文件**，任何一个都可能是对方正在改的那一个 |

所以本文只写**边界、串行点、交接物**。不写愿景。

---

## 一、四条铁律（违反任一条即视为该轮作废）

1. **一个文件同一时刻只有一个写者。** 需要改对方名下的文件 → 在 `docs/devlog/` 记一条「交接请求」，
   由对方改，或由对方显式把该文件**移交**并记录。不直接动手。
2. **禁止 `git add -A` / `git add .`**。只 `git add <具体文件>`。
3. **禁止 `git stash` / `git checkout -- <path>` / `git reset --hard` / `git push --force`**。
   共享工作区里这些命令会不可逆地吃掉对方的工作。
4. **门禁只增不减。** 测试基线**主仓 616 + 包 67 = 683**（2026-09-28 实点）；
   任何提交不得低于它。新加的门禁**必须做变异测试**（故意改坏 → 必须红 → 改回 → 必须绿），
   否则不算门禁（判据见 skill `gate-actually-gates-audit`）。

---

## 二、物理隔离（这是「同时推进」的前提，不是可选项）

**为什么必须隔离**：两个 AI 在**同一个工作目录**里并发工作，`git` 状态是**共享**的——
.git/index、工作区文件、未提交改动全部可见可改。一方执行 `checkout`/`stash`/全量格式化，
另一方的在途工作就没了。这不是纪律问题，是**存在性**问题。

**方案**：

| 工作区 | 位置 | 归属 |
|---|---|---|
| 主工作区（当前仓库目录） | `C:/Users/10393/Desktop/mm-curation-pipeline` | **glm**（它的在途改动在这里，不能搬） |
| 我的工作区 | `C:/Users/10393/Desktop/mmc-doc-frontend`（worktree） | **WorkBuddy** |

建立命令（由我执行，属 R0-3）：

```bash
# 在仓库根执行；新目录与主工作区平级
git worktree add ../mmc-doc-frontend -b workbuddy/doc-frontend main
```

**⚠️ worktree 推送要点（本项目踩过）**：worktree 里 `git push origin main` 推的**不是当前 HEAD**，
`main` 会解析成**主工作区**的本地分支。一律用：

```bash
git push origin HEAD:refs/heads/main
```

**合并方向**：我这条线以**文档 + 前端**为主，不产生 `src/` 语义变更，因此冲突面很小。
合并时机由「推送窗口」串行化（见 §四）。

---

## 三、泳道：按**性质**切，不按目录切

用户确认的分工：**glm 写实现，我写文档 + 前端**。落地时用一条**判据**而不是一张会长腐烂的文件清单：

> **跑得起来、产生产物、定义接口的 → glm。给人看、给人读、自证可信度的 → 我。**

| 泳道 | 典型路径 | 内容 |
|---|---|---|
| **glm（实现/工程面）** | `src/**`、`configs/**`、`dags/**`、`packages/**`、`tests/**`、`Makefile`、`pyproject.toml`、`.github/workflows/**`、`requirements*.txt`、`requirements.lock`、`docker/**`、`.dockerignore`、`.gitignore` | 算子/协议/数仓/平台/调度/服务实现；构建与交付配置；测试 |
| **我（叙事/交互/口径面）** | `docs/**`、`scripts/showcase_app.py`、`scripts/ops_dashboard.py`、`scripts/build_real_interactive.py`、`scripts/templates/**`、`scripts/verify_claims.py`、`docs/claims.json`、`tests/test_verify_claims.py` | 全部文档口径与面试叙事；前端与可视化；**数字口径门禁** |

**两处刻意的例外（写清楚，免得靠猜）**：

1. **前端文件在 `scripts/` 里** → 按文件名划给我（见上表），其余 `scripts/**` 是 glm 的。
2. **口径门禁（`verify_claims.py` + `claims.json` + 它的测试）划给我**。理由：它校验的是
   「文档里写的数字对不对」，属于自证可信度而不是实现。这批文件原本是 glm 的在途产物
   （停在 2026-09-18），**glm 在 R0-1 提交后，维护权移交给我**。

---

## 四、共享文件与串行规则

| 文件 | 规则 |
|---|---|
| `docs/DEV_PLAN.md`（唯一事实源） | **单写者 = 我**。glm 收工**不直接改**；把一行结构化摘要写进 `docs/devlog/YYYY-MM-DD-glm.md`，由我合并进日志表。理由：日志表是「新会话在前」的表格，两人同时加行**必然冲突在同一位置** |
| `docs/design_tables.md` | **append-only**：各自只在自己模块的节里追加，不改他人段落。冲突只可能落在文件末尾，好解 |
| `docs/ENGINEERING_NOTES.md` | 内容各自写，**编号由我分配**——本项目**撞过号**（两条都用了 #56/#57，事后重编为 #58/#59）。glm 写内容时可留 `#?` 由我填 |
| `docs/RUNBOOK.md`、`ROADMAP.md`、`README.md`、`INTERVIEW.md`、`GAP_AUDIT.md` | 我 |
| `tests/conftest.py` | glm（共享 fixture 属测试基础设施） |
| `.gitignore`、`pyproject.toml`、workflow 文件 | glm；我需要改时走「交接请求」 |

**推送窗口（同一时刻只有一个）**：

```
▶ 推送窗口：<who> @ <时间>        ← 开窗口时写进 DEV_PLAN 顶部
✅ 推送完成：<who> @ <时间> <sha>  ← 关窗口时改写
```

推之前**必须**依次做三件事：

```bash
git fetch origin
git log  --oneline origin/main..HEAD     # 逐条确认是「自己的」提交，不是对方的
git status --porcelain                   # 确认没有把对方名下的文件加了进来
```

---

## 五、轮次计划

### R0 · 解锁轮（**串行**，目标：把「并行」变成物理上安全）

| # | 任务 | owner | 依赖 | 验收 |
|---|---|---|---|---|
| R0-1 | 收口提交 8 天在途改动（含 S0② 的 `claims.json` / `verify_claims.py`） | **glm** | — | `git status` 里不再有 09-20 之前的改动 |
| R0-2 | 提交平台轨 S0–S6，拆 **3 个 commit**（核心 / 交付 / 文档） | **我** | — | 三个 commit 各自可独立回滚 |
| R0-3 | 我切 worktree 物理隔离（见 §二） | **我** | R0-2 | 两线不再同目录 |
| R0-4 | 推送（3 个历史提交 + R0-1/R0-2 新提交） | **我** | R0-1、R0-2、网络 | `git log origin/main..HEAD` 为空 |
| R0-5 | 全量 `ruff format`（**实点 55 文件**：scripts 26 / tests 14 / src 11 / packages 4）+ 提交 | **glm** | R0-1 | `ruff format --check` 全绿（现 55 红 / 182 绿） |
| R0-6 | 修复事实源：`DEV_PLAN.md` 快照/基线/日志 + `AGENTS.md` 基线 | **我** | R0-2 | 基线 = 683；日志有 S0–S6 一行 |

> R0-1 与 R0-2 **文件不相交**，可同时做；但 R0-5 必须等 R0-1（否则会改写 glm 正在改的文件，
> 正是 §零 第 4 条那种破坏）。

**R0 执行状态（2026-09-28 更新，第二轮）**

| # | 任务 | 状态 | 证据 |
|---|---|---|---|
| R0-1 | glm 收口 8 天在途 | ⬜ **未做——整条链唯一的闸门** | 工作区仍有 glm 的 13 个 09-06~09-20 在途文件 |
| R0-2 | 提交平台轨 S0–S6（3 个 commit） | ✅ | `cd3072b` 核心 / `3ad0be8` 交付 / `ed89d03` 文档 |
| R0-3 | worktree 物理隔离 | ✅ | `../mmc-doc-frontend`（分支 `workbuddy/doc-frontend`）；**主工作区仍在 `main` 且 HEAD 未动** |
| R0-4 | 推送 | ✅ | **9 个提交**（3 个历史积压 + 6 个本轮 = 5 个实质 + 1 条关窗）已上 `main`（原 `465fe4d`）；关窗条件 `origin/main..HEAD` 为空已满足。**不写死 tip sha**——关窗那条提交也会被推，写死的 sha 会立刻过期 |
| R0-5 | 全量 `ruff format` | ⬜ **未做，等 R0-1** | 实点仍 **55** 文件（scripts 26 / tests 14 / src 11 / packages 4） |
| R0-6 | 修复事实源 | ✅ | 与 R0-2 同批的第 4 个 commit `510462a`（`DEV_PLAN.md` + `AGENTS.md`） |

**本轮把「文件不相交」从假设变成了实测**：按 mtime 分界，glm 的在途文件全部落在
**09-06~09-20**，平台轨全部落在 **09-27/09-28**，两者零交集——所以 R0-2 能安全地不等 R0-1。
**归属判据用 mtime，比读文档可靠**（文档自述的归属已经腐烂过）。

**两个未认领项（本轮刻意不提交）**：`scripts/findata_health_stage.py`（跨仓库 findata 联动脚本）
与 `docs_archive/v4-alpha-fhir/tasks.md`——mtime 均为 09-17，落在 glm 的在途窗口内，
但**既不在它的清单里，也不在我的清单里**。按「宁可欠着、不可猜着提交」处理，等认领。

**两件「知道但暂时不修」的共享配置（写在规约里，免得下一个人重新发现）**：
1. **`.gitattributes` 缺失** → 本机 `core.autocrlf=true`（system 级）让新检出把生成物写成 CRLF
   （实测 3838 B → 3963 B）。现有门禁用 `read_text()` 比较，**自带换行归一，所以两处都看不见这个漂移**。
   详见 `docs/ENGINEERING_NOTES.md` #82 补注。
2. **`.gitignore` / `pyproject.toml` / workflow 文件属 glm 域**（§四），我需要改时走交接请求。

**本文件在 R0 之后不再需要更新**——R1–R3 的分派已固定，进度看 `docs/DEV_PLAN.md` 与各人的 `docs/devlog/`。

### R1 · 可投递收口轮（并行）

| # | 任务 | owner | 依据 |
|---|---|---|---|
| G1 | S6④ 版本发布：`version` `0.1.0` → `1.0.0`、加 `[project.scripts]`（`mmc` 成为真命令）、`CHANGELOG.md`、`git tag` | glm | `DATA_SYSTEM_TRACK` S6④（唯一 ⬜ 项） |
| G2 | S6⑤ 容器冒烟**首次在 CI 真跑**并修通（`container-ci.yml` 已写但从未执行过） | glm | S6⑤ |
| G3 | S6⑥ 「纯 CPU 十分钟」路径实测出数（现在是待实测的承诺） | glm | S6⑥ |
| M1 | `INTERVIEW.md` 数据平台向（S0–S6 的 STAR）+ `README.md` 补真实数据节 | 我 | 面试收尾包 |
| M2 | 门面数字自动化：把散落各文档的 13 处数字接 `claims.json` + `verify_claims` 门禁（**从"人工对齐"变成"CI 拦截"**） | 我 | S0② |
| M3 | `GAP_AUDIT.md` 第三轮复审（前两轮已抓出 N-1/N-2 两条假绿门禁）+ `ROADMAP.md` 进度表同步 | 我 | 复审纪律 |
| M4 | 平台轨可视化**第 10 页签**：run 台账 / 水位线 / 数据新鲜度 / 分区裁剪证据 / DEV→PROD 晋升报告 | 我 | 用户本轮选定 |

### R2 · 补功能轮（并行）

| # | 任务 | owner | 备注 |
|---|---|---|---|
| G4 | **R7 滚动基线**（EWMA / 移动窗）| glm | 真实数据的主要瓶颈是「稳态假设」本身，固定基线无解 —— 笔记里记为「如实未做」 |
| G5 | **人审队列**（V6 设计表 ⑦ / GAP_AUDIT P1-7）| glm | 真实轨误杀率只能是**上界**，终裁必须靠人 |
| G6 | **W4 配比层**（manifest + ledger + 域饥饿/过采样/源漂移三告警）| glm | 设计表已落 |
| M5 | **W5 可视化进包** | 我 | 与 M4 同源 |
| M6 | `docs/RUNBOOK.md` 增「数据系统故障篇」（对齐 JD 的 on-call / postmortem） | 我 | S5 的文档面 |

### R3 · 解冻清单（用户本轮解冻，按优先级，非全部本冲刺）

| 优先级 | 任务 | owner | 说明 |
|---|---|---|---|
| 高 | **J1 合成 / 增强通道（1→N）** | glm | 唯一**需协议变更**的一项：现有通道只能 1→1。**红线：合成样本必须走同一条漏斗**，否则「我合成了一批数据」不可证伪 |
| 中 | **J3 SQL 目录层** | 先由**我**实点核对 | ⚠️ 可能已被平台轨 S2 覆盖 —— **动码前先核，避免重复建设**（这正是「不要浪费别的 AI 的编程计划」） |
| 中 | J4 语料分布体检报告 | glm 实现 + 我出叙事 | |
| 中 | W3 Bad Case 归因 | glm | |
| 低 | J2 视频/音频模态、J5 公开评测生态互通 | glm | 可能超出一个冲刺，不承诺 |

---

## 六、每轮都不得破坏的不变量

1. **测试基线 616 + 67 = 683，只增不减**；改测试后用 `--junitxml` 实点回写，**别沿用旧数**
   （基线数字历史上烂过六次）。
2. **不改 `Sample` / `Operator` 协议签名**；**既有 4 个 config 一字不动**（新需求新建 config）。
3. **非平凡任务先过设计门**：写进 `docs/design_tables.md`，用户确认后再动码（`AGENTS.md` / `AI_CODING_PROTOCOL.md`）。
4. **报告 / 数据 / 模型产物不入库**（`.gitignore` 已配；用 RUNBOOK 命令重生成）。
5. **中文 commit message**，首行阶段前缀（如「S6 收口：…」）。

---

## 七、已知风险与预案

| 风险 | 预案 |
|---|---|
| glm 继续不收口 → R0-1 卡住，连带卡住 R0-4 / R0-5 | 设时间点；过期未收口则改为「我按 mtime + `git diff` 逐个判定后提交」，并在文档里**如实标注**该批未经原作者确认 |
| `github.com:443` 间歇不可达（本机已知）| 改用 Git Data API 推送——**直接跑 skill `github-actions-smoke-debug` 目录下的 `gh_push_api.py`**（先 dry-run 再 `--apply`），不要手写第二遍 |
| worktree 与主工作区分叉后合并冲突 | 我这条线只动 `docs/**` 与前端文件，与 glm 的实现文件不相交；合并前先 `git fetch` + rebase 到 `main` |
| 新门禁是**假绿**（看起来在拦其实没拦）| 强制变异测试；判据见 skill `gate-actually-gates-audit` |
| 文档里的数字再次腐烂 | 每轮收工必须实点回写；`M2` 把它变成 CI 拦截，而不是靠自觉 |

---

## 八、交接物（每轮的固定产出，缺一视为该轮未完成）

1. `docs/devlog/YYYY-MM-DD-<who>.md` —— 一行结构化摘要：**做了什么 / 关键数字 / 改了哪些文件 / 下一个人的前提**。
2. `docs/DEV_PLAN.md` 的**当前轮次**小节（由我维护）。
3. commit 本身（谁的活儿看 `git log`，不看文档）。
