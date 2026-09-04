# 任务清单 — V2 β 阶段：文本语料实例（✅ 已全部完成，2026-08-31）

> 设计见 docs/design_tables.md（五个决策点）。执行中的任务清单现以
> **docs/DEV_PLAN.md** 为准（γ/δ/ε 分解 + 开发日志）；本文件留档 β 任务对账。

- [x] T0 语料 spike：hf-mirror 实测后选维基 zh（MNBVC 分片过大弃用）（~3h）
- [x] T1 语料下载器：`data/text_sources.py` → data/raw/text_corpus.jsonl 302,002 篇（text_article 模态）
- [x] T2 文本污染器：paragraph_repeat / boilerplate_inject / pii_inject / whitespace_pad 进 curation-eval（对靶语义修复：整行复制/内部空白块）
- [x] T3 文本算子：doc_length / chinese_ratio / char_repetition / line_repetition / boilerplate / pii_detect 注册进主仓库 + 单测
- [x] T4 perplexity 算子：GPT-2 zh（`gpt2_weights.ensure_local_gpt2()` 权重入口，本地 safetensors 绕 CVE-2025-32434）
- [x] T5 去重基准：`dedup_fast` 向量化 MinHash-LSH + `text_dedup_benchmark.py` 四档（10 万档 exact 1.0 / near 0.9714 / 21.1s / RSS 825MB）
- [x] T6 文本训练对比：clean 7.16 vs dirty 7.70（+7.5% 超验收线；首跑阴性归因见笔记 #50）
- [x] T7 β 验收测试（B1-B4 + dedup_fast 5 条）+ 文档 + 全量回归推送（commit 73d0227）

## 完成定义（DoD）对账

- [x] 10 万文本文档走完整漏斗 → 超额：30.2 万全量跑通（302,002→181,980，保留 60.3%）
- [x] 去重基准四档扩展曲线 + P/R 三数字 → data/reports/text_dedup_benchmark.md
- [x] 文本模态训练级证据 → dirty_ft ppl +7.5%（>5% 验收线）
- [x] 全部测试绿 → 主仓库 129 + 包 29 全绿，ruff 干净
