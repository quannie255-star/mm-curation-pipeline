# 设计表 — V2 α 阶段：协议收口（curation-eval 0.2 + 主仓库适配）

> 上游决策已拍板（见 docs/ARCHITECTURE_V2.md 与评审记录），本文档只做落地设计。
> 四条拍板决策：①Sample 字段统一+modality ②mm_curation→curation-eval 依赖方向
> ③Executor 一期仅 map 并行（reduce/shuffle 占位）④LLM-judge 普通算子+vLLM 独立服务。

## 1. Sample 字段 schema（正式定义，归属 curation_eval/schema.py）

| 字段 | 类型 | 可空 | 默认 | 语义与约束 |
|---|---|---|---|---|
| id | str | 否（非空校验） | 必填 | 数据集内全局唯一，跨管道运行稳定（由数据源决定，非随机） |
| text | str | 是（可空串） | `""` | 统一文本字段：图文样本=caption，文本样本=正文。**空串合法**——空文本正是质量算子要抓的对象，构造期只校验结构不校验质量 |
| image_path | str \| None | 是 | `None` | 纯文本样本为 None；建议仓库相对路径（serving 静态白名单依赖） |
| modality | str | 否 | 按规则推断 | 开放值集，当前 `image_caption` / `text_article`；每个值在 `MODALITY_FIELDS` 中声明"必然可用字段" |
| meta | dict | 是 | `{}` | 算子分数（`score:<op>` 约定键）与标签透传 |
| labels | dict | 是 | `{}` | 污染器注入的 ground truth（`labels["dirty"]`），干净样本为空 |

### 构造期不变量（`__post_init__`，只管结构不管质量）

```python
MODALITY_FIELDS: dict[str, frozenset[str]] = {
    "image_caption": frozenset({"text", "image_path"}),
    "text_article": frozenset({"text"}),
}  # 开放扩展：新模态在此登记可用字段集


def __post_init__(self) -> None:
    if not self.id:
        raise ValueError("Sample.id 不得为空")
    if self.modality not in MODALITY_FIELDS:
        raise ValueError(f"未知 modality: {self.modality}，已知: {sorted(MODALITY_FIELDS)}")
    if self.image_path is not None and self.modality != "image_caption":
        self.modality = "image_caption"  # 有图自动推断为图文模态（免漏标）
    if self.image_path is None and self.modality == "image_caption":
        raise ValueError("modality=image_caption 要求 image_path 非空")
```

### 序列化兼容（数据层永久兼容 v1 落盘文件）

`to_dict` 只写新键（`text`）；`from_dict` 接受 legacy `caption` 键（存在且无 `text`
时迁移），**写方向不回退**——历史 `data/**/*.jsonl` 可直接加载。

## 2. 算子注册表格式（归属 curation_eval/registry.py）

### 元数据模型

```python
class CostClass(str, Enum):
    RULE = "rule"              # 纯 CPU 规则（长度/正则/字符统计）
    PERCEPTUAL = "perceptual"  # 感知哈希 / 传统 CV（pHash、Laplacian）
    MODEL = "model"            # 神经网络推理（CLIP、CNN 检测器）
    LLM = "llm"                # LLM-as-judge（远程推理服务）


@dataclass(frozen=True)
class OperatorMeta:
    name: str
    modalities: frozenset[str]       # 可处理的样本模态（非空）
    required_fields: frozenset[str]  # 依赖字段 ⊆ 各模态蕴含字段的并集（注册时校验）
    cost_class: CostClass
    shardable: bool = True           # map 语义：输入分片结果不变；全量可见性算子为 False
    superlinear: bool = False        # 复杂度超线性（规模悬崖标注，成本表自动渲染）
    input_signal: str | None = None  # 消费的质量信号 token（如 "embedding:image"），None=原始样本
    output_signal: str | None = None # 产出的信号，默认 "score:<name>"（meta 写入约定键）
```

### 注册装饰器与校验（注册时 fail-fast）

```python
def register_operator(*, modalities, required_fields, cost_class,
                      shardable=True, superlinear=False,
                      input_signal=None, output_signal=None):
    def decorator(cls):
        meta = OperatorMeta(
            name=cls.__name__.lower().replace("op", ""),
            modalities=frozenset(modalities),
            required_fields=frozenset(required_fields),
            cost_class=CostClass(cost_class),
            shardable=shardable, superlinear=superlinear,
            input_signal=input_signal,
            output_signal=output_signal or f"score:{cls.name}",
        )
        implied = frozenset().union(*(MODALITY_FIELDS[m] for m in meta.modalities))
        unknown = meta.required_fields - implied
        if unknown:
            raise ValueError(f"{meta.name}: 依赖字段 {sorted(unknown)} 未被模态 "
                             f"{sorted(meta.modalities)} 蕴含")
        _REGISTRY[meta.name] = (cls, meta)
        cls.name = meta.name
        cls.meta = meta
        return cls
    return decorator
```

### 主仓库算子迁移后的声明示例

```python
@register_operator(
    modalities=frozenset({"text_article", "image_caption"}),
    required_fields=frozenset({"text"}),
    cost_class=CostClass.RULE,
)
class TextLengthOp(Operator):
    """caption/正文长度。score=text 字符数。"""


@register_operator(
    modalities=frozenset({"image_caption"}),
    required_fields=frozenset({"text", "image_path"}),
    cost_class=CostClass.MODEL,
    shardable=False,     # 跨样本判定，需全量可见
    superlinear=True,    # O(n²) 点积
    input_signal="embedding:image",  # 复用 clip_alignment 的图像向量（缓存红利，显式化）
)
class SemanticDedupOp(BatchOperator):
    """图像向量 kNN 语义去重。"""
```

