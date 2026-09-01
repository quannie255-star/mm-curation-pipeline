# 架构 V2 设计稿（草案，待评审）

> 状态：设计门评审中——本文档是讨论基础，非实施承诺。
> 北极星一句话：**从"一条多模态清洗管道"升级为"一套模态可插拔、运行时可替换、
> 协议即产品的数据质量框架"**——管道只是框架的第一个实例。

## 一、框架口诀（面试与设计的一致性锚点）

**一个框架，两个实例，三种运行时，一个产品**

| 口诀项 | 内容 | 对应升级方向 |
|---|---|---|
| 一个框架 | 数据质量协议 + 算子 SDK（污染器协议、P/R 指标、算子注册表） | curation-eval 收口 |
| 两个实例 | ①图文管道（现有）②中文文本语料管道（新） | D1 |
| 三种运行时 | 本地串行（现有）/ Ray 分布式（新）/ 在线质量门（现有） | D2 |
| 一个产品 | curation-eval 包 + 数据 CI（管道变更自动跑污染基准） | D4 |

## 二、现状架构的三个深层耦合（V2 要解的题）

按"改动会波及多少下游"排序：

1. **Sample 模型绑死模态**（最深）：`Sample(id, image_path, caption, ...)` 中
   image_path 实际必填、caption 命名暗示图文对。文本语料进来要么硬塞 caption
   （hack），要么旁路整个漏斗（框架失效）。所有算子、runner、污染器都继承了这个偏置。
2. **协议双实现**：`curation-eval` 包与 `mm_curation/eval` 各有一套
   contamination/metrics——两份真相必然漂移，且"可复用产品"目前是孤岛（主仓库
   不消费它，复用性未被证明）。
3. **执行模型单机硬编码**：`run_funnel` 是进程内 for 循环；批量算子
   O(n²)（phash/语义去重）假设全量内存可见。Ray 化不是加个装饰器，需要
   执行器抽象 + 算子分片语义声明。

次要耦合（顺手解决，不单独立项）：编码器单例直连（换 SigLIP 要动算子）；
配置单 YAML 只描述漏斗一级（无 stage 级编排）；产物无血缘记录。

## 三、目标分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ L5 产品层   curation-eval(PyPI) · 数据 CI(Action+徽章)        │
│             serving(search/ingest/metrics) · Streamlit       │
├─────────────────────────────────────────────────────────────┤
│ L4 应用层   funnel runner · eval(retrieval/operator_pr/      │
│             decontam/ablation) · sampling · monitoring(PSI)  │
│             · cost_model · dataset card / lineage ledger     │
├─────────────────────────────────────────────────────────────┤
│ L3 执行层   Executor 协议: LocalSequential | RayDistributed   │
│             （同一份算子图，两种运行时；分片语义由算子声明）  │
├─────────────────────────────────────────────────────────────┤
│ L2 算子层   Operator SDK + 注册表（带元数据：模态/成本档/     │
│             依赖字段/可分片性）                               │
│             算子集：文本质量·图文对齐·去重·检测器·LLM-judge   │
├─────────────────────────────────────────────────────────────┤
│ L1 核心层   泛化 Sample 模型 · 污染器协议 · 指标协议 ·        │
│             journal/artifact 状态 · Encoder 协议             │
└─────────────────────────────────────────────────────────────┘
     依赖方向永远向下；协议（L1）独立成包 = curation-eval
