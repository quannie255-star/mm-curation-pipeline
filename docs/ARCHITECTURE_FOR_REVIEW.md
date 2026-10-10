# mm-curation-pipeline 架构说明（供外部评审）

> **本文档的目的**：让没读过这份代码的人（尤其是别的大模型）能判断架构是否合理、哪里是瓶颈、
> 该怎么改。因此下面每一条结论都标注了**证据来源**（代码位置或实测数据），没有依据的部分我会明写"未验证"。
>
> **口径纪律**：项目里阴性结论（清洗让模型变差、Ray 跑不过单机、分类器泛化差）与阳性结论同等重要，
> 隐藏它们会让评审失效。规模差距（33 万篇 vs 业界 15T token）不粉饰。

---

## 0. 一句话概括

一个**多模态数据清洗漏斗**：30 个可插拔算子串成流水线，逐级打分/丢弃，每条丢弃都带归因与判决书；
支持本地串行与 Ray 分布式两种运行时。**当前阶段是一个"质量决策系统"，不是"训练数据生产系统"。**

---

## 1. 分层架构

```
                        configs/*.yaml  ← 阈值与算子组合全部外置（配置即契约）
                             │
                    PipelineConfig.from_yaml  ← 校验：算子已注册 / 模态交集非空 / runtime 合法
                             │
        ┌────────────────────┴────────────────────┐
        │        pipeline/runner.py  run_funnel    │  ← 唯一编排入口
        │  · pre_stages（改写通道，先于打分）      │
        │  · wrap_for_verdict（判决书包装器）      │
        │  · 模态不相交 → fail-fast                │
        └────────────────────┬────────────────────┘
                             │
        ┌────────────────────┴────────────────────┐
        │      Executor 协议（packages/curation-eval）│
        │   LocalSequentialExecutor  （默认，串行）  │
        │   RayDistributedExecutor   （runtime: ray）│
        └────────────────────┬────────────────────┘
                             │
        ┌────────────────────┴────────────────────┐
        │   Operator（单样本） / BatchOperator（跨样本）│  ← 30 个算子，registry 注册
        │   score() → meta["score:<name>"] → keep()  │
        └────────────────────┬────────────────────┘
                             │
                    FunnelResult{kept, dropped[(op, sample)], stats[StageStat]}
                             │
        ┌────────────────────┴────────────────────┐
        │  VerdictLedger  判决书（逐条可审计）       │
        │  quality/Scorecard、contamination/、eval/ │
        └─────────────────────────────────────────┘
```

**关键设计决策：协议下沉到独立包 `packages/curation-eval`**，主仓库 `src/mm_curation` re-export。
这样算子协议可独立测试、独立发版，不依赖主仓库的重依赖。

---

## 2. 核心抽象（请重点评审这一节）

### 2.1 Sample —— 统一数据单元

`packages/curation-eval/src/curation_eval/schema.py`

```python
@dataclass
class Sample:
    id: str
    modality: str        # text_article | image_caption | fhir_resource | industrial_sensor
    text: str = ""       # 文本正文
    meta: dict[str, Any] # 算子把分数写进 meta["score:<op>"]；判决书与报告复用
    # 模态特有字段（image_path / vector / sensor 读数等）
```

**设计要点**：`meta` 是**跨层共享的可变状态**。算子写分数进去 → `StageStat` 读出来算分布 →
漏斗报告读出来做阈值扫描 → 判决书读出来做证据。**一条通道，不用为每个消费者加字段。**

⚠️ **这是全项目最值得争议的设计**（见第 7 节 Q1）。

### 2.2 Operator —— 单样本算子

