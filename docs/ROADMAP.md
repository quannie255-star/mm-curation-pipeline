# 项目路线图：多模态图文数据清洗与向量检索 Pipeline

> 面向实习求职的作品级项目。目标：用一套中文图文数据管道，同时证明
> **工程完整度**、**业务思考**、**产品化闭环** 三件事。
>
> 时间预算：3-4 周（每周约 5 个半天）。每周末有明确验收标准，达不到就砍范围，不烂尾。

## 项目一句话

> 构建一条「脏数据进 → 漏斗式多算子清洗 → 质量可量化 → 向量索引 → 检索服务 →
> 清洗收益可证明」的中文图文数据管道，并用同一套评测证明：**清洗让下游检索指标提升了 X%**。

## 与岗位 JD 的映射（面试时每一条都要能讲）

> 2026-08-20 完成了一轮真实 JD 调研（京东/字节 Seed/月之暗面/上海AI Lab/腾讯），
> 详见 [JD_RESEARCH.md](JD_RESEARCH.md)。下表已按调研结论修订。

| 项目模块 | 对应 JD 要求 | 出处 |
|---|---|---|
| 算子化清洗框架（YAML 配置驱动、可组合） | 数据清洗/质检/去重算法开发 | 京东、商汤 |
| CLIP 图文对齐打分、NSFW/水印检测 | 熟悉图像处理、理解多模态模型 | 字节 |
| 去重四件套（精确 md5 / 感知 pHash / 文本 MinHash-LSH / 语义 embedding） | 数据去重；MinHash-LSH 是 JD 生态标配技术词（Data-Juicer/Milvus 2.6） | 京东、字节 |
| 漏斗三层架构：L1 规则 → L2 感知模型 → L3 LLM-judge（可选抽样） | "规则过滤 + 模型评估"（Seed1.6 叙事）；"基于 LLM 的语义级去重/质量筛选" | 京东、字节 Seed |
| 漏斗式质量报告（每级通过率、阈值敏感性） | 数据质量评估体系 | 上海AI实验室、阿里（加分项） |
| 清洗前后 Recall@K / MRR 对比实验 + 配比采样消融 | "清洗规则驱动模型效果迭代"、数据策略/配比 | 腾讯、字节数据策略岗 |
| 算子无状态 + 批量接口（单机跑通，可平移 Ray/Spark，文档化路径） | 大规模数据处理链路 | 京东（PB 级）、字节 |
| Airflow DAG + Docker + CI + 测试 | 工程能力、可复现 | 通用 |
| 成本意识（CPU/GPU 分级推理、批处理、LLM 只抽样打分） | 业务思考 | 面试软区分点 |

## 技术选型（已定）

- 数据：**COCO-CN**（20,341 张 COCO train2014 图 + 人工中文 caption/tags，
  2026-08-20 已验证可得性），图像从 HF 镜像 `justram/COCO2014-Images`（缩放版
  parquet，全量 ~5.5GB）按文件名 join 获取。详见下方「数据源实测结论」。
- 对齐模型：Chinese-CLIP ViT-B/16（`OFA-Sys/chinese-clip-vit-base-patch16`）
  ——英文 CLIP 对中文 caption 无效，选型本身就是面试考点
- 编排：Airflow 2.9（docker-compose，LocalExecutor）
- 检索：FAISS（IVF/HNSW），FastAPI 服务
- GPU 策略：容器内 CPU 跑规则算子与批量推理（小规模可接受），
  本机 RTX 4060 8GB 用于阈值扫描等需要反复重跑的实验——分级算力=成本意识
- 质量/评测：自建漏斗指标 + 基于构造脏数据的 ground truth 评测

### 数据源实测结论（2026-08-20，Windows 本机，最终版）

