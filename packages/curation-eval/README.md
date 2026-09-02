# curation-eval

> 多模态/多模态数据清洗的 **ground-truth 评测框架**——把"清洗算子好不好"从拍脑袋变成可测量。
> V2 起（0.2.0）它同时是**协议与算子 SDK 的单一来源**：Sample schema、算子注册表、
> 执行器协议、污染器协议、评测指标都在这里，主仓库 mm-curation-pipeline 是它的消费者。

## 为什么需要它

Data-Juicer 提供 100+ 清洗算子，LAION 提供清洗配方，但都没有回答一个问题：
**你的清洗算子到底有多少 precision / recall？** 原因很简单：真实脏数据没有
ground truth，扔掉的样本无法追溯对错。

`curation-eval` 的思路：**程序化污染器**向干净种子集注入可控脏数据并保留标注，
于是每个清洗算子都有了可计算的 P/R，每条管道都有了"脏数据召回率 vs 好数据
误杀率"。它不提供算子——它评测算子。与 Data-Juicer 等清洗系统互补。

## 安装

```bash
pip install -e packages/curation-eval   # 本仓库内
# 或发布后: pip install curation-eval
```

## 五分钟上手

```python
from curation_eval import ContaminationPlan, Sample, pr_from_drops

samples = [
    Sample(id="s1", text="一只猫坐在沙发上", image_path="img/1.jpg"),   # 图文模态
    Sample(id="s2", text="城市夜景灯火辉煌"),                            # 纯文本模态
]

plan = ContaminationPlan(inject_rate=0.5, seed=42,
                         kinds={"watermark": 0.5, "mojibake": 0.5})
mixed, manifest = plan.run(samples, images_out="data/contaminated_images")
# mixed 中注入样本 labels={"dirty": "watermark"} —— 这就是 ground truth
# 图像类污染自动只选带图样本；文本类污染对纯文本样本工作

# 你的清洗系统跑完后，用丢弃清单算 P/R：
pr = pr_from_drops(dropped_ids=[...], mixed=mixed)
# -> {"precision": 0.93, "recall": 0.88, "clean_killed": 3, "n_dirty": 486}
```

## 协议约定（接入你的系统只需遵守这些）

| 约定 | 内容 |
|---|---|
| 样本 schema | `Sample(id, text, image_path=None, modality, meta, labels)`；`modality` 由构造推断（有图=image_caption），开放扩展 |
| ground truth | 注入样本 `labels["dirty"]` = 污染类型名；干净样本 labels 为空 |
| 算子注册 | `@register_operator(modalities=..., required_fields=..., cost_class=...)`，注册时校验依赖字段被模态蕴含 |
| score 语义 | 越高越好；写入 `meta["score:<op>"]`；None=无法计分（保留不评判） |
| 丢弃语义 | 你的系统返回"被丢弃的 id 列表"，`pr_from_drops` 据此计算 |
| 检索评测 | `recall_at_k(rankings, k)` / `mrr(rankings)`，ranking=目标名次（1-based，None=未命中） |

## 内置污染器

watermark（参数化：透明度/文案）、blur、low_resolution、exact_duplicate、
truncate_text、mojibake、mismatched_pair。带图污染器声明 `requires_image=True`，
计划运行时自动从纯文本样本中筛除。自定义：继承 `Contaminator` + `@register("your_kind")`。

## 与主仓库的关系

本包从 [mm-curation-pipeline](https://github.com/quannie255-star/mm-curation-pipeline)
的评测体系抽炼而来——在那条 11 级清洗漏斗上，这套协议支撑了
脏数据召回 100% / 干净误杀 2.16% 的可量化清洗，以及 R@1 +21% 的清洗收益证明。

## 变更记录

### 0.2.0（breaking）
- **Sample 泛化**：`caption` → `text`，新增 `modality` 字段（构造自动推断），
  `image_path` 可空（支持纯文本语料）；`from_dict` 永久兼容旧 `caption` 键
- **算子 SDK 下沉**：`Operator`/`BatchOperator`/`Executor`/注册表（带元数据：
  模态/依赖字段/成本档/分片语义）从主仓库迁入
- **contamination API 改为 Sample 协议**（不再接受 dict 样本）
- 0.x 阶段允许 breaking change；1.0 起执行严格 semver

### 0.1.0
- 首版：污染器协议 + 丢弃语义 P/R + 检索指标