```python
class Operator(ABC):
    name: str = "operator"
    meta: Any = None          # OperatorMeta，@register_operator 注入

    def __init__(self, **params): self.params = params

    @abstractmethod
    def score(self, sample: Sample) -> float | None: ...   # 越高越好；None = 无法计分

    def explain(self, sample, score) -> dict[str, Any]:
        return {}            # 可选钩子：返回领域判据，进 verdict.evidence

    def keep(self, score) -> bool:
        if score is None: return True                      # ← 关键：无法计分 → 保留，不误杀
        lo, hi = self.params.get("min"), self.params.get("max")
        if lo is not None and score < lo: return False
        if hi is not None and score > hi: return False
        return True

    def __call__(self, sample) -> Sample | None:
        sample.meta[f"score:{self.name}"] = self.score(sample)   # 分数复用点
        return sample if self.keep(sample.meta[f"score:{self.name}"]) else None
```

**三个值得评审的设计**：

| 设计 | 意图 | 风险 |
|---|---|---|
| `score=None → keep` | "无法计分"不等于"坏数据"，宁漏勿错 | 算子静默失效时全部通过（假绿） |
| 阈值从 `params` 的 min/max 读，不写在算子内 | 换阈值 = 换配置文件 | 算子可能需要非单调阈值（见 7 节 Q2） |
| `__call__` 里写 meta 再判 | 分数可复用 → 换阈值不必重跑昂贵算子（CLIP 编码） | 分数是"上一次运行"的，配置变了可能是脏的（见 7 节 Q3） |

### 2.3 BatchOperator —— 跨样本算子

```python
class BatchOperator(Operator):
    def score(self, sample): raise TypeError(...)     # 显式禁止单样本调用
    def __call__(self, sample): raise TypeError(...)
    @abstractmethod
    def run_batch(self, samples: list[Sample]) -> list[Sample]: ...
```

**分流很干净**：单样本算子与跨样本算子在协议层就是两类，不会被误用。

### 2.4 Executor —— 执行器协议（**分布式扩展的墙在这里**）

```python
class Executor(ABC):
    @abstractmethod
    def run(self, ops, samples) -> FunnelResult: ...

    def reduce(self, shards: list[list[Sample]]) -> list[Sample]:
        raise NotImplementedError("分布式 reduce/shuffle 属二期（分布式去重），显式未实现")
```

**这是全项目最诚实也最致命的一处**：分布式去重**没有实现**，而且是**显式抛异常**而不是静默降级。

---

## 3. 算子清单（实测 30 个，`shardable` 是真实分布）

`shardable=False` = 需要全量视野 = **分布式下必须汇聚单点**。

| 模态 | 算子数 | 其中必须单点 |
|---|---|---|
| image_caption | 15 | **4** |
| text_article | 11 | **2** |
| industrial_sensor | 6 | **5** |
| fhir_resource | 5 | **2** |

**必须单点的 12 个算子**：`text_minhash`、`minhash_lsh`、`md5_exact`、`phash_near`、
`semantic_dedup`、`sensor_drift`、`sensor_stuck`、`sensor_multivariate`、
`fault_vs_maintenance`、`unit_consistency`、`temporal_consistency`、
`referential_integrity_fhir`

**可分片并行 18 个**：`doc_length`、`text_length`、`chinese_ratio`、`char_repetition`、
`line_repetition`、`boilerplate`、`pii_detect`、`perplexity`、`llm_judge`、
`aspect_ratio`、`blur`、`resolution`、`clip_alignment`、`wm_nsfw_cnn`、
`sensor_range`、`code_validity`、`phi_residual`、`unit_normalization`

**⚠️ 工业传感器模态最严重：6 个里 5 个必须单点**（漂移、卡死、多变量、故障分类、单位一致性
都需要跨时间序列的全局视野）。这类算子在 Ray 路线下**完全无法加速**。

### 算子注册机制的一个坑（实测发现）

```python
>>> from curation_eval import available_operator_metas
>>> available_operator_metas()      # 不 import 算子模块
0                                # ← 空列表！
>>> import mm_curation.operators   # 触发 @register_operator 副作用
30
```

