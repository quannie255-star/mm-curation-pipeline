"""清洗漏斗执行：委托 curation-eval 的执行器协议（V2 决策③）。

架构定位（管道-过滤器模式）：
- 上游：operators/ 提供可插拔算子；PipelineConfig 把 YAML 解析成算子序列
- 执行：LocalSequentialExecutor（curation-eval SDK）——单样本算子逐个调用、
  批量算子 run_batch、模态不匹配的样本保留不评判（计 skipped）
- 本模块保留 run_funnel 便捷入口并做配置级 fail-fast：算子模态与数据集
  观测模态完全不相交时直接报错（该级不会评判任何样本，几乎必然是配置错误）

分数复用约定：算子基类已把分数写入 sample.meta["score:<op>"]，执行器只读
不重算——换阈值重跑漏斗时，昂贵算子（CLIP 编码等）的分数可直接复用。
"""

from __future__ import annotations

import logging

from curation_eval import LocalSequentialExecutor

from ..operators.base import FunnelResult, Sample
from .config import PipelineConfig

logger = logging.getLogger(__name__)

_EXECUTOR = LocalSequentialExecutor()


def run_funnel(samples: list[Sample], config: PipelineConfig) -> FunnelResult:
    """按 config.operators 顺序执行漏斗（执行语义见 curation_eval.sdk）。"""
    ops = [spec.build() for spec in config.operators]
    if ops and samples:
        batch_modalities = {s.modality for s in samples}
        disjoint = [
            op.name
            for op in ops
            if getattr(op, "meta", None) is not None
            and op.meta.modalities.isdisjoint(batch_modalities)
        ]
        if disjoint:
            raise ValueError(
                f"配置错误: 算子 {disjoint} 的模态与数据集 "
                f"{sorted(batch_modalities)} 完全不相交（该级不会评判任何样本）"
            )
    result = _EXECUTOR.run(ops, samples)
    for stat in result.stats:
        if stat.skipped:
            logger.info("算子 %s 跳过 %s 条模态不匹配样本（保留不评判）", stat.op, stat.skipped)
    return result
