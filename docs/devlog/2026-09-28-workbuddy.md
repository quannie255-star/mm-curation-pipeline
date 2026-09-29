# 2026-09-28 · WorkBuddy

**轮次**：R0 准备（未开始动手实施，本轮只立规约 + 修事实源）
**泳道**：叙事 / 交互 / 口径（文档 + 前端）

## 做了什么

1. **立 `docs/COLLAB_PLAN.md`**（双 AI 并行开发规约）——用户指令「做一个共同开发规划，
   明确好各自负责的部分，你们俩同时推进」。含：四条铁律、worktree 物理隔离、
   泳道判据、共享文件串行表、推送窗口、R0–R3 轮次与分工、风险预案。
2. **修事实源三处腐烂**：
   - `docs/DEV_PLAN.md`：快照表补 **V7 平台轨 S0–S6** 与「双 AI 协作规约」两行；
     基线 `452+67` → **`616+67=683`**；CI 注记拆成「已修的一半（pytest 现会真跑）/
     未做的一半（全量 format，55 文件）」；「下一阶段任务分解」加**历史存档**警示
     （γ/δ/ε/ζ 全部已完成，别照着动码）；协作硬规则改「进场先读 COLLAB_PLAN」+
     「DEV_PLAN 是单写者文件，各写 devlog」；开发日志补本轮一行。
   - `AGENTS.md`：整个重写。基线 `328+67` → **683**；开工加第 0 步（先读 COLLAB_PLAN）；
     收工改为「写 `docs/devlog/`、不要直接改 DEV_PLAN」；补两条环境坑
     （Git Bash **缺 coreutils**；`pytest --basetemp` 必须盘符形式且指向不存在目录）。
3. **建 `docs/devlog/`**（本文件即机制示范）。

## 关键数字（全部实点，非沿用）

| 项 | 值 | 怎么测的 |
|---|---|---|
| 测试基线 | **主仓 616 + 包 67 = 683** | `--junitxml` 落文件解析 |
| `ruff format` 欠债 | **55 文件**（scripts 26 / tests 14 / src 11 / packages 4），182 已格式化 | `ruff format --check` |
| 本地 vs 远端 | `main`=**bee5c45**，`origin/main`=**465fe4d**，**3 个提交未推** | `git for-each-ref` + `git log origin/main..HEAD` |
| 发布面 | `version` **0.1.0**、无 `[project.scripts]`、无 CHANGELOG、**0 tag** | 读 `pyproject.toml` + `git tag` |
| glm 在途 | 最后修改 **09-20**（停 8 天），当前 683 全绿 | 文件 mtime + 全量测试 |

## 改了哪些文件

- 新增：`docs/COLLAB_PLAN.md`、`docs/devlog/2026-09-28-workbuddy.md`
- 修改：`docs/DEV_PLAN.md`、`AGENTS.md`

**未改任何代码**（本轮是规约与文档轮，符合泳道边界）。

## 下一个人的前提（重要）

1. **R0-1（glm）是整条链的闸门**：glm 在途的 8 天改动不收口，R0-4 推送与 R0-5 全量
   `ruff format` 都做不了。用户已确认「让 glm 先收口提交」。
2. **R0-2 与 R0-1 文件不相交，可并行**：平台轨 S0–S6 的全部产物（`src/mm_curation/platform/`、
   `cli.py`、`configs/platform.yaml`、`configs/rbac.yaml`、`configs/contracts_platform/`、
   `dags/generated_platform_dag.py`、`tests/test_platform_*.py`、`tests/test_lock_file.py`、
   `tests/test_ci_seed_sources.py`、`docs/PLATFORM.md`、`docs/DATA_SYSTEM_TRACK.md`、
   `docs/ENGINEERING_NOTES.md`、`.gitignore`、`requirements-app.txt`、`requirements.lock`、
   `docker/**`、`.dockerignore`、`docker-compose.yaml`、`.github/workflows/container-ci.yml`、
   `scripts/{gen_lock,ci_seed_sources,smoke_container}.py`）**至今全部未提交**，建议拆 3 个 commit。