注册靠 **import 副作用**。任何新调用方（外部 SDK、Notebook、新脚本）忘记 import 就得到
**空注册表 + "未注册的算子" 错误**，而不是一个明确的"你该 import 什么"。
建议评审：是否该改成显式 `register_all()`。

---

## 4. 去重实现（项目性能的核心，也是最复杂的部分）

### 4.1 `dedup_fast.py` —— 向量化 MinHash + banded LSH

**问题**：漏斗算子 `minhash_lsh`（datasketch 参考实现）逐文档算签名，10 万篇长文本需数小时。

**解法**：完全向量化。

```python
_PRIME = (1 << 31) - 1          # 31 位素数域，避免溢出
_WEIGHTS = np.array([1, 256, 65536, 16777216], dtype=np.uint64)   # 字节 4-gram 的滚动哈希

def _signature(text, prefix_chars, a, b):
    data = text.encode("utf-8")[:prefix_chars].ljust(4, b"\x00")
    arr = np.frombuffer(data, dtype=np.uint8)
    win = np.lib.stride_tricks.sliding_window_view(arr, 4).astype(np.uint64)
    h = win @ _WEIGHTS                                       # (n_shingles,) uint64
    # 31 位素数域：a < 2^31、h < 2^32 → a*h < 2^63，uint64 不溢出
    return ((a[:, None] * h[None, :] + b[:, None]) % np.uint64(_PRIME)).min(axis=1)
```

**效果**：30 万文档签名从小时级降到 ~30 秒。

**参数与 LSH 捕获率的关系**（源码注释里的实测记录，值得评审）：

- 80 签名分 8 band × 10 row。捕获率 = `1-(1-J^rows)^bands`
- rows=6 时：模板家族成员（两两 J≈0.75）共桶率 **0.18** → 家族桶反复突破 `max_bucket=2000`
  → 触发跳桶 → **桶内真近重复被连带牺牲**（β 基准实测 near 召回仅 **0.5**）
- rows=10 时：家族共桶率降到 **0.056** → 桶回到可复核规模，再靠加 band 把 J≈0.9 的
  真近重复捕获率拉回 ~0.95

**这是本项目最值得学习的一段**：超大分桶的破坏性是**级联**的（桶太大 → 跳桶 → 真重复被牵连），
而且只在模板化语料上暴露。修法不是调阈值，是**加一级精确签名预聚类**。

**"先到先保留"的确定性保证**（这里踩过一个隐蔽 bug）：

```python
# 固定「小索引做根」：先到先保留必须由合并方向保证。若让 y 做根，
# 注入样本（列表尾部的较大索引）会反过来当簇代表被保留，真源文档被丢
# ——先到先保留退化为随机保留（β 基准实测教训）
parent[max(rx, ry)] = min(rx, ry)
```

配合 `run_batch_mixed_modality` 里的 `items = sorted(items, key=lambda s: s.id)`：
**簇代表只依赖 id，不依赖输入序/分块序**。否则同一份配置在本地与 Ray（块序不保证）
会选出不同代表，跨运行时不可复现。

### 4.2 `dedup_incremental.py` —— 三层增量判重

面向"数据持续流入"场景：新样本到达立刻判定，不攒批重跑全量。

| 层 | 方法 | 复杂度 |
|---|---|---|
| 1 | md5 精确 | O(1) |
| 2 | pHash 感知（海明距离 ≤ threshold） | **线性扫**，万级可接受 |
| 3 | MinHash-LSH 文本（datasketch 原生 insert/query） | O(1) |

**journal 重放机制**：`{id, md5, phash, caption}` 追加式 JSONL。重启时重放重建三层索引，
**只依赖快照哈希，不需要读回图像**（MinHash 由 caption 现算）。

⚠️ 诚实边界（源码自己写了）：**多进程并发追加需文件锁或外置存储，单进程假设是硬约束**。

### 4.3 去重的准确率信号（实测数据，需要解释）

`data/reports/text_dedup_benchmark.json`：

