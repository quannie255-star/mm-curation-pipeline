# 评审请求（可直接整段复制给其他大模型）

---

我维护一个开源数据清洗项目，需要你做架构评审。**你没有读过代码，以下是完整的设计信息**。

## 项目是什么

一个多模态数据清洗漏斗：30 个可插拔算子串成流水线，逐级打分/丢弃，每条丢弃带归因与判决书；
支持本地串行 + Ray 分布式两种运行时。规模：文本 33 万篇、图文 1620 条（COCO）。
模型侧只有 tiny-gpt2 / FakeEncoder CLIP（**这是演示级规模，不与业界 15T token 量级对比**）。

## 核心抽象（请评审这些设计本身）

### Sample
```python
@dataclass
class Sample:
    id: str
    modality: str   # text_article | image_caption | fhir_resource | industrial_sensor
    text: str = ""
    meta: dict[str, Any]   # 算子写 meta["score:<op>"]；Stat/报告/判决书/阈值扫描都读它
```

### Operator（单样本）
```python
def score(self, sample) -> float | None: ...    # 越高越好；None = 无法计分
def keep(self, score) -> bool:
    if score is None: return True                # 无法计分 → 保留，不误杀
    lo, hi = self.params.get("min"), self.params.get("max")
    if lo is not None and score < lo: return False
    if hi is not None and score > hi: return False
    return True
def __call__(self, sample):
    sample.meta[f"score:{self.name}"] = self.score(sample)
    return sample if self.keep(...) else None
```

### Executor（分布式扩展的墙）
```python
class Executor(ABC):
    @abstractmethod
    def run(self, ops, samples) -> FunnelResult: ...
    def reduce(self, shards) -> list[Sample]:
        raise NotImplementedError("分布式 reduce/shuffle 属二期（分布式去重），显式未实现")
```

### 算子分片属性的真实分布（30 个算子实测）
- `shardable=True`（可分片并行）**18 个**：doc_length / text_length / chinese_ratio /
  char_repetition / line_repetition / boilerplate / pii_detect / perplexity / llm_judge /
  aspect_ratio / blur / resolution / clip_alignment / wm_nsfw_cnn / sensor_range /
  code_validity / phi_residual / unit_normalization
- `shardable=False`（需全量视野，分布式下必须单点）**12 个**：text_minhash / minhash_lsh /
  md5_exact / phash_near / semantic_dedup / sensor_drift / sensor_stuck / sensor_multivariate /
  fault_vs_maintenance / unit_consistency / temporal_consistency / referential_integrity_fhir

按模态：image_caption 15 个中 4 个必须单点；text_article 11 个中 2 个；
**industrial_sensor 6 个中 5 个必须单点**（时序算子本质要跨序列全局视野）。

## 两个实测事实（不是我的猜测）

### 1. Ray 分布式跑不过单机
7 算子（6 个单样本 + text_minhash），本地串行 vs Ray：

| 规模 | 本地 | Ray | 其中网络传输 |
|---|---|---|---|
| 100k | 113.6s | 251.6s | 236.7s (94%) |
| 300k | 355.8s | 490.5s | 480.0s (98%) |
| 1m | 1605.5s | 3749.8s | 3737.1s (**99.7%**) |

**1m 内 Ray 无回本点。** 根因：Ray 执行器用 Sample 完整对象过 ray.data，正文被 pickle 跨进程搬运。
且 text_minhash 是 shardable=False，Ray 路线下它不能分片。
（对照：NiFi 只传指针+属性、内容留本地；Talend 传 Avro 列式二进制；data/lake 已有 1137 个 Parquet 但没走列式。）

### 2. 去重在 300k 档误合并 28.6% 非注入样本
| 规模 | near 召回 | 非注入样本被误合并 | 估计 Jaccard p50 |
|---|---|---|---|
| 50k | 0.968 | 13,297 | 0.8375 |
| 100k | 0.971 | 45,466 | 0.80 |
| 300k | 0.966 | **96,396** | 0.80 |

去重算法：向量化 MinHash（字节 4-gram shingle + 60 个哈希函数）+ banded LSH
（80 签名 / 8 band × 10 row）+ 精确签名预聚类 + union-find 传递闭包 + max_bucket=2000 跳桶保护。
确定性靠 `sorted(items, key=lambda s: s.id)` + 合并时固定"小索引做根"。

三种可能我尚未区分：①阈值 0.7 在中文上过低 ②中文有大量合法模板化重复
③union-find 传递闭包把中等相似样本串成大簇（**级联合并**）。

## 请回答这 8 个问题（按重要性排序）

