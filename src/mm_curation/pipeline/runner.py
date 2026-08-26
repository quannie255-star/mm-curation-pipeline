"""清洗漏斗执行器：按配置顺序逐级过滤样本并记录统计与丢弃明细。

架构定位（管道-过滤器模式）：
- 上游：operators/ 提供可插拔算子；PipelineConfig 把 YAML 解析成算子序列
- 本模块：纯内存执行，不做文件 IO（scripts 与 Airflow DAG 负责 IO），
  保证可在测试中直接喂 list[Sample]
- 下游：quality/report.py 消费 stats 渲染报告；eval/ 消费 dropped 计算
  算子 precision/recall（被扔的样本必须可溯源到具体算子，这是评测地基）

分数复用约定：算子基类已把分数写入 sample.meta["score:<op>"]，本模块只读
不重算——换阈值重跑漏斗时，昂贵算子（CLIP 编码等）的分数可直接复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..operators.base import Sample
from ..operators.registry import is_batch
from .config import PipelineConfig


@dataclass
class StageStat:
    """漏斗一级的统计快照（可观测性的最小单元）。"""

    op: str
    n_in: int
    n_out: int
    dropped: int
    score_min: Optional[float] = None
    score_p50: Optional[float] = None
    score_max: Optional[float] = None
    batch: bool = False  # 批量算子无单样本分数，分数字段为 None

    @property
    def pass_rate(self) -> float:
        return self.n_out / self.n_in if self.n_in else 0.0


@dataclass
class FunnelResult:
    """一次漏斗运行的完整结果。"""

    kept: list[Sample] = field(default_factory=list)
    stats: list[StageStat] = field(default_factory=list)
    dropped: list[tuple[str, Sample]] = field(default_factory=list)  # (算子名, 样本)


def _score_stats(samples: list[Sample], op_name: str) -> tuple[Optional[float], ...]:
    """从 meta 收集该级分数分布（跳过 None：无法计分的样本不算入）。"""
    scores = sorted(v for s in samples if (v := s.meta.get(f"score:{op_name}")) is not None)
    if not scores:
        return None, None, None
    mid = len(scores) // 2
    p50 = scores[mid] if len(scores) % 2 else (scores[mid - 1] + scores[mid]) / 2
    return scores[0], p50, scores[-1]


def run_funnel(samples: list[Sample], config: PipelineConfig) -> FunnelResult:
    """按 config.operators 顺序执行漏斗。

    单样本算子：逐个 __call__，返回 None 的记入 dropped；
    批量算子（去重组）：对当前存活集 run_batch，按 id 差集找出被扔样本。
    """
    result = FunnelResult(kept=list(samples))
    for spec in config.operators:
        op = spec.build()
        n_in = len(result.kept)
        survivors: list[Sample]
        dropped_here: list[Sample]
        if is_batch(op):
            survivors = op.run_batch(result.kept)
            kept_ids = {s.id for s in survivors}
            dropped_here = [s for s in result.kept if s.id not in kept_ids]
            stat = StageStat(
                op=spec.op,
                n_in=n_in,
                n_out=len(survivors),
                dropped=len(dropped_here),
                batch=True,
            )
        else:
            survivors, dropped_here = [], []
            for s in result.kept:
                (survivors if op(s) is not None else dropped_here).append(s)
            smin, p50, smax = _score_stats(survivors + dropped_here, spec.op)
            stat = StageStat(
                op=spec.op,
                n_in=n_in,
                n_out=len(survivors),
                dropped=len(dropped_here),
                score_min=smin,
                score_p50=p50,
                score_max=smax,
            )
        result.kept = survivors
        result.stats.append(stat)
        result.dropped.extend((spec.op, s) for s in dropped_here)
    return result