| 规模 | near 召回 | **非注入样本被误合并** | 估计 Jaccard p50 |
|---|---|---|---|
| 50k | 0.9680 | 13,297 | 0.8375 |
| 100k | 0.9714 | 45,466 | 0.80 |
| 300k | 0.9658 | **96,396** | 0.80 |

300k 档把 **9.6 万条**（28.6%）非注入样本判成重复。两种解释，**尚未区分**：
1. MinHash 阈值 0.7 在中文文本上过于激进（Jaccard p50 = 0.80 说明大量真实相似度偏高）
2. 中文语料存在大量合法的模板化重复（同一模板改写的产品页/百科条目）

⚠️ **这个数字是本项目最大的未决风险**，见第 7 节 Q4。

---

## 5. 实测性能（`data/reports/scale_crossover.json`）

7 个算子（`doc_length`/`chinese_ratio`/`char_repetition`/`line_repetition`/`boilerplate`/
`pii_detect`/`text_minhash`）在本地 vs Ray 上的对照：

| 规模 | 本地串行 | Ray | Ray 中网络传输 | 结果 |
|---|---|---|---|---|
| 100k | 113.6s | 251.6s | 236.7s (94%) | **本地快 2.2 倍** |
| 300k | 355.8s | 490.5s | 480.0s (98%) | 本地快 1.4 倍 |
| 1m | 1605.5s | 3749.8s | 3737.1s (**99.7%**) | **本地快 2.3 倍** |

**Ray 在 100 万条规模内没有出现回本点。**

**根因（读代码定位）**：`ray_executor.py` 用 **Sample 完整对象**过 `ray.data`，
文本正文被 pickle 后跨进程搬运：

```python
sigs = np.stack([_signature(s.text, ...) for s in samples])   # samples 是 list[Sample]
```

`data/lake/` 已有 1137 个 Parquet 文件，但**正文走的是对象序列化，不是列式传输**。

**对照业界**（这三家都明确避免了这个坑）：
- **NiFi**：FlowFile 只传**指针 + 属性**，内容留在 content repository（WAL + copy-on-write）
- **Talend**：组件间传 **Avro record**（列式二进制），推荐 Kryo 序列化
- **FineDataLink**：Reader/Writer 插件抽象 + 简化版中间传输格式

**且 1m 档里 `text_minhash` 是 `shardable=False`** → Ray 路线下它**根本不能分片**，
必须先汇聚到单点再算。所以 Ray 版等于"把 77 万条正文搬过网络，然后在一个节点上算去重"。

---

## 6. 其余子系统（简述，便于整体评估）

| 模块 | 职责 | 规模 |
|---|---|---|
| `platform/` | 数据湖/仓库/作业编排/可观测（7 模块，最大 46KB modeling.py） | 视图定义是 `read_parquet` 硬编码本机绝对路径 |
| `agent/` | LLM 路由门禁（policy 488 行 / graph 304 / memory 231） | 4 档 `rule/perceptual/model/llm`，实测省 64.44% LLM 调用 |
| `eval/` | operator_pr / retrieval / decontam / metrics | — |
| `contamination/` | 去污染（base 131 / impl 220 行） | 去重之外的去污染 |
| `verdict/` | 判决书 ledger（250 行）+ recording | 逐条可审计 |
| `quality/` | Scorecard（279 行）/ report | — |
| `lineage/` | 血缘 contract（246）/ graph（218） | — |
| `extract/` | 正文抽取：trafilatura / rawdoc / heuristic / news_cn | 4 种抽取器 |
| `normalize/` | text_normalize（248）/ transformer（53） | **transformer 只能 1→1** |
| `synthesis/` | 数据合成 base（233）/ impl（294） | 协议层只支持 1→1，合成要 1→N = **需协议变更** |
| `serving/` | FastAPI api（221）+ quality_gate（61） | — |
| `tuning/` | preference（613）/ extraction（352）/ judge_cost | — |
| `data/connectors.py` | 597 行，多源连接器 | — |
| `warehouse/` | model（361）/ metrics（242） | — |

