# AGENTS.md — AI 协作开发须知

本仓库由**多名 AI 并行**开发（当前：glm + WorkBuddy）。**任何 AI 进入本仓库，按以下顺序执行：**

## 开工（必做）

0. **读 docs/COLLAB_PLAN.md** —— 先确认**泳道边界与串行点**。本仓库同一时刻有另一个 AI 在工作，
   而它的未提交改动就躺在这个工作区里。不看边界就动手，等于拿别人的在途工作做赌注。
   规约里最关键的三条：**一个文件只有一个写者** / **禁止 `git add -A`** /
   **禁止 `stash` · `checkout --` · `reset --hard` · `push --force`**。
1. 读 **docs/DEV_PLAN.md**——当前状态快照、下一阶段任务分解、协作硬规则都在那里。它是唯一事实源。
2. 非平凡任务先走设计门：把设计表写进 docs/design_tables.md，等用户确认再写码（细则见 docs/AI_CODING_PROTOCOL.md）。
3. 任务清单如果与 DEV_PLAN.md 冲突，以 DEV_PLAN.md 为准。

## 收工（必做，缺一视为未完成）

1. **质量门全绿**：`python -m ruff check .` + `python -X utf8 -m pytest -q --tb=no`（主仓库与 packages/curation-eval 各跑一次）。测试基线：**主仓 623 + 包 67 = 690**，不得倒退（**改测试后实点回写**，基线在 docs/DEV_PLAN.md 顶部，别沿用旧数）。
2. **写本轮交接**：`docs/devlog/YYYY-MM-DD-<你的名字>.md`（做了什么 / 关键数字 / 改了哪些文件 / 下一个人需要知道的前提）。
   **不要直接改 `docs/DEV_PLAN.md`**——它是单写者文件，由文档 owner 合并（见 COLLAB_PLAN.md §四）。
3. **回写任务状态**：有面试价值的现象写进 docs/ENGINEERING_NOTES.md（**编号由文档 owner 分配**，防撞号），格式：现象 → 根因 → 决策 → 话术。
4. 阶段级进展同步 docs/ROADMAP.md 进度表；命令与验收数字变化同步 docs/RUNBOOK.md。
5. commit + push（中文 message，阶段前缀如「S6：…」；**推送前必须 `git fetch` 并逐条确认 `origin/main..HEAD` 都是自己的提交**；推送偶发网络失败，重试即可）。

## 环境速查（本机 Windows，详见 docs/RUNBOOK.md）

- 用系统 Python 3.11（`.venv` 已坏，先 `deactivate`）；Git Bash 无 make、且**缺 coreutils**
  （`ls`/`cat`/`head`/`wc`/`grep` 全无）——文件与文本操作一律走 Python，别用 shell 小工具。
- 中文输出的脚本加 `-X utf8`。
- HF 下载走 `HF_ENDPOINT=https://hf-mirror.com` 且需浏览器 UA。
- 模型权重加载一律走 `mm_curation/gpt2_weights.py` 的 `ensure_local_gpt2()`（本地 safetensors；直接 `torch.load` .bin 会被 CVE-2025-32434 防护拒绝）。
- JSONL 读取一律 `read_text().split("\n")`，禁用 `splitlines()`（U+2028 陷阱，见笔记 #44）。
- `pytest` 必须带 `--basetemp`，且指向**尚不存在的、盘符形式**的目录（`/tmp/...` 会被 Windows Python 拼成 `C:\tmp\...`）。
  取准数用 `--junitxml` 落文件再解析，别数进度点。
- 数据 / 模型 / 报告产物不入库；报告用 RUNBOOK 命令重新生成。
