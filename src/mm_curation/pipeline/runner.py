"""清洗漏斗执行：委托 curation-eval 的执行器协议（V2 决策③）。

架构定位（管道-过滤器模式）：
- 上游：operators/ 提供可插拔算子；PipelineConfig 把 YAML 解析成算子序列
- 执行：curation-eval 的执行器协议双运行时——LocalSequentialExecutor（默认，串行）
  与 RayDistributedExecutor（config.runtime: ray，ray 懒加载）；单样本算子逐个调用、
  批量算子 run_batch、模态不匹配的样本保留不评判（计 skipped）
- 本模块保留 run_funnel 便捷入口并做配置级 fail-fast：算子模态与数据集
  观测模态完全不相交时直接报错（该级不会评判任何样本，几乎必然是配置错误）

分数复用约定：算子基类已把分数写入 sample.meta["score:<op>"]，执行器只读
不重算——换阈值重跑漏斗时，昂贵算子（CLIP 编码等）的分数可直接复用。
"""

from __future__ import annotations

import logging
from typing import Sequence

from curation_eval import LocalSequentialExecutor, Transformer, run_pre_stages

from ..operators.base import FunnelResult, Sample
from ..verdict import VerdictLedger, wrap_for_verdict
from .config import PipelineConfig

logger = logging.getLogger(__name__)


def get_executor(runtime: str):
    """按 config.runtime 选执行器；ray 懒加载（不装 ray 时仅 ray 路线报错）。"""
    if runtime == "ray":
        from curation_eval import RayDistributedExecutor

        return RayDistributedExecutor()
    return LocalSequentialExecutor()


def run_funnel(
    samples: list[Sample],
    config: PipelineConfig,
    *,
    pre_stages: Sequence[Transformer] = (),
    verdict_ledger: VerdictLedger | None = None,
) -> FunnelResult:
    """按 config.operators 顺序执行漏斗（执行语义见 curation_eval.sdk）。

    V6 追加两条可选通道（默认关闭 → 既有调用行为逐位不变）：

    - `pre_stages`：改写通道（决策点 2）。在算子链**之前**逐样本改写并留痕，
      解决「框架只有打分通道、没有改写通道」这个根因（笔记 #65）。
    - `verdict_ledger`：判决书（决策点 5）。把每条判决落成可审计记录——
      通过算子包装器实现，**不改执行器、不改算子协议**。
    """
    transform_stats: list = []
    transform_dropped: list = []
    if pre_stages:
        tr = run_pre_stages(list(pre_stages), samples)
        samples = tr.samples
        transform_stats = list(tr.stats)
        transform_dropped = list(tr.dropped)

    ops = [spec.build() for spec in config.operators]
    if verdict_ledger is not None:
        ops = [
            wrap_for_verdict(op, seq=i, ledger=verdict_ledger)
            for i, op in enumerate(ops, start=1)
        ]
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
    result = get_executor(config.runtime).run(ops, samples)
    result.transform_stats = transform_stats
    result.transform_dropped = transform_dropped
    if verdict_ledger is not None:
        verdict_ledger.close(
            extra={
                "n_pre_stages": len(transform_stats),
                "n_transform_dropped": len(transform_dropped),
                "n_ops": len(config.operators),
                "n_in": len(samples),
                "n_kept": len(result.kept),
            }
        )
    for stat in result.stats:
        if stat.skipped:
            logger.info("算子 %s 跳过 %s 条模态不匹配样本（保留不评判）", stat.op, stat.skipped)
    for stat in transform_stats:
        if stat.changed or stat.dropped:
            logger.info(
                "前置阶段 %s: 改写 %s 条 / 丢弃 %s 条 / 透传 %s 条",
                stat.stage,
                stat.changed,
                stat.dropped,
                stat.skipped,
            )
    return result
