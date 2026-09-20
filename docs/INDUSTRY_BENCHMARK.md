# 业界对标：LLM 数据链路的生产级现状 vs 本项目

> 2026-09-20。目的：把**业界生产级数据链路**实际在用的项目、标准、数字拉齐，
> 逐维对标本项目，**不回避难点**。
> 项目侧数字全部实点（`available_operators()` / `pytest --collect-only` / 产物文件），
> 业界侧数字全部来自公开论文、官方文档或官方 GitHub（链接见 §六）。

---

## 零、先说结论（三句话）

1. **本项目在"协议设计与证据闭环"这一层，比多数开源数据项目更严谨**——
   污染器注入 ground truth、claims 版本锁定、算子级独立评测口径、十条自认局限。
   这在开源数据项目里是罕见的（多数项目连"清洗后到底好了多少"都不测）。
2. **但在"工程深度"这一层，差距是量级而非百分比**——
   业界门槛是 10¹² tokens、GPU 内核、千核集群、可续跑可审计；
   本项目是 30 万篇、单机 CPU、全内存批处理、无 checkpointing。
3. **最致命的一条不是规模，是链条缺了最贵的两段**：
   **① 前端嵌入层**（HTML→干净正文的抽取，"最难且最决定质量"的一步）；
   **② 后端模型裁判**（用固定算力预算训出的模型当唯一裁判）。
   本项目把这两段都留在了系统之外——而它唯一抓到的真实 bug（#65 空白膨胀）
   正好是缺第一段导致的。

---

## 一、业界现状图谱：谁在做，做到什么程度

### 1.1 数据操作系统 / 框架层

