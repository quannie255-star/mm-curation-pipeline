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
