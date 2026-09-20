# 设计表 — V2 β 阶段：文本语料实例（框架通用化的第一次实战）

> 目标：证明 α 的协议与 SDK 在纯文本语料上**零特例**地工作——
> 文本算子注册进同一注册表、文本污染器走同一协议、10 万级去重基准、
> GPT-2 zh 干净/脏训练对比（文本模态的训练级证据，镜像 P4 实验）。
> 这一步完成后，项目定位从"多模态管道"变为"数据质量框架（图文+文本双实例）"。

## 决策点 1：语料源选择

| 选项 | 可得性（hf-mirror 实测待验证） | 脏度 | 结论 |
|---|---|---|---|
| A. MNBVC 子集（真实中文网爬文本） | 待 spike：文件列表/分片大小/嵌套 schema | 高（真实脏） | **首选**——真实脏文本才能体现清洗价值 |
| B. wikimedia/wikipedia zh | 可得性高，结构规整 | 低（太干净） | fallback + 干净对照语料 |
| C. WuDao 开放子集 | 许可证存疑 | 中 | 弃 |

**推荐（T0 spike 已验证，2026-09-02）**：**B 为主（wikimedia/wikipedia 20231101.zh，
6 个 parquet 分片确认可得，最小 126.8MB，pyarrow 按 row-group 增量读取取 10 万文档），
程序化污染提供脏度**；MNBVC 降级为可选扩展——spike 实测其 2.4 万分片的中文类目
结构复杂（wiki 类目实为英文 wikihow），schema 探索成本超出 β 预算。
维基语料偏干净不是缺陷：**脏度由污染器注入并自带 ground truth**（项目签名方法论），
维基 zh 同时充当 PSI 参考分布与训练对比的 held-out 测试集。

规模决策：**10 万文档**（约 50-100MB 文本）。理由：MinHash-LSH 单机可跑
（内存 ~256MB@128perm），去重基准有统计意义，下载/处理时间可控（<30 分钟）。

## 决策点 2：文本算子清单（注册进框架，cost_class 按实分配）

| 算子 | 语义 | cost_class | 对应污染器（靶子） |
|---|---|---|---|
| doc_length | 文档字符数 min/max（默认 50~20000） | rule | truncate/whitespace_pad |
| line_repetition | 行级重复率（正文段落/模板句复制） | rule | paragraph_repeat |
| boilerplate | 广告/版权/导航模板句正则匹配率 | rule | boilerplate_inject |
| pii_detect | 手机号/身份证/邮箱正则命中 | rule | pii_inject |
| perplexity | GPT-2 zh 困惑度（超阈值=乱码/低质） | model | mojibake/字符噪声 |

复用算子：chinese_ratio、char_repetition、minhash_lsh（已有，双模态已声明）。
全部 `modalities=frozenset({"text_article"})`、`required_fields={"text"}`。

## 决策点 3：10 万级去重基准

- 精确去重：md5（已有）
- 近似去重：minhash_lsh（已有，num_perm=128）
- 基准协议：注入 ground truth（exact_duplicate / near_duplicate_text 污染器
  的文本版：8-gram 复制 + 局部删字）后测 P/R + **吞吐/内存曲线**
  （1 万 / 5 万 / 10 万 / 50 万四档）——这组数字是 γ（Ray 分布式）的对照组，
  也是"什么时候必须分布式"的量化答案
- 交付：`data/reports/text_dedup_benchmark.{json,md}` + 扩展曲线

## 决策点 4：文本模态训练对比（镜像 P4）

- 模型：`uer/gpt2-chinese-cluecorpussmall`（HF 镜像可得，~400MB）
- 协议：同一基座，等步数分别在**清洗后语料**与**注入脏语料**上继续训练，
  在**维基 zh held-out 测试集**上测困惑度——脏数据训练的模型 ppl 应显著更高
- 预算：seq_len 256、batch 8、~2000 步，4060 上约 1-2 小时
- 交付：`data/reports/finetune_text_eval.{json,md}`——文本模态的训练级证据
- 风险预案：GPT-2 zh 不可得/太慢 → 降级 bert-base-chinese MLM 困惑度（同样有效）

## 决策点 5：文本污染器（靶子与算子一一对应）

| 污染器 | 注入方式 |
|---|---|
| paragraph_repeat | 随机段落复制 1-3 次 |
| boilerplate_inject | 注入广告/导航模板句 |
| pii_inject | 注入合成手机号/邮箱（合成，无真实 PII） |
| whitespace_pad | 大量空白/换行填充 |
| （复用）truncate_text / mojibake / exact_duplicate | 已有 |

## 模块与任务落点

```
packages/curation-eval: 文本污染器（通用协议，无图像依赖）
src/mm_curation/operators/text_corpus.py: 5 个文本算子（新文件）
src/mm_curation/data/text_sources.py: 语料下载器（MNBVC/维基，走镜像+UA）
scripts/text_dedup_benchmark.py / scripts/finetune_gpt2.py: 两个实验入口
```

## 风险

| 风险 | 预案 |
|---|---|
| MNBVC 分片 schema 复杂/下载慢 | T0 spike 半天验证；降级维基 zh + 程序化增脏 |
| GPT-2 zh 训练超预算 | 降级 bert-base-chinese MLM；或减半步数（对比实验在乎差值不在乎绝对值） |
| 困惑度阈值无参考 | 先在干净语料上采参考分布（复用 PSI 的参考 profile 思路） |

## 验收标准（模块级）

1. 10 万文本文档经 `text_article` 模态走完整漏斗（5 新算子 + 复用算子），配
   置 fail-fast 与模态跳过零特例工作
2. 去重基准：10 万档吞吐/内存/召回三数字 + 四档扩展曲线
3. 文本训练对比：clean_ft vs dirty_ft 的 held-out ppl 差值显著（>5%）且方向正确
4. 全部测试绿（主仓库 112+新增，包 29+新增）；A5 单一来源守卫持续通过

---

# γ 阶段设计表：Ray 分布式执行层（2026-09-03）

> 蓝图来自 ARCHITECTURE_V2 决策 2（方案 B：本地零依赖 + Ray 懒加载双实现）。
> γ0 spike 结论（Windows 本机实测）见 docs/DEV_PLAN.md 开发日志与笔记 #51。

## 决策点 1：Ray 执行器归属与依赖策略

| 项 | 决策 | 理由 |
|---|---|---|
| 归属 | `curation_eval/ray_executor.py`（与 LocalSequentialExecutor 同级） | 执行器协议属 SDK 的产品面；主仓库只是消费方 |
| 依赖 | 懒加载 `import ray`（仅在 `RayDistributedExecutor.__init__`）；pyproject 加 extra `[ray]` | 不装 ray 的环境 import 包/跑本地漏斗零影响 |
| 选型失败预案 | Windows 原生 ray 不可用 → WSL2 路线（文档化）或多进程池降级执行器 | γ0 spike 裁决 |

## 决策点 2：算子在两种运行时下的语义映射

| 算子类别（注册表元数据） | Local 串行 | Ray 分布式 |
|---|---|---|
| 单样本算子（RULE/PERCEPTUAL/…） | 逐个 `op(s)` | `ds.map_batches` 批间并行（算子实例 cloudpickle 下发；模态不匹配保留不评判计 skipped） |
| BatchOperator，shardable=True | 全量 `run_batch` | 按块 `run_batch`（batch 仅为效率包装，逐样本独立 → 分片语义不变） |
| BatchOperator，shardable=False（去重等全量视角） | 全量 `run_batch` | **汇聚单点执行**：`take_all()` → `run_batch` → 重建 Dataset（协议注释已声明 reduce/shuffle 属二期） |
| StageStat 可观测性 | 进程内统计 | 每级 materialize 后用同一 `_score_stats` 统计（分数进 meta，两运行时同源） |

## 决策点 3：等价性口径（γ3 验收的"一致"定义）

- ray map_batches 不保序 → 等价性定义为：**kept 集合按 id 相等 + 每级 StageStat
  数字相等 + 每样本分数（meta score:*）逐 id 相等**，行序不承诺
- 确定性保障（γ3 首跑教训：条数相等但集合不等——去重簇代表依赖块序）：
  批量算子执行前按 id 规范化排序（簇代表 = 最小 id），跨运行时/跨次运行
  的去重输出因此确定；本地 id 为零填充递增，行为与既有结果完全一致
- 等价性测试跑 7 个 CPU 算子（doc_length…text_minhash）；perplexity（MODEL/GPU）
  本期仍走本地（Ray worker 的 GPU 调度与权重分发列为后续，报告注明）
- CI：ray 相关测试 `pytest.importorskip("ray")`——CI 默认不装 ray，自动跳过

## 风险

| 风险 | 预案 |
|---|---|
| Windows 原生 ray 限制（spike 裁决） | 降级路线已定（见决策点 1） |
| map_batches batch_format 对 Python 对象的行为差异 | γ0 spike 实测钉死；Sample 走 cloudpickle |
| ray 本地集群内存（object store）过大 | init 显式 `object_store_memory` 上限 |

---

# δ 阶段设计表：L3 LLM-judge（2026-09-03）

> 蓝图来自 ARCHITECTURE_V2 决策 7：judge 是普通注册算子 + 独立推理边界
> （OpenAI 兼容客户端）；可信度用 Cohen's kappa 证明。

## 决策点 1：服务边界与 Windows 现实

| 项 | 决策 | 理由 |
|---|---|---|
| 客户端 | OpenAI 兼容 `/v1/chat/completions`（base_url/model 配置化，api_key 走 env） | 决策 7 的推理边界；换 provider 不改算子 |
| 服务端 | 附带 `scripts/serve_judge.py`（FastAPI 极简兼容层，包本地 HF Instruct 模型，默认 Qwen2.5-0.5B-Instruct，hf-mirror + safetensors） | vLLM 不支持原生 Windows；本地 0.5B 够跑通协议与实验，Linux 换 vLLM 零改动 |
| 失败语义 | `on_error: skip`（默认：超时/解析失败 → 保留不评判，score=None）/ `fail` | L3 是增强不是阻塞——服务挂了漏斗不该死 |

## 决策点 2：抽样协议（成本意识）

- `sample_rate`（默认 0.1）：确定性抽样 `sha1(seed + sample.id) % 10**9 / 10**9 < rate`
  ——同一 config 重跑抽同一批，可复现可审计
- judge 只看进入该级的存活样本（L1/L2 已拦大头，L3 只裁决歧义区）

## 决策点 3：rubric 与解析

- prompt：中文，角色=LLM 训练语料质量审核员，只输出 JSON
  `{"score": 0-10 整数, "reason": "<=30字"}`
- 解析：正则抽首个 JSON 对象 → 失败置 None；score/10 归一化写
  `meta["score:llm_judge"]`；阈值 `min` 默认 0.5
- 批内并发：ThreadPoolExecutor（服务是 IO 边界）；逐样本独立 → shardable=True

## 决策点 4：kappa 评测协议（δ2 验收）

- `curation_eval.metrics.cohen_kappa()`（框架级指标，非本项目私有）
- `scripts/eval_judge.py`：污染器造带标注脏集 → judge 全评抽样 →
  (a) judge vs 脏标签（可信度主证）；(b) judge vs L1 漏斗判定（增量信息：
  kappa 高 = L3 冗余，低 = 互补）；(c) 分歧样本清单进报告

## 风险

| 风险 | 预案 |
|---|---|
| 本地小模型判力弱 → kappa 低 | 如实报告——kappa 是可信度证明不是宣传数字；换更大模型只改 base_url |
| judge 输出不守格式 | 解析失败 → None 保留不评判 + 计数入报告（诚实呈现解析率） |
| CI/无卡环境 | 算子测试全走 FakeClient，零网络零 GPU |

---

# 补强设计表：图像漏斗 × Ray 等价性验证（2026-09-03）

> 背景：γ3 只验证了文本漏斗（configs/text_funnel.yaml），图像漏斗
> （configs/pipeline.example.yaml，cn_flickr_curation_v2）从未在 Ray 下跑过。
> 候补池盘点（DEV_PLAN 2026-09-03 第四会话）确认 19/19 算子 V2 元数据齐备、
> 框架层确定性修复（run_batch_mixed_modality 执行前按 id 排序，sdk.py）已覆盖
> 全部批量算子——理论风险低，但"从未实测"本身就是债。

## 决策点 1：语料与漏斗来源

| 项 | 决策 | 理由 |
|---|---|---|
| 漏斗 | `pipeline.example.yaml` 剔除 GPU 算子（clip_alignment / semantic_dedup），余 9 级 CPU（text_length…minhash_lsh） | 与 γ3 同套路：GPU worker 调度属后续；phash_near O(n²) 991 张无压力 |
| 语料 | `data/interim/contaminated/samples.jsonl` 全量（991 条，含注入污染） | 验证等价性必须有重复对——污染集是现成的靶子；量小全量跑，不抽样 |

## 决策点 2：装载方式

- `Sample.from_dict` 装载（v1 caption 键永久兼容，schema.py 已声明）；
  image_path 非空自动推断 image_caption 模态，装载代码不需要特判
- 读图失败的样本（OSError）：算子内静默跳过，两运行时行为同源——
  等价性口径天然覆盖，无需预处理

## 决策点 3：等价性口径（γ3 三口径 + 一条图像专属）

1. kept 集按 id 相等；2. 每级 StageStat 数字相等；3. 逐 id 分数（score:*）相等
4. **新增：dedup 标记逐 id 相等**（`meta["dedup:*"]["duplicate_of"]` 映射）——
   簇代表选择是本次靶子，只比 kept 集比不出"代表换了谁"

## 决策点 4：实现形式

| 方案 | 决策 | 理由 |
|---|---|---|
| A 泛化 γ3 脚本加 --config | 否 | 装载逻辑文本专属（按 text 字段），泛化会把两个模态的装载揉进一个脚本 |
| B 镜像新脚本 `scripts/ray_image_funnel_benchmark.py`，共用 stage_diff 等价函数 | **采用** | 与 γ3 报告并列（data/reports/ray_image_funnel_benchmark.{json,md}），口径代码从 γ3 脚本 import 不复制 |

## 风险

| 风险 | 预案 |
|---|---|
| 框架层 id 排序修复未覆盖某算子路径（如 v1 无元数据算子） | 等价性不通过即如实落报告——阴性结果照 γ3 先例处理，反查 run_batch_mixed_modality 覆盖面 |
| Ray 下读图路径（相对路径 image_path）在 worker 的 CWD 不同 | γ0/γ3 已验 Windows 本地集群同 CWD；报告注明前提，多机属二期 |
| phash_near 在 991 张上的 O(n²) 耗时 | 实测预估秒级（991² ≈ 10⁶ 次海明比对），不构成风险，报告记耗时即可 |

## 验收标准

- 991 条全量 local/ray 双跑，口径 1-4 全等 → 报告 + 等价性测试入 tests/
  （ray importorskip 守卫，与 γ 同款）；任一不等 → 报告如实呈现差异清单，
  转入根因分析而非强行对齐