| 项目 | 归属 | 关键能力 | 公开数字 |
|---|---|---|---|
| **Data-Juicer** | 阿里 | 200+ 算子（文本/图/音/视频/多模态）、YAML recipe、Ray 弹性多节点、HDFS/S3、自动 OP 融合、外部算子 pip 插件（entry points 自动注册） | **50 个 Ray 节点（6400 核）2 小时处理 700 亿样本**；**1280 核 2.8 小时去重 5TB**；OP 融合 2-10× 加速；v1.5.5 把重复度过滤的内存峰值从 768 MiB 压到 272 MiB（流式 n-gram 计数） |
| **NeMo Curator** | NVIDIA | GPU 加速（cuDF/cuML/**cuGraph**）、30+ 启发式过滤器、精确/模糊/**语义**去重、PII 脱敏、Ray + Xenna executor、1.x 起围绕 Stage 架构重写 | **模糊去重比 CPU 快 16×**（8TB RedPajama v2）；**TCO 低 40%**；跨 GPU 节点近线性扩展 |
| **datatrove** | HuggingFace | 4 阶段 MinHash（签名→分桶→聚类→过滤）、executor 自带 **checkpointing 与失败恢复**、per-snapshot 去重 | FineWeb 全套脚本开源；LocalPipelineExecutor 显式 `tasks` / `workers` 并行 |
| **dolma** | Allen AI | 数据工具包（Dolma-V1 被 DCLM 当基线比较） | — |

### 1.2 真正的大规模成品数据集（配方的存在证明）

| 数据集 | 规模 | 管线关键抉择 |
|---|---|---|
| **FineWeb** | **15T tokens**（96 个 Common Crawl dump，2013–2024） | trafilatura 抽取 → URL 黑名单 → fastText 语言 ≥0.65 → Gopher 重复/C4 质量/FineWeb 自定义过滤 → MinHash（5-gram、**112 哈希 = 14 桶 × 8**）→ PII 匿名化。**关键 ablation：per-dump 单独去重优于全局去重**（原本打算全局做，被实验推翻）。还在 1.82B 模型 × 28–350B tokens 上做了完整消融 |
| **FineWeb-Edu** | 1.3T（从 15T 里筛） | 用 Llama-3-70B-Instruct 给样本打 0-5 教育价值分 → 在 embeddings 上回归出小模型 → 按阈值筛。**削掉 90%+ tokens，MMLU/ARC/推理仍显著提升** |
| **DCLM-Baseline** | 240T token 池 | **resiliparse 抽取**（比 WET/trafilatura 高约 2.5 分且快 8×）→ Bloom filter 文档级去重 + **段落级去重** → **fastText 二分类器质量排序，只取分最高的约 10%**。结论：**model-based filtering 是关键**（Core 指标 +3.5 分）。产物让 7B 模型在 2.6T tokens 上 MMLU 5-shot 达 **64%**，比 MAP-Neo 高 6.6pp、算力少 40% |
| **MINT-1T** | **1.02T tokens / 3.42B 图 / 10.54 亿文档** | 多模态**交织**（interleaved）数据，来源 HTML + **PDF + arXiv**（不止 HTML）。工程代价：**平均 2350 CPU 核、约 420 万 CPU 小时**。Bloom filter 段落/文档去重 + 图片哈希去重 |
| **OBELICS**（前一代） | 115B tokens / 353M 图 / 141M 文档 | DOM 树解析抽取交织文档 |

### 1.3 人机协同 / 数据工作台

| 工具 | 归属 | 能力 |
|---|---|---|
| **Argilla** | Argilla/HF | 人审与标注工作台：模型输出复核、多人协作、质量校验、进度监控、与 HF Hub 双向同步。**战绩**：用它的 UI 过滤器在原始 UltraFeedback 里找出生成代码的 bug，重造数据集后训出 Notus，多个 benchmark 超过 Zephyr |
| **Lilac** | Databricks | 数据集探索/清洗/质检：LLM 驱动的搜索、过滤、聚类、标注；去重、PII、冷僻内容识别。**Cohere / Databricks 在用**；"20 分钟给百万文档聚类并起标题" |
| **distilabel** | Argilla | AI 反馈管线（AI feedback 与人工审查结合，如 Intel Orca DPO 改进版） |

### 1.4 治理与元数据标准

| 标准/项目 | 内容 |
|---|---|
| **Croissant 1.1**（MLCommons，2026-01-29 发布） | ML 数据集的机器可读元数据标准，schema.org 扩展。1.1 新增：**PROV-O 机器可执行血缘**（实体/活动/代理三级链式审计）、**DUO + ODRL 使用政策**（"非商用"等许可可直接被 agent 校验）、跨本体的语义互操作。**HuggingFace/Kaggle/OpenML 已内置，HF 上 70-80 万数据集带 Croissant 元数据**；`mlcroissant` 库可校验与加载 |
| OpenLineage / Marquez | 数据血缘事件标准与后端 |
| DVC / lakeFS / Delta / Iceberg | 数据与实验版本化 |

### 1.5 数据配比科学（2025-2026 的新前沿）

| 方法 | 内容 |
|---|---|
| **DoReMi** | 用参考模型与代理模型的**域级 excess loss** 重新加权——聚焦"还没学会"的域，而非简单给高熵噪声域加权 |
| **RegMix**（ICLR 2025） | 训多个小代理模型跑不同配比，拟合"配比→性能"回归（常用 LightGBM），搜最优。**只用 DoReMi 10% 算力**、效果超过人工选择 |
| **Data Mixing Laws** | 配比 × 模型规模 × 训练步数 → 性能的可预测关系（含联合缩放律：配比不仅改"曲线终点"，还改"收敛快慢"） |
| **CausalMix**（2026，清华） | 指出 RegMix 系假设"数据池静态"在生产环境不成立；把配比优化重构成**因果推断**问题（数据池统计特征为协变量、配比为 treatment、拟合 CATE），用 512 次 Qwen2.5-0.5B 代理训练 → 外推到 80 万文档池 → 训 7B，**数据池分布漂移时不需重跑代理搜索** |
| 生产侧工程要求 | mixture manifest（版本化配比清单）+ **token ledger**（每域计划 vs 实际配比、耗尽率、重复率、域级 loss）+ 域饥饿/过采样保护（`min_tokens_per_window` / `max_repeat_epochs` / `max_weight_delta` / `staleness_limit` / `fallback_policy`）+ 动态调权必须走发布审批与回滚 |

### 1.6 评测基准

| 基准 | 内容 |
|---|---|
| **DCLM benchmark** | 240T token 标准池 + 标准化训练配方（OpenLM）+ **53 个下游评测**，模型规模 412M / 1.4B / 2.8B / 6.9B，每档给出 pool size 与 H100 小时数（如 7B-1x：138B tokens、3700 H100 小时）。**让"数据配方的价值"可被固定算力预算下的模型分数公平比较** |
| DataComp（多模态版） | 同思想用于图文数据 |

---

## 二、逐维对标：本项目在哪一格

项目侧实点基线：**29 个算子**（`available_operators()` 实读）、**主仓 273 + 包 49 测试**、
最大真实语料 **302,002 篇中文维基**、图文 **2,106 对**、工业 **41,478 窗**。

| # | 维度 | 业界生产级 | 本项目 | 差距性质 |
|---|---|---|---|---|
| 1 | **前端嵌入层**（HTML→正文） | trafilatura / **resiliparse**（DCLM 实测高 2.5 分、快 8×）、DOM 树解析 | **完全没有**——输入是已抽好的 jsonl | **结构性缺失** |
| 2 | 规模 | 15T tokens（FineWeb）/ 1.02T tokens + 3.42B 图（MINT-1T）/ 240T 池（DCLM） | 30.2 万篇 / 41,478 窗 | 量级差（~10⁴） |
| 3 | 算力 | 2350 核 × 420 万 CPU 小时（MINT-1T）；6400 核（Data-Juicer 实测） | 单机 RTX 4060 8GB + CPU | 量级差 |
| 4 | 执行引擎 | Ray 弹性多节点 + **checkpointing / 失败恢复 / 有序合并 / 重试**（datatrove、Data-Juicer）、GPU 内核（cuDF/cuGraph） | `run_funnel(samples: list[Sample])` **全内存**；本地 Ray 已验证等价性但无续跑 | 工程深度差 |
| 5 | 算子生态 | 200+（Data-Juicer）、30+ 过滤器（NeMo）；**第三方算子可作 pip 包装**（entry points 自动注册） | **29 个**，注册表 + 元数据齐全，但生态封闭 | 数量差，机制反而对 |
| 6 | **建模式质量分** | fastText/DeBERTa 分类器做质量排序（**DCLM 证明这是关键**，+3.5 分）；FineWeb-Edu 用 70B 打分再蒸馏成小模型 | 以**启发式规则**为主 + GPT-2 困惑度兜底；`llm_judge` 算子存在但只服务于 V3 域判官 | **方向性差距** |
| 7 | 去重规模与策略 | FineWeb：per-snapshot 去重（**被 ablation 推翻过全局方案**）；DCLM：Bloom filter + **段落级**；NeMo：语义去重 | MinHash-LSH 单机（10 万档 21s，30 万档 60s），含精确/近似/语义三层 | 规模差；**"哪个粒度对"没做 ablation** |
| 8 | **人审工作台** | Argilla / Lilac（生产在用，有明确战绩） | **无**（只有落盘留痕 override） | **结构性缺失** |
| 9 | 数据配比科学 | RegMix / DoReMi / CausalMix + mixture manifest + token ledger + 域饥饿保护 | 只有分层采样（vs 随机 +24% R@1） | **整层为空** |
| 10 | 元数据与治理 | **Croissant 1.1**（PROV-O 血缘 + DUO/ODRL 机器可执行许可）、OpenLineage | 自研 `claims.json`（**只锁简历数字，不描述数据本身**） | 标准对齐缺失 |
| 11 | 下游评测 | **53 个任务 × 4 档模型规模 × 固定算力预算**（DCLM）；FineWeb 在 1.82B 模型 × 28-350B tokens 上消融 | R@1（0.459→0.556）、GPT-2 ppl（7.16→7.70）、CLIP R@1 差 5.2pp；held_out 仅 119 条、**单 seed** | **证据强度差** |
| 12 | 多模态形态 | 交织文档（interleaved，HTML + PDF + arXiv，保序） | image-caption **对**（unpaired 交织不涉及） | 形态差 |
| 13 | 复现粒度 | 容器 + 锁 + 版本化数据集；Croissant 让数据集可被框架直接加载 | 无锁文件；README 命令复现；数据脚本重下 | 工程差 |
| 14 | **消融驱动的抉择** | FineWeb 用 ablation 推翻自己的全局去重计划；DCLM 用 ablation 选抽取器 | **有**（分组消融找到"只有去重组显著，R@1 -0.017"） | **这一条最接近业界** |

---

## 三、不避难的九条（最硬的话）

### 3.1 你的"清洗能力"缺少终极裁判，而且裁判的样本量太小

业界共识（DCLM / FineWeb 用行动证明）：**数据管线的价值只能用"固定算力预算下训出的模型分数"来定义**。
你有一个 R@1 +21%、一个 ppl +7.5%、一个 CLIP 差 5.2pp——**方向完全正确**，是很少见的好品味。

但规模上：DCLM 用 **53 个任务 × 7B 模型**；你用 **119 条 held-out 查询 × R@1 × 单 seed**。
自己文档里也承认"±1pp 波动"。含义是：
**在你这个规模上，"数据好"和"随机噪声"在统计上还分不开。**
不是方法错，是**样本量不足以支撑结论**。

### 3.2 你的抽象是对的，但只做到"协议层"，没做到"执行层"

`Sample` 协议 + 算子注册表 + `Executor` + YAML 配置——这和 Data-Juicer 的 recipe、
NeMo Curator 的 Stage、datatrove 的 Pipeline **是同一个思路**，而且你零特例接入了 4 种模态，
这一点做得比很多项目干净。

差距在执行层：**checkpointing、弹性多节点分片、失败隔离与重试、有序合并、GPU 内核、
算子的 pip 插件生态**——这些别人都有，你都没有。
你的 Ray 验证的是"**等价性**"（逐 id 零差异，这很严谨），不是"**可扩展性**"。

### 3.3 你把最贵、最决定质量的一段留在了系统之外

生产管线的**第一步**是 HTML→干净正文（trafilatura / resiliparse / DOM 解析）。
FineWeb 靠 trafilatura，DCLM 靠 resiliparse 拿到 +2.5 分且快 8×，MINT-1T 为此写了三套抽取器
（HTML / PDF / arXiv），花了 420 万 CPU 小时。

你的输入是**已经抽好的 jsonl**。这意味着：
- 最影响最终质量的环节，你不控制；
- **而你唯一抓到的真实数据 bug（#65：爬虫空白膨胀把 `chinese_ratio` 的分母撑爆，778 篇误杀）正是这一步缺失造成的。**
  你自己当时的结论写得很准——"域迁移把『管线缺前置归一化算子』这个缺口照了出来"。
  但业界的解法是**在抽取层解决**，你选择在算子层打补丁（改 `chinese_ratio` 语义）。
  补丁对，但**治的是症状**。

### 3.4 规则算子的边际收益已经见底

DCLM 的结论直接：**model-based filtering 是关键**，fastText 分类器排序取 top-10% 带来 Core +3.5 分。
FineWeb-Edu 更进一步：**用 Llama-3-70B 打分，再蒸馏成小模型**，砍掉 90% tokens 仍然全面变好。

你的 29 个算子里，**绝大多数是启发式规则**（长度、中文占比、重复率、模板句、PII 正则、分辨率、宽高比……），
加一个 GPT-2 困惑度兜底，加一个 `llm_judge`（但服务于域判官而非数据筛选）。

规则算子的天花板是明确的，而你恰好**已经有微调小模型的能力**（LoRA κ +0.560 那条线）。
**把 V3 的判官能力用回数据筛选**，是性价比最高的一步——业界已经给出配方（先用大模型打标，
再蒸馏成小分类器），而你两边都有现成件。

### 3.5 数据配比整层是空的

你有分层采样（vs 随机 +24% R@1，这是好证据）。
但业界已经把"配比"变成**实验设计问题**：RegMix 的代理回归、DoReMi 的域级 excess loss、
Data Mixing Laws 的联合缩放律，到 2026 的 CausalMix 处理"数据池漂移"。

生产侧还要求 **mixture manifest（版本化配比清单）+ token ledger（每域计划 vs 实际）+ 域饥饿/过采样保护**。
在真实训练里，**"配比写对了但执行漂了"是常态**——低权重域长期不出样本、高权重小域被反复重复、
某域读取失败后被静默按比例摊给别的域。这些你一个都没有。

### 3.6 治理停留在"自己锁自己"

`claims.json` + `verify_claims.py` 是很聪明的自研方案，但它**只锁简历数字**，
不描述数据集本身，也不可被外部工具消费。

业界的答案已经是**标准**：Croissant 1.1 的 **PROV-O 机器可执行血缘**（数据集/文件/单条记录
能追溯到来源数据与处理步骤、以及负责的 agent）+ **DUO/ODRL 使用政策**（许可限制可被 agent 自动校验）。
HF 上 **70-80 万数据集已带 Croissant 元数据**，HF/Kaggle/OpenML 内置。

你有 FHIR（含 PII 脱敏）和工业数据——**合规在业界是要机器可执行的，不是写在文档里的**。
这一条对"产品级"是硬伤，而且**补齐成本不高**（输出一个 JSON-LD 元数据文件 + `mlcroissant` 校验）。

### 3.7 没有"不洗"的决策依据，也没有"该洗哪一步"的科学

FineWeb 最有价值的一句话是：**他们本来打算全局去重，ablation 说 per-dump 更好，于是改了计划。**
你做过分组消融（找到"只有去重组显著"）——这一点**你是对的**、很少见。

但只做了一次、在一个模态、在 2,106 条上。
业界是在 1.82B 模型 × 5 档 token 预算上做的。**你的消融结论无法外推。**

### 3.8 统计效力：单 seed、119 条、100 倍外推

自己承认的三条（PROOF_CHAIN §九）：微调级单 seed 无置信区间；held_out 119 条 ±1pp；
规模停留在 100 万档实测（100TB 是推演）。
**"诚实地标注局限"是优点，但局限本身仍然是局限。** 对生产级而言这是不可交付的。

### 3.9 真实分布的证据链是空的（昨天已经说透，这里只记结论）

三个真实工业数据集：召回 24~72% / 误杀 16~28%（合成 100% / 1.23%）。
真实数据无法给出"漏了多少"；判据是对着注入形态写的；**且人审环节缺失**。
业界虽然也没有完美的真实分布 ground truth，但他们有**规模 + 人审工作台 + 下游模型裁判**三重兜底，
你三者都薄。

---

## 四、你的项目真实处在什么位置

**不是"落后"，是"站在另一条轴上"。**

- 在**规模轴**上：你和 Data-Juicer / NeMo Curator 差 3-4 个数量级。这条轴你**不该比，也比不了**
  （那是 6400 核集群和 420 万 CPU 小时）。拿这条轴自责没有意义。
- 在**证据严谨轴**上：**你处在开源项目里的上游**。开源数据项目普遍的问题是
  "有管线、有算子、没有验证"——Dolma/OBLICS 这类数据集也只有产出报告，没有 claims 锁定。
  你的**污染器注入 ground truth + 算子级独立评测 + 主靶算子对靶 + 劣化注入实测变红 +
  版本锁数字 + 十条自认局限**，这套东西多数开源项目没有。
- 在**工程深度轴**上：你在"能在一台机器上跑通并证明"；业界门槛是"能在集群上可续跑、可审计、可治理"。

**所以最该补的是什么，排序很清楚：**

| 优先级 | 差距 | 为什么是这个顺序 |
|---|---|---|
| **1** | **后端裁判的规模**（多 seed + 更大 held-out + 一个公开基准） | 决定"你的结论能不能被采信"。成本低、收益最大 |
| **2** | **人审工作台**（真实轨的唯一裁决途径） | 163 小时冻结那个证据说明必须靠人。且补齐成本可控 |
| **3** | **前端抽取层 + model-based 质量分** | 补上最贵的两段；而且能直接复用你已有的 LoRA/判官能力 |
| 4 | 配比科学（manifest + ledger） | 一旦进入真实训练就会立刻需要 |
| 5 | Croissant 元数据 + 血缘 | 标准化、成本低、对"产品级"是门面 |
| 6 | 执行层工程（checkpointing / 弹性分片） | 没有真实规模需求前，收益不明显 |
| 7 | 规模（GPU / 千核） | 个人项目追不上，也不该追 |

---

## 五、如果只做五件事（性价比排序）

1. **把下游评测做扎实，而不是加算子。**
   多 seed + 把 held_out 从 119 条扩到千条级 + 至少接一个公开基准（哪怕 DCLM 的 400M-1x 档）。
   **先让"数据好"和"噪声"统计可分。**
2. **补人审队列。** 这是唯一能把"真实轨误杀率只能是上界"变成"可裁决"的东西，
   也是把评测脚本升级成产品功能的第一步。业界对标物：Argilla / Lilac。
3. **把 V3 的判官能力用回数据筛选。** 业界配方现成：大模型打标 → 蒸馏小分类器 → 全量排序。
   你有 LoRA 微调链路（κ +0.560 达标）和 `llm_judge` 算子，**缺的只是把它接到漏斗主链上**。
4. **补前端抽取层。** HTML→正文的抽取 + 归一化。
   直接对标：trafilatura / resiliparse。**并回头用 #65 那个 bug 验证它真的解决了。**
5. **输出 Croissant 元数据。** 一个 JSON-LD 文件 + `mlcroissant` 校验，
   把血缘（PROV-O）与许可（DUO/ODRL）做成机器可读——这是从"个人项目"迈向"产品级"最便宜的一步。

**明确不建议做的**：鉴权、多租户、GPU 内核、千核扩展。
理由不变——**先证明有效性，再谈规模与可用性。**

---

## 六、参考资料

**框架 / 工具**
- Data-Juicer — https://github.com/datajuicer/data-juicer ｜ 文档 https://datajuicer.github.io/data-juicer/
- NeMo Curator — NVIDIA，GPU 加速数据整理（cuDF/cuML/cuGraph，16× 模糊去重，40% 更低 TCO）
- datatrove — https://github.com/huggingface/datatrove ｜ FineWeb 脚本 https://github.com/huggingface/datatrove/blob/main/examples/fineweb.py
- dolma — Allen AI 数据工具包
- Ray Data / Daft — 通用数据引擎

**数据集 / 管线**
- FineWeb — https://huggingface.co/datasets/HuggingFaceFW/fineweb ｜ 博客 https://huggingface.co/spaces/HuggingFaceFW/blogpost-fineweb-v1
- DCLM-Baseline / DataComp-LM — arXiv:2406.11794
- MINT-1T — arXiv:2406.11271（1.02T tokens / 3.42B 图 / 420 万 CPU 小时）
- OBELICS — 115B tokens / 353M 图 / 141M 文档

**人机协同**
- Argilla — 人审工作台（https://huggingface.co/docs/hub/datasets-argilla）
- Lilac — https://github.com/databricks/lilac（Cohere / Databricks 在用）
- distilabel — AI 反馈管线

**治理 / 元数据**
- Croissant 1.1 规范 — https://docs.mlcommons.org/croissant/docs/croissant-spec-1.1.html
- Croissant 工作组 — https://mlcommons.org/working-groups/data/croissant/
- OpenLineage / Marquez — 血缘事件标准
- DVC / lakeFS / Delta Lake / Iceberg — 数据版本化

**配比科学**
- RegMix（ICLR 2025）｜ DoReMi ｜ Data Mixing Laws（survey 见 arXiv 综述）
- CausalMix（2026，清华）— 数据池漂移下的配比因果推断
- 生产侧工程实践：mixture manifest + token ledger + 域饥饿保护

**评测基准**
- DCLM benchmark（53 任务 / 412M–7B / 固定算力预算）— arXiv:2406.11794
- DataComp（多模态版）