**入口**：`python -m mm_curation.cli`（463 行）。**无 `platform` 层的 CLI**（那是内部模块）。

**质量门**：`scripts/` 下 113 个脚本，含 `verify_claims.py`（19 条 claim + 87 条文档门面
+ 2 条派生计数的数字门禁）、`no_absolute_paths`、`encoding_hygiene`、三个变异测试脚本。

**测试**：主仓 760 + 包 67 = **827 条**（junitxml 实测）。

---

## 7. 请重点回答的 8 个问题

### Q1：`meta` 作为跨层可变状态，是好抽象还是定时炸弹？

现状：算子写 `meta["score:<op>"]`，`StageStat` 读、`StageStat` 报告读、判决书读、阈值扫描读。
好处是加消费者不用改算子。

风险：
- **分数是"上一次运行"的**。`runner.py` 注释说"换阈值重跑时分数可复用"——
  但如果同时改了**算子实现**（不是阈值），旧分数会被静默复用，产出错误结果且无告警。
- 进程内并发时 `meta` 无隔离。

**问**：应该给分数加版本/内容哈希（输入变了自动失效），
还是坚持"分数可复用"这条优化并在换算子时强制清空？

### Q2：阈值只能是 `min`/`max`，够用吗？

`keep()` 只支持区间。工业传感器的"卡死 vs 正常"、"漂移 vs 突变"、
"故障 vs 维护"这些本质是**条件组合**（如"值不变且方差=0 且持续>3 个采样周期"），
用区间表达不了。目前靠 `shardable=False` 的批量算子硬编码实现。

**问**：是否该给算子协议加一条**声明式规则**通道（表达式而非分数）？
还是保持"复杂逻辑写在批量算子里"？

### Q3：`score=None → 保留` 与"算子静默失效"如何区分？

`keep()` 对 `None` 返回 True。如果算子因为 bug 恒返回 None（例如读取了不存在的字段、
异常被吞），**整级会 100% 通过且报出"保留率 100%"** —— 完美的假绿。

**问**：该不该加一道"`None` 率超过阈值就 fail-fast 或告警"？
这会不会误伤"确实无法计分"的合法场景（如图片缺失导致 CLIP 算不了）？

### Q4：300k 档误合并 9.6 万条（28.6%），这是 bug 还是特性？

三种可能，本项目尚未区分：
1. MinHash 阈值 0.7 在中文上过低（估计 Jaccard p50 = 0.80 偏高）
2. 中文语料有大量合法模板化重复
3. union-find 传递闭包把中等相似样本串联成大簇（**级联合并**）

第 3 种最危险：簇的大小是平方级增长的（桶内 `combinations` 两两比较），
且 `max_bucket=2000` 只防住了超大桶，没防住"多次合并出的大簇"。

**问**：该怎么诊断？有没有低成本的方法区分"真重复"与"过度合并"？
（我倾向做**簇大小分布直方图** + **人工抽检 100 条**，但不确定这是不是最优。）

### Q5：分布式去重（`Executor.reduce()`）该怎么实现？

当前 `raise NotImplementedError`。约束：
- LSH 天然可分片（band hash 可分区）
- **但跨桶的近重复会漏检** —— 分桶边界把相似样本切开了
- 需要报"分桶后召回 vs 全局召回"的 gap，不能只报分桶后的数
- `data/lake/` 已是 Parquet，可以不传正文只传 `id + 签名`

**问**：有没有更简单的路线，比如
(a) 原地单机去重 + 只并行化前后两段；
(b) 按 band 分区但接受漏检，换取线性加速；
(c) 换成 Spark/Arrow 列式运行时（放弃当前 Sample 对象协议）？
还是说本项目规模（33 万篇）**根本不需要**做分布式去重，
应该把精力放在提高单机效率上？

### Q6：算子注册靠 import 副作用（不 import 得到空注册表），这是好设计吗？

