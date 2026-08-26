"""程序化污染器：构造带 ground truth 的脏数据集（见 base.py 模块文档）。"""

from . import impl  # noqa: F401  # 导入以触发 @register_contaminator 注册
from .base import (
    ContaminationContext,
    ContaminationPlan,
    Contaminator,
    available_contaminators,
)

__all__ = [
    "ContaminationPlan",
    "Contaminator",
    "ContaminationContext",
    "available_contaminators",
]
