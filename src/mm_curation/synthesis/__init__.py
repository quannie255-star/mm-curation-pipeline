"""数据合成与增强：从真实样本派生 1→N 条增强样本。

对外只暴露三个入口（与 `contamination/__init__.py` 对称）::

    from mm_curation.synthesis import SynthesisPlan, available_synthesizers

    plan = SynthesisPlan(augment_per_sample=2, coverage=0.5,
                         kinds={"text_typo_fix": 1.0})
    samples, manifest = plan.run(clean_samples, images_out=out_dir)

**红线**：合成样本必须走同一条清洗漏斗（`operators/`），否则
「我合成了一批数据」不可证伪——分不清检出率变化是因为合成样本更干净，
还是因为它绕过了链路。本模块只负责产出，不负责验收。
"""

from . import impl  # noqa: F401  # 导入以触发 @register_synthesizer 注册
from .base import (
    SynthesisContext,
    SynthesisPlan,
    Synthesizer,
    available_synthesizers,
)

__all__ = [
    "SynthesisPlan",
    "Synthesizer",
    "SynthesisContext",
    "available_synthesizers",
]