| 源 | 状态 | 说明 |
|---|---|---|
| huggingface.co 直连 | ❌ 超时 | 国内网络常态 |
| hf-mirror.com | ✅ 可用 | **必须带浏览器 UA 头**，裸 urllib 被 403 |
| images.cocodatasets.org（COCO 官方） | ❌ DNS 不通 | — |
| AIMClab-RUC/COCO-CN | ✅ 15.4MB | 标注包：train 18341 / val 1000 / test 1000，全部对应 train2014 图 |
| justram/COCO2014-Images (parquet) | ⚠️ 弃用 | **无文件名字段，image_id 是自增序号而非 COCO 官方 id**，与 COCO-CN 无法对齐（实测交集仅 3.5%，且为巧合碰撞）。教训：镜像数据集"存在"≠"可用"，先验 schema 再谈下载 |
| ali-sh07/COCO-train2014 | ✅ 当前源 | ~10k 张原始分辨率 JPEG，保留 COCO 原始文件名，与 COCO-CN 随机交集 16.2%（**1,620 对**），文件级并发下载 |
| wanng/wukong100m | ❌ 弃用 | 仅 url+caption，图像为死链 |

**扩展路径**：需要全量 20,341 对时，叠加 HF 上其他 train2014 镜像
（搜索 `coco_train2014` 有多个候选），`list_remote_files` 已支持多 repo 合并。

工程含义：下载链路必须内建「镜像端点 + UA + 重试 + 幂等（已存在跳过）」，
`scripts/download_dataset.py` 按此实现——这本身就是国内数据岗的真实日常。

## 数据策略（本项目最聪明的一步）

干净种子集 + **程序化污染器**：向干净数据注入 9 类可控脏数据并保留标注：

| 脏数据类型 | 注入方式 | 对应清洗算子 |
|---|---|---|
| 精确重复 | 复制样本 | md5 去重 |
| 近似重复（图像） | 重编码/裁剪/压缩 | pHash 感知去重 |
| 近似重复（文本） | caption 词序打乱/局部重复/截断拼接 | MinHash-LSH 文本去重 |
| 语义重复 | 同图不同 caption 重复 | embedding 语义去重 |
| 低分辨率 | 下采样 | 分辨率算子 |
| 模糊 | 高斯模糊 | 模糊度算子 |
| 图文错配 | 打乱 caption 配对 | CLIP 对齐分 |
| 低质文本 | 截断/乱码/重复字符 | 文本质量算子 |
| 水印 | 程序化叠加半透明水印 | 水印检测 |
| 违规内容占位 | 以"水印广告图"等 SFW 替代并标注 | NSFW 检测（占位实现） |

**为什么这样做**：真实脏数据没有 ground truth，无法量化清洗好坏；构造带标注的脏数据后，
每个算子可算 precision/recall，整条管道可算"脏数据召回率 vs 好数据误杀率"——
这正是 JD 加分项里的"数据质量评估体系"，也直接支撑"清洗收益"叙事。

### 去重阈值校准记录（2026-08-20，真实数据 2,106 样本扫描）

**校准方法论**：污染强度与算子阈值是一对耦合参数，必须联合校准——
先扫阈值看"召回-误杀"曲线，若全阈值区间无拐点则调污染强度（模拟的脏数据
要么太脏要么太干净都会让校准失真），直到出现可用的决策边界。

| 算子 | 阈值 | 召回 | 误杀干净 | 结论 |
|---|---|---|---|---|
| md5_exact | — | 62/62 (100%) | 0 | 完美，字节级无歧义 |
| phash_near | 10 | 20/31 (65%) | 0 | 太紧 |
| phash_near | **12（默认）** | 26/31 (84%) | 5 (0.3%) | **拐点** |
| phash_near | 14 | 28/31 (90%) | 44 (2.7%) | 误杀悬崖 |
| minhash_lsh | 0.5 | 31/31 | 47 (2.9%) | 模板句自然相似被误杀 |
| minhash_lsh | **0.65（默认）** | 29/31 (94%) | 12 (0.7%) | **拐点** |

污染强度的配套调整（原因都在 impl.py 注释里）：
- `near_duplicate_image` 裁剪从每边 6~14% 收紧到 4~10%：>10% 的裁剪会把
  pHash 距离推出安全区，与自然相似照片不可区分
- `near_duplicate_text` 改为 8-gram 重复 + 仅长文本(≥20字)删 3% 字符：
  使注入对 Jaccard 稳定在 0.72~0.90，与"模板句自然相似"(0.5~0.6) 拉开
- `minhash_lsh` 增加短 caption(<8字) 守卫：几字文本的 3-gram 无区分力，
  截断类低质文本会制造虚假碰撞