---

# 补强设计表：ε 数据 CI 门禁扩到图像去重（2026-09-03）

> 背景：data_ci_benchmark.py 只锁了文本 dedup_fast；图像去重（md5_exact /
> phash_near）没有任何质量门禁，换源漂移抓不到。方法论沿用 α/ε：
> **先标定生成器（损伤强度）再定门限，避免门禁测成生成器**。

## 决策点 1：脚本形式

| 方案 | 决策 | 理由 |
|---|---|---|
| A 扩展 data_ci_benchmark.py 加 --modality | 否 | 装载/损伤/比对全不同，一个脚本两种人格 |
| B 镜像新脚本 `scripts/data_ci_image_benchmark.py` | **采用** | 与文本门禁并列；门限判错仅 10 行，不为它建公共抽象 |

## 决策点 2：合成图像语料（seed 固定可复现）

- base 2000 张：8x8 随机灰度块放大到 64x64（笔记 #57 结论：低频结构是
  phash 可区分的前提）。两两塌缩靠 64bit 随机 hash 的统计距离（期望 32，
  P(≤12)≈6e-7 → 2000 张期望塌缩 ~1 对），1% 误杀门限兜底，
  **不做两两自校验**（200 万对不可行也不必要——那是 10 张量级的手段）
- exact 300 张：base 字节复制（新 id）→ md5_exact 靶子
- near 500 张：base 轻度裁剪 fx,fy∈U(0.90,0.96) + JPEG q∈U(35,50)
  ——**与 V1 污染器 near_duplicate_image 同参数**（真实数据校准过的损伤），
  脚本内手工实现（V1 Contaminator 绑定 ContaminationContext，不适配门禁的
  定向注入），参数注释对齐 V1

## 决策点 3：门限标定流程（先标定后定限）

1. 标定跑：全量注入后打印 near 样本的 phash 距离分布（对 base 代表）
   与实测召回/误杀
2. 门限 = 实测值向下取安全位（预期 exact 1.0 → ≥0.99；near ~0.9 → ≥0.85
   待实测定；误杀 ≤1% 同文本）；实测达不到预期就先修生成器再定门限
3. 劣化注入验证：加重损伤（如 crop 0.80）确认 gate 真会红（exit 1）——
   与文本 ε 收官时的验证同款

## 风险

| 风险 | 预案 |
|---|---|
| near 注入后 phash 距离超出 12（召回低） | 先标定后定限；若召回 <0.85 说明生成器损伤过重，修生成器参数而非放水门限 |
| base 两两塌缩超过 1% | 统计上不可能（期望 1 对），真发生则说明 base 构图有系统性问题，回查 |
| phash O(n²) 2800 张耗时 | 读图+phash ~45s，比对 ~10s，CI 可接受；留 --scale |

## 验收标准

- 门禁脚本默认参数跑出 GATE PASSED，门限经标定背书（数字写进脚本 docstring）
- 劣化注入（加重裁剪）实测 exit 1
- ruff 全绿；报告数字回写 DEV_PLAN 开发日志

---

# η 阶段设计表：偏好闭环试点 + 迁移验证（2026-09-04）

> 依据：docs/PRD.md §九三条差距。η-c 是热身（补验收 4 欠账），η-a 是本阶段核心，
> η-b 视 η-a 结果再立项。本表经确认后才动代码。

## η-a 决策点 1：「偏好」的操作化定义

| 项 | 决策 | 理由 |
|---|---|---|
| 偏好维度 | **详略偏好**：精炼派（PA）vs 求全派（PB） | 改写可程序化构造（无 LLM 偏差、可复现）；两 persona 在同一维度上取向相反，天然对称；「分歧率」证据直观 |
| persona 协议 | 选择规则**文本化入 manifest**：PA=保导语要素（时间/地点/主体/结果），容忍删细节；PB=保数字/引语/背景细节，容忍篇幅 | 「偏好协议」是产品资产的一部分——换 persona = 换一段协议文本（这正是「个人化」的最小可行形态） |
| 诚实边界 | v1 的标注是 **persona-oracle**（确定性规则函数模拟真人 A/B 选择），不是真人标注；manifest 与报告如实写 | 真人标注是 η 后续；先把「偏好进训练信号→偏好改变模型行为」的机制链路打通并量化 |

## η-a 决策点 2：偏好对构造

- 源文档切分：导语段（首段）+ 细节段（含数字/引语的后续段落，规则识别）
- 变体 S = 标题 + 导语；变体 F = 标题 + 导语 + 全部细节段；偏好对 = (S, F)
- **候选顺序随机化**（甲/乙 50/50）防位置偏置
- 数据量：400 文档 × 2 persona = 800 判定题（DPO 三元组或 SFT 行）
- 防退化（不许学成纯长度分类器）的保障：验收集含**内容对照题**——同长度的
  S vs F、以及「带损伤的 F vs 干净的 S」对照子集（各 ≥30 题），oracle 判定
  依赖要素计数而非字节数；对照题上两判官的表现单独报告

## η-a 决策点 3：训练方案（主案 DPO，退化案 SFT）

| 方案 | 形式 | 依赖/显存 | 触发条件 |
|---|---|---|---|
| 主案 DPO | prompt = persona 协议 + 候选甲/乙；chosen/rejected = 正确/错误选择的同格式 JSON `{"choice":"甲","reason":"…"}`。trl DPOTrainer + peft LoRA（Qwen2.5-0.5B-Instruct，beta=0.1，lr 5e-6 级） | trl 未装（装 0.12-0.17 区间兼容 transformers 4.57）；ref model 用 adapter-disabled base，显存 ≈3GB，8GB 可跑 | trl 可装且 DPOTrainer 跑通 |
| 退化案 SFT | 同 prompt 同 completion，正例直接 SFT（现有 finetune 管线加 preference 模式） | 零新依赖 | trl 不可用 / DPO 学崩（表现为命中率不升或格式崩坏）——如实记录后降级 |

- 工程红线沿用 #58/#59：训推同 chat template；completion 完整进窗口
  （persona 协议 + 两候选各截 600 字符 + JSON → **窗口 1024**，batch 4 显存不够就 2+梯度累积）
- 数据隔离：DPO 数据排除 judge_news_v1 源文档（结构性隔离沿用 ζ），seed 用新族（如 31）

## η-a 决策点 4：评测口径与验收线

- 冻结 benchmark `benchmarks/pref_news_v1`：held-out 100 文档 × 2 persona = 200 题；
  manifest 含 persona 协议文本、seed、对 SFT/DPO 数据文件的泄漏检查
- 指标：①**命中率**（与对应 persona oracle 一致比例）②**分歧率**（PA-判官与
  PB-判官对同题选择不同的比例——「偏好进了信号」的直接证据）③通用基线
  （0.5B 不微调、同 prompt）命中率
- **验收线**：两判官命中率各 ≥0.75 且**分歧率 ≥40%**；通用基线预期 ≈0.5
  （如显著偏离如实报告并解释）；对照题子集单独出数
- 产物：runs/experiments.jsonl 逐 run 落账 + data/reports/pref_alignment.md 钱表

## η-b（待 η-a 后立项，预研性记录）

第二任务候选：文风模仿判官（同文体的两段续写哪个「更像原文风格」——
构造靠句长分布/连接词风格程序化克隆，难度高于详略偏好）或抽取质量判官
（两份抽取结果哪个漏了原文数字——可用对齐计数构造）。骨架复用已验证，
难点在 oracle 设计，届时单独走设计门。

## η-c 决策点：域外泛化补账（热身，已确认可先行）

| 项 | 内容 |
|---|---|
| 做法 | 现有脚本零改动跑通：wiki 语料建 `benchmarks/judge_wiki_ood`（100/100，seed 9500，--train-jsonl 对 judge_sft.jsonl 跑泄漏检查）→ v3 adapter 与 generic 各跑一遍 |
| **工具补丁披露（需随 η-a 一起确认）** | build_judge_benchmark.py 的 name/domain 目前硬编码 judge_news_v1——OOD 产物会贴错域标签。需加 `--name/--domain/--seed` 三个 CLI 透传参数（约 5 行，不改逻辑） |
| 指标 | κ / P / R 相对域内（+0.560/0.706/0.960）的衰减幅度，如实出报告 data/reports/judge_ood_report.md |
| 预期 | 泛化衰减真实存在（域专属正是卖点）；若 κ 崩到 0 也如实记——「域专属」本来就是产品主张 |

---

# η-b 设计表：第二任务示范——抽取忠实性判官（2026-09-05）

> 目的：兑现 PRD 验收 4 的另一半——「同一骨架可套多任务」。η-a 证明了主观
> 偏好可训练，η-b 证明客观任务同样走这套四步骨架，且任务语义/oracle/数据
> 构造全部更换、只有接口形态与训练配方复用。

## 决策点 1：任务选型

| 项 | 决策 | 理由 |
|---|---|---|
| 任务 | **抽取忠实性判官**：给定新闻原文与两份要点抽取，判哪份忠实 | oracle 完全客观可数（事实-原文对齐），与 η-a 的主观偏好形成「主观/客观」轴多样化；构造稳健（预研实测：排除既有占用后空闲语料最初仅 ~310 篇——预研初版漏排 pref 占用，已修正；增量爬取后满足 500 需求，事实句中位 8 条/文） |
| 接口形态 | 刻意复用 A/B 双候选 + JSON 裁决 | 「骨架复用」的证明点——变的必须是任务语义，不变的是协议位 |
| 弃选 | 文风模仿判官（风格特征距离 oracle 噪声大、构造难），列入候补 | 风险表预判过 |

## 决策点 2：数据构造（oracle 客观可数）

- **好抽取**：从原文事实句（含数字/引语，≥15 字）中按出现序取 k 条（k∈3-5 随机），**句序打乱**（防「前 k 句」表面捷径，笔记 #49 同源教训）
- **坏抽取**三损伤均匀分布：
  a. 数字篡改：事实句中数字替换为相近值（±1 位或改 2-3 位——防过易）
  b. 幻觉注入：混入一条他文事实句（跨文档幻觉，最可判）
  c. 关键遗漏：删 1-2 条事实句（最难，预期命中率最低）
- 候选对 50/50 甲乙随机化；chosen/rejected **最小对**（#60 教训直接内建）
- 数据量：400 训练文档 × 1 对 + 对照分层子集；eval 100 文档冻结

## 决策点 3：训练与评测（复用 η-a 全套配方）

- 训练：trl DPO + peft LoRA + Qwen2.5-0.5B + bf16/sdpa + precompute_ref_log_probs + 窗口预算（候选截 350 字）——配方零改动，只换数据文件与协议槽位（persona 协议 → 忠实性协议：「抽取中的每条事实须能在原文找到依据，关键事实不得遗漏」）
- 冻结 benchmark `benchmarks/ext_news_v1`：100 题 + 损伤分层；seed 新族 41；结构性排除全部既有占用（judge benchmark 150 / judge SFT 500 / pref 数据与 benchmark 源）+ 对 DPO 数据文件跑泄漏检查
- 指标：总命中率（验收线 **≥0.75**，通用基线对照 ≈0.5）+ 分损伤命中率（篡改/幻觉/遗漏三层，不设线如实报告）

## 风险

| 风险 | 对策 |
|---|---|
| 「前 k 句」表面捷径 | 句序打乱 + k 随机 |
| 数字篡改过易 | 相近值篡改 |
| 遗漏类过难拉低总分 | 分层报告不混算，验收线只压总命中率 |
| 判官学成「挑短的」之类伪启发式 | 好坏抽取长度分布交叠（坏抽取不改变篇幅的篡改占 1/3） |

---

# η-b' 设计表：分解式逐条忠实性判官（2026-09-06）

> 动机：η-b 实测「整体 A/B 忠实性裁决」超出 0.5B/1.5B 能力下限（两基座 ≈ 随机）。
> 重设计思路：**分解**——把「两份抽取哪份忠实」拆成 N 个「单条事实是否有原文
> 依据」的二元判定，单条任务难度大幅下降；抽取级忠实性得分 = 逐条判定聚合。

## 决策点 1：任务形式

| 项 | 决策 | 理由 |
|---|---|---|
| 输入 | 原文（500 字窗口）+ **单条**事实陈述 | 单次只做一个对齐比较，0.5B 能力圈内 |
| 输出 | `{"supported": true/false, "reason": "依据比对"}`——chosen/rejected 最小对只差 true/false（#60 红线内建） | 字母级最小差异，梯度无模板可逃逸 |
| 损伤类型 | supported（原文逐字）/ number_swap（相近数字篡改）/ cross_doc（他文事实=纯幻觉） | 遗漏类在单条形式下不适用（天然消失），如实记录 |

## 决策点 2：数据与隔离

- 训练 250 文档 → 500 三元组（每文 1 supported + 1 unsupported 交替）
- 冻结 benchmark `fact_ext_v1`：80 文档 × (1 supported + 1 corrupted) = 160 题
  （corrupted 类型 number_swap / cross_doc 交替），seed 新族 47
- 源文档复用 ext 任务占用文档：任务不同则标签独立，跨任务复用源文档不构成
  泄漏（泄漏检查只对同任务数据文件执行）；judge/pref 占用仍全数排除

## 决策点 3：验收线

- 总命中率 ≥0.75（通用基线对照 ≈0.5）
- 分层：supported / number_swap / cross_doc 三层各自命中率如实报告
- 业务聚合口径：一份抽取的忠实性得分 = 其事实逐条判定通过率（本表只验收
  单条判定，聚合 demo 属后续）

## 风险

| 风险 | 对策 |
|---|---|
| number_swap 对 0.5B 仍是细粒度数字比对 | 分层报告如实呈现；若该项拖垮总分，验收线只压 supported+cross_doc 并注明 |
| 与 ext 任务共享源文档的交叉污染 | 不同任务的标签空间独立（supported vs 甲/乙），机制上无共享信号 |

---

# θ 设计表：偏好判官工坊——真人偏好闭环 + 大众可用向导（2026-09-06）

> 动机：PRD §九差距 1（偏好闭环还是 persona-oracle）+ 差距 3（「个人可驾驭」=
> 开发者可驾驭）。η-a 已证明「偏好进训练信号」机制成立（DPO 命中率 0.933/0.867）；
> θ 把标注来源换成真人点击，并给「数据→benchmark→训练→评测→试用」全流程一个
> 非开发者可用的外壳。用户已确认方向（个人偏好判官 + Streamlit 全流程向导）。

## 决策点 1：真人标注数据链路（现有工具的硬缺口）