实测：不 `import mm_curation.operators` 则 `available_operator_metas() == []`。

**问**：显式 `register_all()` 是不是必须的？还是说依赖 SDK 约定 import 是可接受的？

### Q7：工业传感器模态 6 个算子里 5 个必须单点，值得保留这个模态吗？

`industrial_sensor` 的算子（漂移、卡死、多变量、故障分类、单位一致性）
本质都需要跨时间序列的全局视野，`shardable=False` 是**语义要求**不是实现偷懒。

**问**：这类算子是否应该走**独立的时序存储/计算路径**（如按设备分区流式处理，
而不走"全量样本集合"的漏斗模型）？现有漏斗抽象是不是不适合时序模态？

### Q8：协议层 `Transformer` 只能 1→1，合成需要 1→N，是该改协议还是改用法？

`normalize/transformer.py` 只有 53 行，`synthesis/` 233+294 行。
ROADMAP 里把"数据合成与增强"列为唯一真缺口，红线是"合成样本必须走同一条漏斗"。

**问**：让合成样本走同一条漏斗，需要的协议变更是哪些？
（我看到至少需要：`Transformer` 支持 1→N、合成样本携带"来源样本 id"以便漏斗后仍可归因。
但这样一来漏斗的 `n_in` 语义就变了 —— 它不再等于"原始输入数"。这个歧义怎么处理？）

---

## 8. 已知的诚实边界（不用修，但请评审是否该改）

| 事实 | 状态 |
|---|---|
| 分布式去重未实现 | 显式 `NotImplementedError` |
| Ray 跑不过单机（1m 内无回本点） | 实测在案，未修 |
| 工业/时序算子无法分布式 | 语义要求 |
| 视图定义硬编码本机绝对路径 | 发布前必须物化 |
| 训练效用结论：**清洗让模型显著更差**（Δ = −0.195，是噪声地板 0.0437 的 4.46 倍） | 已修正判据后翻转，旧结论是恒假判据的产物 |
| 文本质量分类器 held-out 0.546、泛化 gap −0.381 | 判定 FAIL，已停止调参 |
| 真实 COCO 丢弃 2.16% 里只有 20% 是硬事实（过度清洗 5.6 倍） | 实测在案 |
| 协议层合成只支持 1→1 | ROADMAP 认定的唯一真缺口 |

---

## 9. 如果只能改三件事

如果评审者时间有限，请优先给这三条的意见：

1. **Q5 —— 分布式去重要不要做、怎么做**（决定项目定位：大规模分布式 vs 单机精做）
2. **Q4 —— 28.6% 误合并是 bug 还是特性**（决定去重算法要不要重做）
3. **Q1 —— `meta` 复用机制的风险是否可控**（决定整个算子协议能不能长期演进）

---

## 附：文档准确性声明

本文件中的所有数字与代码行为均在 2026-10-09 由实测得出（脚本现取，非手抄）：
算子清单与 `shardable` 分布来自 `available_operator_metas()` 实跑；
性能数字来自 `data/reports/scale_crossover.json` 与 `text_dedup_benchmark.json`；
"注册返回 0 个"来自实跑复现。

**代码位置引用均可核对**：`packages/curation-eval/src/curation_eval/`（协议层）、
`src/mm_curation/pipeline/`（编排）、`src/mm_curation/dedup_fast.py`（去重）、
`src/mm_curation/operators/`（算子）、`data/reports/`（实测报告）。

---

## 12. 第三轮外部评审（SOTA 对标）的处置与落地状态

> 评审来源：DeepSeek 对标 Data-Juicer / NeMo Curator / DolphinDB / DeltaCAT / EcoDatum /
> DataFlow 的六条差距分析（2026-10-09）。处置人：glm。纪律沿用 `REVIEW_DISPOSITION.md`：
> 评审给的方案先在代码里核实，错的指出错在哪。

### 12.1 六条差距的处置总表

