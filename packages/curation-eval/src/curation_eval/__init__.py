"""curation-eval：多模态数据清洗的 ground-truth 评测框架（v0.1）。

程序化污染 + 丢弃语义 P/R + 检索指标。不提供清洗算子——评测算子。
"""

from .contamination import (
    ContaminationPlan,
    Contaminator,
    available_kinds,
    register,
)
from .metrics import mrr, pr_from_drops, recall_at_k

__all__ = [
    "ContaminationPlan",
    "Contaminator",
    "available_kinds",
    "register",
    "pr_from_drops",
    "recall_at_k",
    "mrr",
]
__version__ = "0.1.0"
