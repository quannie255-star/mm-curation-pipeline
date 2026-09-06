# tasks.md — 原子任务拆解（现行模块：θ 偏好判官工坊）

> 按 docs/AI_CODING_PROTOCOL.md 生成：设计表（design_tables.md θ 节）经用户确认后拆任务，
> 逐任务交付「代码 + pytest 证据 + 状态报告」。历史模块任务清单验收后归档 docs_archive/，
> 本文件只保留现行模块。

## θ 层序声明

数据逻辑层 → CLI 脚本层 → UI 层 → 文档回写层（下层依赖上层，不跨层跳写）。

## θ0 设计门

- [x] 设计表入 docs/design_tables.md θ 节，用户确认（2026-09-06，commit 1068e34）

## θ1 数据逻辑层（src/mm_curation/tuning/preference.py 增量）

- [x] v2 标注行工厂 `make_label_row()`（全文 + 变体元数据 + labeler）
- [x] 示例语料装载 `load_news_corpus_excluded()`（结构性排除 judge/pref/ext 全部既有占用）
- [x] 模拟用户生成器 `oracle_labels_from_corpus()`（labeler=oracle，两本账通道之一）
- [x] 数据构造 `build_user_pref_data()`：最小对三元组 / 按对 holdout / REJECT 剔除进统计 /
      训练对照 + 评测对照题 / 协议一致性校验
- [x] 冻结 `write_user_benchmark()`（manifest 含 labeler / 协议原文 / 泄漏检查）
- [x] 单测 tests/test_pref_user.py（≥7 条：oracle 规则 / 最小对 / holdout 不相交 /
      REJECT 剔除 / 对照题金标 / 不足与协议不一致退出 / manifest 字段）

## θ2 CLI 脚本层

- [x] `scripts/build_user_pref_data.py`：--labels/--out-dpo/--out-benchmark/--holdout/--limit；
      标注不足或协议不一致 exit 2
- [x] `scripts/build_oracle_labels.py`：模拟用户标注生成（独立文件，不与真人标注混）
- [x] `run_pref_benchmark.py` 报告按 benchmark 名落盘（pref_alignment_<name>.json，
      防向导评测覆盖 η-a 报告）

## θ3 UI 层（scripts/judge_studio.py 五步向导）

- [x] ①导入：粘贴/上传 txt·md / 一键示例语料 → S/F 变体对生成 + 可用率报告
- [x] ②标注：甲/乙/都不合格 大按钮 + 进度条（建议 150 对）→ v2 文件逐条落盘
- [x] ③训练：GPU 明示 → subprocess 现有 DPO 脚本（全默认参数）+ 实时日志 tail
- [x] ④评测：subprocess 冻结评测（+通用基线）→ 对比出分页
- [x] ⑤试用：贴任意 甲/乙 对 → 判官裁决（adapter 缓存加载，未训练时明示为通用基线）
- [x] 冒烟：headless 启动 HTTP 200

## θ3.5 流程验收（模拟用户账）

- [x] oracle 250 条首训未达标（0.532≈通用，欠训练）→ 加量 550 条（536 三元组，
      冻结考卷不变）→ **main 0.839 ≥0.75 达标**（通用 0.532 / 对照 0.80 vs 0.40）

## θ4 文档回写层

- [ ] RUNBOOK θ 段（含学习曲线实验命令）/ DEV_PLAN 日志 / ROADMAP 进度
- [ ] 质量门全绿（ruff + 主仓 + 包）→ commit + push
