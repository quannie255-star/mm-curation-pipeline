"""程序化污染器：向干净样本注入可控脏数据并保留 ground truth。

样本协议：{"id": str, "image_path": str, "caption": str, "labels": dict}。
注入样本写 labels["dirty"] = 污染类型名，并指向新渲染的图像文件
（不改原图；纯文本类污染不落新文件）。
"""

from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any, Optional

_REGISTRY: dict[str, type["Contaminator"]] = {}


def register(kind: str):
    def deco(cls):
        _REGISTRY[kind] = cls
        cls.kind = kind
        return cls

    return deco


def available_kinds() -> list[str]:
    return sorted(_REGISTRY)


class Contaminator:
    """单类脏数据注入器。apply 原地修改传入的样本副本并返回它。"""

    kind: str = "base"

    def apply(self, sample: dict, ctx: "Context") -> dict: ...


class Context:
    def __init__(self, pool: list[dict], images_out: Path, rng: random.Random):
        self.pool = pool  # 全量样本（错配污染需要供体）
        self.images_out = images_out
        self.rng = rng

    def font(self, size: int):
        from PIL import ImageFont

        for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
            for root in ("C:/Windows/Fonts", "/usr/share/fonts", "/System/Library/Fonts"):
                p = Path(root) / name
                if p.exists():
                    return ImageFont.truetype(str(p), size)
        return ImageFont.load_default(size=size)


def _save_jpeg(img, sample: dict, ctx: Context, quality: int = 88) -> None:
    safe = sample["id"].replace(":", "-").replace("/", "-")
    dest = ctx.images_out / f"{safe}.jpg"
    ctx.images_out.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=quality)
    sample["image_path"] = str(dest)


@register("watermark")
class Watermark(Contaminator):
    """半透明文字叠加。params: alpha（默认 0.45）、text。"""

    def __init__(self, alpha: float = 0.45, text: str = "example-sample.com"):
        self.alpha, self.text = alpha, text

    def apply(self, sample, ctx):
        from PIL import Image, ImageDraw

        img = Image.open(sample["image_path"]).convert("RGBA")
        w, h = img.size
        font = ctx.font(max(18, w // 24))
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        fill = (255, 255, 255, int(255 * self.alpha))
        step = max(60, w // 5)
        for y in range(-h // 2, h, step):
            for x in range(0, w, step * 2):
                draw.text((x, y), self.text, fill=fill, font=font)
        out = Image.alpha_composite(img, overlay).convert("RGB")
        _save_jpeg(out, sample, ctx)
        return sample


@register("blur")
class Blur(Contaminator):
    def apply(self, sample, ctx):
        from PIL import Image, ImageFilter

        img = Image.open(sample["image_path"]).convert("RGB")
        _save_jpeg(img.filter(ImageFilter.GaussianBlur(ctx.rng.uniform(3.5, 6.0))), sample, ctx)
        return sample


@register("low_resolution")
class LowResolution(Contaminator):
    def apply(self, sample, ctx):
        from PIL import Image

        img = Image.open(sample["image_path"]).convert("RGB")
        w, h = img.size
        small = ctx.rng.randint(40, 56)
        _save_jpeg(img.resize((small, max(1, round(h * small / w)))).resize((w, h)), sample, ctx)
        return sample


@register("exact_duplicate")
class ExactDuplicate(Contaminator):
    def apply(self, sample, ctx):
        return sample  # 图文原样引用，字节级去重的靶子


@register("truncate_text")
class TruncateText(Contaminator):
    def apply(self, sample, ctx):
        sample["caption"] = sample["caption"][: ctx.rng.randint(1, 3)]
        return sample


@register("mojibake")
class Mojibake(Contaminator):
    def apply(self, sample, ctx):
        garbled = sample["caption"].encode("gbk", errors="ignore").decode("utf-8", errors="ignore")
        sample["caption"] = garbled or "?" * 20
        return sample


@register("mismatched_pair")
class MismatchedPair(Contaminator):
    def apply(self, sample, ctx):
        donor = ctx.pool[ctx.rng.randrange(len(ctx.pool))]
        sample["caption"] = donor["caption"]
        return sample


class ContaminationPlan:
    """注入计划：inject_rate 相对干净集比例；kinds 为构成（自动归一化）。"""

    def __init__(
        self,
        inject_rate: float,
        seed: int,
        kinds: dict[str, float],
        params: Optional[dict[str, dict[str, Any]]] = None,
    ):
        unknown = set(kinds) - set(_REGISTRY)
        if unknown:
            raise ValueError(f"未注册的污染类型: {sorted(unknown)}，可用: {sorted(_REGISTRY)}")
        if not kinds:
            raise ValueError("kinds 为空")
        self.inject_rate, self.seed, self.kinds = inject_rate, seed, kinds
        self.params = params or {}

    def run(self, samples: list[dict], images_out: str | Path):
        if not samples:
            raise ValueError("干净样本集为空")
        rng = random.Random(self.seed)
        ctx = Context(samples, Path(images_out), rng)
        weighted = [(kind, w / sum(self.kinds.values())) for kind, w in self.kinds.items()]
        n_inject = round(len(samples) * self.inject_rate)
        injected, counts = [], {}
        for i in range(n_inject):
            r, acc = rng.random(), 0.0
            kind = weighted[-1][0]
            for k, w in weighted:
                acc += w
                if r <= acc:
                    kind = k
                    break
            src = samples[rng.randrange(len(samples))]
            dirty = copy.deepcopy(src)
            dirty["id"] = f"{src['id']}::{kind}{i}"
            dirty["labels"] = {"dirty": kind}
            _REGISTRY[kind](**self.params.get(kind, {})).apply(dirty, ctx)
            injected.append(dirty)
            counts[kind] = counts.get(kind, 0) + 1
        manifest = {
            "seed": self.seed,
            "n_clean": len(samples),
            "n_injected": n_inject,
            "counts": dict(sorted(counts.items())),
        }
        return samples + injected, manifest
