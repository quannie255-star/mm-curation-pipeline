"""清洗算子库。新算子模块在这里导入以触发注册（机制在 curation_eval.registry）。"""

from . import (  # noqa: F401
    clip_quality,
    dedup,
    detector_quality,
    fhir_quality,
    image_quality,
    industrial_quality,
    llm_judge,
    text_corpus,
    text_quality,
)
from .base import BatchOperator, Executor, FunnelResult, Operator, Sample, StageStat
from .registry import available_operators, build_operator, is_batch


def register_all() -> int:
    """显式触发算子注册导入，返回注册表中的算子数。

    注册机制是「import 即注册」，而本包的 import 已经把全部算子模块拉一遍——
    这个函数存在的意义是把意图显式化：从包侧起步的消费方（服务容器、外部集成）
    不需要知道主仓有哪些算子模块，`from mm_curation.operators import register_all;
    register_all()` 即可。幂等：重复调用返回同一计数。
    （Q6 处置：`curation_eval.available_operator_metas()` 在未导入时返回 `{}` 且
    无提示——外部评审实测确认，这是「注册表为空」最常见的踩法。）
    """
    return len(available_operators())

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
