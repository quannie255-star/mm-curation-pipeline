# 任务清单 — V2 β 阶段：文本语料实例

> 设计见 docs/design_tables.md（五个决策点）。T0 spike 是一切的前提。

- [ ] T0 语料 spike：hf-mirror 实测 MNBVC 文件清单/schema/下载速度 + 维基 zh 备选确认；产出选型结论（~3h，无依赖）
- [ ] T1 语料下载器：`data/text_sources.py`（镜像+UA+重试+断点，10 万文档 → data/raw/text_corpus.jsonl，text_article 模态）（~5h，依赖 T0）
- [ ] T2 文本污染器：paragraph_repeat / boilerplate_inject / pii_inject / whitespace_pad 进 curation-eval（requires_image=False）+ 单测（~5h，无依赖，可与 T1 并行）
- [ ] T3 文本算子：doc_length / line_repetition / boilerplate / pii_detect 注册进主仓库（text_article 模态）+ 单测（~5h，依赖 T0 选型定长度阈值）
- [ ] T4 perplexity 算子：GPT-2 zh 困惑度打分 + 参考分布校准说明 + 单测（mock 优先）（~6h，依赖 T3）
- [ ] T5 去重基准：`scripts/text_dedup_benchmark.py`（四档规模吞吐/内存/P/R + 扩展曲线）（~6h，依赖 T1/T2）
- [ ] T6 文本训练对比：`scripts/finetune_gpt2.py`（clean/dirty 等步数微调 + held-out ppl 对比报告）（~8h，依赖 T1/T4）
- [ ] T7 β 验收测试 + 文档（design 验收四条 + RUNBOOK/ROADMAP 同步）+ 全量回归推送（~4h，依赖 T3-T6）

## 完成定义（DoD）

- [ ] 10 万文本文档走完整漏斗（零框架特例；A5 单一来源守卫通过）
- [ ] 去重基准四档扩展曲线 + P/R 三数字
- [ ] 文本模态训练级证据：dirty_ft 的 held-out ppl 显著高于 clean_ft
- [ ] 全部测试绿（主仓库 112+，包 29+）