**已知残留**：phash 在 t=12 漏掉的 5 个是最强裁剪样本；解法是
`imagehash.crop_resistant_hash`（抗裁剪感知哈希），列为扩展项。
另：误杀的"干净"样本里有一部分其实是 COCO 自然存在的近重复
（同场景连拍），真实业务中这恰是该去重的——这正是"ground truth
也有局限"的好案例。

## Phase 2 计划：寒假实习冲刺（2026-08-27 启动，目标 10 月中前完成）

> Week1-4 主线已完成。Phase 2 按「先堵面试攻击面、再加生态位故事」排序，
> 每模块仍走 AI_CODING_PROTOCOL 设计门。评估背景：目标寒假实习（面试约在 10-12 月）。

| 序 | 模块 | 目标 | 预估 | 状态 |
|---|---|---|---|---|
| P1 | 水印/NSFW 合成数据自训检测器 | 占位实现→真模型；风格错开泛化测试防循环论证 | 2 天 | ✅ 完成（四轮迭代：testB 泛化 87.3%，主靶全量召回 100%/误杀 0.8%） |
| P2 | 实时质量评分 API（POST /api/ingest） | 批处理→在线质量门，对接 JD"数据链路" | 1 天 | ✅ 完成（与漏斗同源算子/阈值） |
| P3 | 增量去重（LSH insert/query + pHash 表） | 批处理→持续服务 | 2-3 天 | ✅ 完成（三层查-判-插，与 /api/ingest 一体） |
| P4 | Chinese-CLIP 干净/脏集微调对比 | 代理验证→训练级直接验证 | 2 天 GPU | ✅ 完成（干净微调 R@1=0.688 vs 脏微调 0.636，差 5.2pp；base 与 D3 基线互证一致） |
| P5 | 评测框架减肥版（pip 包 v0.1） | "做了管道"→"创造了评测评框架"；Data-Juicer 有算子无评测的生态位 | 3-5 天 | ✅ 完成（`packages/curation-eval/`：污染器协议+P/R+检索指标，6 协议测试，pip 可装） |
| P7 | 跨集去污染（decontamination） | 大模型实验室必做工序：语料×评测集重叠检测 | 1 天 | ✅ 完成（pHash+MinHash 双层；真实实验可达召回 94.4%/precision 97.1%，日志 #40） |
| P8 | 分数分布漂移监控（PSI） | 阈值腐烂告警：上游换源时分数分布静默漂移 | 1 天 | ✅ 完成（对照实验：同源批 PSI≈0.01 稳定 / 换源批 0.36-0.66 告警；灵敏度边界已量化，日志 #41） |
| P6 | 规模扩展 8-15k（叠加 train2014 镜像） | 消融非零 delta；分层层采样规模效应 | 1-2 周 | ⏸ 暂缓（求职优先；消融全零已有"冗余+分组消融"完整解释，见 ENGINEERING_NOTES #31） |

明确不做：Next.js 真前端（数据岗零回报）、W&B 云依赖（MLflow 本地备选）、
长期愿景（算子市场/influence-guided/主动清洗）只进 INTERVIEW.md 话术不进代码。

## 周计划

### Week 1：地基 + 数据准备
- D1-2 ✅ 仓库骨架、算子框架（base + registry + YAML 配置解析）、docker-compose Airflow 起来、pytest 跑通
- D3 数据源确定与下载（验证 Flickr30k-CN / COCO-CN / Wukong 子集的可得性，写 `scripts/download_dataset.py`），统一为 `samples.jsonl` 格式
- D4 污染器 `scripts/contaminate.py`：9 类脏数据注入 + ground truth 标注
- D5 DVC 初始化 + 首个数据版本；数据集 EDA 脚本（分布、尺寸、caption 长度）
- **验收**：`make data` 一条命令从零产出带标注的脏数据集；Airflow UI 能看到 DAG

