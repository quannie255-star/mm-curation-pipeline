"""改写通道协议（V6 决策点 2）：与「打分→阈值」并列的前置阶段。

## 为什么需要这条通道

框架原有的唯一通道是 `Operator`：score → 阈值判决。没有「改写」通道。
笔记 #65 记录了后果：真实新闻语料上 `chinese_ratio` 一级拦 778/2066（37.7%），
根因是爬虫抽取缺陷造成的**空白膨胀**（被拦篇目空白占比中位数 77.2%）把
「汉字/全文长度」的分母撑爆。当时的修复是把去空白写进 `chinese_ratio` 的语义里
——补丁正确，但**只救了这一个算子**：`text_length` / `char_repetition` /
`text_minhash` / `boilerplate` 仍然在被污染的文本上打分。

`Transformer` 是那条修复的语义正确位置：在算子链之前、逐样本、一次。

## 协议约定（与 Operator 对称）

- 语义正交：Operator 产出 score 并被阈值判决；Transformer 产出**新样本**
- 模态不匹配的样本**原样透传并计 skipped**，不误杀（与 Operator 同款约定）
- 改写日志写 `sample.meta["transform:<name>"]`（与 `score:<op>` 命名对称）
- 返回 `TransformResult(sample=None)` 表示改写后不可用（如归一化后为空串）
  → 该阶段丢弃并计数，不计入任何算子的账
- 确定性：同输入必同输出（`transform` 不得有随机/IO）
- 允许原地修改 sample 或返回新对象，调用方只看返回值

## 范围声明（一期）

前置阶段在**单机**执行（归一化是逐样本纯计算，成本远低于算子链）。
Ray 运行时的分布式前置阶段属二期——与 γ 阶段「GPU worker 调度属后续」
同款的显式范围声明，不假装已覆盖。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .schema import MODALITY_FIELDS, Sample


@dataclass(frozen=True)
class TransformerMeta:
    """改写器的框架级声明（注册时一次性校验，与 OperatorMeta 同款 fail-fast）。"""

    name: str
    modalities: frozenset[str]
    required_fields: frozenset[str] = frozenset({"text"})

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("TransformerMeta.name 不得为空")
        if not self.modalities:
            raise ValueError(f"{self.name}: modalities 不得为空")
        unknown_mod = set(self.modalities) - set(MODALITY_FIELDS)
        if unknown_mod:
            raise ValueError(
                f"{self.name}: 未知模态 {sorted(unknown_mod)}，已知: {sorted(MODALITY_FIELDS)}"
            )
        implied = frozenset().union(*(MODALITY_FIELDS[m] for m in self.modalities))
        unknown = set(self.required_fields) - implied
        if unknown:
            raise ValueError(
                f"{self.name}: 依赖字段 {sorted(unknown)} 未被模态 "
                f"{sorted(self.modalities)} 蕴含（蕴含: {sorted(implied)}）"
            )


@dataclass
class TransformResult:
    """一次改写的产物。`sample=None` = 改写后不可用（丢弃）。"""

    sample: Sample | None
    changed: bool = False
    log: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unchanged(cls, sample: Sample) -> "TransformResult":
        """原样通过（规则未命中任何一条）——不算改写，不写日志。"""
        return cls(sample=sample, changed=False)

    @classmethod
    def replaced(cls, sample: Sample, log: dict[str, Any]) -> "TransformResult":
        """已改写：`log` 必填——没留痕的改写不可审计。"""
        if not log:
            raise ValueError("replaced 必须提供非空 log（改写必须留痕）")
        return cls(sample=sample, changed=True, log=log)

    @classmethod
    def unusable(cls, log: dict[str, Any]) -> "TransformResult":
        """改写后不可用（如归一化后为空串）→ 该阶段丢弃。"""
        if not log:
            raise ValueError("unusable 必须提供非空 log（丢弃必须记因）")
        return cls(sample=None, changed=True, log=log)


class Transformer(ABC):
    """单样本改写器基类（前置阶段的最小单元）。"""

    name: str = "transformer"
    meta: TransformerMeta | None = None  # 由 @register_transformer 注入

    @abstractmethod
    def transform(self, sample: Sample) -> TransformResult: ...

    def applies_to(self, sample: Sample) -> bool:
        """模态是否适用；无元数据（v1 风格）= 全模态适用。"""
        meta = getattr(self, "meta", None)
        return meta is None or sample.modality in meta.modalities

    def __call__(self, sample: Sample) -> Sample | None:
        """便捷调用：只取样本，日志由 run_pre_stages 统一落盘。"""
        return self.transform(sample).sample


@dataclass
class StageTransformStat:
    """前置阶段一级的统计快照（与 StageStat 对称的可观测性单元）。"""

    stage: str
    n_in: int
    n_out: int
    changed: int = 0  # 实际被改写的样本数
    dropped: int = 0  # 改写后不可用而丢弃的样本数
    skipped: int = 0  # 模态不匹配原样透传的样本数

    @property
    def pass_rate(self) -> float:
        return self.n_out / self.n_in if self.n_in else 0.0


@dataclass
class TransformRunResult:
    """前置阶段全链结果（可并入 FunnelResult 的对应字段）。"""

    samples: list[Sample] = field(default_factory=list)
    stats: list[StageTransformStat] = field(default_factory=list)
    dropped: list[tuple[str, Sample]] = field(default_factory=list)  # (阶段名, 样本)


_REGISTRY: dict[str, TransformerMeta] = {}


def register_transformer(
    *,
    name: str,
    modalities: Iterable[str],
    required_fields: Iterable[str] = ("text",),
):
    """类装饰器：登记改写器元数据（重名拒绝，与算子注册表同款）。"""

    def decorator(cls):
        meta = TransformerMeta(
            name=name,
            modalities=frozenset(modalities),
            required_fields=frozenset(required_fields),
        )
        if meta.name in _REGISTRY:
            raise ValueError(f"改写器名冲突: {meta.name} 已注册")
        _REGISTRY[meta.name] = meta
        cls.name = meta.name
        cls.meta = meta
        return cls

    return decorator


def unregister_transformer(name: str) -> None:
    """移除注册（测试隔离用）。"""
    _REGISTRY.pop(name, None)


def get_transformer_meta(name: str) -> TransformerMeta:
    return _REGISTRY[name]


def available_transformers() -> dict[str, TransformerMeta]:
    return dict(_REGISTRY)


def run_pre_stages(stages: Sequence[Transformer], samples: list[Sample]) -> TransformRunResult:
    """按顺序执行前置阶段链（逐样本；模态不匹配透传；改写日志落 meta）。

    与执行器无关（不关心 Local/Ray）——前置阶段语义上是「进漏斗之前」。
    """
    kept = list(samples)
    stats: list[StageTransformStat] = []
    dropped: list[tuple[str, Sample]] = []
    for stage in stages:
        n_in = len(kept)
        survivors: list[Sample] = []
        changed = 0
        n_dropped = 0
        skipped = 0
        for s in kept:
            if not stage.applies_to(s):
                skipped += 1
                survivors.append(s)
                continue
            result = stage.transform(s)
            if result.sample is None:
                n_dropped += 1
                dropped.append((stage.name, s))
                if result.log:
                    s.meta[f"transform:{stage.name}"] = {**result.log, "outcome": "unusable"}
                continue
            if result.changed:
                changed += 1
                result.sample.meta[f"transform:{stage.name}"] = {
                    **result.log,
                    "outcome": "replaced",
                }
            survivors.append(result.sample)
        kept = survivors
        stats.append(
            StageTransformStat(
                stage=stage.name,
                n_in=n_in,
                n_out=len(kept),
                changed=changed,
                dropped=n_dropped,
                skipped=skipped,
            )
        )
    return TransformRunResult(samples=kept, stats=stats, dropped=dropped)
