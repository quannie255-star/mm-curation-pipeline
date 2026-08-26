# 设计表 — Week3 D4：算子级 P/R 独立评测 + 阈值敏感性曲线

> 目标：回答"11 级漏斗里每个算子各自贡献了什么、成本多少、阈值还有没有优化空间"。
> 口径根基（ENGINEERING_NOTES #9）：**每个算子在全量脏集上独立评**，不穿漏斗——
> 否则上游算子"抢功"，下游算子的真实能力被掩盖。

## 1. 数据结构表

### 1.1 OperatorEvalResult（单算子评测，JSON 节点）

| 字段 | 类型 | 说明 |
|---|---|---|
| op | str | 算子名 |
| target_kinds | list[str] | 设计靶子（映射表 1.2），用于"靶子召回"视角 |
| n_input | int | 输入样本数（=全量脏集） |
| n_dropped | int | 该算子独立丢弃数 |
| precision | float | TP/(TP+FP)：丢弃中真脏占比 |
| recall_all | float | 对全部脏数据的召回 |
| recall_target | float | 只对靶子类型的召回（设计符合度） |
| false_kills | int | 丢弃中的干净样本数（误杀绝对数） |
| per_kind_dropped | dict[str,int] | 每类脏数据被该算子丢弃的数量（捕获矩阵） |
| seconds | float | 独立运行耗时（成本视角，JD 加分项） |

### 1.2 算子 → 设计靶子映射（模块内常量，可被 YAML 覆盖）

| 算子 | target_kinds |
|---|---|
| text_length / chinese_ratio / char_repetition | [low_quality_text] |
| resolution / aspect_ratio / blur | [blur, low_resolution] |
| md5_exact | [exact_duplicate] |
| phash_near | [near_duplicate_image] |
| minhash_lsh | [near_duplicate_text] |
| clip_alignment | [mismatched_pair, nsfw_placeholder, low_quality_text] |
| semantic_dedup | [semantic_duplicate] |

### 1.3 SweepPoint / SweepResult（阈值扫描）

| 字段 | 类型 | 说明 |
|---|---|---|
| value | float | 阈值取值 |
| precision / recall_all / recall_target | float | 该阈值下的指标 |
| n_dropped / false_kills | int | 丢弃数 / 误杀数 |

SweepResult = {op, param, points[]}，附带拐点标注（当前配置值 vs 帕累托前沿）。

## 2. 接口约定表

### 2.1 模块（eval/operators.py）

| 函数 | 入参 | 出参 | 说明 |
|---|---|---|---|
| evaluate_operator(samples, spec) | 全量脏集 + OperatorSpec | OperatorEvalResult | 单样本算子放宽阈值跑一遍（分数落 meta）后按阈值判定；批量算子 run_batch 差集 |
| evaluate_all(samples, config) | 配置里的全部算子 | list[Result] | 逐个独立评（每次用深拷贝样本，防 meta 串扰） |
| sweep_threshold(samples, spec, values) | 候选阈值列表 | SweepResult | 单样本算子分数复用（一次打分多次判定）；批量算子逐值重跑（哈希不缓存，时长标注） |

### 2.2 CLI（scripts/eval_operators.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| --input | data/interim/contaminated/samples.jsonl | 全量脏集 |
| --config | configs/pipeline.example.yaml | 被评算子清单与当前阈值 |
| --sweep | （空） | 逗号分隔的扫描算子名单，如 `blur,phash_near,clip_alignment` |
| --out | data/reports/operator_eval.json | 报告路径（+.md+曲线 PNG） |
| 退出码 | 0 / 1(输入缺失) / 2(评测异常) | |

## 3. 流转表与关键决策

### 3.1 评测流水

```
全量脏集（2106）→ 逐算子独立评（深拷贝隔离）
  ├─ 单样本算子：放宽阈值跑 __call__（全过，分数落 meta）→ 按真实阈值判定
  └─ 批量算子：run_batch → 输入/输出差集 = 丢弃集
→ P/R（总体 + 靶子）+ 捕获矩阵 + 耗时 → JSON/Markdown
→ （可选）阈值扫描：单样本算子零成本复用分数；批量算子逐值重跑
```

### 3.2 需门审确认的决策

1. **P/R 定义在"丢弃"行为上**（drop=positive）：precision=丢得准，recall=抓得全。
   这是数据清洗业务的本位视角
2. **独立评测用深拷贝**：算子会往 meta 写分数，逐算子隔离防止跨算子污染
3. **recall_target 的分母是靶子类型的全部注入数**（如 phash 的 31），即使别的算子
   也能抓它们——衡量"这个算子对它的设计问题解得多好"
4. 曲线 PNG 用 matplotlib（中文标签用默认字体渲染为方块亦可接受，轴标签用英文，
   图内不算交付物重点；表格 Markdown 才是）
