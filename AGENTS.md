# AGENTS.md — AI 协作开发须知

本仓库由多名 AI 协作开发。**任何 AI 进入本仓库，按以下顺序执行：**

## 开工（必做）

1. 读 **docs/DEV_PLAN.md**——当前状态快照、下一阶段任务分解（γ→δ→ε）、协作硬规则都在那里。它是唯一事实源。
2. 非平凡任务先走设计门：把设计表写进 docs/design_tables.md，等用户确认再写码（细则见 docs/AI_CODING_PROTOCOL.md）。
3. 任务清单如果与 DEV_PLAN.md 冲突，以 DEV_PLAN.md 为准。

## 收工（必做，缺一视为未完成）

1. **质量门全绿**：`python -m ruff check .` + `python -X utf8 -m pytest -q --tb=no`（主仓库与 packages/curation-eval 各跑一次）。测试基线：主仓库 129 + 包 29，不得倒退。
2. **回写 docs/DEV_PLAN.md**：更新任务状态 + 「开发日志」表加一行（日期 / 内容 / 关键数字 / commit）。
3. **有面试价值的现象**（诡异 bug、反直觉结论、关键抉择）写进 docs/ENGINEERING_NOTES.md，格式：现象 → 根因 → 决策 → 话术。
4. 阶段级进展同步 docs/ROADMAP.md 进度表；命令与验收数字变化同步 docs/RUNBOOK.md。
5. commit + push（中文 message，阶段前缀如「V2 γ：…」；推送偶发网络失败，重试即可）。

## 环境速查（本机 Windows，详见 docs/RUNBOOK.md）

- 用系统 Python 3.11（`.venv` 已坏，先 `deactivate`）；Git Bash 无 make。
- 中文输出的脚本加 `-X utf8`。
- HF 下载走 `HF_ENDPOINT=https://hf-mirror.com` 且需浏览器 UA。
- 模型权重加载一律走 `mm_curation/gpt2_weights.py` 的 `ensure_local_gpt2()`（本地 safetensors；直接 `torch.load` .bin 会被 CVE-2025-32434 防护拒绝）。
- JSONL 读取一律 `read_text().split("\n")`，禁用 `splitlines()`（U+2028 陷阱，见笔记 #44）。
- 数据 / 模型 / 报告产物不入库；报告用 RUNBOOK 命令重新生成。