3. **glm 不要直接改 `docs/DEV_PLAN.md`**（单写者文件，见 COLLAB_PLAN §四）。
4. **推送前**：`git fetch` + `git log --oneline origin/main..HEAD` 逐条确认是自己的提交。

---

## 追加：R0 解锁轮已执行（同日第二轮）

用户指令：「你先做你能够做的，剩下的交给 glm」。

**做了**（R0-2 / R0-3 / R0-4 / R0-6）：

1. **先实点再动手**：`--junitxml` 落文件解析得 **616+67=683**（0 失败/0 错误/0 跳过；
   主仓 305.6s / 包 33.4s），与文档一致才允许提交——上一轮刚抓到三处腐烂数字。
2. **R0-2**：平台轨 S0–S6 拆 **3 个 commit** —— `cd3072b` 核心（`platform/` 十模块 + `cli.py` +
   configs + 生成 DAG + 8 个平台测试 + `conftest.py` + `mmc.py` + `pyproject.toml`）、
   `3ad0be8` 交付（lock 21 pin + `Dockerfile.app` + 三个 workflow + 三个脚本 + 其测试）、
   `ed89d03` 文档（`PLATFORM.md` + `DATA_SYSTEM_TRACK.md` + `ENGINEERING_NOTES` #81–#89 +
   `.gitignore` + `test_lock_file.py`）。
   **提交脚本里写死了两条护栏**：只 `git add <具体路径>`；每次提交前断言 staged 清单里
   **不出现** glm 在途的 13 个文件。
3. **R0-3**：worktree `../mmc-doc-frontend`（分支 `workbuddy/doc-frontend`）已建；
   **主工作区仍在 `main`、HEAD 未动**——`git worktree add` 不碰别人的工作区。
4. **R0-6**：`510462a`（`DEV_PLAN.md` + `AGENTS.md`）。
5. **R0-4**：推送 **9 个提交**（3 个历史积压：`b491ffd`/`b5ff1fb`/`bee5c45` + 5 个实质：
   `cd3072b`/`3ad0be8`/`ed89d03`/`510462a`/`1f9922e` + 1 条关窗提交），`origin/main` 由 `465fe4d` 推进；
   关窗条件 `origin/main..HEAD` 为空已满足。（关窗那条提交不列 sha——它本身会被推，
   写死的 sha 会立刻过期。首次推送秒过；第二次推送超时 180s，属瞬时网络抖动，重试即过。）

**实测推翻了我自己的一个猜想**（这条值得记）：我以为 `core.autocrlf=true` 会让 worktree 里
「生成物逐字节一致」那条门禁假红。**实测：文件确实是 CRLF（3838 B → 3963 B），但门禁仍然绿**——
因为它用 `read_text()` 比较，通用换行模式会归一。所以那条门禁**覆盖内容漂移、不覆盖 EOL 漂移**，
而 CI 在 Linux 上检出就是 LF 也看不见。已写进 `ENGINEERING_NOTES` #82 补注。
**没顺手加 `.gitattributes`**：它属共享配置域，单方面改会与并行的一方冲突。

**没做（= 交给 glm）**：R0-1（glm 收口，整条链唯一的闸门）、R0-5（全量 `ruff format`，实点
**55** 文件，必须等 R0-1，否则会改写 glm 正在改的文件）。

**两个未认领项刻意没提交**：`scripts/findata_health_stage.py` 与
`docs_archive/v4-alpha-fhir/tasks.md`（mtime 均 09-17，落在 glm 在途窗口内，但两边清单都没有）。
按「宁可欠着、不可猜着提交」处理。

**本轮未改任何代码、未降低任何门禁**（基线 683 一分未动）。

## 交接给 glm 的三件事

1. **R0-1 优先**：收口那 13 个在途文件（`Makefile`、`benchmarks/capability_matrix.json`、
   `docs/PROOF_CHAIN.md`、`runs/experiments.jsonl`、
   `scripts/eval_{fhir,industrial,judge,random_drop_baseline}.py`、`scripts/finetune_clip.py`、
   `src/mm_curation/tuning/extraction.py`、`docs/claims.json`、`scripts/verify_claims.py`、
   `tests/test_verify_claims.py`）。⚠️ 收口后按 `COLLAB_PLAN.md` §三 的移交条款，
   **`claims.json` + `verify_claims.py` + 它的测试归我维护**。
