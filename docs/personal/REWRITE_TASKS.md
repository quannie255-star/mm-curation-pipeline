# REWRITE_TASKS：核心模块重写需求书

> **给谁看**：想「自己把控项目」的人。目标不是让项目更好，是让**你**能说"这模块是我写的"。
>
> **最重要的一条用法**：**规格在测试里，本文档不给实现。**  
> 每个任务的"怎么做"要你自己从 `packages/curation-eval/tests/` 与源码 docstring 里读出来。  
> 本文档只给四样东西：**目标 / 接口契约 / 验收命令 / 自检问题**。
>
> 卡住了再来问——但先跑一遍测试，把**失败信息原样贴出来**（见文末求助格式）。



---

## 零、动手前

### 0.1 骨架地图（依赖是单向的，照着这个顺序做）

```
schema.py        唯一数据结构 Sample（模态登记表）
   ↑
registry.py      算子元数据 + 注册机制（注册期 fail-fast 校验）
   ↑
sdk.py           Operator / BatchOperator / Executor + 漏斗统计
   ↑
mm_curation/operators/    具体算子（一算子一文件，@register_operator 注册）
mm_curation/pipeline/     YAML → PipelineConfig → run_funnel
   ↑
下游：scripts / platform / studio / agent / dataset
```

**为什么靶子都在最上面两三层**：它们是所有下游的地基，**而且每一层都有现成测试当规格**。  
改下面（算子/脚本）只能证明你会用框架；改上面才证明你懂框架。

### 0.2 三条铁律（碰了就白做）

1. **不改 `Sample` / `Operator` 的公开签名**（全仓库都依赖它）。
2. **既有 4 个 config 一字不动**（`configs/` 里那 4 个是别人的实验基线）。
3. **测试基线只增不减**——重写后条数应当**一样**；少了说明你删/跳了测试。  
   另外：**不许改测试文件**。测试是规格，不是你实现的对齐目标。

### 0.3 验收命令（Windows，系统 Python 3.11）

```bash
# 单模块验收（换掉 <测试文件>）—— --basetemp 必须是「盘符形式 + 全新目录」
python -X utf8 -m pytest <测试文件> --basetemp=C:/Users/10393/Desktop/mm-curation-pipeline/.pytest_tmp/rw

# 收工前全量（两个都要跑）
python -X utf8 -m pytest --basetemp=C:/Users/10393/Desktop/mm-curation-pipeline/.pytest_tmp/rw_main
python -X utf8 -m pytest packages/curation-eval/tests --basetemp=C:/Users/10393/Desktop/mm-curation-pipeline/.pytest_tmp/rw_pkg
python -X utf8 -m ruff check .
python -X utf8 scripts/verify_claims.py
```

> ⚠️ `--basetemp` 一定要写**盘符形式的绝对路径**（`C:/...`）。  
> 写成 `/tmp/...` 会让 pytest 报出几十条看似"测试坏了"的 setup error——那是**装置**的问题，不是你的代码。

### 0.4 工作方式（这条决定你能不能真的"拿走"这块知识）

- **关掉 AI 自动补全**再写（斯坦福 CS336 的 AI 政策明写这一点，理由是它会让你更难深入）。
- 顺序：**先读测试 → 写出你对规格的理解 → 再写实现 → 跑测试**。
- 一个模块一次提交，message 里写**你为什么这么实现**（不是"完成了 X"）。

---

## R1 —— `curation_eval/schema.py`：唯一数据结构（热身）

**目标**：全项目所有数据都长这个样。你要负责的是**结构不变量**：什么组合合法、什么必须在构造期就死。

**接口契约（不许改字段名与顺序）**

```python
Sample(id: str, text: str = "", image_path: str | None = None,
       modality: str = "text_article", meta: dict = {}, labels: dict = {})
MODALITY_FIELDS: dict[str, frozenset[str]]   # 模态 → 该模态必然可用的字段
```

