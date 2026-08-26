"""分层采样器：清洗后按质量分层 + 类目（tags）配比出训练集配方。

业务定位（ROADMAP Week3 D5）：清洗去掉了脏数据，但剩下的干净集在类目分布上
仍然不均衡（COCO-CN 里「街道/男人/食物」类目远多于「乐器/工艺品」）。
直接随机采样会让长尾类目在训练集里更稀缺；分层采样按 (质量分桶 × 类目)
交叉分层、配比抽取，保证每层都有代表性——这正是 JD 加分项里的「数据策略/配比」。

质量信号复用漏斗已写入 meta 的 score:clip_alignment（图文对齐分越高越高质量）；
类目取 sample.meta.tags[0]（首标签作为主类目，无标签归入「其他」）。
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..operators.base import Sample

# 默认质量分桶边界（clip_alignment 余弦相似度量级 ~0.2-0.55）。
# 三档：低 < 0.40，中 0.40-0.48，高 >= 0.48。可用 SamplingConfig 覆盖。
DEFAULT_QUALITY_BUCKETS: list[tuple[str, float, float]] = [
    ("low", 0.0, 0.40),
    ("mid", 0.40, 0.48),
    ("high", 0.48, 1.01),
]


@dataclass
class SamplingRecipe:
    """采样配方：哪些样本被选中 + 分层统计。"""

    name: str
    n_total: int
    n_sampled: int
    sampled_ids: list[str] = field(default_factory=list)
    strata_summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "n_total": self.n_total,
            "n_sampled": self.n_sampled,
            "strata_summary": dict(self.strata_summary),
        }


@dataclass
class SamplingConfig:
    """采样配置。

    quality_key: meta 里的质量分键名（默认 score:clip_alignment）。
    budget: 采样数量上限（None = 全量，仅用于分层统计不抽取）。
    seed: 随机种子，保证可复现。
    oversample_high: 高质量层是否过采样（stratified 模式下，high 层权重倍数）。
    """

    quality_key: str = "score:clip_alignment"
    budget: Optional[int] = None
    seed: int = 42
    oversample_high: float = 1.5
    buckets: list[tuple[str, float, float]] = field(
        default_factory=lambda: list(DEFAULT_QUALITY_BUCKETS)
    )


def _quality_bucket(score: Optional[float], buckets: list[tuple[str, float, float]]) -> str:
    """分数落到哪个桶。None（缺分）归入最低桶——质量信号缺失本身就是低质量信号。"""
    if score is None:
        return "low"
    for name, lo, hi in buckets:
        if lo <= score < hi:
            return name
    return buckets[-1][0]


def _category(sample: Sample) -> str:
    """主类目 = tags[0]，无标签归「其他」。"""
    tags = sample.meta.get("tags") or []
    return tags[0] if tags else "其他"


class Sampler(ABC):
    """采样器基类：输入全量样本，返回配方（选中 id + 分层统计）。"""

    name: str = "sampler"

    @abstractmethod
    def sample(self, samples: list[Sample], config: SamplingConfig) -> SamplingRecipe: ...

    @staticmethod
    def _strata_of(samples: list[Sample], config: SamplingConfig) -> dict[str, list[Sample]]:
        """按 (质量桶, 类目) 交叉分层。键格式 'quality/category'。"""
        strata: dict[str, list[Sample]] = defaultdict(list)
        for s in samples:
            q = _quality_bucket(s.meta.get(config.quality_key), config.buckets)
            cat = _category(s)
            strata[f"{q}/{cat}"].append(s)
        return strata


class RandomSampler(Sampler):
    """均匀随机采样（对照组）。"""

    name = "random"

    def sample(self, samples: list[Sample], config: SamplingConfig) -> SamplingRecipe:
        rng = random.Random(config.seed)
        pool = list(samples)
        rng.shuffle(pool)
        budget = config.budget if config.budget is not None else len(pool)
        picked = pool[:budget]
        strata = Sampler._strata_of(picked, config)
        return SamplingRecipe(
            name=self.name,
            n_total=len(samples),
            n_sampled=len(picked),
            sampled_ids=[s.id for s in picked],
            strata_summary={k: len(v) for k, v in sorted(strata.items())},
        )


class StratifiedSampler(Sampler):
    """(质量桶 × 类目) 交叉分层 + 配比抽取。

    配比策略：每层按其在全集的占比分配预算，但高质量层乘以 oversample_high
    倍权重（牺牲长尾低质层、保高质量层代表性）。层内随机抽取。
    过采样后总量可能超出预算，按权重归一化截断到 budget。
    """

    name = "stratified"

    def sample(self, samples: list[Sample], config: SamplingConfig) -> SamplingRecipe:
        rng = random.Random(config.seed)
        strata = Sampler._strata_of(samples, config)
        budget = config.budget if config.budget is not None else len(samples)

        # 每层权重：基础 1.0，high 桶乘 oversample_high
        weights: dict[str, float] = {}
        for key in strata:
            q = key.split("/", 1)[0]
            weights[key] = config.oversample_high if q == "high" else 1.0
        total_w = sum(len(strata[k]) * weights[k] for k in strata)

        # 最大余数法分配额度（保证 sum(quotas) == budget，不超层容量）：
        # 先按加权占比取整（floor），再把余数按小数部分大小逐个补给。
        raw: dict[str, float] = {}
        floors: dict[str, int] = {}
        for key, items in strata.items():
            frac = budget * len(items) * weights[key] / total_w
            raw[key] = frac
            floors[key] = min(int(frac), len(items))  # 不超层容量
        remaining = budget - sum(floors.values())
        # 按小数部分降序补给容量未满的层
        order = sorted(
            strata,
            key=lambda k: (raw[k] - int(raw[k]), len(strata[k])),
            reverse=True,
        )
        for key in order:
            if remaining <= 0:
                break
            if floors[key] < len(strata[key]):
                floors[key] += 1
                remaining -= 1
        quotas = floors

        picked: list[Sample] = []
        for key, items in strata.items():
            shuffled = list(items)
            rng.shuffle(shuffled)
            picked.extend(shuffled[: quotas[key]])

        # 若仍有缺口（某层容量不足），从剩余池随机补足
        if len(picked) < budget:
            picked_ids = {s.id for s in picked}
            rest = [s for s in samples if s.id not in picked_ids]
            rng.shuffle(rest)
            picked.extend(rest[: budget - len(picked)])

        picked_strata = Sampler._strata_of(picked, config)
        return SamplingRecipe(
            name=self.name,
            n_total=len(samples),
            n_sampled=len(picked),
            sampled_ids=[s.id for s in picked],
            strata_summary={k: len(v) for k, v in sorted(picked_strata.items())},
        )