2. **R0-5**：全量 `ruff format`（55 文件）+ 提交。
3. **共享配置域**（`.gitignore` / `pyproject.toml` / workflow / `.gitattributes`）由 glm 定；
   我需要改时走交接请求。

---

## 追加：R0 全关闭之后（同日第三轮）——隔离归位 + 两条交接请求

**背景**：glm 已完成 **R0-1**（`8fd8669`，8 天在途全部入库 + 认领两个无主文件）、**R0-5**
（`dda99c9`，全量 `ruff format` 55 文件），并顺势做掉 **R1 的 G1+G3**（`7471b1e`：版本 1.0.0 +
`[project.scripts] mmc` 真命令 + CHANGELOG + 真实三源纯 CPU 实测 ≪10 分钟）。**R0 六项全部关闭**，
且 `verify_claims.py` + `claims.json` + `tests/test_verify_claims.py` **正式移交给我**
（COLLAB_PLAN §三例外 2 条件满足）→ **M2 可开工**。

### 隔离归位（我做的一处纠正）

上一轮我的两份文档改动（`COLLAB_PLAN.md` / `DEV_PLAN.md`）**误落在主工作区**（= glm 域），
而 glm 当时正在同目录实编辑（`industrial_quality.py` 一轮内 `15→20` 行）。**先把这两个文件
逐字节迁进 worktree**（留备份于临时目录），再把主工作区里它们**还原到 HEAD**
（`git restore --source=HEAD --staged --worktree -- <这两个文件>`，只碰我独占的文件）。
此后主工作区 `git status` 只剩 **glm 的 8 个在途文件**，无我的残留。

### 交接请求 1（低优先，非火警）：`.gitignore` 补两条规则

`git add --dry-run` 曾实测：`.venv-ci/`（**179.7 MB** venv）、`data/_r0_backup/`、
`data/tmp_serve.log` 都**会被逐条暂存**（当时 `git check-ignore` 给的是**假绿**，见 #90）。
**2026-09-28 复核**：这三个产物**已不在盘上**（你已清理），但 `.gitignore` **仍未**补规则
（`git diff HEAD -- .gitignore` 为空）。→ 由「在案泄漏」降级为**潜在泄漏**：只要有人再建一个
`.venv-ci/`，`git add -A` 就会重演。建议补：

```
# 复现 CI 用的临时 venv（R0-5 建过 .venv-ci/，179.7 MB）
.venv*/
# data/ 根下的零散日志 / 临时文件
data/*.log
```

（`.venv34`/`.venv-ci` 这类一次性目录用 `.venv*/` 一并覆盖；`data/_r0_backup/` 是一次性备份，
删掉即可，不必写进规则。）

### 交接请求 2（低优先）：`7471b1e` 上格式门禁又红了（属平台轨）

在 worktree（= 已提交的 `7471b1e`）实点：`ruff format --check src tests scripts dags packages`
→ **rc=1，1 file would be reformatted / 236 already formatted**，就是
`tests/test_platform_modeling.py`（`--diff` 是**真代码差**：隐式字符串拼接该并成一行，**非 CRLF**）；
同一时刻 `ruff check` 全绿。即 **R0-5 转绿的「下一个」提交就把门禁弄红了**（见 #91）。
修法：`ruff format tests/test_platform_modeling.py`。属平台轨，交给你。

> 两条都**不急**、不阻塞任何人；写在这里是为了不让它们腐烂。`.gitattributes` 缺失（#82 补注）
> 仍挂在「共享配置域」名下，一并由你定。

---

## 追加：M2 落地（门面数字接门禁）

**目标**（`GAP_AUDIT` P3-1 的根治建议 + `COLLAB_PLAN` M2）：把散落各文档、**手写**的数字接
`claims.json` + `verify_claims` 门禁，从「靠人记着对齐」变成「CI 拦截」。此前门禁只覆盖
`claims.json ↔ 落盘报告`，**从不读文档**——README/INTERVIEW/RESUME 里那些数字全是裸奔的。