**规格来源**：`packages/curation-eval/tests/test_schema.py`（10 条）

**验收**：`pytest packages/curation-eval/tests/test_schema.py` 全绿

**自检问题（做之前先答）**

1. 一个样本带了 `image_path`，但 `modality` 写的是 `text_article`——该报错还是该修正？**为什么？**
2. 反过来，声明 `image_caption` 却不给 `image_path`——报错还是放行？**这两条为什么不对称？**
3. `text` 允许是空串吗？如果禁止空串，哪个算子会第一个失效？
4. 旧版本的数据里键叫 `caption`，新版本叫 `text`——兼容性该放在构造期还是反序列化期？

**参考资料方向**：dataclass 的 `__post_init__` 校验、序列化兼容（schema evolution / forward compatibility）。

---

## R2 —— 指标：`curation_eval/metrics.py` + `mm_curation/eval/metrics.py`

**目标**：把"我丢了这些样本"翻译成**可比较的数字**。这是全项目所有"效果结论"的数学地基。

**接口契约**

```python
# packages/curation-eval/src/curation_eval/metrics.py
pr_from_drops(dropped_ids: Sequence[str], mixed: list[Sample]) -> dict
recall_at_k(rankings: list[int | None], k: int) -> float
mrr(rankings: list[int | None]) -> float
cohen_kappa(y1: Sequence, y2: Sequence) -> float | None
```

**规格来源**：`packages/curation-eval/tests/test_metrics_kappa.py`（6 条）+ `tests/test_metrics.py`（3 条）

**验收**：两个文件都全绿

**自检问题**

1. `pr_from_drops` 里 **positive 指的是"脏"还是"干净"**？为什么这么定？（想清楚"precision"在这里的业务含义）
2. κ 在**两列标签完全一致**时应等于多少？在**只出现一个类别**时呢？  
   后者如果返回 `0.0`，会误导读者以为"判定者很差"——那正确的表达是什么？
3. 两列长度不等时返回什么？为什么不静默截断？
4. `recall_at_k([2], 0)` 应该是几？为什么？

**★ 一个值得你追问的架构观察**：`recall_at_k` / `mrr` 在**包**和**主仓**里各有一份实现。  
去读两处，然后回答：**为什么要两份？该不该合并？合并的代价是什么？**  
（提示：先看两边的 import 关系，再看谁依赖谁。这个问题没有标准答案，但面试官会喜欢你想过它。）

**参考资料方向**：Cohen's kappa 的定义与退化情形、Precision/Recall 的正类约定、IR 里的 MRR / Recall@K。

---

## R3 —— `curation_eval/registry.py`：元数据与注册机制

**目标**：让**协议矛盾在 import 时就死**，而不是等到运行时才发现"这个算子根本不适用于这个模态"。

**接口契约**

```python
class CostClass(str, Enum): RULE / PERCEPTUAL / MODEL / LLM

@dataclass(frozen=True)
class OperatorMeta:
    name: str; modalities: frozenset[str]; required_fields: frozenset[str]
    cost_class: CostClass; shardable: bool = True; superlinear: bool = False
    input_signal: str | None = None; output_signal: str | None = None

register_operator(*, modalities, required_fields, cost_class,
                  shardable=True, superlinear=False, name=None) -> 类装饰器
get_operator_class(name) / get_operator_meta(name) / available_operator_metas() / unregister(name)
```

**规格来源**：`packages/curation-eval/tests/test_registry.py`（5 条）+ `tests/test_operator_pr.py`（8 条）

**验收**：两个文件全绿

**自检问题**

1. 一个算子声明「我要用 `image_path`」，但它的 `modalities` 只有 `text_article`——为什么这必须报错？  
   报错要发生在**哪个时刻**（import / 构造 / 运行）？为什么不能晚一点？
