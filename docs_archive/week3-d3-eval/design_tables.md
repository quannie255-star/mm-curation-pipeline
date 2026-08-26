# 设计表 — Week3 D3：脏索引 vs 净索引检索评测（项目灵魂实验）

> 对比实验：同一查询集在 clean_v2 / dirty_raw 两个索引上的检索质量，
> 把"清洗的价值"从定性证据（ENGINEERING_NOTES #21）变成量化指标。

## 1. 数据结构表

### 1.1 QuerySpec（评测查询，jsonl）

| 字段 | 类型 | 说明 |
|---|---|---|
| query_id | str | = 目标样本 id（一图一查询） |
| text | str | 查询文本：优先 meta.extra_captions[0]（held-out，未参与索引构建侧 caption 选择），否则回落 caption（自检索） |
| origin | str | `held_out` / `self`（两类分开统计，自检索机制上偏乐观，须显式标注） |
| target_id | str | ground truth 图像样本 id |

### 1.2 EvalResult（单索引指标，JSON）

| 字段 | 类型 | 说明 |
|---|---|---|
| index | str | 索引名 |
| n_queries | int | 查询数（按 origin 分列时另计） |
| recall_at_k | dict[int, float] | K=1/5/10：target 出现在 top-K 的查询占比 |
| mrr | float | 平均倒数排名（target 首次命中排名的倒数均值） |
| per_origin | dict[str, dict] | held_out / self 两类各自的上述指标 |

### 1.3 ComparisonReport（对比汇总，JSON + Markdown）

| 字段 | 类型 | 说明 |
|---|---|---|
| clean / dirty | EvalResult | 两侧完整指标 |
| delta | dict | recall@k 与 mrr 的差值与相对提升百分比 |
| n_queries / origin 构成 | — | 实验规模与查询来源透明化 |

## 2. 接口约定表

### 2.1 模块（eval/retrieval.py）

| 函数 | 入参 | 出参 | 说明 |
|---|---|---|---|
| build_queries(samples) | 净索引样本列表 | list[QuerySpec] | 有 extra_captions 用之，否则回落 caption |
| recall_at_k / mrr | (rankings: list[int\|None], k) | float | 纯函数，rank=None 表示未命中 |
| evaluate(searcher, queries, query_vecs, k_list) | 查询器+向量 | EvalResult | 批量向量直查（见流转表） |

### 2.2 CLI（scripts/eval_retrieval.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| --indexes | clean_v2 dirty_raw | 参与对比的索引名列表 |
| --queries-from | clean_v2 | 查询集来源索引（其 store 的干净样本+extra_captions） |
| --out | data/reports/retrieval_eval.json | 报告输出 |
| 退出码 | 0 / 1(索引缺失) / 2(评测异常) | |

## 3. 流转表与关键设计决策

### 3.1 评测流水

```
净索引 store 构建查询集（一图一查询）
  → 全部查询文本一次性批量编码（复用编码器单例，避免逐条 tokenize）
  → 逐索引：批量向量检索 top-max(K) → 逐查询找 target 排名 → 指标聚合
  → 对比报告（JSON + Markdown 表）
```

### 3.2 公平性决策（面试考点，需在门审确认）

1. **同查询同目标**：查询集只含 clean_v2 的样本——它们在 dirty_raw 中同样存在
   （脏索引是净索引 + 486 注入的超集），两侧 target 均可达，差异只来自污染
2. **held_out 优先**：extra_captions 未参与任何索引构建侧的 caption 字段，
   是真正的泛化查询；self（自检索）单独统计并标注偏乐观
3. **L1 层小扩展（需确认）**：IndexSearcher 增加公开方法
   `search_many_by_vectors(vectors, top_k)`（原 `_search_vec` 的批量版），
   评测批量查询不逐条走 HTTP/text 编码路径——修改已确认层，按协议报备
4. **排名并列**：FAISS 分数并列时 target 排名取最坏位次（保守口径）
