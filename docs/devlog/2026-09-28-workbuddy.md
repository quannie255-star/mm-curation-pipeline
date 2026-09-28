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