2. `WmNsfwCnnOp` 这个名字怎么变成 `wm_nsfw_cnn` 的？为什么缩写要"连写保持一组"？
3. 注册表是**模块级全局字典**。"import 即注册"带来什么后果？  
   ——如果一个外部使用者只 `import curation_eval`，注册表里有什么？（提示：有个函数专门为此存在）
4. 重名算子为什么要**拒绝**而不是覆盖？覆盖会掩盖什么 bug？

**参考资料方向**：插件注册表（plugin registry）模式、Python 装饰器与类属性注入、fail-fast 原则。  
**对照读**：`huggingface/datatrove` 之类的 `register_module` 机制（搜 datatrove 的 registry）。

---

## R4 —— `curation_eval/sdk.py`：算子与执行器语义

**目标**：定义"一次漏斗运行"的完整语义——谁被评、谁被跳过、谁被丢、分数怎么记、结果怎么可复现。

**接口契约**

```python
class Operator(ABC):        score(sample) -> float | None
                            keep(score) -> bool          # 由 params.min / params.max 决定
                            explain(sample, score) -> dict
                            __call__(sample) -> Sample | None
class BatchOperator(Operator):  run_batch(samples) -> list[Sample]
class Executor(ABC):        run(ops, samples) -> FunnelResult
                            reduce(shards) -> list[Sample]
StageStat(op, n_in, n_out, dropped, skipped, score_min, score_p50, score_max, batch)
FunnelResult(kept, stats, dropped, transform_stats, transform_dropped)
run_batch_mixed_modality(op, items)
```

**规格来源**：`packages/curation-eval/tests/test_sdk.py`（7 条）+ `tests/test_runner.py`（4 条）

**验收**：全绿

**自检问题**

1. **`score` 返回 `None` 时该保留还是丢弃？为什么？**（这条决定了"量不出来"会不会变成"误杀"）
2. 模态不匹配的样本，是丢弃、报错，还是**保留但不评判**？为什么要有第三种？
3. 分数写进 `sample.meta["score:<op>"]` 而不是直接返回——**这样做能换来什么**？  
   （提示：想一个"换阈值重跑"的场景）
4. ★ **批量算子为什么必须在执行前按 `id` 排序？**  
   去源码里找这条约定的注释，然后回答：如果不排序，**本地跑两次**结果会不会一样？  
   **本地和 Ray** 呢？（这是项目实测踩出来的坑，不是理论问题）
5. `Executor.reduce` 为什么是 `NotImplementedError` 而不是抽象方法？这个"显式占位"想表达什么？

**参考资料方向**：管道-过滤器（pipes and filters）模式、map/reduce 语义、确定性执行与幂等性。

---

## R5 —— `mm_curation/pipeline/config.py` + `runner.py`：配置即契约

**目标**：把"业务阈值"全部外置成 YAML，**并且让配置错误在跑之前就报出来、且报得能直接改**。

**接口契约**

```python
OperatorSpec(op: str, params: dict)          .build() -> Operator
PipelineConfig(name, raw_jsonl, output_dir, operators, description="", runtime="local")
    .from_yaml(path) -> PipelineConfig
run_funnel(samples, config, *, pre_stages=(), verdict_ledger=None) -> FunnelResult
```

**规格来源**：`tests/test_gamma_config.py`（4 条）+ `tests/test_runner.py`

**验收**：全绿

**自检问题**

1. YAML 里引用了一个**不存在的算子名**——报错信息里除了"找不到"，还必须包含什么？  
   为什么这条对使用者体验是决定性的？
2. 算子模态与数据集模态**完全不相交**时，为什么要在跑之前直接报错，而不是跑完说"这一级丢了 0 条"？
3. `runtime` 只允许 `local | ray`——为什么把校验放在**配置解析期**而不是`get_executor`里？
4. 两个"最贵的错误信息"分别属于哪一类：语法错 / 语义错 / 环境错？  
   你有没有见过一条报错信息**本身就告诉你怎么修**？把那种写法偷过来。

**参考资料方向**：配置即代码（config as code）、错误信息的可操作性（actionable error messages）、  
十二要素应用（12-factor）里的 config 那一条。