| 项 | 决策 | 理由 |
|---|---|---|
| 现状缺口 | platform_app.py 标注页只落 `cand_a_chars/cand_b_chars`（字数），候选全文不落盘——存量标注无法还原成 DPO 对，真人闭环事实上断裂 | 现场盘点发现；标注工具先于数据构造需求存在，属设计时序缺口 |
| 标注 v2 | 新文件 `data/annot/pref_labels_v2.jsonl`：每行落**候选全文**+变体元数据（source_id / variant_a / variant_b / 协议文本 / labeler=human\|oracle / choice）；旧 v1 文件保留不动 | 全文是 DPO 构造的必要输入；labeler 字段为「模拟用户先行验收」留通道 |
| 标注来源 ×2 | ①程序化变体对：导入/示例文档 → 复用 preference.py 的 S/F 切分生成器（候选截 350 字沿用）；②自由对：用户自己贴 甲/乙 两段 | ①零门槛可批量；②兜底非新闻结构文档（切分失败不阻塞标注） |

## 决策点 2：从点击到训练数据（η-a 配方复用）

- DPO 三元组：prompt = PREF_PROMPT(用户协议原文, 甲全文, 乙全文)；chosen/rejected =
  只差「甲/乙」字母的**最小对**（reason 固定）——#60 纪律内建，训练脚本零改动
  （`finetune_judge_dpo.py --persona USER --data data/interim/pref_user_dpo.jsonl`）
- holdout **按偏好对为单位**切 25% 进 benchmark（同一对的文本不得同时进训练与评测）；
  REJECT（两个都不合格）不进训练、只进统计——chosen 不存在无法构造最小对，如实记录
- 对照题：程序化损伤对（广告样板注入 vs 干净版，复用 η-a 的 _BOILERPLATE）以
  kind=control 入 benchmark——「先看污染，再谈偏好」的质检位保留
- 偏好协议文本：用户在向导第 1 步用自己的话写（默认给精炼派模板），原文进 prompt 与 manifest

## 决策点 3：个人版冻结 benchmark 与验收线（两本账）

| 项 | 决策 |
|---|---|
| benchmark | `benchmarks/pref_user_v1`：held-out 真人点击题 + 对照题；manifest 含协议原文、标注量、labeler=human 声明、对 DPO 文件的泄漏检查 |
| 流程验收（模拟用户） | oracle 按 v2 通道造 ≥200 条标注先行跑通五步，held-out 命中率 **≥0.75**（oracle 信号应接近 η-a 水平；不达标如实归因通道差异） |
| 真人验收 | 用户真实点击 **≥150 对**，held-out 命中率 **≥0.70**（真人噪声，较 oracle 线 0.75 放宽并注明），通用基线（0.5B 同 prompt）对照预期 ≈0.5 |
| 学习曲线 | RUNBOOK 级实验（非向导默认路径）：50/100/200 点击子集各自训练评测 → 「最少多少次点击显著超随机」数字 |
| 两本账 | 模拟用户与真人的数字分开报告，不混算 |

## 决策点 4：向导形态（scripts/judge_studio.py，新入口）

- 五步向导（侧边栏步骤条）：①导入（粘贴/上传 txt·md，或一键「用示例语料」= 新闻爬取
  产物排除 judge 占用）→ ②点击标注（甲/乙大按钮 + REJECT + 进度条 + 建议量提示）→
  ③一键训练（subprocess 现有脚本，全默认参数，实时 tail loss 日志）→ ④冻结 benchmark +
  评测（对比出分：通用 vs 你的判官）→ ⑤试用（贴任意 甲/乙 对，判官替你选）
- 每步一句白话解释（「这一步在做什么」）；不暴露任何路径 / YAML / make
- 旧 platform_app.py 标注页保留不动（兼容）；工坊为独立入口 `streamlit run scripts/judge_studio.py`
- run_pref_benchmark.py 报告落盘名改为按 benchmark 名区分（`pref_alignment_<name>.json`，
  约 5 行）——避免向导评测覆盖 η-a 的 pref_alignment.json（ledger 本就可回溯、报告不入库，
  此处只为并行工作流互不覆盖）

## 数据结构表

| 数据 | 字段 | 消费方 |
|---|---|---|
| `data/annot/pref_labels_v2.jsonl` | ts / session / protocol / source_id / cand_a / cand_b / variant_a / variant_b / choice(甲\|乙\|REJECT) / labeler(human\|oracle) / note | build_user_pref_data.py |
| `data/interim/pref_user_dpo.jsonl` | 同 pref_dpo.jsonl：persona/kind/prompt/chosen/rejected/gold/gold_variant/source_id | finetune_judge_dpo.py（零改动） |
| `benchmarks/pref_user_v1/{items.jsonl,manifest.json}` | 同 pref_news_v1 + manifest.labeler=human + 标注统计 | run_pref_benchmark.py / 控制台矩阵页 |
| `runs/experiments.jsonl` | 不变（共享追加账本） | 控制台 / 报告 |

## 接口约定表（CLI）

| 命令 | 输入 → 输出 | 退出码 |
|---|---|---|
| `python -X utf8 scripts/build_user_pref_data.py --labels <v2.jsonl> --out-dpo <...> --out-benchmark <dir> [--holdout 0.25] [--limit N]` | v2 标注 → DPO 文件 + 冻结 benchmark（manifest 含泄漏检查） | 0=成；2=标注不足（低于最低可训量） |
| `python -X utf8 scripts/finetune_judge_dpo.py --persona USER --data ... --out models/judge_pref_USER` | 现有脚本零改动 | 不变 |
| `python -X utf8 scripts/run_pref_benchmark.py --benchmark benchmarks/pref_user_v1 --adapters USER=models/judge_pref_USER --generic` | 现有脚本 + 报告名小改 | 不变 |
| `streamlit run scripts/judge_studio.py` | 向导入口（内部 subprocess 调上面三步） | — |

## 流转表（标注 → 判官 生命周期）

```
导入文档 ──→ 变体对生成 ──→ 待标注队列 ──(点击 甲/乙/REJECT)──→ v2 标注文件
                                                                    │
                              ┌─────────────────────────────────────┘
                              ▼
              build_user_pref_data（按对 holdout 25% + 对照题生成 + 泄漏检查）
                ├── train 75% → pref_user_dpo.jsonl ──→ DPO/SFT 训练 → adapter
                └── eval  25% → benchmarks/pref_user_v1 ──→ 冻结评测（+通用基线）
                                                                │
                                        ledger 落账 → 向导出分页 → 试用页
```

向导状态机：EMPTY → COLLECTING(<150) → READY(≥150) → TRAINING → EVALUATED
（可回第②步追加标注后重训，adapter 按 out 目录版本化）

## 风险

| 风险 | 对策 |
|---|---|
| 真人标注噪声/自相矛盾 | 验收线放宽 0.70 + 注明；学习曲线如实；产品语义 =「判官复现你的显性一致性」，manifest 写明 |
| 标注疲劳半途而废 | 候选截 350 字（η-a 沿用）；建议量进度条；150 对起步（学习曲线可能证明更少即可） |
| 用户文档非新闻结构，切分失败率高 | 导入步报告可用率；自由粘贴对兜底 |
| 真人标签下 DPO 学崩（η-a 首训教训在噪声下放大） | 最小对纪律内建；SFT 退化案已就绪（--sft），如实降级 |
| GPU 被在途任务占用 | 向导启动时 cuda 检测明示；训练/评测分步手动触发，不自动排队 |
| 与 η-b' 在途代码冲突 | 工坊全部为新文件 + preference.py 增量函数；不触碰 tuning/extraction.py |

## 验收标准汇总

1. 五步向导 e2e（模拟用户 oracle 标注 ≥200 条）：标注→DPO→冻结 benchmark→训练→评测→试用全通，held-out 命中率 ≥0.75
2. 真人标注 ≥150 对：held-out 命中率 ≥0.70 vs 通用基线 ≈0.5（两本账分开报告）
3. 对照题（损伤否决）单独出数不设线
4. 测试基线不倒退（当前 162+40），新增单测覆盖 v2 schema 解析 / 按对 holdout / 最小对构造 / REJECT 剔除
5. ruff 全绿；RUNBOOK θ 段 + DEV_PLAN / ROADMAP / 笔记回写

# ι 设计表：判官能力阶梯——通用 1.5B 补档 + ζ 任务 1.5B LoRA（2026-09-15）

> 动机：对标调研确认 Data-Juicer 2026-08 发布 Juicer 模型（NL 指令数据精炼）正面进入
> 域判官方向；本仓能力矩阵（benchmarks/capability_matrix.json）现状是「悬崖在 1.5B 之后」
> 的结论只有 η-b 单任务支撑，且「通用 1.5B」整档缺失、「换大模型只改 base_url」从未实测。
> 本表经用户批准（2026-09-15 会话计划），落表即动码。

## 决策点 1：范围收缩——η-b 1.5B 重训不重做
- 已在案：DPO 1.5B 在 8GB 不可行（258s/step，2026-09-05 实测）；1.5B SFT 退化路径
  0.591/0.515/0.48 ≈ 随机（矩阵第二行，judge_ext_1p5b 适配器）。
- 协作方在途迭代 extraction.py（η-b' 分解式重设计），避让不碰。

## 决策点 2：δ 任务走真实漏斗路径
- `serve_judge.py --model Qwen/Qwen2.5-1.5B-Instruct` 起 OpenAI 兼容服务 →
  `eval_judge.py --base-url` 走 LlmJudgeOp 全链路（确定性抽样 + rubric 解析 + κ）。
- 同时实测 δ 阶段「换大模型只改 base_url」的架构声明。

## 决策点 3：ζ 任务 1.5B LoRA SFT 零改动
- `finetune_judge_lora.py --model Qwen/Qwen2.5-1.5B-Instruct --out models/judge_lora_1p5b`
  （脚本原生支持 --model；SFT 数据 judge_sft.jsonl 现成）。
- 评测 `run_judge_benchmark.py --model ... --adapter models/judge_lora_1p5b --tag tuned_1p5b`。

## 决策点 4：7B 云端档
- 默认无 API key → 矩阵 note 记「待租卡」，不阻塞。

## 数据结构表
| 产物 | 变更 |
|---|---|
| benchmarks/capability_matrix.json | models 增 2 行：通用基线（不微调 1.5B）全任务；Qwen2.5-1.5B LoRA（ζ 列） |
| runs/experiments.jsonl | 追加式，零改动 |
| data/reports/judge_kappa_0p5b.json | 旧 δ 报告备份（评测脚本报告名固定，跑前备份） |
| data/reports/pref_answers_generic_0p5b.json | 旧通用答案备份（同上） |

## 风险
| 风险 | 对策 |
|---|---|
| 1.5B 训练 OOM（8GB） | batch 8 首跑，OOM 降 batch 4 并记录；fp16 |
| GPU 串行争用 | 先评测（server→基准→偏好）后训练，全程单 GPU 任务 |
| ζ 1.5B 训练 2-3h | 后台跑，与 κ 块（CPU/网络）并行 |
| 覆盖旧报告 | 跑前备份 judge_kappa / pref_answers_generic |

## 验收标准汇总
1. 矩阵出「3 任务 × ≥4 模型档」（通用 0.5B / 通用 1.5B / 微调 1.5B / 微调 0.5B），
   每格数字可回溯 ledger。
2. 「能力悬崖在 1.5B之后」被多任务证实或证伪，如实记录。
3. 「只改 base_url」声明实测结论落笔记。
4. 测试基线不倒退；ruff 全绿；DEV_PLAN/ROADMAP/INTERVIEW/笔记回写。

# λ 设计表：SemDeDup 语义剪枝采样（2026-09-15）

> 动机：对标调研确认语义去重/剪枝（NeMo Curator SemDeDup 内置实现）已是头部
> 系统标配，而本仓采样器只有质量×类目分层。池向量（clean_v2 图像塔嵌入）现成，
> 接入成本低，且剪枝发生在检索指标自己的空间——「语义冗余」与「检索」同度量。
> 本表经用户批准（2026-09-15 会话计划），落表即动码。

## 决策点 1：剪枝空间 = 索引空间
- 向量直接 `searcher.index.reconstruct(row)` 取回（零重编码）：clean_v2 索引存
  图像塔嵌入（文搜图），在此空间聚类 = 剪掉「检索视角下的语义冗余」。

## 决策点 2：算法三步（faiss 单依赖，不引 sklearn）
- ① L2 归一化 → `faiss.Kmeans(spherical=True)` 聚 k=64 簇（1586 池，均值 25/簇）；
- ② 每簇按到质心相似度降序保留 (1-ε)，ε 默认 0.2（SemDeDup 论文量级）；
- ③ 存活池沿用 StratifiedSampler 配比补足 budget——与 stratified 的差异被
  隔离在「池子缩小」这一步，消融可归因。
- 向量缺失样本不聚类、视为存活（保守保留）。并列相似度按 id 字典序破平，跨运行确定。

## 决策点 3：接口与回归红线
- `SemanticPruneSampler(vectors, n_clusters=64, prune_frac=0.2)` 进注册导出；
  SamplingRecipe 增 `extra` 字段（n_pruned 等统计，默认空，旧调用零破坏）。
- eval_sampling.py 加 `--prune-fracs`（消融 0.1/0.2/0.3）；**random/stratified
  两条既有数字必须逐位复现**（新增采样器不改它们任何计算路径）。

## 风险
| 风险 | 对策 |
|---|---|
| 1586 小池子上语义冗余本就少，剪枝无增益 | 如实阴性 + 归因（方法-规模匹配话题，本身是面试素材） |
| 球面 k-means 把反义向量分给对面簇（余弦 -1 < 0） | 实现层面无影响（真实 embedding 无反义簇）；单测夹具已按此设计 |
| 剪枝后存活池 < budget | 全出存活池，n_sampled < budget 如实呈现 |

## 验收标准汇总
1. 单测 ≥5 条：离群点剔除 / 预算守恒 / 存活不足 / 可复现 / 无向量保守保留。
2. 消融表 ε ∈ {0.1, 0.2, 0.3} × budget {1200, 1000, 800} 对照 random/stratified。
3. SemDeDup 在 ≥1 个 budget 点 ≥ stratified，否则如实阴性。
4. random/stratified 既有数字逐位复现；测试基线不倒退；ruff 全绿；文档回写。

---

# OPS w1：运维飞轮一期（R0-R4，PRD 见 docs/OPS_PRD.md，2026-09-15）

> PRD 经用户确认开工。宿主仓库决策：mm-curation 内开发（用户确认），抽离触发条件
> 已写入 PRD。勘误两条：① 股票池对齐 findata UNIVERSE 实际口径 25 只（PRD 写 50）；
> ② findata 已有 `scripts/daily_pipeline.py`（采集→巡检→推送→归档，含 schtasks
> 说明与 wecom/钉钉等推送通道）——ops 壳复用它，R1 的告警推送在 findata 侧免费获得，
> ops 侧只做行数预期带判断。本表落档即动码（用户 2026-09-15 明确指示开工）。