### Week 2：清洗漏斗核心（三层架构：L1 规则 → L2 感知模型 → L3 LLM-judge）
- D1-2 L1 规则算子：分辨率、长宽比、模糊度（Laplacian）、文本长度、中文字符占比、乱码检测
- D3 L2 模型算子：Chinese-CLIP 对齐分批量推理（GPU）、水印检测（方案二选一：LAION 预训练检测器 / 用污染器合成数据自训轻量 CNN——推荐后者，有故事）
- D4 去重四件套：md5 / pHash / MinHash-LSH（datasketch，单机可测）/ embedding kNN 语义去重
- D5 漏斗执行器 `pipeline/runner.py`：逐级记录通过率与分数分布 → `quality/report.py` 产出 JSON + Markdown 质量报告；L3 LLM-judge 算子接口预留（抽样打分、成本可控）
- **验收**：YAML 换阈值即可重跑整条漏斗；质量报告能看到每一级的漏斗数字

### Week 3：检索服务 + 评测闭环（项目灵魂）
- D1 CLIP 双塔编码入库 + FAISS 索引构建（text/image 双索引）
- D2 FastAPI 检索服务（文搜图 / 图搜图），Docker 化
- D3 检索评测：构造检索测试集，算 Recall@1/@5/@10、MRR；**脏索引 vs 净索引对比实验**
- D4 算子级评测：用 ground truth 算每个算子 precision/recall；阈值敏感性曲线（阈值扫描脚本）
- D5 **分层采样器 `sampling/`**：清洗后按质量分层 + 类目（tags 字段）配比出训练集配方；「高质量分层采样 vs 随机采样」下游检索指标对比；Airflow DAG 串全流程（download → contaminate → clean → index → evaluate → report），一键复现
- **验收**：一张对比表：「清洗使 Recall@10 从 A 提升到 B」+「配比采样再提升 C」+ 每算子 P/R 表

### Week 4：产品化 + 打磨
- D1-2 Streamlit Demo：文搜图/图搜图界面 + 清洗漏斗 dashboard + 被丢弃样本抽样浏览
- D3 GitHub Actions CI（ruff + pytest）；README 重写（架构图、指标表、一键复现、设计权衡）；Ray/Spark 扩展路径文档
- D4 消融实验：逐个关算子看指标变化 → "哪个算子贡献最大"的业务结论；文档化成本估算（规则/感知/LLM 三层单价对比）
- D5 `docs/INTERVIEW.md`：面试叙事（STAR 版项目故事、预想追问与回答）、简历 bullet 定稿
- **验收**：陌生人按 README 能 30 分钟内复现；面试 3 分钟能讲清闭环

环境记录：transformers 5.x 要求 torch≥2.4；本机 torch 初始为 2.2.1+cpu
（CUDA 版从未装过），`pip install -U torch --index-url
https://download.pytorch.org/whl/cu121` 后 GPU 可用（RTX 4060）。

## 风险与降级预案

| 风险 | 预案 |
|---|---|
| 中文数据集下载困难（墙/失效链接） | 备选顺序：COCO-CN → Flickr30k-CN → Wukong 子集 → 英文集+机翻中文名义标注"机翻数据清洗"叙事 |
| Airflow 容器挂载 Windows 路径出问题 | 降级为 host 上 `airflow standalone`（pip 装进 venv），编排逻辑不变 |
| NSFW 真实检测器不可得 | 用"占位实现 + 文档说明合规考量"处理，重点讲清检测框架设计而非模型本身 |
| 时间不够 | 优先级：评测闭环 > Demo 前端 > CI/DVC。评测闭环是灵魂，不可砍 |

## 进度记录