---

## 往上限：两个增量（不是重写，是长出新东西）

> 这两个才是往"高上限"走的方向。R1–R5 让你**拥有**这个项目；P1–P2 让它**长出上限**。

### P1 —— 自训一个算子，走完最短闭环（对应深度学习课）

**需求**：自己训练一个小分类器（任一模态：文本质量 / 图像质量），  
把它接进算子协议，跑通「**训练 → P/R 评测 → 注册 → 进漏斗**」。

**验收（全部要能满足）**

- 新算子有 `OperatorMeta`，模态/依赖字段声明与实现一致（R3 的校验能过）；
- 用项目的算子级评测口径算出该算子的 **precision / recall / 误杀率**；
- 它在既有配置里能跑通，且**不降低**任何已有算子的指标；
- 交一份 5 行的说明：它打什么靶、误杀在哪里。

**自检问题**：为什么不直接用通用模型？——去读 `NARRATIVE.md` 证据 4 与 `train_detector.py`，  
回答"**自训检测器**"在方法论上解决了什么问题（提示：循环论证）。

**参考资料方向**：CS336 A4 的 quality classifier 部分、`scripts/train_detector.py`、`train_text_quality_classifier.py`。

### P2 —— 数据配方对照实验（毕业级，对应统计课 + 并行课 + 数据库课）

**需求**：固定一份样本与一份配置，**只改一个数据干预**（增/删一个算子、或改一个阈值），  
输出「干预 → 各指标的变化量」，并且给出**这个变化是否超过噪声地板**。

**必须做到（少一条这实验就没有意义）**

1. **先测噪声地板**：同一配置、只换随机种子 / 采样顺序，重复 N 次，量出指标的自然波动；
2. **差值必须与噪声地板比较**，而不是只看"变好了"；
3. **配对**：两次运行必须在**同一批样本**上比（配对检验，不是两组独立均值）；
4. 结论必须写清**显著性**与**样本量**；
5. 结果落成一张表（可由脚本重新生成，不进仓库）。

> ⚠️ **这个项目在这件事上栽过**：第一版"清洗更好"的判据是**假阳性**——  
> 差值几乎等于噪声地板。去读 `ENGINEERING_NOTES.md` 里关于配对符号检验的那条，  
> **那就是本题的参考答案（方法论层面）**。

**自检问题**

1. 如果我只跑一次干预、一次基线，得到的差值能不能下结论？为什么？
2. 噪声地板应该用"绝对差值"还是"相对比例"来定？两种会不会得出相反结论？
3. 换一个随机种子，结论会翻转吗？——**这个问题的答案本身就是一个结论**。

---

## 附一、求助格式（我按这个格式回你）

```
任务：R3
我读的规格：[测试文件 + 具体测试函数名]
我写的实现：[贴代码或贴路径]
我预期：[一句话]
实际发生：[贴测试失败信息原文，别转述]
我已经试过：[试了什么，结果如何]
```

**不要问"帮我写"。** 可以问"我这里理解得对吗"、"这个报错说明我哪一步想错了"。

## 附二、进度与判据（自己打勾，不看完成度看"能不能讲"）

| 任务               | 测试全绿 | 我能不看代码讲 10 分钟 | 我能当场改一个行为 |
| ---------------- | ---- | ------------- | --------- |
| R1 schema        | ☐    | ☐             | ☐         |
| R2 metrics       | ☐    | ☐             | ☐         |
| R3 registry      | ☐    | ☐             | ☐         |
| R4 sdk           | ☐    | ☐             | ☐         |
| R5 config/runner | ☐    | ☐             | ☐         |
| P1 自训算子          | ☐    | ☐             | ☐         |
| P2 配方对照          | ☐    | ☐             | ☐         |

> **第三列才是验收标准。** 测试绿只说明你抄对了地方；能当场改一个行为，才说明这模块是你的。
