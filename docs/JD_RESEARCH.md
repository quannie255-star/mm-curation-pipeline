# 岗位 JD 调研：大模型数据链路方向（2026-08）

> 目的：让项目的每一块能力都对得上真实岗位的要求，而不是自嗨式造轮子。
> 调研日期：2026-08-20。招聘信息时效性强，面试前建议再核对一轮。

## 一、搜集到的代表岗位

### 1. 京东 — 数据模型实习生（数据体系）
- **职责**：设计和优化 PB 级文本训练数据的筛选、去重、清洗体系；基于 LLM 模型的语义级去重算法、质量筛选算法优化。
- **关键词**：筛选体系、去重（语义级）、LLM-based 质量筛选、规模（PB 级）。
- 来源：BOSS直聘（岗位时效性链接略）

### 2. 字节跳动 Seed — AI 数据策略实习生（模型数据工程）
- **职责**：高质量数据寻源、采集解析、清洗去重、筛选；与算法团队合作制定训练/评估数据标准。
- 来源：[jobs.bytedance.com 校招岗位](https://jobs.bytedance.com/campus/m/position/detail/7671197964542789941)
- 关联：[Seed 数据策略产品实习](https://jobs.bytedance.com/campus/m/position/detail/7667137415750191365)、[多模态数据服务实习](https://jobs.bytedance.com/campus/m/position/detail/7622244529194649861)（视频/图像数据存储、处理、安全）

### 3. 字节跳动 — 多模态大模型数据工程师（社招，方向代表）
- **职责**：设计和开发大规模预训练数据处理链路——数据寻源、抓取/采集、数据解析（OCR、图片、网页），为基座模型提供高质量数据。
- 来源：[字节招聘官网](https://jobs.bytedance.com/experienced/m/position/detail/7461826832884173064)

### 4. 月之暗面 Kimi — LLM 模型能力实习生（数据方向）
- **职责**：数据获取、清洗、筛选、规模化构建和高质量数据生成；研究与优化 LLM 的模型和数据评估方法。
- 来源：[BOSS直聘/月之暗面](https://www.zhipin.com/job_detail/a0b769aee5e0f17c03d809i9FlBU.html)，官方入口 [careers.kimi.com/campus](https://careers.kimi.com/campus)

### 5. 上海 AI 实验室 — 大模型中心（实习/校招）
- **要求**：熟悉 NLP 常见模型，有大模型训练语料调优经验者优先。
- 来源：[上海AI实验室招聘](https://www.shlab.org.cn/news/5444011)

### 6. 腾讯 — 多模态方向岗位
- **要求**：视频/NLP/多模态算法基础，大模型（视频/图文/音频/文本）数据处理能力。
- 来源：[腾讯招聘](https://careers.tencent.com/jobdesc.html?postId=2072330949517029376)

### 7. 技术生态佐证（JD 背后的技术栈）
- MinHash-LSH 是当前去重的事实标准：[Milvus 2.6 MinHash LSH 解读](https://zilliz.com.cn/blog/MinHash-LSH-best-dedup-for-model-training)、[阿里云 EMR Serverless Spark MinHash-LSH 方案](https://www.alibabacloud.com/help/zh/emr/emr-serverless-spark/use-cases/minhash-lsh-based-large-scale-text-duplication-scheme)、[Data-Juicer on Ray 的分布式去重](https://docs.rayai.org.cn/en/latest/ray-more-libs/data_juicer_distributed_data_processing.html)
- [BigCode 大规模去重（HF 博客中文版）](https://huggingface.co/blog/zh/dedup)
- Seed1.6 公开技术叙事：「规则过滤 + 模型评估」结合的清洗策略、多轮去重与采样优化。

## 二、JD 要求聚类 → 项目能力映射

| # | JD 高频要求（出现岗位数） | 本项目对应模块 | 状态 |
|---|---|---|---|
| 1 | 数据清洗/质检/去重体系（几乎全部） | 算子化漏斗 + 去重（精确/感知/语义） | Week2 |
| 2 | **LLM/模型驱动的质量评估**（京东、字节 Seed 叙事、月之暗面） | 模型打分算子：Chinese-CLIP 对齐分 + LLM-judge 可选算子 | Week2/3 |
| 3 | **质量评估体系可量化**（上海AI Lab、月之暗面"数据评估方法"） | 构造带 ground truth 的脏数据 → 算子 P/R + 漏斗报告 | Week1-3 |
| 4 | 多模态数据处理（字节多模态数据工程师/数据服务） | 图文对清洗全链路（本项目的主体） | 全程 |
| 5 | **规模化/分布式**（京东 PB 级、字节"大规模链路"） | 单机可跑 + 算子无状态/批量接口已为分布式预留 + 扩展路径文档（Ray/Data-Juicer 对标） | 架构层 |
| 6 | 数据寻源/采集/解析（字节） | download_dataset.py 多源下载 + 镜像适配 + 格式统一 | Week1 |
| 7 | 数据安全/合规（字节"数据安全"） | NSFW/水印检测位 + 合规占位叙事 | Week2 |
| 8 | **采样与配比**（Seed1.6"采样优化"、数据策略岗"数据标准/配比"） | 清洗后分层采样模块（按质量分层/类目配比） | **新增** |
| 9 | 工程能力：可复现、服务化（通用） | Airflow DAG + Docker + CI + FastAPI 检索服务 | Week1/3/4 |

## 三、差距结论（对原 ROADMAP 的修订输入）

1. **文本近似去重缺 MinHash-LSH**：原计划只有 md5/pHash/embedding。JD 生态里 MinHash-LSH 是标配（Data-Juicer、Milvus 2.6 都在主打）。**补**：`minhash_lsh` 去重算子（datasketch 实现，单机可测，文档说明如何平移到 Spark/Ray）。
2. **"规则过滤 + 模型评估"两层架构不够显式**：京东明确要"基于 LLM 的语义级去重/质量筛选"。**补**：漏斗显式分层——L1 规则（廉价）→ L2 感知模型（CLIP/分类器）→ L3 LLM-judge（可选、抽样执行），并把"成本分级"做成可讲的设计。
3. **清洗后没有配比/采样环节**：真实业务里清洗不是终点，"清洗后怎么配比进训练集"才是数据策略岗的核心问题。**补**：`sampling/` 分层采样器（按质量分层 × 类目标签配比），输出训练集配方清单。
4. **评测叙事要加上"配比后下游收益"**：不只证明"清洗提升检索指标"，再进一步证明"高质量分层采样比随机采样提升更多"——这是区别于所有 toy 项目的关键实验。
5. **数据源现实约束（本次实测）**：HuggingFace 直连超时、COCO 官方源 DNS 不通，hf-mirror.com 需 UA 头。下载脚本必须内建镜像与重试——这本身就是国内数据岗位每天面对的真实工程问题，值得写进文档。

## 四、面试叙事锚点

- "我调研了 N 个岗位的 JD，发现 XX% 都提到去重和模型驱动质检，所以我的漏斗分了三层……"（证明业务敏感度）
- "真实脏数据没有 ground truth，所以我用程序化污染器构造带标注的评测集，让每个算子都有 P/R……"（证明方法论）
- "我的算子是无状态的、批量接口独立，单机跑通后可以平移到 Ray……"（证明规模意识）
