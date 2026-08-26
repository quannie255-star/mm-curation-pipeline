"""采样模块：清洗后按质量 + 类目分层配比出训练集配方（Week3 D5）。"""

from .sampler import (
    DEFAULT_QUALITY_BUCKETS,
    RandomSampler,
    Sampler,
    SamplingConfig,
    SamplingRecipe,
    StratifiedSampler,
)

__all__ = [
    "DEFAULT_QUALITY_BUCKETS",
    "RandomSampler",
    "Sampler",
    "SamplingConfig",
    "SamplingRecipe",
    "StratifiedSampler",
]
