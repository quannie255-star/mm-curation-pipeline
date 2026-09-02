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
