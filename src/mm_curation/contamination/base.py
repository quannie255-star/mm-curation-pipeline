"""污染器框架：向干净种子集注入可控脏数据并保留 ground truth。

为什么需要污染器（ROADMAP「数据策略」）：真实脏数据没有 ground truth，
无法量化清洗好坏；构造带标注的脏数据后，每个算子可算 precision/recall，
整条管道可算「脏数据召回率 vs 好数据误杀率」。

设计约定（与 operators/ 对称，形成"配置驱动"的统一风格）：
- 污染器不修改原始文件：动图的污染器把新图写到独立目录，jsonl 里指向新路径
- 注入即复制：脏样本是新增条目（id 加后缀），原始样本保持干净，
  ground truth 因此天然清晰（labels.dirty = 污染类型）
- 去重类污染遵循「先到先保留」约定：原始样本在前、注入样本在后，
  去重算子按出现顺序保留首个即可拿满召回
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..operators.base import Sample

ContaminatorRegistry = dict[type, str]


class ContaminationContext:
    """一次污染计划运行期的共享环境：随机源、字体、输出目录、全量样本池。"""

    def __init__(self, samples: list[Sample], images_out: Path, rng: random.Random):
        self.samples = samples
        self.images_out = images_out
        self.rng = rng
        self._font = None

    def load_font(self, size: int):
        """中文字体（水印/占位图需要）；Windows 常见字体回退到 PIL 默认。"""
        from PIL import ImageFont

        for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"):
            for root in ("C:/Windows/Fonts", "/usr/share/fonts", "/System/Library/Fonts"):
                p = Path(root) / name
                if p.exists():
                    return ImageFont.truetype(str(p), size)
        return ImageFont.load_default(size=size)


class Contaminator(ABC):
    """单类脏数据注入器。返回新 Sample（含 labels.dirty），不修改入参。"""

    kind: str = "base"

    @abstractmethod
    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample: ...


_REGISTRY: dict[str, type[Contaminator]] = {}


def register_contaminator(kind: str):
    def deco(cls):
        _REGISTRY[kind] = cls
        cls.kind = kind
        return cls

    return deco


def available_contaminators() -> list[str]:
    return sorted(_REGISTRY)


def build_contaminators(kinds: dict[str, float]) -> list[tuple[Contaminator, float]]:
    unknown = set(kinds) - set(_REGISTRY)
    if unknown:
        raise ValueError(f"未注册的污染类型: {sorted(unknown)}，可用: {sorted(_REGISTRY)}")
    total = sum(kinds.values())
    if total <= 0:
        raise ValueError("污染比例之和必须为正")
    return [(_REGISTRY[k](), w / total) for k, w in kinds.items()]


@dataclass
class ContaminationPlan:
    """注入计划：inject_rate 相对干净集的注入比例；kinds 为各类型构成（归一化）。"""

    inject_rate: float = 0.30
    seed: int = 42
    kinds: dict[str, float] = field(default_factory=dict)

    def run(self, samples: list[Sample], images_out: Path) -> tuple[list[Sample], dict]:
        """返回 ( originals + injected , manifest )。"""
        import copy

        if not samples:
            raise ValueError("干净样本集为空")
        if not self.kinds:
            raise ValueError("kinds 为空")
        images_out.mkdir(parents=True, exist_ok=True)
        rng = random.Random(self.seed)
        ctx = ContaminationContext(samples, images_out, rng)
        weighted = build_contaminators(self.kinds)

        n_inject = round(len(samples) * self.inject_rate)
        counts: dict[str, int] = {}
        injected: list[Sample] = []
        for i in range(n_inject):
            r, acc = rng.random(), 0.0
            chosen = weighted[-1][0]
            for contaminator, w in weighted:
                acc += w
                if r <= acc:
                    chosen = contaminator
                    break
            source = ctx.samples[rng.randrange(len(samples))]
            dirty = copy.deepcopy(source)
            dirty.labels = {"dirty": chosen.kind}
            dirty.id = f"{source.id}::{chosen.kind}{i}"
            result = chosen.apply(dirty, i, ctx)
            injected.append(result)
            counts[chosen.kind] = counts.get(chosen.kind, 0) + 1

        manifest = {
            "seed": self.seed,
            "inject_rate": self.inject_rate,
            "n_clean": len(samples),
            "n_injected": len(injected),
            "counts": dict(sorted(counts.items())),
            "kinds": self.kinds,
        }
        return samples + injected, manifest