| 日期 | 完成内容 | 备注 |
|---|---|---|
| 2026-08-20 | 仓库骨架、算子框架、Airflow compose、ROADMAP | 环境确认：RTX4060 8G / Py3.11 / Docker 29 |
| 2026-08-20 | JD 调研（5 家公司岗位 + 技术生态佐证）→ 差距分析 → ROADMAP 修订：去重四件套（+MinHash-LSH）、漏斗三层架构（+LLM-judge）、配比采样模块 | 见 [JD_RESEARCH.md](JD_RESEARCH.md) |
| 2026-08-20 | 数据源实测：HF 直连不通、hf-mirror 可用（需 UA）、COCO 官方 DNS 不通；锁定 COCO-CN（20,341 对）+ justram/COCO2014-Images parquet join 方案 | 见「数据源实测结论」 |
| 2026-08-20 | **数据源踩坑与切换**：parquet 镜像无文件名无法对齐（弃用，教训已记录）；切换 ali-sh07/COCO-train2014 原始 JPEG，命中 1,620 对；下载器重写（幂等+并发+UA 镜像） | Week1 D3 完成 |
| 2026-08-20 | 污染器 10 类注入 + ground truth 标注 + manifest；data-download/data-contaminate 入 Makefile；27 测试全绿 | Week1 D4 完成 |
| 2026-08-20 | L1 图像质量算子（resolution/aspect_ratio/blur）+ 去重三件套（md5_exact/phash_near/minhash_lsh）已注册；**测试留给作者本人编写**（见交接清单） | Week2 D1/D4 代码就位 |
| 2026-08-20 | **`make data` 端到端验证通过**：1,620 干净对 + 486 注入（10 类分布符合配置）；途中修复 3 张断连损坏图（fetch 改原子写 + PIL verify 清理）；污染器注册表未触发的 import bug 修复 | Week1 D3-D4 验收完成 |
| 2026-08-20 | L1 图像算子与去重三件套补齐测试（39 测试全绿）；**真实数据阈值联合校准**：phash t=12 / minhash t=0.65 + 污染强度配套收紧，校准表与残留问题记录见上 | Week2 D1/D2/D4 完成 |
| 2026-08-22 | **漏斗执行器 + 质量报告落地（D5）**：`pipeline/runner.py`、`quality/report.py`、`scripts/run_pipeline.py` + `make funnel`。L1+去重 9 级漏斗：召回 99.2%/误杀 1.1%，残留 4 条正好是 L2 靶子。发现：只改 caption 不动图的注入被 md5 拦截 → 算子级 P/R 须在全量脏集独立评（Week3 注意） | Week2 D5 完成 |
| 2026-08-22 | **L2 模型算子上线（D3 完成，Week2 收官）**：`embedding/clip_encoder.py`（Chinese-CLIP ViT-B/16，本地 safetensors，向量缓存复用）+ `clip_alignment`（图文对齐）+ `semantic_dedup`（图像向量 kNN）。真实数据校准：对齐 t=0.38（错配召回 96%/误杀 0.2%，顺带压住 nsfw 占位与刷字文本）、语义 t=0.93（语义重复召回 100%）。**11 级漏斗最终：脏数据召回 100%（486/486，零残留）、干净误杀 2.16%**（含自然近重复的正确合并）。46 测试全绿 | 环境坑三个：transformers 5.x 强制 torch≥2.6→降 4.57；官方仓库只有 .bin（CVE 限制 torch.load）→ `scripts/convert_clip_weights.py` 本地转 safetensors；transformers 4.57 中文 CLIP 文本塔回归 bug（pooler_output=None）→ 编码器按官方实现取 CLS+projection |
| 2026-08-26 | **Week3 D1-D2 完成（检索服务上线）**：FAISS IndexFlatIP 双索引（clean_v2=1585 / dirty_raw=2106，dim=512）+ FastAPI 服务（文搜图/图搜图/静态白名单/404-422 语义，热查询 67ms）。AI Coding 协议落地（design_tables + tasks 分层确认门，见 docs/AI_CODING_PROTOCOL.md），60 测试绿。**脏索引污染检索的直接证据**：同查询脏索引 top3 中 2 条脏数据、模糊图居首 | D3 对比实验素材就绪 |
| 2026-08-26 | **Week3 D3 完成（灵魂实验出数）**：同查询集（1585 条，held_out 119 / self 1466）双索引对比——**清洗使 Recall@1 0.459→0.556（相对 +21%）、MRR 0.599→0.670、R@10 0.874→0.901**；发现"头部挤压"效应（R@1 退化 9.7pp vs R@10 仅 2.7pp——污染挤的是头部名次）；held_out/self 退化幅度一致，自检索偏置未扭曲对比。67 测试绿；工程日志 ENGINEERING_NOTES.md 建立（28 条，面试弹药库，持续更新） | 下一步 D4：算子级 P/R 独立评测 + 阈值敏感性曲线 |
| 2026-08-26 | **Week3 D4 完成（算子级评测 + 阈值曲线）**：`mm_curation/eval/operator_pr.py` 独立评测模块（每个算子在全量脏集上独立跑一次）+ `scripts/eval_operators.py` + `scripts/threshold_scan.py`（5 算子 × 8 阈值点 + matplotlib 拐点图）。**核心发现**：md5 会 100% 拦下仅改 caption 的注入（low_quality_text/mismatched_pair）——验证了 ROADMAP 中"漏斗串联口径低估下游"的预判；`blur` 算子同时 100% 召回 `low_resolution`（因为污染器下采样后上采样成模糊图，`resolution` 算子因此召回 0%——可消融）；三个文本算子（text_length/chinese_ratio/char_repetition）各召回 25-30% 的 low_quality_text，互补而非冗余。75 测试全绿（+8 新）；`make eval-op` / `make threshold-scan` 一键复现；报告在 `data/reports/operator_pr.{json,md}` + `data/reports/threshold_scan.{json,md}` + 5 张阈值曲线 PNG | Week3 D4 收官。下一步 D5：分层采样器 + 完整 DAG 串联；Week4 产品化 |
| 2026-08-26 | **Week3 D5 完成（分层采样器 + DAG 串联，Week3 收官）**：`mm_curation/sampling/sampler.py`（Sampler 基类 + RandomSampler + StratifiedSampler，按 clip_alignment 质量分桶 × tags 类目交叉分层 + 高质量过采样，最大余数法分配额度保证预算守恒）+ `scripts/eval_sampling.py`（固定预算下 random vs stratified 对比，零重编码加速技巧：复用 clean_v2 全量索引过滤子集，IndexFlatIP 精确检索相对排名不变）+ DAG 全流程串联（7 阶段真实命令，SCAFFOLD 全部移除）。**核心结果**：分层采样在 3 个预算点全胜——budget=1200 时 R@1 0.420→0.496（+18%）、MRR 0.509→0.572；budget=1000 时 R@1 0.353→0.437（+24%，优势最大）；target 命中率 stratified 普遍更高（高质层代表性更好）。**Week3 灵魂叙事闭环**：清洗 R@1 +21% → 再加分层采样 +18~24%（复合收益可证）。86 测试全绿（+11 新）；`make eval-sampling` 一键复现 | **Week3 收官**。Week4：Streamlit Demo / CI / 消融实验 / INTERVIEW.md |
| 2026-08-26 | **Week4 D1 完成（Streamlit Demo 上线）**：`scripts/streamlit_app.py` 四 tab 可交互演示——①检索（文搜图/图搜图，索引切换 dirty/clean，top-k 结果网格带脏数据标注）②清洗漏斗（各级通过率柱图 + 检索对比表 + 采样对比表 + 召回/误杀 metric 卡）③算子评测（P/R 表 + 完整召回矩阵 + 阈值敏感性曲线 PNG 多选）④丢弃样本（按算子过滤 + 随机抽样 15 条图文网格，带 dirty 标签与丢弃原因）。Streamlit 1.55，`make demo` 一键启动，已冒烟验证 HTTP 200。86 测试绿；ruff 干净 | 下一步 D3：GitHub Actions CI；D4：消融实验；D5：INTERVIEW.md 面试叙事 |
| 2026-08-26 | **Week4 D4-D5 完成（消融实验 + 面试叙事，Week4 收官）**：`scripts/eval_ablation.py`（逐个 + 分组移除算子，零重编码：重跑漏斗后过滤 dirty_raw 索引子集评测）+ `docs/INTERVIEW.md`（STAR 故事 + 8 个预想追问 + 简历 bullet）。**消融核心发现**：单个算子 ΔR@1 均为 0（非 load-bearing），但分组消融显示**去重组移除后 R@1 -0.017**（246 条重复回流，唯一显著组）——证明清洗是系统性工程，去重四件套贡献最大。文本/图像质量算子对 held_out 检索无影响（它们丢的样本本就不会被查询匹配）。86 测试绿；`make eval-ablation` 一键复现 | **Week4 收官**（D1 Demo + D4 消融 + D5 INTERVIEW）。仅剩 D3 CI（可选） |
| 2026-08-26 | **Week4 D3 完成（GitHub Actions CI）**：`.github/workflows/ci.yml`——push/PR 触发 ruff lint + format check + pytest（CPU torch + FakeEncoder，3 个数据依赖测试 skipif 自动跳过）。README 加 CI 徽章。本地验证全绿 | **Week1-4 主线 100% 完成** |
