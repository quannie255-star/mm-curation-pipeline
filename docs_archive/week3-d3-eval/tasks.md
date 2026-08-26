# 任务清单 — Week3 D3：检索对比评测

> 层序：**L1 指标层（纯函数）→ L2 实验层（查询构造+批量评测）→ L3 报告层（CLI+真数据+归档）**

| # | 任务 | 层 | 产物 | 预估行数 | 依赖 | 状态 |
|---|---|---|---|---|---|---|
| T1 | 指标纯函数：recall_at_k / mrr + 测试 | L1 | `src/mm_curation/eval/metrics.py`, `tests/test_metrics.py` | ~80 | design 1.2 | ✅ 完成 |
| T2 | 查询构造 + 批量评测 + IndexSearcher 批量向量方法 + 测试 | L2 | `src/mm_curation/eval/retrieval.py`, searcher 增补, `tests/test_retrieval.py` | ~150 | T1 + design 3.2 | ✅ 完成（设计偏差报备：查询集从 manifest.source_jsonl 构造，extra_captions 在样本 meta 不在索引 store） |
| T3 | CLI + 真实双索引跑分 + Markdown 报告 + 命令归档 | L3 | `scripts/eval_retrieval.py`, `data/reports/`, `docs/test_cases.md` | ~80 | T2 | ✅ 完成（**R@1 0.459→0.556 相对+21%**，held_out/self 退化一致） |

## 验收标准（模块级）
1. 一张对比表：净 vs 脏的 Recall@1/5/10 + MRR（总览 + held_out/self 分列）
2. 结论一句话可写进 README：「清洗使 Recall@10 从 A → B（+C%）」
3. 全部测试绿（预计 60 → 68+）