## 决策点 1：ops 壳 = 数据驱动的步骤表 + 产物校验 + 非零即停
- `scripts/ops_daily.py` 步骤表（序号对应 PRD 六步，①③合并为一步）：
  1. `findata_daily`：subprocess 调 findata venv python 跑 `scripts/daily_pipeline.py`
     （cwd=FINDDATA_PATH；覆盖 PRD 的结构化采集 + DQ 巡检两步）
  2. `fetch_text`：`scripts/fetch_finance_news.py`（增量新闻 → Sample JSONL）
  3. `funnel`：`run_pipeline.py --config configs/text_funnel_finance.yaml`（全量重跑，
     text_minhash 全局视角需要全量；30 天 ≈3 万条规模无压力）
  4. `audit`：dropped.jsonl 按 dropped_by 聚合 + 每类抽 3 条
  5. `report`：日报渲染 + 磁盘水位 + 台账追加
- 每步带产物检查（路径 + 最小行数），任一步 returncode≠0 或产物缺失 → 记失败、
  跳过其余步骤、**仍渲染日报**（顶部「今日异常」），exit 1。
- 教训对齐：subprocess 显式 PIPE + encoding utf-8 + errors replace（findata
  daily_pipeline 注释在案：schtasks 无控制台会话捕获句柄可为 None）。

## 决策点 2：findata 调用方式
- 解释器：`{FINDATA_PATH}/.venv/Scripts/python.exe`（win32）/ `.venv/bin/python`
  （其余）；FINDATA_PATH 缺省桌面路径（与 findata_health_stage.py 同约定），
  可环境变量覆盖；venv 缺失 → 该步失败并给可行动信息。
- 不用 `uv run`（避免调度会话下重新 resolve/sync 的不确定性）；findata DuckDB
  路径由其自身 settings 管理，ops 不触碰。

## 决策点 3：文本适配器（fetch_finance_news.py）
- 数据源：`ak.stock_news_em(symbol)`（东财个股新闻，akshare 1.18.35 实装验证）。
- 幂等：读既有 JSONL 建 url 集，重跑只补增量（沿用 fetch_news_corpus.py 惯例）。
- 行结构：`{id, text, modality: "text_article", meta:{source, symbol, symbol_name,
  url, published_at, crawled_at, fetch_run_id}}`；`id = news_{symbol}_{sha1(url)[:12]}`
  （跨运行稳定）；text = 标题 + 空行 + 正文。
- 定量：每 symbol 截前 N=20 条；限速 sleep 1s/symbol；单 symbol 失败入失败清单
  不阻塞整批；全部失败才 exit 1。
- 股票池：内嵌 25 只（findata UNIVERSE 2026-09-15 快照，注释注明对齐来源），
  `--symbols` 可覆盖——零 import 耦合，对齐靠注释与周检查。
- 产物：`data/raw/finance_news/news_corpus.jsonl`（raw 层，git 忽略）。

## 决策点 4：金融漏斗配置（独立 config，防混入维基基线）
- `configs/text_funnel_finance.yaml`：κ 块教训（独立文件 + 独立 output.dir），算子
  链与维基版相同；两处调整待真实数据轮校准：doc_length min 30→20（快讯类短文本）、
  output 指向 `data/processed/finance_news_funnel`。

## 决策点 5：预期带告警 + 台账
- `data/ops/stats.jsonl` 追加式台账：`{date, text_total, text_new, funnel_in,
  funnel_kept, disk_used_pct, failures[]}`。
- 告警规则：历史 ≥3 天且当日 text_new < 0.5 × median(近 7 天) → 日报标红；
  无历史首周跳过。结构化侧健康由 findata daily_pipeline 退出码 + 其自带告警承担。
- 磁盘水位：used >80% → 日报标红（R7 前置）。

## 决策点 6：调度安装器（不自动武装）
- `scripts/ops_install_schedule.py`：包装 schtasks /create（每日 20:00，
  `python -X utf8 scripts\ops_daily.py >> data\ops\schtasks.log 2>&1`）；
  默认只打印命令，`--arm` 才真装。R8（环境冻结/电源设置）未完成前不武装——
  武装是显式动作，留给用户在环境还债后执行。

## 风险
| 风险 | 对策 |
|---|---|
| 东财接口超时/限流（勘测实测超时一次） | 单源失败清单 + 次日增量补齐（幂等）；连续低量触发预期带告警 |
| 新闻正文含大量模板/广告 | 漏斗现有 boilerplate/pii 算子先跑，误杀模式由 audit 步逐日暴露（飞轮本职） |
| findata venv 路径漂移 | 步骤前置检查 venv 存在，失败信息给安装指引 |
| ops 壳自身 bug 吞错 | 每步产物校验 + 日报必渲染（失败也可见）+ --dry-run 冒烟 |

## 验收标准汇总
1. 单测 ≥6 条全离线：步骤表构建 / 预期带告警（无历史/健康/骤降）/ 日报渲染 /
   审计聚合 / 幂等去重 / id 稳定性。
2. `--dry-run` 冒烟：五步命令与产物路径正确。
3. 质量门：ruff 绿；主仓 pytest 实点 ≥183 不倒退（新增不计回归）；包侧 40 不受影响。
4. 真跑验收（联网，用户执行）：`fetch_finance_news.py --symbols 600519` 落 ≥1 条；
   `ops_daily.py --skip-findata` 出首份日报。

# V4 设计表：医疗模态协议扩展 + 评测量化闭环（2026-09-17）

> 来源：外部任务书（方向一：curation-eval 协议扩展至医疗 FHIR 模态；方向二：评测体系
> 升级为可对标学术基准的量化闭环）。任务书五条硬约束全盘接受：①不破坏既有评测数字
> （主仓 194 + 包 40 基线只增不减，图文/文本管道既有指标逐项相等）；②离线可跑、
> 无外部 API 依赖；③不改 Sample 协议方法签名，新模态走注册表扩展；④确定性优先
> （语料 --seed 逐字节一致）；⑤测试先行（每算子 ≥4 条单测）。
> 分期：**V4 α 医疗模态协议扩展 → V4 β 评测量化闭环 → V4 γ CI 失败诊断**；
> δ（医疗端到端检索闭环）依赖 α 实测数据，届时另开设计门（边界见文末）。
> 在途避让：`tuning/extraction.py` / `eval_judge.py` / `capability_matrix.json` /
> `runs/experiments.jsonl` / `findata_health_stage.py` 为协作方在途文件，本轮不触碰。

## V4 α 决策点 1：FHIR 资源如何扁平化为 Sample（数据结构表）

| 方案 | 做法 | 弃/用 |
|---|---|---|
| A. text = canonical JSON | 资源 JSON 规范化序列化（sort_keys、ensure_ascii=False）进 `sample.text`；meta 带资源类型/版本/更新时间三键 | **采用** |
| B. 结构化塞 meta["fhir"]，text 留叙事摘要 | 双份事实源，roundtrip 依赖 meta，序列化不对称 | 弃 |
| C. FHIRSample 子类化 Sample | dataclass 继承后 `Sample.from_dict` 返回基类类型，协议出现两套构造路径 | 弃 |

A 的理由：MODALITY_FIELDS 只需登记 `fhir_resource: frozenset({"text"})`；五个医疗算子
required_fields 全部为 `{"text"}`（结构化内容由算子从 text 解析）；既有文本/图文算子靠
modalities 声明天然跳过 fhir 样本（不会拿 JSON 长度误判）；执行器与算子级评测器
（含 `run_operator` 的批量口径）零改动——任务书「现有算子执行器不需要修改」直接满足。

| FHIR 资源 | Sample 映射 | 说明 |
|---|---|---|
| resourceType + id | `id = f"{resourceType[:3].lower()}_{logical_id}"` | resourceType 本体留在 JSON 内 |
| 全资源 JSON | `text = json.dumps(..., sort_keys=True, ensure_ascii=False)` | 单一事实源 |
| resourceType | `meta["fhir_resource_type"]` | Patient/Observation/Encounter/MedicationRequest |
| （版本） | `meta["fhir_version"] = "R4"` | 本期只做 R4；OMOP 不在本期范围 |
| meta.lastUpdated | `meta["fhir_last_updated"]` | |
| subject/encounter 等引用 | 留在 JSON 内，不提升为顶层字段 | referential/temporal 算子解析 JSON |

## V4 α 决策点 2：FHIRSample 适配器（包侧新公共模块，只增不改）

位置：`packages/curation-eval/src/curation_eval/fhir.py`；包版本 0.2.0 → 0.3.0。
MODALITY_FIELDS 登记走 schema.py 既有登记点（一行扩展，不算改协议签名）。

| API | 签名 | 语义 |
|---|---|---|
| `FHIRSample.from_resource` | `(resource: dict, *, fhir_version="R4") -> Sample` | 校验 resourceType/id → 展平为 Sample（modality="fhir_resource"） |
| `FHIRSample.to_resource` | `(sample: Sample) -> dict` | 逆映射；roundtrip 保真：`from_resource(to_resource(s))` 与 s 逐字段相等 |
| `FHIRSample.parse` | `(sample: Sample) -> dict` | 算子用：text → 资源 dict |

错误输入约定：缺 resourceType / 缺 id / 未知 resourceType / 非 fhir 样本误用 →
ValueError（构造期 fail-fast，与注册表同哲学）。

## V4 α 决策点 3：五个医疗算子（主仓 `src/mm_curation/operators/fhir_quality.py`）

score 语义沿用「越高越好」，None = 无法计分保留并记录；cost_class 全部 = RULE
（码表/单位表为静态查表，无推理开销；任务书的「中成本」指表维护成本，不进 CostClass）。

| 算子 | score 定义 | 默认阈值 | 执行形态 | 主靶（OPERATOR_TARGETS） |
|---|---|---|---|---|
| phi_residual | 1 - PHI 命中字段数/扫描字段数（扫描 name/address/telecom/identifier 叶值；正则族：手机号/身份证/SSN/邮箱 + 未脱敏 given 名） | min=1.0 | 单样本 | fhir_phi_leak |
| code_validity | 合法编码字段占比（ICD-10/LOINC 格式校验 + 内嵌码表成员；无 code 字段 = 1.0） | min=1.0 | 单样本 | fhir_code_invalid |
| unit_normalization | valueQuantity 单位 ∈ UCUM 表占比（大小写敏感；缺 unit = 违规；无 valueQuantity = 1.0） | min=1.0 | 单样本 | fhir_unit_off |
| temporal_consistency | 时间检查通过占比（effectiveDateTime 晚于 Patient.birthDate 且落在 Encounter 周期内；引用缺失 = None 不误杀） | min=1.0 | **批量，shardable=False**（需跨资源查 birthDate/周期） | fhir_time_inverted |
| referential_integrity_fhir | 可解析引用占比（subject/encounter 等引用指向集内存在的资源；无引用 = 1.0） | min=1.0 | **批量，shardable=False**（需全量 id 集） | fhir_ref_broken |

两点有意偏离任务书字面：①「一算子一文件」收进家族文件 fhir_quality.py（与
text_quality.py/image_quality.py 同型，仓库既有惯例优先）；②「跨资源查询」的两算子
做批量算子而非把引用提升为顶层字段——保持 FHIR 语义原样，批量算子协议（run_batch +
id 差集丢弃 + shardable=False 全局视角）是现成机制（minhash_lsh 同型）。批量算子
run_batch 假设输入已按模态过滤（漏斗经 run_batch_mixed_modality 预过滤，评测集本就
全 fhir 模态），与 text_minhash 同约定。

## V4 α 决策点 4：医疗污染器（实施时落**包侧** curation_eval/fhir_contamination.py）

> 实施修订（2026-09-17 动码时）：原定主仓 V1 套（contamination/base.py），盘点发现
> 仓库双注册表并存——V2 协议套在包内（benchmarks/judge_data/β 文本污染器都在那），
> V1 主仓套仅剩 scripts/contaminate.py 一个消费方。按 β 先例改投包侧 V2 套，
> 供体重抽走 ctx.pool，ContaminationPlan 零改动。下表不变：

| kind | 注入动作 | 靶算子 |
|---|---|---|
| fhir_phi_leak | 脱敏名（given=["*"]）替换为假名池真实样式姓名；变体：telecom 复原完整手机号 | phi_residual |
| fhir_code_invalid | E11.9 → E119 / e11.9（格式破坏），或换成码表外合法格式码 | code_validity |
| fhir_time_inverted | Observation.effectiveDateTime 移到其 Patient.birthDate 之前 | temporal_consistency |
| fhir_ref_broken | subject 指向不存在的 Patient id，或 encounter 引用断链 | referential_integrity_fhir |
| fhir_unit_off | mg/dL → mg/dl（UCUM 大小写违规），或删除 unit | unit_normalization |

ground truth 与现有污染器一致：labels.dirty = kind；注入不修改原始样本（新增条目）。

## V4 α 决策点 5：合成 FHIR R4 语料生成器（`src/mm_curation/data/fhir_synth.py`）

- 构成：Patient 100 / Observation 200 / Encounter 100 / MedicationRequest 100 = **500 条**；
  Observation 引用 Patient+Encounter、MedicationRequest 引用 Patient+Encounter，
  引用闭合保证干净侧在 referential 算子上误杀 = 0 可归因。
- 确定性：id 顺序编号（pat-000001…）；内嵌静态表——假名池 40 个（脱敏基线与注入池
  两用）、ICD-10 码表 30 个、LOINC 码表 15 个、UCUM 单位表 10 个；随机只走
  `random.Random(seed)` 控制 value/时间偏移/变体字段；同 seed 输出逐字节一致。
- 无真实患者数据：全部字段程序生成，假名池为通用常见姓名样式、不指向真实个体
  （报告固定脚注声明）。
- 合法业务异常（不注入、不标注、计干净侧）：约 8% Observation 缺 valueQuantity、
  部分 Encounter 无 end、个别资源无 telecom——防止算子靠「字段必须齐全」作弊拿召回；
  异常清单进 manifest，误杀审计可区分。

## V4 α 决策点 6：评测入口（make eval-fhir）

- `configs/funnel_fhir.yaml`：五算子链（批量两算子按依赖排后），name = fhir_r4_quality。
- `scripts/eval_fhir.py`：生成语料（--seed，默认 42）→ ContaminationPlan 注入
  （--inject-rate，默认 0.3）→ evaluate_all 独立 P/R → 报告
  `data/reports/operator_pr_fhir.{json,md}`（格式与 make eval-op 一致）。
- **门禁内置**（沿用 data_ci 哲学：单测管逻辑、门禁管数字）：总体故障召回 ≥0.90
  （分类型在报告列出）且干净误杀率 ≤0.05，跌破 exit 1；--no-gate 观测模式。
- Makefile 增 `eval-fhir`；RUNBOOK 补裸命令（本机 Git Bash 无 make）。

## V4 α 流转表（样本生命周期）

语料生成（labels={}，干净）→ ContaminationPlan 注入（labels.dirty=kind、id 加后缀、
原始样本保持干净）→ 两条互不影响路径：①算子级独立评测（run_operator 全量单跑，
evaluate_operator 现成口径）；②漏斗串联（funnel_fhir.yaml，StageStat 逐级统计）。
批量算子内部：按 id 规范化排序（协议既有确定性约定）→ 建跨资源索引（Patient
birthDate 表 / 全量 id 集）→ 逐样本计分写 meta["score:<op>"] → 低于阈值剔除。

