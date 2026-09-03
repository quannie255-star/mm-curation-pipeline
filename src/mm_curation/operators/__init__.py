"""清洗算子库。新算子模块在这里导入以触发注册（机制在 curation_eval.registry）。"""

from . import (  # noqa: F401
    clip_quality,
    dedup,
    detector_quality,
    image_quality,
    llm_judge,
    text_corpus,
    text_quality,
)
from .base import BatchOperator, Executor, FunnelResult, Operator, Sample, StageStat
from .registry import available_operators, build_operator, is_batch

__all__ = [
    "BatchOperator",
    "Executor",
    "FunnelResult",
    "Operator",
    "Sample",
    "StageStat",
    "available_operators",
    "build_operator",
    "is_batch",
]