**做了什么**：

1. **`claims.json` 增两区**：
   - `baselines`：`tests_main=616` / `tests_pkg=67` / `tests_total=683`（不来自报告 JSON，
     但同样要求全文档一致；改一处，门禁列出所有没跟上的文档）。
   - `facades`：**66 条**，覆盖 **16 份文档**（README / INTERVIEW / RESUME / ANALYSIS_REPORT /
     INTERVIEW_SELFTEST / PROOF_CHAIN / QUICKSTART / RUNBOOK / DS_DA_TRACK / INDUSTRY_BENCHMARK /
     REAL_DATA_REPORT / PLATFORM / COLLAB_PLAN / DEV_PLAN / AGENTS / showcase_app.py），
     把 8 个 claim（0.459 / 0.556 / 0.688 / 0.636 / 7.16 / 7.70 / 0.131 / 1.23%）与 3 条基线，
     绑到「文档里那个字面量」。
2. **`verify_claims.py` 增门面校验**：每条门面**两件事同时成立**才算过——
   ①`literal == render(来源值, fmt[, scale, suffix])`（否则 `registry-stale`）；
   ②文档里能找到该字面量（否则 `doc-stale`）。字面量计数带**边界**（`0.556` 不在 `10.556` 里被误计、
   `67` 不在 `267` 里被误计）。`--update` 按来源重锁 `literal` 并提示「文档仍需手工同步」。
3. **可进 CI（关键性质）**：门面校验**只读 `claims.json` 的 `expected`**，**不需要 `data/reports/`**
   （生成物、不入库）——所以在 CI 上真的会拦。这是它比 claim 校验更强的地方。
4. **顺手修掉两处「当前态基线腐烂」**：`README.md` 的 `328 + 67` → `616 + 67 = 683`；
   `PROOF_CHAIN.md` 的 `主仓 328 + 包 67` → `616 + 67`。
5. **抓到并修掉一颗 CI 地雷**（→ `ENGINEERING_NOTES` **#92**）：`test_verify_claims.py` 的
   `test_registry_all_pointers_resolve_in_repo` **断言 `data/reports/` 必须存在**，而它是生成物、
   已忽略 → 这个测试**只在跑过生成步骤的机器上绿、在任何新克隆/CI 红**。是 #80 的镜像
   （#80 = CI 一直红我以为绿；这条 = 本机绿、CI 红），且被 S0「让 pytest 真跑」**激活**。
   修法：拆成①字段完整性（CI 可查）②报告在盘才校验指针，并加「有报告时必须真校验到 ≥1 条」防退化。
6. **测试 + 变异**：`tests/test_verify_claims.py` 从 4 → **10** 条（字数边界 / render /
   pass / registry-stale / doc-stale / source-missing / **集成 + 变异**：改坏来源必须变红）。

**实点（2026-09-28）**：`verify_claims.py` → **66 门面 66 PASS / 0 漂移**；
以新注册表对**真实报告**复验 → **13 PASS / 0 DRIFT / 1 历史（不校验）**、数据指纹 `pass`；
`pytest tests/test_verify_claims.py` → **10 passed**；`ruff check` + `format --check` 双绿。

**诚实边界（还没做）**：①`baselines` 的值仍靠人实点回写（可进一步用 CI 的 `--junitxml` 自动比对，
属下一步）；②`ROADMAP` / `design_tables` 里的历史基线**刻意没登记**（按日期冻结的历史记录，不是当前态）。

### 交接请求 3（低优先）：把 `verify-claims` 接进 CI

`make verify-claims` 已是入口，但 `.github/workflows/*.yml` 里**没有任何 workflow 调它**（已 grep 确认）
→ M2 的「CI 拦截」目前只在**本地**兑现。建议在 `ci.yml` 加一步：
`python -X utf8 scripts/verify_claims.py`（它不依赖 `data/reports/`，CI 上可直接跑）。
workflow 属 glm 域，故走交接请求。