**Q5（最关键）分布式去重要不要做、怎么做？**
约束：LSH 天然可分片（band hash 可分区），但跨桶近重复会漏检。
选项：(a) 原地单机去重 + 只并行化前后两段 (b) 按 band 分区接受漏检换线性加速
(c) 换 Spark/Arrow 列式运行时，放弃当前 Sample 对象协议 (d) **33 万篇规模根本不需要分布式去重，
应该提高单机效率**
—— 我倾向 (d) 但不确定。请给出判断依据。

**Q4 28.6% 误合并是 bug 还是特性？怎么低成本诊断？**
我倾向做簇大小分布直方图 + 人工抽检 100 条，但不确定是否最优。特别问：
union-find 传递闭包在这里的级联风险有多大，`max_bucket` 只防超大桶、没防"多次合并出的大簇"，够吗？

**Q1 `meta` 作为跨层可变共享状态，是好抽象还是定时炸弹？**
现状：算子写分数，Stat/报告/判决书/阈值扫描都读它，好处是加消费者不用改算子。
风险：`runner.py` 注释说"换阈值重跑时分数可复用"，但如果同时改了**算子实现**（不是阈值），
旧分数被静默复用 → 错误结果且无告警。
该给分数加内容哈希自动失效，还是坚持复用并在换算子时强制清空？

**Q3 `score=None → 保留` 与"算子静默失效"如何区分？**
如果算子因 bug 恒返回 None，整级会 100% 通过并报"保留率 100%"—— 完美假绿。
该不该加"None 率超阈值就告警/fail-fast"？会不会误伤合法场景（图片缺失导致 CLIP 算不了）？

**Q2 阈值只能是 min/max 够用吗？**
工业传感器的"卡死""漂移""故障 vs 维护"本质是条件组合（值不变 + 方差=0 + 持续>3 周期），
区间表达不了，现在靠 shardable=False 的批量算子硬编码。
该给协议加声明式规则通道（表达式而非分数）吗？

**Q6 算子注册靠 import 副作用（不 import 得到空注册表 + "未注册"错误）是好设计吗？**
该改成显式 `register_all()` 吗？

**Q7 industrial_sensor 模态 6 个里 5 个必须单点，这个模态该保留吗？**
这类算子（漂移/卡死/多变量/故障分类/单位一致性）本质要跨时间序列全局视野，
shardable=False 是语义要求不是偷懒。
该走独立时序路径（按设备分区流式）而不是"全量样本集合"的漏斗模型吗？现有漏斗抽象不适合时序模态？

**Q8 协议层 Transformer 只能 1→1，数据合成需要 1→N，怎么改？**
红线是"合成样本必须走同一条漏斗"。
我看到至少需要：Transformer 支持 1→N + 合成样本携带来源 id 以便漏斗后仍可归因。
但这样漏斗的 `n_in` 语义就变了（不再等于原始输入数）。这个歧义怎么处理？

## 评审时请特别挑战这些假设

1. **"协议下沉到独立包 curation-eval，主仓库 re-export"** —— 这个拆分是必要的还是过度设计？
   算子协议只有 204 行 sdk.py，是否值得一个独立包？
2. **`Operator` 与 `BatchOperator` 二分**是否够？真实需求里有没有第三类
   （如"需要有限窗口视野"的滑窗算子，介于单样本与全量之间）？
3. **配置即契约（阈值全在 YAML）** —— 30 个算子的参数类型无 schema 校验，
   只有 `min`/`max` 被 `keep()` 读。写错参数名会怎样？
4. 我们引入了 19 条 claim + 87 条文档门禁 + 变异测试来保证"数字可信"。
   这个投入相对"把清洗做得更干净"是否失衡？
5. **规模定位**：33 万篇 / tiny-gpt2 明显是演示级。这个规模下，
   "大规模 + 分布式"这个方向讲得出口吗？还是应该彻底转向单机精做？

## 我的已知边界（不必重复指出）

- 分布式去重未实现（显式 NotImplementedError）
- Ray 跑不过单机（1m 内无回本点）
- 工业/时序算子无法分布式（语义要求）
- 训练效用：**清洗让模型显著更差**（Δ=-0.195，是噪声地板 0.0437 的 4.46 倍）——
  这条判据本身曾恒假过，已修
- 文本质量分类器 held-out 0.546、泛化 gap −0.381，判 FAIL
- 真实 COCO 丢弃 2.16% 里只有 20% 是硬事实（过度清洗 5.6 倍）

---

**完整架构文档见** `docs/ARCHITECTURE_FOR_REVIEW.md`（含全部代码片段与实测数据表）。
请针对具体设计给出**可执行的修改建议**，而不是泛泛的最佳实践。