信号表达约定：`output_signal` 默认自动派生（`score:<name>`，写 `meta` 的约定键，
与全项目"score 越高越好"语义一致）；`input_signal` 是可选的显式声明，α 阶段只
存储与渲染（成本表/前沿分析消费），**不做运行时依赖解析**——过度设计红线。

### Executor 占位（决策 ③ 的预留，实现在 γ）

```python
class Executor(ABC):
    """漏斗执行器协议。一期仅 map 并行：单样本算子与 shardable 批量算子可分片；
    shardable=False 的批量算子必须单机全量运行。"""

    @abstractmethod
    def run(self, ops: Sequence[Operator], samples: list[Sample]) -> "FunnelResult": ...

    def reduce(self, shards: list[list[Sample]]) -> list[Sample]:
        raise NotImplementedError("分布式 reduce/shuffle 属二期（分布式去重），显式未实现")
```

`LocalSequentialExecutor` 为默认实现（现 runner 逻辑移植 + 模态跳过）；FunnelResult/
StageStat **一并下沉到包**——决策修订（α 实施时记录）：原计划留在主仓库 γ 再评估，
但 LocalSequentialExecutor 在包内实现必须构造结果对象，不下沉就得引入 TYPE_CHECKING
假引用；协议类型随 Executor 一起下沉后主仓库 re-export，方向与决策 ② 一致。

## 3. 现有图文管道迁移方案

### 3.1 字段重命名清单（caption → text，机械改动）

| 位置 | 改动 |
|---|---|
| operators/base.py | 删除本地 Sample/Operator/BatchOperator 定义 → **纯 re-export from curation_eval** |
| operators/text_quality.py | 3 个算子读 `sample.text`；注册元数据（RULE / 双模态 / text） |
| operators/image_quality.py | 注册元数据（PERCEPTUAL / image_caption / image_path）；无字段改名 |
| operators/dedup.py | minhash 读 `sample.text`；md5/phash/minhash 元数据（PERCEPTUAL，shardable=False，phash superlinear） |
| operators/clip_quality.py | encode_texts 入参改 `sample.text`；元数据（MODEL；semantic_dedup 标 input_signal） |
| operators/detector_quality.py | 元数据（MODEL / image_caption / image_path，shardable=True） |
| contamination/impl.py | 5 个改 caption 的污染器改 text；基类换 curation_eval 的 Contaminator 协议 |
| data/download.py | emit 样本键 caption→text（modality 由构造自动推断，**零显式标记**） |
| serving/quality_gate.py + api.py | assess(text=...)；IngestRequest.caption 字段保留（对外 API 名不变），映射到 text |
| eval/decontam.py, retrieval.py | caption 读取点改 text |
| scripts/ + tests/ | 机械跟随（grep `\.caption|\"caption\"` 清点） |

### 3.2 modality 标记迁移成本：≈0

构造期自动推断（有图→image_caption）覆盖现有全部构造点：
download emit（有图）、contamination deepcopy（继承）、测试夹具（有图）。
显式标记只在**未来文本管道**构造 text_article 时出现。

### 3.3 迁移步骤（顺序即依赖）

1. curation-eval 0.2.0：schema/registry/sdk 三模块 + contamination 迁 Sample + 包测试
2. 主仓库 base.py 变纯 re-export（旧 import 路径立即失效，fail-fast 防脑裂）
3. 全仓库 caption→text 原子重命名（单 PR，保 bisect 能力）
4. 12 个算子补注册元数据
5. runner 委托 LocalSequentialExecutor + 模态跳过计数 + 配置 fail-fast
6. legacy 数据对账 + 双仓库 CI 绿

## 4. α 阶段验收标准

- **A1 协议往返**：10 样本（5 image_caption 真图 + 5 text_article）jsonl 往返字段全等；legacy `caption` 键文件可加载。
- **A2 混合漏斗**：单配置（text 算子 + 图文算子 + 去重）跑 10 样本——text 算子处理 10 条、图文算子处理 5 条（模态跳过计数=5），结果与分子集手跑一致。
- **A3 注册校验**：required_fields 未被模态蕴含 → 注册时 ValueError；未知 modality → 构造 ValueError；重名算子 → ValueError。
- **A4 Executor 语义**：LocalSequentialExecutor 与手写串行循环的 kept/stats 完全一致；`reduce()` 占位抛 NotImplementedError。
- **A5 单一来源**：grep 守卫进 CI——协议类型只允许 `from curation_eval import`；包内不得 import mm_curation。
- **A6 存量回归**：105 项测试全绿；legacy 数据跑漏斗，指标与基线差 ≤1pp。
- **A7 元数据消费**：cost_model 的 superlinear 标注改读注册表元数据，删除硬编码集合后输出一致。

## 5. 潜在风险（各一条规避）

| 风险 | 规避 |
|---|---|
| **R1 改名广度 + 落盘数据兼容**：caption→text 波及 ~20 文件与全部历史 jsonl，漏一处就是运行时炸点 | 数据层永久兼容（from_dict 收 legacy 键，写方向只写 text）；代码层单 PR 原子改名 + A6 存量对账门槛（≤1pp）；展示层（Streamlit）单独 grep 清点 |
| **R2 迁移窗口协议脑裂**：Sample/Operator 在包与主仓库短暂双实现，各自演化产生细微 drift | T6（re-export）与 T7（全局切换）同一 PR 落地，旧路径立即失效；A5 grep 守卫当轮进 CI，绕过 re-export 的新 import 直接红 |
