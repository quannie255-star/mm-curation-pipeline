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
