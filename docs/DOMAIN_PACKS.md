# 领域增强包规范（Domain Pack Spec）

> 本项目的形态：**多模态数据清洗与预处理平台 = curation-eval 基座 + 领域增强包**。
> 基座提供机制（Sample 协议、算子注册表、执行器、污染器框架、指标与门禁渲染），
> 增强包提供领域实例。本文档定义增强包的标准构成与扩展步骤。

## 一、基座提供什么（curation-eval 包，pip 可装）

| 机制 | 位置 |
|---|---|
| Sample 协议 + MODALITY_FIELDS 模态登记 | `curation_eval.schema` |
| 算子注册表（元数据声明 + fail-fast 校验） | `curation_eval.registry` |
| 执行器协议（local 串行 / Ray 分布式，模态跳过语义） | `curation_eval.sdk` / `ray_executor` |
| 污染器框架（注册 + 注入计划 + ground truth 约定） | `curation_eval.contamination` |
| P/R / Recall@K / Cohen's κ 指标 | `curation_eval.metrics` |

## 二、增强包六件套（每个领域一个包）

| 件 | 位置约定 | 参考实现（FHIR / 工业） |
|---|---|---|
| ① 模态适配器 | 包侧 `curation_eval/<domain>.py` | `fhir.py` / `sensor.py` |
| ② 领域算子 | 主仓 `src/mm_curation/operators/<domain>_quality.py` | `fhir_quality.py` / `industrial_quality.py` |
| ③ 领域污染器 | 包侧 `curation_eval/<domain>_contamination.py` | `fhir_contamination.py` / `sensor_contamination.py` |
| ④ 确定性合成语料 | 主仓 `src/mm_curation/data/<domain>_synth.py` | `fhir_synth.py` / `sensor_synth.py` |
| ⑤ 漏斗配置 | `configs/funnel_<domain>.yaml` | `funnel_fhir.yaml` / `funnel_industrial.yaml` |
| ⑥ 评测门禁 | `scripts/eval_<domain>.py` + Makefile 目标 | `eval_fhir.py` / `eval_industrial.py` |

**硬约定（六件套必须遵守）**：
- 适配器：text = payload canonical JSON（sort_keys、ensure_ascii=False），roundtrip
  保真（`from(to(s)) == s`），构造期 fail-fast；结构化内容不提升为 Sample 顶层字段。
- 算子：score「越高越好」，None = 无法计分保留并记录；批量算子跨资源/跨窗统计
  走 shardable=False；排序等确定性约定只依赖 payload 字段。
- 污染器：labels.dirty = kind、注入即复制不改原始样本；供体重抽走 ctx.pool +
  ctx.rng（同 seed 确定性）；码表/词表与语料同源（包内侧持同值副本并注释）。
- 语料：`--seed` 逐字节一致；内嵌全部静态表；**合法业务异常**不注入不标注
  （防止算子靠「字段必须齐全」作弊）；无任何真实数据（报告脚注声明）。
- 门禁：总体故障召回 ≥90% 且误杀 ≤5%，跌破 exit 1；报告与 operator_pr 同格式。

## 三、已落地增强包

| 包 | 模态 | 一句话 | 验收 |
|---|---|---|---|
| 图文（V1） | image_caption | COCO-CN + 10 类污染，11 级漏斗 | R@1 +21%，召回 100%/误杀 2.16% |
| 文本（V2 β） | text_article | 30 万维基 + 4 类文本污染 | 去重 exact 1.0/near 0.97；ppl +7.5% |
| 医疗（V4 α） | fhir_resource | FHIR R4 五算子（PHI/编码/单位/时间/引用） | 召回 100%/误杀 0% |
| 工业（V5 α） | industrial_sensor | 传感器五算子（卡死/量程/漂移/单位/计划判别） | 召回 100%/误杀 1.04% |

## 四、新增一个领域包的步骤（模板复制路径）

1. `schema.py` 登记模态（一行）；包侧新增 `<domain>.py` 适配器（照 `sensor.py` 抄结构）。
2. 主仓新增 `<domain>_synth.py`：先造语料（内嵌静态表 + 合法业务异常 + seed 确定性）。
3. 主仓新增 `<domain>_quality.py` 算子：每个算子想清楚「合法形态 vs 脏形态」的区分
   依据（业务事件源进样本流，不让算子猜业务）。
4. 包侧新增 `<domain>_contamination.py`：每类注入对应一个主靶算子；供体重抽满足
   检出前提（如漂移注入必须在基线窗之后）。
5. `configs/funnel_<domain>.yaml` + `scripts/eval_<domain>.py`（照 eval_industrial 抄）
   + Makefile 目标 + OPERATOR_TARGETS 注册。
6. 测试：适配器 roundtrip ≥6（包侧）+ 算子四向 ≥4×N + 语料确定性 + 污染器靶向与
   干净侧零误杀 + 评测冒烟。

## 五、红线（不做的事）

- 不做插件系统/算子市场——「基座+包」是文档约定与目录结构，不是运行时机制。
- 不引入改写型算子语义——预处理（换算/对齐/窗口化的*执行*）走漏斗外
  前置/后置阶段；漏斗内只有打分器。
- 领域知识不进基座：码表/量程表/检修计划等全部留在包内或语料生成器，基座零领域词。