```

### 模块迁移映射（现状 → V2 归属）

| 现状 | V2 归属 | 改动 |
|---|---|---|
| operators/base.py 的 Sample | L1 curation-eval（协议）+ mm_curation（实现） | caption→text 统一，image_path 可空 |
| operators/registry.py | L1 → L2 | 注册项增加元数据（见决策 2） |
| pipeline/runner.py | L4，执行委托给 L3 Executor | run_funnel 变薄壳 |
| eval/metrics + contamination | **合并进 curation-eval**（协议单一来源） | mm_curation 转为消费方 |
| dedup_incremental / index / detector | L1-L2 不动 | encoder 走协议解析 |
| serving / monitoring / cost_model | L4-L5 不动 | 获取注册表元数据自动生成成本表 |

## 四、八个关键架构决策（每条：选项 → 推荐 → 理由）

### 决策 1：Sample 泛化——字段统一 vs 模态子类 vs 能力声明
- A. 字段统一：`Sample(id, text, image_path=None, meta, labels)`，caption 成别名
- B. 模态子类：TextSample / ImageTextSample，注册表按模态分册
- C. 能力声明：算子声明 `requires=["image"]`，runner 路由校验
- **推荐 A+C 混合**：字段统一（text 承载一切文本，image_path 可空）+ 注册表
  元数据声明依赖字段，runner 启动时校验配置合法性（fail-fast）而非逐样本路由。
- 理由：B 的类型分册会让"混合管道"（文本+图文算子接力）变别扭；A+C 改动最小
  （caption→text 一次机械重命名 + alias 兼容），且校验前置比运行时过滤好调试。

### 决策 2：注册表元数据——把"成本分级"从口头变成数据
注册项增加：`modalities=("text",|("image","text")|...)`、`cost_class=rule|perceptual|model|llm`、
`requires=("text",)`、`shardable=bool`、`superlinear=bool`。
- 收益：漏斗配置启动校验；成本-质量前沿曲线（D3）从注册表自动生成；
  文档表格（ARCHITECTURE 规模悬崖表）由元数据渲染，不再手工维护。

### 决策 3：Executor 抽象——并行化边界划在哪
- A. Executor 只并行化"单样本算子"（map 语义），批量算子留在 driver 全量跑
- B. 全量 Ray Dataset 原生（含分布式 MinHash-LSH，对标 Data-Juicer）
- **推荐 A 先行，B 二期**。A 的接口：`Executor.run(ops, samples) -> FunnelResult`，
  本地实现零依赖（不装 ray 也能跑），Ray 实现懒加载。
- 理由：去重的分布式化是算法问题（分片内近似 + 全局合并验证，召回会变），
  和执行框架问题耦合在一起会让两件事都难验收。先把 map 并行 + 扩展曲线
  做出来（大数据专业的对口展示），分布式去重单独立项。

### 决策 4：模型推理边界——进程内 vs 服务化
- **推荐：进程内 + Encoder 协议隔离**（`Encoder` Protocol + 按名解析），
  vLLM 服务化只发生在 L3 judge（它天然是独立服务）。
- 理由：1.6k-50k 规模下模型服务化是过度设计；但 LLM-judge 必须走服务
  （显存隔离、批处理、可替换模型），两者边界划清即可。

### 决策 5：协议单一来源——依赖方向定为 mm_curation → curation-eval
- 现状双实现合并：**协议与纯函数（污染器协议、指标、Sample schema）下沉到
  curation-eval；模态相关重实现（图像渲染、CLIP 算子）留在主仓库并消费包**。
- 理由：主仓库变成自己产品的消费者（吃狗粮），是"可复用"的最强证明；
  风险（包 API 不稳定拖累主仓库）用版本 pin + 薄适配层控制。
- 备选：反向（包依赖主仓库）被否——包会背上整条管道的依赖，失去独立安装价值。

### 决策 6：编排——流水线 manifest 与 DAG 生成
一个 manifest 描述 stage 有向图（download→contaminate→funnel→index→eval），
每 stage 声明输入/输出 artifact；**Airflow DAG 从 manifest 生成**（不再手写 bash 链）。
- 理由：数据管道 as code 的成熟度台阶，且 lineage（决策 8）需要 stage 图做骨架。
- 克制点：不做通用 DSL，stage 就是现有 CLI 的类型化包装。

### 决策 7：LLM-judge 的架构位置
- 作为普通注册算子（L2）+ 独立推理边界（vLLM OpenAI 兼容客户端）；
  成本元数据（每次调用 token 估算）进注册表，前沿分析自动可得。
- 评测协议：与 ground truth 的一致性用 Cohen's kappa 报告（judge 不是真理，
  一致性才是它的可信度证明——这条本身是面试弹药）。

### 决策 8：产物血缘——轻量 ledger，不上数据库
每个 stage 追加一条 `{stage, input_hash, config_hash, output, stats, ts}` 到
`runs/ledger.jsonl`；dataset card 由 ledger join 自动渲染。DVC 只管大文件指针。

## 五、反目标（明确不做，防架构过度设计）

- ❌ 流式/Kafka：批处理+微批足够（当前与可预见规模下），在线质量门已是流式语义
- ❌ 元数据数据库：ledger 文件够用，查询需求出现再说
- ❌ entry-points 插件发现：import 注册够用，第三方算子生态出现再升级
- ❌ K8s/多租户：单机+Ray 单集群假设
- ❌ 通用 DSL：stage 图 YAML 化即可，不做图编程界面

## 六、升级路线（六次外科手术，每刀可独立验收）

| 阶段 | 内容 | 交付物/验收 | 预估 |
|---|---|---|---|
| α 协议收口 | 决策 1/2/5：Sample 泛化 + 注册表元数据 + 协议合并进包 | 主仓库消费 curation-eval；105 测试保持绿 | 3-4 天 |
| β 文本实例 | D1：文本污染器+算子（困惑度/重复度/boilerplate/PII）+10 万级文本去重基准 + GPT-2 zh 干净/脏训练对比 | 文本管道端到端 + 训练级证据 | 1-2 周 |
| γ 执行层 | 决策 3：Executor 协议 + Ray 后端 | 1→4 进程扩展曲线图 | 1 周 |
| δ L3 judge | 决策 7：vLLM + judge 算子 + kappa + 成本-质量前沿 | 三层前沿曲线图 | 1 周 |
| ε 产品收口 | 决策 6/8 + D4：manifest+DAG 生成 + ledger/dataset card + 数据 CI + PyPI | CI 徽章 + 可 pip install | 3-5 天 |

依赖关系：α 是所有后续的地基（必须最先）；β/γ 可并行；δ 依赖 α（元数据）；
ε 收口。总量约 6-8 周。

## 七、迁移风险

| 风险 | 缓解 |
|---|---|
| caption→text 重命名波及全仓库 | alias property 兼容一个版本；机械改动用测试兜底（105 项全绿门槛） |
| curation-eval API 不稳拖累主仓库 | 版本 pin + 薄适配层；包内 API 变更走 changelog |
| Ray 在 Windows 本机不可用 | Executor 本地实现是默认；Ray 实测放 Linux/WSL2，曲线照出 |
| 去重分布式化改变召回（分片近似） | 二期单独立项，先在文档声明语义差异 |
