"""curation-eval：多模态数据清洗的 ground-truth 评测框架（V2 α：协议收口）。

协议与 SDK 单一来源：Sample schema、算子注册表（元数据）、算子基类、
执行器协议 + 污染器 + 指标。不提供具体清洗算子——评测算子。
"""

from .contamination import ContaminationPlan, Contaminator, available_kinds, register
from .metrics import mrr, pr_from_drops, recall_at_k
from .ray_executor import RayDistributedExecutor
from .registry import (
    CostClass,
    OperatorMeta,
    available_operator_metas,
    get_operator_meta,
    register_operator,
    unregister,
)
from .schema import MODALITY_FIELDS, Sample
from .sdk import (
    BatchOperator,
    Executor,
    FunnelResult,
    LocalSequentialExecutor,
    Operator,
    StageStat,
)

__all__ = [
    # 协议核心（V2 α）
    "MODALITY_FIELDS",
    "Sample",
    "CostClass",
    "OperatorMeta",
    "register_operator",
    "get_operator_meta",
    "available_operator_metas",
    "unregister",
    "Operator",
    "BatchOperator",
    "Executor",
    "LocalSequentialExecutor",
    "RayDistributedExecutor",
    "StageStat",
    "FunnelResult",
    # 污染器与指标
    "ContaminationPlan",
    "Contaminator",
    "available_kinds",
    "register",
    "pr_from_drops",
    "recall_at_k",
    "mrr",
]
__version__ = "0.2.0"
