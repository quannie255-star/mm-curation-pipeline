"""算子 SDK 与执行器协议（V2 α：协议下沉到包，主仓库 re-export）。

score 语义（全项目统一）：越高越好；None = 无法计分（保留并记录缺失，
不误杀）。阈值经 params 的 min/max 表达；算子把分数写入
sample.meta["score:<name>"]（漏斗报告与阈值扫描直接复用）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from .schema import Sample


@dataclass
class StageStat:
    """漏斗一级的统计快照（可观测性最小单元）。"""

    op: str
    n_in: int
    n_out: int
    dropped: int
    skipped: int = 0  # 模态不匹配被跳过（保留不评判）的样本数
    score_min: float | None = None
    score_p50: float | None = None
    score_max: float | None = None
    batch: bool = False

    @property
    def pass_rate(self) -> float:
        return self.n_out / self.n_in if self.n_in else 0.0


@dataclass
class FunnelResult:
    """一次漏斗运行的完整结果。"""

    kept: list[Sample] = field(default_factory=list)
    stats: list[StageStat] = field(default_factory=list)
    dropped: list[tuple[str, Sample]] = field(default_factory=list)  # (算子名, 样本)


class Operator(ABC):
    """单样本过滤算子基类。"""

    name: str = "operator"
    meta: Any = None  # OperatorMeta，由 @register_operator 注入；无元数据=全模态全字段

    def __init__(self, **params: Any):
        self.params = params

    @abstractmethod
    def score(self, sample: Sample) -> float | None: ...

    def keep(self, score: float | None) -> bool:
        if score is None:
            return True
        lo = self.params.get("min")
        hi = self.params.get("max")
        if lo is not None and score < lo:
            return False
        if hi is not None and score > hi:
            return False
        return True

    def __call__(self, sample: Sample) -> Sample | None:
        score = self.score(sample)
        sample.meta[f"score:{self.name}"] = score
        return sample if self.keep(score) else None


class BatchOperator(Operator):
    """跨样本算子基类（去重等需要全量视角的阶段）。"""

    def score(self, sample: Sample) -> float | None:
        raise TypeError(f"{type(self).__name__} 是批量算子，无单样本 score")

    def __call__(self, sample: Sample) -> Sample | None:
        raise TypeError(f"{type(self).__name__} 是批量算子，请通过 run_batch() 调用")

    @abstractmethod
    def run_batch(self, samples: list[Sample]) -> list[Sample]: ...


class Executor(ABC):
    """漏斗执行器协议（ARCHITECTURE_V2 决策③：一期仅 map 并行）。

    shardable=True 的批量算子与全部单样本算子可分片并行；
    shardable=False（全量可见性）必须单机运行。分布式的 reduce/shuffle
    属二期（分布式去重），此处显式占位。
    """

    @abstractmethod
    def run(self, ops: Sequence[Operator], samples: list[Sample]) -> FunnelResult: ...

    def reduce(self, shards: list[list[Sample]]) -> list[Sample]:
        raise NotImplementedError("分布式 reduce/shuffle 属二期（分布式去重），显式未实现")


def _score_stats(samples: list[Sample], op_name: str) -> tuple[float | None, ...]:
    """从 meta 收集该级分数分布（None 分数不算入——无法计分不污染分布）。"""
    scores = sorted(v for s in samples if (v := s.meta.get(f"score:{op_name}")) is not None)
    if not scores:
        return None, None, None
    mid = len(scores) // 2
    p50 = scores[mid] if len(scores) % 2 else (scores[mid - 1] + scores[mid]) / 2
    return scores[0], p50, scores[-1]


class LocalSequentialExecutor(Executor):
    """单机串行执行（v1 run_funnel 语义 + V2 模态跳过）。

    - 单样本算子：逐个调用；模态不匹配的样本保留不评判（计 skipped）
    - 批量算子：有元数据时按模态切分（不适用部分直通且保序），无元数据全量
    - shardable 语义在一期串行实现中无行为差异（并行化属 Ray 后端）
    - 无元数据的 v1 风格算子：全模态全字段，行为与 v1 run_funnel 完全一致
    """

    def run(self, ops: Sequence[Operator], samples: list[Sample]) -> FunnelResult:
        result = FunnelResult(kept=list(samples))
        for op in ops:
            meta = getattr(op, "meta", None)
            n_in = len(result.kept)
            survivors: list[Sample]
            dropped_here: list[Sample]
            skipped = 0
            if isinstance(op, BatchOperator):
                if meta is not None:
                    applicable = [s for s in result.kept if s.modality in meta.modalities]
                    passthrough = [s for s in result.kept if s.modality not in meta.modalities]
                    skipped = len(passthrough)
                    done = {s.id for s in op.run_batch(applicable)} | {s.id for s in passthrough}
                    survivors = [s for s in result.kept if s.id in done]
                    dropped_here = [s for s in result.kept if s.id not in done]
                else:
                    survivors = op.run_batch(result.kept)
                    done = {s.id for s in survivors}
                    dropped_here = [s for s in result.kept if s.id not in done]
                stat = StageStat(
                    op=op.name,
                    n_in=n_in,
                    n_out=len(survivors),
                    dropped=len(dropped_here),
                    skipped=skipped,
                    batch=True,
                )
            else:
                survivors, dropped_here = [], []
                for s in result.kept:
                    if meta is not None and s.modality not in meta.modalities:
                        skipped += 1
                        survivors.append(s)
                        continue
                    (survivors if op(s) is not None else dropped_here).append(s)
                smin, p50, smax = _score_stats(survivors + dropped_here, op.name)
                stat = StageStat(
                    op=op.name,
                    n_in=n_in,
                    n_out=len(survivors),
                    dropped=len(dropped_here),
                    skipped=skipped,
                    score_min=smin,
                    score_p50=p50,
                    score_max=smax,
                )
            result.kept = survivors
            result.stats.append(stat)
            result.dropped.extend((op.name, s) for s in dropped_here)
        return result