## V4 α 测试计划（基线主仓 194 + 包 40 只增不减）

- 包侧 ≥6：roundtrip 保真 / MODALITY 登记校验（未知模态仍拒）/ 混合模态漏斗跳过语义
  （fhir 样本过文本算子计 skipped 不误杀）/ from_dict 兼容 / 批量算子 id 排序确定性 /
  错误输入 ValueError。
- 主仓：5 算子 × ≥4（正常通过/正常拒绝/边界值/错误输入）；污染器 3（同 seed 确定性/
  五类注入靶向命中/比例归一）；语料 2（seed 逐字节一致/构成与引用闭合断言）；
  eval_fhir 冒烟 2（小规模全绿落盘/劣化注入 exit 1）。

## V4 β 决策点 1：benchmark_compare 的诚实边界

对标的是**协议与维度**，不是复现其数字——KramaBench/Evian 数据集不入仓（离线红线），
其评测协议作为内嵌参照系；产出是本项目证据映射进两个学术框架的对标表：

| 对标框架 | 取用协议 | 本项目证据源 |
|---|---|---|
| KramaBench 端到端层 | 任务级最终指标 | 检索 R@K + 训练级证据（finetune_eval） |
| KramaBench 管道设计层 | 管道组件贡献 | 漏斗 StageStat + 阈值扫描曲线 |
| KramaBench 子任务实现层 | 子任务 P/R | 算子级 P/R（operator_pr） |
| Evian 分解后评估 | 正交维度分解 | 清洗收益按维度分组消融：一致性=去重组 / 连贯性=规则组 / 事实性=模型组 |

路径说明：任务书写「eval/ 目录」，仓库既有惯例是逻辑进 `src/mm_curation/eval/`、
CLI 薄壳进 `scripts/`（eval_operators/eval_ablation 同型），照惯例走。
输出 `data/reports/benchmark_compare.{json,md}`。

## V4 β 决策点 2：attribution_report 三块

1. 算子级边际贡献：泛化 eval_ablation（零重编码技巧复用），逐算子 + 分组
   （去重/规则/模型）双粒度；验收 = 「去重组 ΔR@1 = -0.017」在报告中复现。
2. 模态级对比：清洗强度 = 漏斗前缀长度 k（前 k 个算子后的存活集）；图文侧 held_out
   R@1 曲线（零重编码）；文本侧 GPT-2 zh PPL 曲线（5,000 条固定 seed 子采样——全量
   PPL × 8 个前缀点成本不可接受，代理口径如实标注）→ `attribution_by_modality.json`。
3. 成本-收益比：每算子「每 1% 误杀率对应的墙钟成本」，沿用 cost_model 计时口径，
   输出值得保留/可以关闭的分档建议。

## V4 β 决策点 3：train-evidence 自动化管线

- `scripts/train_evidence.py` 编排：干净/脏集划分 → 分别微调 → 固定 held-out 评测
  → 对比报告（扩展 finetune_eval.json 格式，新增 reproducibility 字段）。
- 双任务：clip（R@1）与 ppl（GPT-2 困惑度），`--tasks clip,ppl` 可选跑。
- 可复现性检查：同 seed 训两次，R@1 差异 ≤0.5% 否则 exit 1（GPU 全量约数小时，
  --smoke 降规模冒烟进 CI）。
- Makefile 增 `train-evidence`。

## V4 γ 决策点：CI 门禁失败诊断

- 共享诊断模块 `src/mm_curation/eval/diagnostics.py`：
  ①门禁跌破阈值 → 落退步样本清单（样本 id + 注入类型 + 各算子分数与阈值）；
  ②算子触发差异表：本次 vs 上次运行逐样本「通过↔被丢」翻转清单，标注责任算子；
  ③前后状态落盘 `data/reports/ci_diagnostics/<gate>/<date>.json`（本地对比上一次），
  data-ci.yml 补 actions/upload-artifact，GitHub 页面直接可读。
- 接入三个门禁脚本：data_ci_benchmark / data_ci_image_benchmark /
  threshold_regression_gate（共享 helper，各自门禁逻辑不动）。
- 验收：故意注入回归（如 phash_near 阈值调松）→ 日志出现退步清单与差异表而非裸 FAIL。

## 风险

| 风险 | 对策 |
|---|---|
| FHIR JSON 进 text 后，文本算子若误配 modalities 会拿 JSON 长度误判 | 算子 modalities 精确声明；包侧测试锁「混合模态跳过」语义 |
| 批量算子（temporal/referential）在 Ray 下的全局视角正确性 | shardable=False 走既有单机汇聚路径；α 先 local，Ray 等价性留 δ 顺带验证 |
| temporal_consistency 遇引用缺失样本被误杀 | 协议 None 语义：无法计分保留并记录，断链责任归 referential 算子 |
| 文本 PPL 前缀曲线成本失控 | 5,000 条固定 seed 子采样 + 前缀点 ≤9 个 + 代理口径如实标注 |
| train-evidence GPU 时长阻塞合入 | --smoke 降规模进 CI；全量由用户择机执行 |
| 合成语料「合法业务异常」被算子误杀虚增误杀率 | 异常清单入 manifest，误杀审计可区分 |

## 验收标准汇总（对照任务书）

- α：make eval-fhir 跑通，P/R 报告格式与 operator_pr 一致；五算子双向单测齐；污染器
  总体故障召回 ≥90% / 误杀 ≤5%（门禁 exit code 锁）；图文+文本管道既有测试全绿且
  评测数字逐项相等（全量 pytest 前后对比）。
- β：benchmark_compare 出 Markdown+JSON 对标表；attribution_report 复现去重组
  -0.017；train-evidence 图文管道跑通且双跑 R@1 差 ≤0.5%。
- γ：故意注入回归时门禁输出退步样本清单 + 算子触发差异表。
- δ（另开设计门）：医疗模态接入 清洗漏斗 → 向量索引 → 检索评测 链路，有独立
  Recall@K 报告与清洗前后量化对比。
- 每阶段收工按 AGENTS.md 四件套（质量门 / DEV_PLAN 回写 / 工程笔记 / ROADMAP+RUNBOOK）
  + 中文 commit push。

# V5 设计表：基座 + 领域增强包——工业传感器包（V5 α，2026-09-17）

> 定位升级（对用户确认的叙事）：项目 = **多模态数据清洗与预处理平台** =
> curation-eval 基座（协议/注册表/执行器/污染器框架/门禁指标，pip 包）+ 领域增强包。
> **增强包六件套**：适配器 / 领域算子 / 领域污染器 / 确定性语料 / 漏斗 config / 评测门禁
> ——V4 α 的 FHIR 包为参考实现，本包（industrial_sensor）为第二实例。
> 本设计**不新增框架机制**：增强包是既有扩展点的实例化；不做插件系统/算子市场（红线）；
> 不引入改写型算子语义，预处理（换算/对齐的执行）走漏斗外前置/后置阶段（红线确认）。

## V5 α 决策点 1：样本粒度——一窗一条，canonical JSON 展平

一个 Sample = 一个 **通道×时间窗**（默认 256 读数），text = 窗口数据 canonical JSON
（读数数组 + 起止时间戳 + 采样率），meta 键：`sensor_record_type`（reading_window /
maintenance_event）、device_id、channel、unit、sampling_hz、operating_mode、window_start。
镜像 FHIR 模式（text=单一事实源；`SensorSample` 适配器入包，roundtrip 保真）。

- 弃选 A：逐读数一条——过滤粒度太碎，时序算子需全局排序，成本高。
- 弃选 B：整通道一条——窗口过大，污染注入无法局部化，P/R 归因变粗。
- **检修计划事件进样本流**（maintenance_event，镜像 FHIR 多资源类型先例）：
  fault_vs_maintenance 所需的业务事件源在同模态内闭合，执行器零改动。

## V5 α 决策点 2：五个工业算子（主仓 operators/industrial_quality.py）

score 语义沿用「越高越好」；全部 cost_class=RULE（信号统计是纯算术）。

| 算子 | score 定义 | 形态 | 主靶 |
|---|---|---|---|
| sensor_stuck | 窗口标准差≈0 判卡死；operating_mode=idle 的平坦窗合法放行 | 单样本 | sensor_flatline |
| sensor_range | 超量程/物理不可能值占比（内嵌量程表） | 单样本 | sensor_out_of_range |
| sensor_drift | 同通道**同工况**后续窗均值 vs 基线窗（首 K 窗）系统偏移超阈——跨工况不比较（工况差异是合法的） | **批量 shardable=False** | sensor_cal_offset |
| unit_consistency | 同测点多源单位一致性（MPa vs bar 混源），内嵌工控单位表 | **批量 shardable=False** | sensor_unit_swap |
| fault_vs_maintenance | 哨兵填充窗（-999 工业惯例）∩ 检修计划窗 → 合法放行；计划外哨兵窗 → 检出。计划索引由 maintenance_event 样本在 run_batch 构建 | **批量 shardable=False** | sensor_unplanned_silence |

## V5 α 决策点 3：污染器（包侧 sensor_contamination.py，供体重抽模式复用）

| kind | 注入动作 | 靶算子 |
|---|---|---|
| sensor_cal_offset | 窗口读数整体加系统性偏移（模拟校准漂移） | sensor_drift |
| sensor_flatline | 非 idle 工况窗塞平坦读数 | sensor_stuck |
| sensor_out_of_range | 塞超量程值/负值 | sensor_range |
| sensor_unit_swap | meta.unit MPa→bar 不标注（读数值按新单位重写，值不变） | unit_consistency |
| sensor_unplanned_silence | 读数改哨兵值 -999 且不在任何计划窗内 | fault_vs_maintenance |

ground truth 与既有污染器一致（labels.dirty=kind，注入即复制不改原始样本）。

## V5 α 决策点 4：确定性合成语料（主仓 data/sensor_synth.py）

- 构成：3 类设备（泵/风机/加热炉）× 每类 2 通道 × 约 200 窗 + maintenance_event
  40-60 条 ≈ **读数窗 1200+**（满足任务 ≥1000）；AR(1)+高斯噪声，全部走
  random.Random(seed)；内嵌量程表/单位表/工况 schedule（idle/run/changeover）。
- 合法业务异常（不注入不标注）：changeover 工况均值偏移、idle 平坦窗、
  计划检修窗**直接缺席**（真实静默，由事件样本佐证）——误杀可归因。
- --seed 切换版本，同 seed 逐字节一致；无真实产线数据（脚注声明）。

## V5 α 决策点 5：评测入口（镜像 eval_fhir）

configs/funnel_industrial.yaml + scripts/eval_industrial.py（operator_pr 格式报告 +
漏斗串联门禁 召回 ≥90% / 误杀 ≤5% exit code）+ Makefile eval-industrial +
OPERATOR_TARGETS 五条 + RUNBOOK 段。

## V5 α 决策点 6：双轨（α 只做合成轨；公开数据集为 V5 β）

NASA C-MAPSS 等公开数据集**没有污染 ground truth**，不入 CI 门禁——作为 V5 β
「真实底座试跑」（下载器幂等 + 前置窗口化/量纲标注 stage + 注入实验报告），
沿用维基/金融双轨方法论。PHM 下游任务证据（清洗前后 F1）同归 V5 β。

## 风险

| 风险 | 对策 |
|---|---|
| 时序算子对窗口顺序敏感，破坏确定性 | run_batch 内按 (device, channel, window_start) 规范化排序（id 排序约定的领域版），测试锁双跑一致 |
| drift 检测把合法 changeover 误判漂移 | 同工况比较前置（meta.operating_mode 分组）；changeover 窗归合法业务异常清单 |
| 哨兵值 -999 与真实读数域冲突 | 量程表外值即哨兵；语料生成保证真实读数远离哨兵域 |
| 四个模态后成本表/报告拥挤 | 维持既有模态跳过语义，报告按 pipeline 名分文件（operator_pr_<domain>） |

## 验收标准汇总

- eval-industrial 全量跑通：故障召回 ≥90% / 误杀 ≤5%（门禁 exit code 锁），
  五算子四向单测 + 语料确定性 + 污染器靶向命中；前三模态数字零回归（基线 229+47 只增）。
- 《领域增强包规范》落 docs（六件套清单 + FHIR/工业双参考实现），README 重定位段。
- V5 β（C-MAPSS 真实底座 + PHM 下游 F1 证据）/ V4 βγ（评测闭环 + CI 诊断）顺延另拆。

# V5 β 设计表：平台演示门户（可交互展示前端，2026-09-17）

> 需求：可交互前端，直观展示项目。仓库红线「不做真前端」（React 类产品级投入）
> 在案 → 走 Streamlit 演示应用（仓库已有四个先例：streamlit_app / ops_dashboard /
> judge_studio / platform_app）。本期的真缺口不是又一个应用，而是**统一演示入口**：
> 没有任何一屏能讲出「四模态一个框架」的平台故事。

## 决策点 1：入口定位——新增总入门户，不改存量

新增 `scripts/showcase_app.py`（数据质量平台演示门户）；四个存量应用保持不动，
门户内给跳转指引（各自启动命令 + 定位一句话）。避免把 platform_app（V3 微调
控制台）等既有应用越改越重。

## 决策点 2：页签结构（六页签）