| # | 评审主张 | 处置 | 证据 / 落点 |
|---|---|---|---|
| 1 执行引擎：对象序列化 vs 列式 | 方向**采纳**，按三段式拆解而非一步换传输 | **第一步已落地** | `dedup_fast.compute_signatures()` 拆出独立可测的签名计算（2026-10-09）——并行清洗顺带算签名、集中去重只传 id+签名，这是「id+字段引用」模式的最小可测形态；列式（Arrow/ray.data）按处置记录 §4 排最后（前两步后数据量已小一个量级） |
| 2 去重：算法 vs 系统 | **部分已完成，部分待拍板** | 簇大小直方图 / 桥接率 / num_perm 扫描**上轮已实测**（REVIEW_DISPOSITION §1：最大簇=2、桥接率 0%、num_perm 80→320 误判 −53%）；本轮新增**合成豁免**（§12.3）；num_perm 80→160 默认值变更**待拍板**（会动 benchmark 基线数字）；EcoDatum 质量引导去重记为方向 |
| 3 时序：漏斗 vs 流式 | 方向**采纳，本轮不动手** | 双范式（`StreamingExecutor`）列 R2+ 设计门；前置判断未决：工业传感器是否核心业务（REVIEW_DISPOSITION §6-2）。诚实边界：本项目定位离线质量，流式是"明确不做"清单的候选而非缺口 |
| 4 合成 1→N | **采纳其更优方案并已基本落地** | `synthesis/` 计划级 1→N（协议零变更）+ labels 溯源三元组（synthesized_by/source_id/clean）已在；本轮补上评审点名的最后一块：**去重与合成的互作**——`dedup_texts(protect_synthetic=True)` 豁免「合成样本 ↔ 其声明源」的合并（否则增强对照被洗掉），四场景测试锁定语义（源豁免/无关照常/合成集内照常/opt-out 可关） |
| 5 自动调参 Agent | **承认差距，记 backlog** | 已有基础：`threshold_scan` 的召回-误杀曲线 + claims 门禁。Recommendation Agent 不承诺（无 LLM 服务预算；离线红线） |
| 6 下游效用闭环 | **部分已答，训练级在途** | 见 §12.2 |

### 12.2 「判据修正后的翻转是否在下游效用上验证过」——正面回答

评审问得对：**没有在训练级验证过**。当前的因果证据链是：

1. 随机删同比例对照：清洗 R@1 0.575 vs 随机删 0.444（净贡献 +0.131~+0.162）；
2. 脏残留剂量-响应：残留 486→0.413 / 363→0.444 / 0→0.575，单调；
3. 微调级：干净 0.688 vs 脏 0.636 vs 不训 0.556（单 seed，PROOF_CHAIN §九）。

这三条证明的是「**删掉的是脏样本**」与「**脏数据伤模型**」，但「修正后的清洗策略
在训练效用上优于旧策略」确实是**训练级实验未覆盖的空白**——专用工具
`scripts/eval_training_utility.py` 已在途（本工作区未提交状态）。在此之前，
对外口径维持 PROOF_CHAIN §九第 9 条：「单 seed 观测，无训练方差估计」。

### 12.3 对评审本身的两处核实（沿用「评审不是权威」纪律）

- 「Ray 99.7% 耗时花在网络上」：方向对（Ray 负收益实测 2.3×，scale_crossover.json），
  **99.7% 这个分解数字未在本仓实测过**，引用时用「对象序列化开销使 Ray 慢 2.3×」。
- 「28.6% 误合并」：来源未核对（REVIEW_DISPOSITION §5：与本轮 2280 篇语料的
  2.98% 合并率语料不同、口径不同），**在核对前不作为已确认缺陷规模对外引用**。
- DeepSeek 的合成方案（meta.origin + source_ids、不改协议）与本仓实现一致
  （labels 溯源三元组，语义等价），其「去重互作要写进协议」的提醒是本轮真正
  的新增价值——已实现。
