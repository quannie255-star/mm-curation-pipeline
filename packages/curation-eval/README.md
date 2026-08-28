# curation-eval

> 多模态数据清洗的 **ground-truth 评测框架**——把"清洗算子好不好"从拍脑袋变成可测量。

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

## 三分钟上手

```python
from curation_eval.contamination import ContaminationPlan

samples = [  # 任何系统的样本，只需三个字段
    {"id": "s1", "image_path": "img/1.jpg", "caption": "一只猫坐在沙发上"},
    {"id": "s2", "image_path": "img/2.jpg", "caption": "城市夜景灯火辉煌"},
]

plan = ContaminationPlan(inject_rate=0.5, seed=42,
                         kinds={"watermark": 0.5, "blur": 0.5})
mixed, manifest = plan.run(samples, images_out="data/contaminated_images")
# mixed 中注入样本带 labels={"dirty": "watermark"} —— 这就是 ground truth

# 你的清洗系统跑完后，用丢弃清单算 P/R：
from curation_eval.metrics import pr_from_drops

pr = pr_from_drops(dropped_ids=[...], mixed=mixed)
# -> {"precision": 0.93, "recall": 0.88, "clean_killed": 3, "n_dirty": 486}
```

## 协议约定（接入你的系统只需遵守这些）

| 约定 | 内容 |
|---|---|
| 样本 schema | `{"id": str, "image_path": str, "caption": str, "labels": dict}` |
| ground truth | 注入样本 `labels["dirty"]` = 污染类型名；干净样本 labels 为空 |
| 丢弃语义 | 你的系统返回"被丢弃的 id 列表"，P/R 据此计算 |
| 检索评测 | `recall_at_k(rankings, k)` / `mrr(rankings)`，ranking=目标名次（1-based，None=未命中） |

## 内置污染器

watermark（参数化：布局/字体/透明度/文本池）、blur、low_resolution、
exact_duplicate、truncate_text、mojibake、mismatched_pair。
自定义：继承 `Contaminator` + `@register("your_kind")`。

## 与主仓库的关系

本包从 [mm-curation-pipeline](https://github.com/quannie255-star/mm-curation-pipeline)
的评测体系抽炼而来——在那条 11 级清洗漏斗上，这套协议支撑了
脏数据召回 100% / 干净误杀 2.16% 的可量化清洗，以及 R@1 +21% 的清洗收益证明。
