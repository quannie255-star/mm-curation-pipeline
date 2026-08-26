# 任务清单 — Week3 D4：算子级 P/R 评测 + 阈值敏感性

> 层序：**L1 指标层（P/R 纯函数+映射表）→ L2 执行层（独立评测+扫描）→ L3 报告层（CLI+真数据+归档）**

| # | 任务 | 层 | 产物 | 预估行数 | 依赖 | 状态 |
|---|---|---|---|---|---|---|
| T1 | P/R 纯函数 + 靶子映射表 + 测试 | L1 | `src/mm_curation/eval/operator_eval.py`, `tests/test_operator_eval.py` | ~90 | design 1.1/1.2 | ⬜ |
| T2 | 独立评测执行 + 阈值扫描（分数复用）+ 测试 | L2 | 同文件增补, `tests/test_operator_eval.py` 增补 | ~120 | T1 | ⬜ |
| T3 | CLI + 真数据跑分 + Markdown/PNG 报告 + 归档 | L3 | `scripts/eval_operators.py`, `data/reports/`, `docs/test_cases.md` | ~90 | T2 | ⬜ |

## 验收标准
1. 一张表：11 个算子的 precision / recall_all / recall_target / 误杀 / 耗时
2. 捕获矩阵：哪类脏数据被谁抓住（一石三鸟的算子直接可见）
3. 至少 3 个算子的阈值曲线 + 拐点 vs 当前配置的对比结论
4. 全部测试绿（预计 67 → 75+）