| 页签 | 内容 | 数据源 |
|---|---|---|
| ① 平台总览 | 四模态门禁卡（st.metric）/ 基座+六件套表 / 灵魂数字（R@1、ppl、κ）/ 测试基线 | data/reports/*.json + DOMAIN_PACKS.md 口径 |
| ② 图文 | 门禁卡 + 算子 P/R 表 + 现场重跑（约 4 分钟，默认只给命令） | operator_pr.json |
| ③ 文本 | 去重门禁（exact/near/误杀）+ 漏斗数字 + 命令 | data_ci 报告（缺失给命令） |
| ④ 医疗 FHIR | 门禁卡 + P/R 表 + **现场重跑按钮（~6 秒 CPU）** | operator_pr_fhir.json |
| ⑤ 工业传感器 | 同上（~6 秒 CPU）+ 口径注（批量算子独立评测说明） | operator_pr_industrial.json |
| ⑥ 证据链 | 检索 R@1 对比 / CLIP+ppl 微调对比 / 消融 ΔR@1 / 采样对比 | finetune_eval、ablation_eval 等报告 |

## 决策点 3：数据驱动与「现场跑给你看」

- 只读 data/reports/*.json（报告不入库）；缺失时显示「未生成 + 生成命令 + 耗时」。
- 现场重跑按钮只给轻量评测（eval-fhir / eval-industrial，纯 CPU ~6 秒）：
  subprocess 显式 PIPE + utf-8 + 实时日志 tail（judge_studio 同款模式），跑完
  重新加载报告。重量级评测不进按钮（面试现场不可等 4 分钟）。
- 图表用 st.bar_chart / st.dataframe，不新增依赖。

## 决策点 4：测试与验收

- 镜像 test_ops_dashboard：headless 启动 HTTP 200 冒烟；
- 报告解析/门禁卡数据准备抽纯函数，配单测（含报告缺失降级）；
- 质量门全绿，基线 263+54 不倒退；RUNBOOK 补启动命令；DEV_PLAN/ROADMAP 回写。

## 风险

| 风险 | 对策 |
|---|---|
| 新环境报告未生成，门户空转 | 每页签缺报告时明示生成命令与耗时；总览卡显示「未生成」态 |
| 现场重跑按钮误触发重活 | 白名单只有两个轻量脚本 + 固定 --scale 档位 |
| 五个应用入口混乱 | 门户即唯一入口，其他四个在「深入页签」中定位为专题深潜 |

---

# V6 设计表：全链路补全 + 统一可视化工作台（2026-09-18）

> **来源**：用户 2026-09-18 指示——「三个域都保留；源接入、抽取层、人审层等缺失层级尽快补完；
> 其他环节对标业界成熟方案升级；中间过程前端可视化；**保留业界完整架构**，但做成
> **比业界更易上手、交互更多、更可视化**的版本，并补业界空缺生态位」。
> 事实依据：`docs/INDUSTRY_BENCHMARK.md`（业界图谱）/ `docs/STRATEGY_V6.md`（战略）/
> `docs/GAP_AUDIT.md`（产品级缺口）。
> **本表落盘，等用户确认后动码**（AGENTS.md 设计门）。
> **在途避让**（协作方 2026-09-18 未提交）：`Makefile` / `benchmarks/capability_matrix.json` /
> `docs/DEV_PLAN.md` / `docs/PROOF_CHAIN.md` / `runs/experiments.jsonl` / `scripts/eval_*.py` /
> `scripts/finetune_clip.py` / `src/mm_curation/tuning/extraction.py`。本轮设计不触碰。

---

## 零、先更正我自己写错的两处（诚实性优先）

写本表前实点全仓，推翻了我上一轮在 `STRATEGY_V6.md` §四 的两条描述。**先更正，再设计**——
这是本项目的立身之本（「动手前先实点，别信描述」）。

| 我在 V6 战略里写的 | 实点结果 | 结论 |
|---|---|---|
| 「1. 源接入 ❌ 完全缺失」 | `src/mm_curation/data/web_sources.py` 已实现 `fetch`（UA+重试+退避）/ `can_fetch`（robots.txt+按 host 缓存）/ `extract_links` / `crawl`（限速+幂等+断点续爬），训练 4 条测试 | **不是缺失，是半成品** |
| 「2. 抽取层 ❌ 完全缺失」 | 同上文件已有 `extract_article`：硬编码 `left_zw` 容器 + `backtop`/`ydtj`/`share` 结束标记 + 剥 `<a>` 块 + 段落 `≥20` 字粗滤 | **不是缺失，是「站点耦合的手写正则版」** |

**这三处的真实差距（比「缺失」更精确）**：

1. **抽取是站点耦合的**——`extract_article` 写死了中国新闻网的容器名与结束标记，
   换一个源就要重写整个函数。业界「抽取层」的含义是**结构无关的正文抽取**
   （trafilatura / resiliparse 那一层）。这是真差距。
2. **没有 RawDoc 中间态**——HTML 抓完立刻解析成 `text` 落盘，**原始 HTML 从不落盘**。
   后果：无法「换抽取器重跑同一批 HTML」、无法做抽取质量对照实验、无法复现 #65 的现场。
   实测确认 `data/raw/` 下无任何 HTML 存档。这是真差距，且是**方法论级**的。
3. **归一化层的缺失根因在执行框架**——笔记 #65 的修复记录原文写着：
   > 「盘点算子框架后确认所有算子都是打分器（score→阈值），**没有文本改写通道**，
   > 『归一化算子』不符合协议形态」——故修复被推进了 `chinese_ratio` 的语义里。
   **真根因：框架只有「打分→阈值」一条通道，没有「改写」通道。** 这不是算子缺一个，
   是协议缺一条。见决策点 1。

另外纠正 `STRATEGY_V6.md` §四 的计数：那一节写「4 处全缺 / 6 处半成品」但表里只有 3 个 ❌。
按实点修正为 **「源接入/抽取：半成品（站点耦合）；归一化：缺失（框架无改写通道）；
人审：缺失；配比/血缘：缺失」**——详见决策点 1 的层表。原文已就地更正。

---

## 决策点 1：层序纪律（先做什么，以及为什么这个顺序不是摊薄）

用户要求「尽快补完」。我接受并行路线，**但层序有纪律**，理由不是保守，而是**依赖**：

```
第 1 组  让证据可信        归一化层 → 抽取层 → RawDoc 存档
         （不补这组，后面所有「真实分布上的数字」都不可信——#65 就是这么栽的）
              │
第 2 组  让决策可审计      判决书（数据结构）→ 人审层（消费者）→ 血缘层（导出形态）
         （判决书是数据记录，不是层；它被 4 个层消费，是生态位的骨架）
              │
第 3 组  让架构完整        配比层 → checkpointing → 统一 CLI
              │
第 4 组  让外部能用         Workbench（UI）→ pip 分发 → Data-Juicer 插件
```

**为什么第 1 组必须最先**（这是本表最关键的一条论证）：三支柱的核心差异化是
「**基于真实分布的可信度**」——阈值校准依据、迁移代价、每条删除的裁决。
但 `docs/REAL_DATA_REPORT.md` 里的真实分布数字**本身就被抽取缺陷污染**（#65：2066 篇里
778 篇误杀中，绝大多数是空白膨胀而非真的低中文占比）。**在不修抽取/归一化的前提下
去量化「迁移代价」，量化的是一个被污染的量。** 所以补第 1 组不是摊薄，是**给核心差异化
清地基**。

**「补层即出数字」的执行纪律**（回应摊薄风险）：
每个补层任务**必须附带一个对照实验**（A/B 或 A/B/C 三口径），实验数字是验收的一部分。
不允许出现「层补好了，但没有数字说它有用」的任务。这样补层过程本身持续产出证据，
而不是等全补完再回头找数字——这是对 `STRATEGY_V6.md` §九 风险 1（摊薄）的具体对策。

**明确不做（本轮红线，与 V6 §九 一致）**：
❌ 新算子 ❌ 新模态 ❌ fork 任何业界框架 ❌ 重造 Argilla/Lilac
❌ 绝对规模追赶 ❌ 7B 训练级裁判 ❌ RegMix 代理训练 ❌ 鉴权/多租户

---

## 决策点 2：改写通道 + 归一化层（框架真缺口，W1 第一件事）

### 2.1 改写通道（包侧协议，`curation-eval` v0.4.0 → v0.5.0）

**不改 `Operator` 协议**（V4 红线：不改协议签名）。新增一条**并列**协议：

| 项 | 设计 | 理由 |
|---|---|---|
| 协议名 | `Transformer`（与 `Operator` 并列，不继承） | 打分器与改写器语义正交：一个产出 score→阈值，一个产出新样本 |
| 关键方法 | `transform(sample: Sample) -> TransformResult` | 单向、无状态、逐样本 |
| `TransformResult` | `{sample: Sample \| None, changed: bool, log: dict}` | `None` = 改写后不可用（如归一化后为空）→ 该阶段丢弃并记因 |
| 模态声明 | `modalities: frozenset[str]`（与 Operator 同款语义） | 不匹配 = 原样透传，计 `skipped`，**不误杀** |
| 确定性 | 同输入必同输出（纯函数，无随机无 IO） | 与项目确定性约定一致 |
| 接入点 | `run_funnel(samples, config, *, pre_stages: Sequence[Transformer] = ())` — **新增可选关键字参数，默认空** | 旧调用零破坏；V4 红线只锁 `Sample` 协议签名，未锁 `run_funnel` |

**为什么不是「加一个归一化算子」**：因为改写型与打分型在漏斗里的**位置语义不同**——
改写必须发生在**所有打分之前**（且一次），而算子是逐级串联的。做成算子会出现
「改写算子在第 5 级，前 4 级算子在未改写文本上打分」的病态。前置阶段（pre-stage）
是唯一语义正确的位置。

### 2.2 归一化层（主仓 `src/mm_curation/normalize/text_normalize.py`）

七条规则，**逐条可开关**，每条独立计数：

| # | 规则 | 依据 |
|---|---|---|
| 1 | Unicode NFC 规范化 | 兼容字符（全角拉丁/兼容汉字）会污染字符统计 |
| 2 | 零宽字符剥除（U+200B/200C/200D/2060/FEFF） | 网页复制粘贴的隐形垃圾，让「长度」失真 |
| 3 | 控制字符剥除（保留 `\n` `\t`） | 同上 |
| 4 | **换行统一**（`\r\n`/`\r` → `\n`） | **#65 现场实测的 `\r` 残留** |
| 5 | **空白折叠**（同段内连续空白 → 1 空格；`\n{3,}` → `\n\n`） | **#65 的 3000+ 空格根因** |
| 6 | **U+2028/U+2029 处理**（行分隔符 → `\n`） | **笔记 #44 陷阱：`splitlines()` 会在此错切 JSONL** |
| 7 | 首尾空白剥除 + 段落级 strip | 段落拼接残留 |

**默认 opt-in，不改既有 config**：归一化会改写所有下游算子的输入，从而改变
图文/文本/医疗/工业四个模态的**全部既有数字**。V4 设计表有「既有指标逐项相等」的硬约束，
因此：**既有 4 个 config 一字不动**；归一化以 `pre_stages` 显式开启，新实验/新 config 用。
这同时让「同批数据开/关归一化」成为天然 A/B 对照组。

**可审计性（判决书的前身）**：每个被归一化的样本在 `meta["normalize"]` 落
`{rules_applied: [...], orig_len, new_len, chars_removed, whitespace_ratio_before}`。
**「我改了你 3001 字里的 2571 个字符，因为规则 4+5」——这是可审计的最小形态。**

### 2.3 W1 的对照实验（补层即出数字）

| 口径 | 抽取 | 归一化 |
|---|---|---|
| **A**（历史基线） | 现有 `extract_article` | 无 |
| **B** | 现有 `extract_article` | **有** |

- 语料：`data/raw/news_corpus.jsonl`（**2066 篇真实爬取新闻，9.8MB，#65 的同一现场**）
- 指标：`chinese_ratio` 拦截数 / 空白占比中位数 / 中文占比中位数 / 保留率 /
  `text_length` 通过率 / `text_minhash` 召回对数的变化
- **验收预期的锚点**（笔记 #65 已给出的数字）：`chinese_ratio` 拦截预期回到 ~5 篇、
  保留率 ~98%。实验的价值不在重复这个数，而在**量化「归一化层相对 chinese_ratio 单点补丁的增量」**——
  即：`text_length`、`perplexity`、`text_minhash` 这几个**当年没被补丁覆盖的算子**，
  在归一化层下是否也改变了判决。**若增量 ≈ 0，如实记为阴性**（说明单点补丁已够，
  归一化层的价值退化为「架构正确性」而非「数字提升」——这本身是诚实的面试素材）。

---

## 决策点 3：抽取层 + RawDoc 中间态（W2）

### 3.1 RawDoc：让「换抽取器重跑」成为可能（补的是方法论）

这是本次补层里**我认为最有价值的一条**——不是补一个解析器，是补**一个中间态**。

| 数据结构 | 字段 | 落盘位置 |
|---|---|---|
| `RawDoc` | `uri / host / fetched_at / http_status / media_type / content_sha256 / content_path / connector / fetcher_version` | 正文按**内容寻址**：`data/raw/html/<sha256[:2]>/<sha256>.html.gz` |
| `ExtractedDoc` | `uri / source_doc_sha256 / title / text / lang_guess / extractor / extractor_version / warnings[] / extracted_at` | `data/interim/extracted/<run_id>.jsonl` |

**内容寻址的三个红利**：①多 URI 同内容天然只存一份（去重免费）；
②抽取结果可标注「来自哪份 HTML 的哪个哈希」（血缘起点）；③**抽取器升级后可对同一批
RawDoc 全量重放**，产出「抽取器版本 × 质量」的对照曲线——这是业界做 ablation 的做法，
本项目此前做不了。

### 3.2 抽取器三级链（依赖降级是硬要求）

| 优先级 | 抽取器 | 依赖 | 角色 |
|---|---|---|---|
| 1（最高） | `NewsCnExtractor` | 零 | **收编现有 `extract_article`**，按 host 注册覆盖（`chinanews` 走这条） |
| 2 | `TrafilaturaExtractor` | `[extract]` extra（懒加载） | 结构无关通用抽取 |
| 3（永远可用） | `HeuristicExtractor` | **零依赖（stdlib `html.parser` + 文本密度打分）** | 兜底：CI 与裸环境可跑 |

**依赖现状实测**：`trafilatura` ❌ 未装 / `resiliparse` ❌ 未装 / `lxml` ✅ 6.0.2 /
`bs4` ✅ 4.14.3 / `httpx` ✅ 0.28.1。→ trafilatura 走 extra + 懒加载，
**不装也能跑全链路**（降级到 Heuristic）；`lxml`/`bs4` 已在，Heuristic 可选用 bs4 但**不用**
（保持 stdlib 零依赖，CI 才能裸跑——这是 ε 阶段那条「本地绿 ≠ CI 绿」教训的复用）。

**回归红线**：`NewsCnExtractor` 对同一份 HTML 的输出必须与旧 `extract_article`
**逐字等价**（红格单测锁死），否则历史语料不可比。

### 3.3 抽取层的语料来源（需要重抓 HTML，合规做法已现成）

`data/raw/news_corpus.jsonl` 只有 text、**原始 HTML 从未落盘**，所以抽取层的对照实验
必须重放一批真实 HTML。做法沿用 `web_sources.crawl` 的现成合规骨架：
**robots.txt 检查 + 1s/页限速 + 明体面 UA + 幂等断点续爬，抓 200 页中国新闻网文章**。
**明确拒绝合成 HTML**——那会重蹈「闭环自证」（自己造 HTML、自己写抽取器、自己测 100%）
的覆辙，是本项目已经识别并写进方法论的反模式。

### 3.4 抽取层的对照实验（三口径）

| 口径 | 抽取器 | 归一化 |
|---|---|---|
| A | 旧 `extract_article` | 无（= 历史基线，须与 `news_corpus.jsonl` 逐字对齐） |
| B | 旧 `extract_article` | 有 |
| C | `TrafilaturaExtractor` | 有 |

- 指标：正文长度分布 / 空白占比 / `chinese_ratio` / 与**人工抽检 30 篇**的正文完整度评级
  （抽检结果进报告，人工评级表落 `data/reports/extract_manual_audit.md`）
- **预留阴性出口**：若 C ≤ B（通用抽取器在此源上无增益），如实记录并**把抽取层的定位
  改写为「换源成本从重写函数降为加一行配置」**而非「质量提升」。抢来的数字不写。

---

## 决策点 4：源接入层协议化（W2）

现状是**能用的单文件脚本**，缺的是**协议**——所以补的是「可换源」而不是「再写一个爬虫」。

| 项 | 设计 |
|---|---|
| 协议位置 | **包侧** `curation_eval/sources.py`：`RawDoc` + `SourceConnector` Protocol（`iter_raw() -> Iterator[RawDoc]`） |
| 实现位置 | **主仓** `src/mm_curation/sources/`（网络/IO 属数据面，包保持零网络依赖——离线红线） |
| 实现清单 | `LocalFilesSource`（glob + 后缀分派）/ `JsonlSource`（直通，保留现有路径）/ `HttpListSource`（URL 清单 + 幂等）/ `WarcSource`（`[warc]` extra，懒加载） |
| 收编 | `web_sources.crawl` 改造成 `NewsCnConnector`（`SourceConnector` 的实现），**合规逻辑（robots/限速/重试）原样保留**，只换外壳 |
| 幂等约定 | 已抓 URI 集合入 `data/state/<connector>_seen.txt`；断点续抓；`fetched_at` 与 `etag` 入库供增量判断 |

**三域各自的源接入**（用户要求三域保留）：

| 域 | 源 | 连接器 |
|---|---|---|
| 中文网页新闻 | chinanews / 东财新闻 | `NewsCnConnector` / `JsonlSource`（akshare 产物已结构化） |
| 工业传感器 | `data/raw/real/{skab,metropt3,cmapss}/*.csv` | `LocalFilesSource` + 现有 `ingest_real_sensor.py` 收编 |
| 医疗 FHIR | FHIR bundle JSON 目录 | `FhirDirSource`（新，薄） |

---

## 决策点 5：判决书（三支柱的骨架 + 四层共用的数据结构，W1 起）

**判决书不是「一个层」，是一条数据记录。** 它被 4 处消费：
漏斗（写入）→ 人审层（读队列）→ 血缘层（导出）→ UI（展示/统计）。放这里定义，避免重复。

`data/verdicts/<run_id>/verdict.jsonl` 每行一条裁决：

| 字段 | 类型 | 说明 |
|---|---|---|
| `v` | int | schema 版本（=1），未来演进不改旧行 |
| `run_id` | str | 本次运行 |
| `seq` | int | 漏斗第几级 |
| `op` | str | 算子名 |
| `decision` | enum | `drop \| keep \| skip \| review` |
| `score` / `threshold` | float / null | 判决依据的数值 |
| `rule` | str | **机器可读判据代号**（如 `ratio_below_min` / `whitespace_dominant`） |
| `evidence` | dict | **判据真正用到的量**（如 `{chars_han: 430, chars_total: 3001, whitespace_ratio: 0.772}`）——这一格是「判决书」与「一行日志」的分界 |
| `input_fingerprint` | str | 该级输入文本 sha256（可回溯到具体输入） |
| `normalize` | dict\|null | 该样本在前置阶段被改写的内容（若有） |
| `review` | dict\|null | **人工裁决回填位**（由人审层写入） |
| `prov` | dict | PROV-O 三元：`{activity, used, generated}`——**记录级血缘** |

**为什么这条记录是生态位**：Data-Juicer 的 `tracer` 能告诉你「哪些样本被过滤了」，
但那是**运行期内存态、不落盘、无判据依据、无输入指纹**；Croissant 的 PROV-O
只做到**数据集/文件级**。**「单条样本为什么被删、依据是什么、能不能被推翻」这一层，
业界是空的。** 判决书就是把这一层落成数据。

**与「不破坏既有数字」的兼容**：判决书是**旁路产物**，`dropped_by` 等既有统计一字不改；
只有显式开启 `--verdict-ledger <dir>` 才落盘。既有 4 个 config 行为不变。

---

## 决策点 6：人审层（W3，最小面）

**红线：不重造 Argilla。** 只做三件事：**队列 + 裁决 + 回写**。

### 6.1 队列的排序策略（这是有技术含量的地方，不是随机抽）

| 优先级 | 策略 | 理由 |
|---|---|---|
| 1 | **边界带**：`\|score - threshold\| / threshold < 0.1` | 最不确定的判决——人审的边际信息量最大 |
| 2 | **算子分歧**：同一样本被 ≥2 个算子给出不一致倾向 | 系统性错判的征兆 |
| 3 | **配额**：每 `rule` 最多 N 条 | 防某一类（如 boilerplate）淹没队列 |
| 4 | 兜底：确定性哈希排序 | 可复现 |

### 6.2 数据结构

| 文件 | 字段 |
|---|---|
| `data/review/cases.jsonl` | `case_id`（确定性：`sha1(run_id|sample_id|op)`）/ `run_id` / `sample_id` / `op` / `score` / `threshold` / `rule` / `evidence` / `priority` / `priority_reason` / `status(pending\|decided)` |
| `data/review/verdicts.jsonl` | `case_id` / `decision(accept\|overturn\|unsure)` / `corrected_label` / `labeler` / `ts` / `note` |

### 6.3 闭环：人审不是摆设，它产出标签

`overturn`（人判「这条不该删」）→ 记为**人工确认的误杀** → 喂给
**阈值校准**（W2 的阈值沙盘用它算「误杀预算」的实际消耗）与
**迁移代价报告**（合成标定在真实分布上的衰减有了人工背书的分母）。

**这是本表里唯一一条「人审 → 数字」的链路**，没有它人审层就是装饰。
验收要求：`--review-report` 能输出「人工复核 N 条，推翻 M 条，推翻率 M/N，
推翻样本在各算子上的分布」——**M/N 是「校准阈值」支柱的直接输入**。

---

## 决策点 7：配比层（W4，轻量）

**红线：不做 RegMix 代理训练**（需要算力，且非差异化）。只做 **manifest + ledger**。

| 产物 | 内容 |
|---|---|
| `configs/mixture_*.yaml` | `MixtureSpec`：`domains: [{name, source_glob, weight_target, max_repeat}]` |
| `data/mixtures/<name>/manifest.json` | 各域 `weight_target` vs `weight_actual` / `docs` / `tokens` / `repeat_factor` / 源文件 `sha256` 清单 / `spec_sha256` |
| `data/mixtures/<name>/ledger.jsonl` | 追加式 token 台账：`{ts, domain, docs, tokens, source_sha256}` |
| token 口径 | `chars/4` 启发式（默认，注明为估算）；`[tokenizer]` extra 装 transformers 后可用真实 tokenizer，报告注明用的是哪个 |

**三条保护性告警**（业界生产必备，本项目此前没有）：
`domain_starved`（实际权重 < 目标 80%）/ `domain_oversampled`（实际 > 目标 200%）/
`source_drift`（源文件 sha256 与上次不同）。**告警进 UI 与报告，不静默。**

---

## 决策点 8：血缘层（W4，Croissant 1.1）

| 产物 | 内容 |
|---|---|
| `data/croissant/<run_id>/croissant.jsonld` | Croissant 1.1：`@type: sc:Dataset` / `name` / `description` / `license`（DUO 或 ODRL）/ `version` / `distribution[]`（`cr:FileObject`: `contentUrl` + `sha256` + `contentSize` + `encodingFormat`）/ `recordSet[]`（字段定义） |
| **项目扩展** | `curation:verdictRecordSet`——**把判决书 expose 成 Croissant 的记录集**，字段对齐 PROV-O（`prov:Activity` / `prov:Entity` / `prov:used`）。**这是「业界只到文件级、我们到记录级」的落地处。** |
| 校验 | `--validate`：装了 `mlcroissant`（extra）走官方校验；未装则跑本地 JSON-LD shape 检查（必需键/类型/引用完整性）。**实测两者均未装 → 走本地检查，CI 零新依赖。** |

---

## 决策点 9：执行层 checkpointing（W4，装饰器不改协议）

**不改 `Executor` 协议**（γ 阶段已定型的协议不动）。用**装饰器组合**：

```
CheckpointedExecutor(inner: Executor, store: Path)
  ├─ 每级 materialize 后落 data/checkpoints/<run_id>/stage_<k>.jsonl + state.json
  ├─ state.json = {completed_stages: [..], input_fingerprint: sha256(上游语料), config_sha256}
  └─ --resume <run_id>：校验 input_fingerprint 与 config_sha256 一致才续跑，否则拒绝并说明
```

**为什么用装饰器**：γ 的等价性验收（2106 条四口径全等，commit `0d7f86b`）依赖
`LocalSequentialExecutor` 与 `RayDistributedExecutor` 的行为契约。装饰器让 checkpoint
成为**可选外包层**，两个执行器内部零改动，**既有等价性测试的语义不被触碰**。

---

## 决策点 10：可视化工作台（诚实定位 + 新增 4 页签）

### 10.1 必须先说清楚的事：可视化**不是**真空

我上一轮暗示「中间过程可视化是空缺生态位」。**搜索后的事实推翻了这个前提**——
Data-Juicer 已经有完整的可视化体系：

| Data-Juicer 已有 | 内容 |
|---|---|
| `demos/data_visualization_op_effect` | **交互式参数调节 + 保留/丢弃样本展示** |
| `demos/data_visualization_op_insight` | 实时算子执行、多模态 |
| `demos/overview_scan` | 算子目录浏览（显示算子源码） |
| `demos/data_visualization_statistics` / `_diversity` | 统计 / 动词-名词对 sunburst |
| `tracer` 模块 | 跟踪被过滤样本 / 被 mapper 改过的样本 / 重复样本 |
| `dj-analyze`（`analyze_data.py`） | `overall.csv`/`overall.md` + 相关系数热图 + 箱线图 + 直方图 |
| `monitor` / `adapter` | CPU/内存/GPU/耗时监控 |
| `checkpoint` 模块、**DJ-Sandbox** + HPO | 数据沙盒、超参优化 |

**所以「做一个可视化前端」不是差异化。** 真实的差异化只在两处，且都是可验证的：

| 差异化 | Data-Juicer 的现状 | 本项目的落点 |
|---|---|---|
| **① 可视化的是「有没有依据」，不是「长什么样」** | op_effect 能调参数、看保留/丢弃——但**不告诉你阈值该定多少、依据是什么**；analyze 出的是相关性热图，不是校准反推 | 阈值沙盘：**分数分布曲线 + 误杀预算线 → 推荐阈值**，并列出「推荐 vs 当前」的差 |
| **② 决策可追溯到单条，且能被人推翻** | tracer 是**运行期内存态**，不落盘成账本；Croissant 只到文件级 | 判决台账：`verdict.jsonl` 落盘、可按算子过滤、可下钻、**可直接送人审** |
| **③ 比业界更易上手** | 跑 demos **必须 clone 源码 + `pip install -e .[all]` + `cd demos/xxx && streamlit run app.py`**；且 **ray 模式下 `analyze` / `monitor` / `checkpoint` 全部不支持**（官方明确标注） | `pip install curation-eval[ui]` → **一条命令起完整工作台**，单机/Ray 两种模式都有 |

**必须把这段写进文档（而不是藏起来）**——「我知道业界已经有 X，所以我做的是 Y，
因为 X 在 Z 上不成立」，这比「我做了一个可视化」强一个量级。

### 10.2 新增 4 个页签（现有 6 页签之上，共 10）

| 页签 | 内容 | 对着业界的哪块空白 |
|---|---|---|
| ⑦ **漏斗解剖与判决台账** | 逐级水位图（StageStat 漏斗/桑基）+ 点击下钻到样本 + 判决台账表（op/score/threshold/rule/evidence）+ 导出 CSV | 水位图 DJ 无「点击下钻到样本」；台账对应 tracer 的**落盘化** |
| ⑧ **阈值沙盘** | 单算子分数分布直方图 + 误杀预算滑块（横线）+ 推荐阈值 + 「推荐 vs 当前」差异 + 调整后 kept/dropped 预览 | op_effect 有滑块，**但无预算线、无推荐值、无依据**——差异化在这三样 |
| ⑨ **人审队列** | 卡片流 + 键盘快捷键（通过/推翻/不确定）+ 进度 + 推翻率统计 + 一致性核对 | Argilla 更全但需独立部署；这里是**同屏内**且与人审→阈值闭环打通 |
| ⑩ **迁移代价对照** | 三域（工业/新闻/医疗）「合成标定 vs 真实分布」的召回/误杀双柱 + 衰减区间条 + 人工复核推翻率 | **业界无此视图**（迁移代价没人公开量化过） |

**技术约束**：只用 `st.bar_chart` / `st.dataframe` / `st.metric`（**不新增依赖**）；
数据源统一走「报告 → 纯函数 → UI」三层，纯函数可单测（沿用 `test_showcase_app.py` 4 条的模式）。

---

## 决策点 11：易上手（W5，把 Workbench 搬进包）

### 11.1 这是「比业界更易上手」的唯一实质手段

| 路径 | Data-Juicer | 本项目的目标 |
|---|---|---|
| 上手步骤 | `git clone` → `pip install -e .[all]` → `cd demos/<app>` → `streamlit run app.py` | `pip install curation-eval[ui]` → **`curation-eval demo`** |

**架构代价与做法**：UI 必须住在包里才能在 pip 装完后可用。

| 项 | 设计 |
|---|---|
| 位置 | `packages/curation-eval/src/curation_eval/ui/`（包的产品面） |
| 主仓 | `scripts/showcase_app.py` 变**薄壳**（两行 import + 调 `ui.main()`），既有行为与命令不变 |
| 默认数据 | `curation-eval demo` **不用静态示例 JSON**，而是**现场跑一遍**：包内 20 条**合成**样本（明确标注「示例数据，非真实」）→ 跑真实算子链（纯 CPU、秒级）→ 生成判决书 → 起 UI。**是「活的」演示**，展示的正是中间过程 |
| 真实数据 | `--run-dir <path>` 指向用户自己的运行产物（报告 + 判决书），UI 切到真实模式 |
| 依赖 | `streamlit` 进 `[ui]` extra，**不是核心依赖**；未装时 `curation-eval demo` 打印安装指引并 exit 2 |
| 风险 | 包体积增大 → 示例数据 <100KB，可接受；包侧测试数上升 → 只增不减，符合基线纪律 |

### 11.2 统一 CLI（顺手补 GAP_AUDIT 的 P1 项：50 个脚本无统一入口）

`curation-eval` 二级子命令：`run`（跑漏斗）/ `eval`（算子级 P/R）/ `demo`（工作台）/
`verdict`（判决书查询/导出）/ `review`（人审 CLI）/ `croissant`（血缘导出）/ `mixture`（配比构建）。
**存量 `scripts/*.py` 一律不动**（在途文件多，且已有 RUNBOOK 引用）——子命令是**新增薄壳**，
内部调既有脚本。避免「统一 CLI 变成大规模重命名」这种高危动作。

---

## 决策点 12：Data-Juicer 插件接入（W5，先 spike）

事实：**Data-Juicer v1.5.5（2026-08-07）新增 External OP Plugins——第三方算子可作独立
pip 包经 Python entry points 自动注册。** 这是「不 fork、但接入业界框架」的正解。

| 项 | 设计 |
|---|---|
| 形式 | 包内新增 `curation_eval/dj_plugin/`，`pyproject.toml` 声明 entry point |
| **待实测项（spike 半天，不许猜）** | **entry-point 组名**（本表按 `data_juicer.ops` 占位）、算子基类/注册装饰器的确切名字与签名、`Sample` 与 DJ 数据形态（dict/JSON）的适配点 |
| 首个算子 | `VerdictOp`：**把判决书能力反向输出给 DJ** ——DJ 侧跑完算子后，用本项目的判决书格式记录「每条被删样本的判据与依据」。**这是「评测层反过来服务业界框架」的最小可运行形态。** |
| 失败预案 | 若 entry point 机制与预期不符 → 降级为「导出脚本」形式（`curation-eval verdict export --format data-juicer`），**并如实记录 spike 结论**（这本身是笔记素材） |

---

## 数据结构表（汇总）

| 数据 | 位置 | 关键字段 | 消费方 |
|---|---|---|---|
| `RawDoc` | `data/raw/html/<sha2>/<sha256>.html.gz` + 索引 jsonl | uri/host/fetched_at/media_type/content_sha256/content_path/connector | 抽取层 |
| `ExtractedDoc` | `data/interim/extracted/<run_id>.jsonl` | uri/source_doc_sha256/title/text/extractor/extractor_version/warnings | 归一化层 |
| `Sample.meta["normalize"]` | 内存/漏斗产物 | rules_applied/orig_len/new_len/chars_removed/whitespace_ratio_before | 判决书、UI |
| **判决书** | `data/verdicts/<run_id>/verdict.jsonl` | v/run_id/seq/op/decision/score/threshold/rule/evidence/input_fingerprint/normalize/review/prov | 人审层、血缘层、UI、阈值校准 |
| `review/cases.jsonl` | `data/review/` | case_id/run_id/sample_id/op/score/threshold/rule/evidence/priority/priority_reason/status | 人审 UI/CLI |
| `review/verdicts.jsonl` | `data/review/` | case_id/decision/corrected_label/labeler/ts/note | 阈值校准、迁移代价报告 |
| `MixtureManifest` | `data/mixtures/<name>/manifest.json` | domains[]{weight_target,weight_actual,docs,tokens,repeat_factor,sha256_list}/spec_sha256/warnings[] | 报告/UI |
| `TokenLedger` | `data/mixtures/<name>/ledger.jsonl` | ts/domain/docs/tokens/source_sha256 | 报告 |
| `Croissant` | `data/croissant/<run_id>/croissant.jsonld` | distribution[]/recordSet[]/**curation:verdictRecordSet** | 外部消费者、校验 |
| `CkptState` | `data/checkpoints/<run_id>/state.json` | completed_stages/input_fingerprint/config_sha256 | 执行器 resume |

---

## 接口约定表（CLI）

| 命令 | 输入 → 输出 | 退出码 |
|---|---|---|
| `python -X utf8 -m mm_curation.normalize --in <jsonl> --out <jsonl> [--rules nfc,zero_width,...]` | 语料 → 归一化语料 + `--stats` 打印逐规则计数 | 0=成；2=输入缺失 |
| `python -X utf8 scripts/fetch_raw_html.py --source chinanews --max 200 --out data/raw/html` | URL → RawDoc（robots+限速+幂等） | 0=成；1=全失败；3=robots 全禁 |
| `python -X utf8 scripts/extract_docs.py --raw data/raw/html --extractor auto --out data/interim/extracted/<run>.jsonl` | RawDoc → ExtractedDoc（三级链降级） | 0=成；2=无可用抽取器 |
| `python -X utf8 scripts/run_pipeline.py --config <yaml> --verdict-ledger data/verdicts/<run>` | 漏斗 + **可选**判决书落盘（不传则行为与今日完全一致） | 既有语义不变 |
| `python -X utf8 scripts/review_cli.py list --run <id> --batch 50` / `decide --case <id> --decision overturn` / `stats --run <id>` | 队列消费 → 裁决回写 | 0=成；2=队列空 |
| `python -X utf8 scripts/calibrate_threshold.py --verdicts <jsonl> --op <name> --false-drop-budget 0.02` | 判决书 → 推荐阈值 + 依据曲线 | 0=成；3=预算不可达 |
| `python -X utf8 scripts/build_mixture.py --spec configs/mixture_demo.yaml --out data/mixtures/demo` | MixtureSpec → manifest + ledger（含告警） | 0=成；4=域饥饿告警 |
| `python -X utf8 scripts/export_croissant.py --run <id> --out data/croissant/<id> [--validate]` | 产物 → croissant.jsonld（+ 校验） | 0=成；5=校验失败 |
| `python -X utf8 scripts/run_pipeline.py --resume <run_id>` | 续跑 | 0=成；6=指纹不一致拒绝续跑 |
| `curation-eval demo [--run-dir <path>]` | 现场跑示例 → 起 UI | 0=成；2=未装 streamlit |

---

## 流转表（样本生命周期 · V6 全链路版）

```
[源接入]  URI/本地文件/CSV/FHIR bundle
             │ SourceConnector.iter_raw()
             ▼
[RawDoc]  内容寻址存档  data/raw/html/<sha2>/<sha>.html.gz     ← 可重放，抽取器升级不丢现场
             │ ExtractorChain: NewsCn(host覆盖) → Trafilatura([extract]) → Heuristic(零依赖)
             ▼
[ExtractedDoc]  data/interim/extracted/<run_id>.jsonl
             │ 归一化前置阶段（Transformer；opt-in，逐条记 normalize_log）
             ▼
[Sample 规范化态]  meta["normalize"] = {rules_applied, chars_removed, ...}
             │ run_funnel(..., pre_stages=[normalize], verdict_ledger=...)
             ▼
[算子链]   seq=1..N   每级判决 ──写──▶ verdict.jsonl（旁路，不传参则零落盘）
             │                                    │
             ├── kept ──▶ 下游（索引/微调/配比）    ├──▶ 人审层（队列排序：边界带>分歧>配额）
             └── dropped ──▶ 既有 dropped.jsonl     │         │ 人工裁决
                                                    │         ▼
                                                    │   review/verdicts.jsonl
                                                    │         │ 推翻率 M/N
                                                    ▼         ▼
                                          阈值校准（分布+误杀预算→推荐阈值）
                                                    │
                        ┌───────────────────────────┴───────────────────────┐
                        ▼                                                   ▼
              Croissant 1.1 导出                                 UI 工作台（10 页签）
              （distribution + recordSet                         ⑦漏斗解剖⑧阈值沙盘
               + curation:verdictRecordSet 记录级）                 ⑨人审队列⑩迁移代价
```

**checkpointing**：`CheckpointedExecutor` 包裹上述链条，每级落 `stage_<k>.jsonl`；
`--resume` 校验 `input_fingerprint` + `config_sha256`。

---

## 分期与原子任务拆解（确认后落 `docs/tasks.md`）

> 纪律：**每个补层任务必须附带对照实验**（见决策点 1）。任务粒度 50–150 行。

### W1 — 改写通道 + 归一化层 + 判决书骨架（零新依赖，纯 CPU，可立即动码）

| # | 任务 | 验收 | 状态（2026-09-20 实点） |
|---|---|---|---|
| W1-1 | 包侧 `Transformer` 协议 + `TransformResult` | ≥4 单测（协议实现/模态跳过/改写日志/changed 语义） | ✅ `test_transform.py` 13 条 |
| W1-2 | `run_funnel` 增 `pre_stages` 可选参数（默认空） | ≥3 单测（空=旧行为/单级/改写后不可用丢样本） | ✅ 漏斗集成三条红线（不传 ledger 零痕迹） |
| W1-3 | `normalize/text_normalize.py` 七规则 | ≥8 单测（含 #65 现场样本：`\r` 残留 + 3000 空格） | ✅ `test_normalize.py` 24 条；**规则正交化**后逐规则归因纯（笔记 #70） |
| W1-4 | `TextNormalizeTransformer` 接入 + `normalize_log` | ≥3 单测 | ✅ 只声明自然语言模态（`fhir_resource` canonical JSON 刻意排除） |
| W1-5 | 判决书 `verdict/ledger.py`（schema v1） | ≥5 单测（落盘/过滤/版本兼容/evidence 完整性） | ✅ `test_verdict_ledger.py` 21 条 |
| W1-6 | 漏斗集成：判决自动写账本（opt-in） | ≥3 单测（不传参零落盘 = 旧行为逐位一致） | ✅ 另加**不变量断言**「台账行数 = 各滤级进入数之和」 |
| W1-7 | **A/B 对照实验**（2066 篇）→ `normalize_ablation.{json,md}` | 6 项指标出数；阴性也落报告 | ✅ 同码双跑**逐字节相同**；补上追加式台账陷阱的修复（笔记 #71） |
| W1-8 | UI ⑦ 漏斗解剖与判决台账 | ≥3 单测（解析/报告缺失降级/AppTest 渲染） | ✅ 5 条（含「裸仓库报告全缺失」降级 + 批量算子分支回归） |
| **W2-7** | **UI ⑧ 阈值沙盘**（原排 W2，实际提前到 W1 一并做） | ≥3 单测 | ✅ 与 ⑦ 同批落地；批量算子显式「不适用」；口径（丢弃预算 ≠ 误杀率）界面分两处标 |

> W1 收尾基线：主仓 **328** + 包 **67**（实点），ruff 双仓全绿。
> **W2 动手时不要再做 W2-7**（已在 W1 完成）；W2 剩余 = RawDoc/抽取三口径/源接入协议化。

### W2 — 抽取层 + RawDoc + 源接入协议化

W2-1 RawDoc + 内容寻址落盘（≥4）· W2-2 `HeuristicExtractor` 零依赖（≥5）·
W2-3 `TrafilaturaExtractor` + 降级路径（≥3）· W2-4 `NewsCnExtractor` 收编
（**逐字等价回归红线**，≥3）· W2-5 `SourceConnector` + 三实现（≥5）·
W2-6 **三口径抽取对照实验** + 人工抽检 30 篇 → `extract_ablation.{json,md}`（≥1）·
W2-7 UI ⑧ 阈值沙盘（≥3）

### W3 — 人审层

W3-1 队列构造 + `case_id` 确定性 + 三排序策略（≥6）· W3-2 裁决落盘 + 回写判决书（≥4）·
W3-3 `review_cli.py`（≥3）· W3-4 **人审 → 阈值校准闭环**（≥3）· W3-5 UI ⑨ 人审队列（≥3）

### W4 — 配比层 + 血缘层 + 执行层

W4-1 MixtureSpec（≥4）· W4-2 TokenLedger + 三告警（≥5）· W4-3 `build_mixture.py`（≥2）·
W4-4 Croissant 生成器（≥5）· W4-5 `curation:verdictRecordSet` 记录级扩展（≥3）·
W4-6 校验（本地 shape + 可选 mlcroissant，≥2）· W4-7 checkpointing 装饰器（≥5）·
W4-8 `--resume` 指纹校验（≥3）· W4-9 UI ⑩ 迁移代价（≥3）

### W5 — 分发与生态位对接

W5-1 UI 进包 + 主仓薄壳（≥4）· W5-2 `curation-eval demo` 单命令（≥2，含无 streamlit 降级）·
W5-3 统一 CLI 七子命令（≥3）· W5-4 **DJ 插件 spike** + 最小 `VerdictOp`（≥2，含 spike 结论落笔记）·
W5-5 文档四件套（README 重定位 / RUNBOOK / DOMAIN_PACKS / DEV_PLAN）

---

## 风险

| 风险 | 对策 |
|---|---|
| **摊薄**（最大） | 每任务强制带对照实验；层序按依赖而非按兴趣；第 1 组未出数字不进第 2 组 |
| 归一化改变全部既有数字 | **默认 opt-in**，既有 4 个 config 一字不动；既有数字逐项相等的红线由全量跑前后对比守住 |
| `trafilatura` 装不上 / 版本冲突 | 走 extra + 懒加载 + 三级链降级（Heuristic 永远可用）；CI 零新依赖 |
| 重抓 HTML 被源站限流/拒绝 | 200 页 + 1s 限速 + robots 前置；失败则缩小到 50 页并注明样本量 |
| 抽取层「无增益」被掩盖 | 预留阴性出口：C ≤ B 时如实记录，并把定位从「质量提升」改写为「换源成本降低」 |
| 人审层做成人造摆设 | 硬性要求 `--review-report` 产出推翻率 M/N 并喂给阈值校准；无闭环不算完成 |
| UI 进包导致包变重/测试面扩大 | streamlit 进 `[ui]` extra；示例数据 <100KB；包侧基线只增不减并实点 |
| DJ 插件 entry point 与预期不符 | 先 spike（半天）后动码；失败降级为导出脚本 + spike 结论落笔记 |
| 与协作方在途文件冲突 | 本轮全部**新增文件**；`run_pipeline.py` / `sdk.py` 的改动仅在确认后单独小步提交；提交只 `git add` 具体文件 |

---

## 验收标准汇总

1. **层完整性**：10 层各有可运行实现 + 至少一条对照实验数字（不要求每层都「优于」，
   但必须**有数**）。
2. **零破坏**：既有 4 个 config 的数字逐项相等（归一化 opt-in 未启用时）；
   测试基线不倒退（**2026-09-18 实点：主仓 274 + 包 54，其中包侧 5 条为 ray 测试，
   ray 2.58.0 已装**）。
3. **可审计**：每条 `drop` 判决在账本里都能查到 `op / score / threshold / rule / evidence /
   input_fingerprint`；人审可推翻且推翻率可统计。
4. **易上手**：`pip install curation-eval[ui]` → `curation-eval demo` 一条命令起完整工作台，
   零 clone。
5. **业界可介入**：DJ 插件 spike 有结论（成功或如实失败），最小 `VerdictOp` 可跑。
6. **文档四件套**：ruff 全绿；DEV_PLAN 回写（含开发日志行）；工程笔记新增（#65 复验结论、
   DJ 可视化事实校准、抽取层阴性出口若触发）；ROADMAP/RUNBOOK 同步。

---

## 待用户裁决（三处，其余本表已自决）

| # | 决策 | 我的建议 | 不确认的后果 |
|---|---|---|---|
| **1** | **可视化定位承认「DJ 已有」后，是否仍投入 4 页签** | **是**——但叙事从「我做了一个可视化」改写为「我做的三件事 DJ 没做」：有依据的阈值 / 记录级可推翻的判决 / 单机+Ray 都能用的工作台 | 若按原叙事写简历，被懂行的面试官问一句「Data-Juicer 的 op_effect 你看过吗」就会挂 |
| **2** | **归一化层默认开还是 opt-in** | **opt-in**（既有 config 不动） | 若默认开，四个模态的既有数字全变，V4「逐项相等」红线破，需全量重生成所有报告 |
| **3** | **UI 是否搬进 pip 包**（W5） | **是**——这是「比业界更易上手」唯一实质手段 | 不做的话「易上手」只能停留在 `scripts/` 里，pip 用户享受不到 |
