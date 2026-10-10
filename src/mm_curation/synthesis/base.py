"""合成器框架：从真实样本派生增强样本，并保留可归因的来源链。

## 为什么需要它（ROADMAP 第三节第 2 项）
10 个在招岗位里 5 个要求 Data Synthesis / Augmentation，而本项目此前只有
**反向**能力——`contamination/` 往干净集里注入脏数据。
合成是它的**镜像**：从真实样本出发，产出更多**仍然干净但分布不同**的样本。

## 与 contamination 的对称关系（这是刻意的设计，不是巧合）

- **输入**：污染是干净种子集，合成是真实样本集
- **输出**：污染得「种子 + N 条脏样本」，合成得「原样本 + M 条增强样本」
- **ground truth**：污染是 `labels.dirty = 污染类型`；
  合成是 `labels.synthesized_by` + `labels.source_id` + `labels.clean`
- **1→N 发生在**：两边的 `Plan.run`（都是**计划级**，不在协议层）
- **不修改入参**：脏样本 / 增强样本都写到独立目录，jsonl 里指向新路径

**为什么落在「计划级」而不是协议层的 `Transformer`**
协议层 `Transformer.transform(sample) -> TransformResult` 里
`TransformResult.sample` 是**单个** `Sample | None`，形状是 1→1；
而下游 `run_pre_stages` 的 `StageTransformStat.n_out` 按样本数计数——
把它改成 1→N 会同时动包侧协议与所有既有算子的回归。
而污染器那边**早就是计划级 1→N**（一次 `run` 产 N 条），
所以合成沿用同一形状：**不用改协议，就能拿到 1→N**。
协议层要不要变成 1→N 是独立议题，不该被合成这个需求裹挟。

## 红线：合成样本必须走同一条漏斗
否则「我合成了一批数据」不可证伪——分不清检出率变化是因为合成样本更干净，
还是因为它绕过了清洗链路。本模块只负责**产出**，
检出率必须由既有的 `operators/` 漏斗在同一口径下测出来。
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..operators.base import Sample

SynthesizerRegistry = dict[str, type["Synthesizer"]]


class SynthesisContext:
    """一次合成计划运行期的共享环境：随机源、输出目录、原始样本池。

    与 `ContaminationContext` 字段同名同义，方便两边对照阅读。
    """

    def __init__(self, samples: list[Sample], images_out: Path, rng: random.Random):
        self.samples = samples
        self.images_out = images_out
        self.rng = rng

    def load_font(self, size: int):
        """中文字体（画布类增强手段需要）；Windows 常见字体回退到 PIL 默认。"""
        from PIL import ImageFont

        for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"):
            for root in ("C:/Windows/Fonts", "/usr/share/fonts", "/System/Library/Fonts"):
                p = Path(root) / name
                if p.exists():
                    return ImageFont.truetype(str(p), size)
        return ImageFont.load_default(size=size)


class Synthesizer(ABC):
    """单类增强器。返回新 Sample（含 labels.synthesized_by），不修改入参。

    契约（与 `Contaminator` 对称）：
    - 返回**新对象**，入参不许被改（调用方依赖 `deepcopy` 后再传入）；
    - 增强后样本仍然是**干净**的——`labels.dirty` 必须为空。
      这条是合成与污染的分界线，也是「合成样本走同一条漏斗」的前提。
    """

    kind: str = "base"
    #: 该手段是否需要写图片文件（纯文本增强为 False，省一次 IO）
    needs_image: bool = False

    @abstractmethod
    def apply(self, source: Sample, index: int, ctx: SynthesisContext) -> Sample: ...


_REGISTRY: dict[str, type[Synthesizer]] = {}


def register_synthesizer(kind: str, *, needs_image: bool = False):
    def deco(cls):
        _REGISTRY[kind] = cls
        cls.kind = kind
        cls.needs_image = needs_image
        return cls

    return deco


def available_synthesizers() -> list[str]:
    return sorted(_REGISTRY)


def build_synthesizers(kinds: dict[str, float]) -> list[tuple[Synthesizer, float]]:
    """按构成比构建增强器列表（权重归一化）。

    与 `build_contaminators` 同款逻辑与同款报错策略——
    未注册的 kind 必须**报错**而不是静默跳过：
    静默跳过会让「我配了 5 种增强」实际只跑 3 种，而 manifest 里看不出来。
    """
    unknown = set(kinds) - set(_REGISTRY)
    if unknown:
        raise ValueError(f"未注册的增强类型: {sorted(unknown)}，可用: {sorted(_REGISTRY)}")
    total = sum(kinds.values())
    if total <= 0:
        raise ValueError("增强比例之和必须为正")
    return [(_REGISTRY[k](), w / total) for k, w in kinds.items()]


@dataclass
class SynthesisPlan:
    """增强计划。

    - `augment_per_sample`：每条被选中的样本派生出几条增强样本（1→N 的 N）。
    - `coverage`：有多少比例的原始样本参与派生（0~1）。
      留出没被派生的样本很重要——否则「增强后训练集」与「原训练集」
      就无法做同口径对照。
    - `kinds`：各增强手段的构成比（归一化）。
    """

    augment_per_sample: int = 2
    coverage: float = 0.5
    seed: int = 42
    kinds: dict[str, float] = field(default_factory=dict)

    def run(self, samples: list[Sample], images_out: Path) -> tuple[list[Sample], dict]:
        """返回 ( originals + synthesized , manifest )。

        顺序约定与污染器一致：**原始样本在前、增强样本在后**。
        去重类算子按出现顺序保留首个，两边行为才对齐。
        """
        import copy

        if not samples:
            raise ValueError("样本集为空")
        if not self.kinds:
            raise ValueError("kinds 为空")
        if self.augment_per_sample < 1:
            raise ValueError("augment_per_sample 必须 >= 1")
        if not 0 < self.coverage <= 1:
            raise ValueError("coverage 必须在 (0, 1] 区间")

        images_out.mkdir(parents=True, exist_ok=True)
        rng = random.Random(self.seed)
        ctx = SynthesisContext(samples, images_out, rng)
        weighted = build_synthesizers(self.kinds)

        # 参与派生的样本下标：固定 seed 决定，manifest 里会登记
        pool = list(range(len(samples)))
        rng.shuffle(pool)
        n_cover = round(len(samples) * self.coverage)
        chosen = sorted(pool[:n_cover])

        counts: dict[str, int] = {}
        synthesized: list[Sample] = []
        n_attempt = 0
        n_noop = 0
        for idx in chosen:
            source = samples[idx]
            for k in range(self.augment_per_sample):
                r, acc = rng.random(), 0.0
                synth = weighted[-1][0]
                for candidate, w in weighted:
                    acc += w
                    if r <= acc:
                        synth = candidate
                        break
                new = copy.deepcopy(source)
                # ★ 溯源三元组：合成样本的**可归因性**全靠这三个字段。
                #   缺了它们，合成集与真实集混在一起之后就分不清哪条是哪条，
                #   而「合成样本走同一条漏斗」这句话也就无从验证。
                new.labels = {
                    "synthesized_by": synth.kind,
                    "source_id": source.id,
                    "clean": True,
                }
                # ★ id 必须全局唯一，且**不能**只用 (source_id, kind, k)：
                #   同一条源样本抽到同一种手段两次时会撞名
                #   （2026-10-05 实跑抓到：s3::text_paraphrase0 出现两次）。
                #   撞名的后果不是「不好看」，而是去重算子会把第二条当成
                #   重复项删掉——于是增强样本被静默吃掉，manifest 里的
                #   n_synthesized 与实际留存量对不上，而没有任何报错。
                #   用全局序号做后缀，从构造上排除碰撞。
                new.id = f"{source.id}::{synth.kind}#{len(synthesized)}"
                result = synth.apply(new, k, ctx)
                # 契约兜底：增强结果绝不能带dirty 标记。
                # 这条断言是「合成 = 更干净而非更脏」的最后一道闸门，
                # 而它必须在**运行时**检查，不能只写在 docstring 里。
                assert not result.labels.get("dirty"), (
                    f"合成器 {synth.kind} 给样本打了 dirty 标记："
                    "增强样本必须保持干净，否则检出率口径不可比"
                )
                n_attempt += 1
                # ★ 丢弃「什么都没改」的样本（2026-10-05 实跑抓到）。
                #   合成器对已合规的样本会**正确地什么都不做**
                #   （例：文本已有句号，truncate_repair 无需补）。
                #   但如果照样产出，那是一条与原文逐字相同的样本——
                #   它会让「我合成了 N 条」这个数字**虚增一倍**，
                #   而 manifest 与实际留存量对不上，没有任何报错。
                #   这与「去重算子会静默吃掉重复项」是同一类危害：
                #   **数量被悄悄改了，报表还好看。**
                #
                #   判据用 `meta.augment.changed`——由各增强器自己如实申报，
                #   而不由 Plan 拿原文做 diff（那样会漏掉「改了又改回来」的情况）。
                if not result.meta.get("augment", {}).get("changed", False):
                    n_noop += 1
                    continue
                synthesized.append(result)
                counts[synth.kind] = counts.get(synth.kind, 0) + 1

        manifest = {
            "seed": self.seed,
            "augment_per_sample": self.augment_per_sample,
            "coverage": self.coverage,
            "n_source": len(samples),
            "n_covered": len(chosen),
            "n_attempt": n_attempt,
            "n_synthesized": len(synthesized),
            # 「尝试了但没改动」的条数必须**显式登记**，不能让人从
            # n_attempt 与 n_synthesized 的差自己猜——差值就是 noop，
            # 但把它写进 manifest 才算「如实」。
            "n_noop": n_noop,
            "counts": {k: v for k, v in sorted(counts.items()) if v > 0},
            "kinds": self.kinds,
        }
        return samples + synthesized, manifest
