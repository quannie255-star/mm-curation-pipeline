# AGENTS.md — 开发须知

本仓库**单写者**开发（2026-09-30 起，原多 AI 并行模式已收工，见 `docs/ROADMAP.md` 第一节）。
任何 AI / 任何人进入本仓库，按以下顺序执行：

## 开工（必做）

0. **读 `docs/ROADMAP.md`** —— 项目已收敛成一条线（脏数据 → 可信训练数据，每道闸门可复跑）。
   动手前先答一句：**我要做的这一步，是让链路上哪个原本靠人判断的环节变成可复跑的门禁？**
   答不出来就别做。路线图第三节列的就是当前唯一允许推进的三件事。
1. 读 `docs/RUNBOOK.md` —— 命令与验收数字的唯一来源。
2. 非平凡任务先说清设计（要改哪些文件、接口是否变、验收命令是什么），**等用户确认再写码**。
   硬约束：不改 `Sample` / `Operator` 签名；既有 4 个 config 一字不动；测试基线只增不减。

## 收工（必做，缺一视为未完成）

1. **质量门全绿**：`python -X utf8 -m ruff check .` + `python -X utf8 -m pytest -q --tb=no`
   （主仓库与 `packages/curation-eval` 各跑一次）。测试基线：**主仓 624 + 包 67 = 691**，不得倒退
   （**改测试后实点回写**；`pytest` 必带 `--basetemp=` 指向盘符形式的全新目录，取准数用 `--junitxml`）。
2. **数字门禁全绿**：`python -X utf8 scripts/verify_claims.py`。
   任何对外数字只能改 `docs/claims.json`（唯一真相源），不许直接改文档里的字面量——
   门禁会把没跟上的文档逐条列出来。
3. **写本轮交接**：`docs/devlog/YYYY-MM-DD-<你的名字>.md`（做了什么 / 关键数字 / 改了哪些文件 / 下一个人需要知道的前提）。
4. **回写工程发现**：有面试价值的现象写进 `docs/ENGINEERING_NOTES.md`（追加编号，不许重排既有编号），
   格式：现象 → 根因 → 决策 → 话术。
5. 命令与验收数字变化同步 `docs/RUNBOOK.md`；阶段级进展同步 `docs/ROADMAP.md`。
6. commit + push（中文 message）。**推送前必须 `git fetch` 并逐条确认 `origin/main..HEAD` 都是自己的提交**；
   推送偶发网络失败，原样重试即可。**禁止** `git add -A` / `stash` / `checkout --` / `reset --hard` /
   `push --force` / `rebase` / `prune` / `gc --prune=now`。

## 环境速查（本机 Windows，详见 `docs/RUNBOOK.md`）

- 用系统 Python 3.11（`.venv` 已坏，先 `deactivate`）；Git Bash 无 make、且**缺 coreutils**
  （`ls`/`cat`/`head`/`wc`/`grep` 全无）——文件与文本操作一律走 Python，别用 shell 小工具。
- 中文输出的脚本加 `-X utf8`。
- HF 下载走 `HF_ENDPOINT=https://hf-mirror.com` 且需浏览器 UA。
- 模型权重加载一律走 `mm_curation/gpt2_weights.py` 的 `ensure_local_gpt2()`（本地 safetensors；直接 `torch.load` .bin 会被 CVE-2025-32434 防护拒绝）。
- JSONL 读取一律 `read_text().split("\n")`，禁用 `splitlines()`（U+2028 陷阱，见笔记 #44）。
- 数据 / 模型 / 报告产物不入库；报告用 RUNBOOK 命令重新生成。