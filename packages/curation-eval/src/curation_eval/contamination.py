"""程序化污染器：向干净样本注入可控脏数据并保留 ground truth（V2：Sample 协议）。

样本协议：curation_eval.schema.Sample。注入样本写 labels["dirty"] = 污染类型名，
图像类污染渲染新图并改写 image_path（不改原图）；文本类污染只改 text。
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import Sample

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
    """单类脏数据注入器。apply 原地修改传入的样本副本并返回它。

    requires_image=True 的污染器只适用于带图样本（计划运行时自动从
    纯文本样本中筛选掉来源池）。
    """

    kind: str = "base"
    requires_image: bool = False

    def apply(self, sample: Sample, ctx: "Context") -> Sample: ...


class Context:
    def __init__(self, pool: list[Sample], images_out: Path, rng: random.Random):
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


def _save_jpeg(img, sample: Sample, ctx: Context, quality: int = 88) -> None:
    safe = sample.id.replace(":", "-").replace("/", "-")
    ctx.images_out.mkdir(parents=True, exist_ok=True)
    dest = ctx.images_out / f"{safe}.jpg"
    img.save(dest, "JPEG", quality=quality)
    sample.image_path = str(dest)


@register("watermark")
class Watermark(Contaminator):
    """半透明文字叠加。params: alpha（默认 0.45）、text。"""

    requires_image = True

    def __init__(self, alpha: float = 0.45, text: str = "example-sample.com"):
        self.alpha, self.text = alpha, text

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        from PIL import Image, ImageDraw

        img = Image.open(sample.image_path).convert("RGBA")
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
    requires_image = True

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        from PIL import Image, ImageFilter

        img = Image.open(sample.image_path).convert("RGB")
        _save_jpeg(img.filter(ImageFilter.GaussianBlur(ctx.rng.uniform(3.5, 6.0))), sample, ctx)
        return sample


@register("low_resolution")
class LowResolution(Contaminator):
    requires_image = True

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        from PIL import Image

        img = Image.open(sample.image_path).convert("RGB")
        w, h = img.size
        small = ctx.rng.randint(40, 56)
        _save_jpeg(
            img.resize((small, max(1, round(h * small / w)))).resize((w, h)),
            sample,
            ctx,
        )
        return sample


@register("exact_duplicate")
class ExactDuplicate(Contaminator):
    """原样引用（图与文都不变）——字节级与语义去重共同的靶子。"""

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        return sample


@register("truncate_text")
class TruncateText(Contaminator):
    def apply(self, sample: Sample, ctx: Context) -> Sample:
        sample.text = sample.text[: ctx.rng.randint(1, 3)]
        return sample


@register("mojibake")
class Mojibake(Contaminator):
    def apply(self, sample: Sample, ctx: Context) -> Sample:
        garbled = sample.text.encode("gbk", errors="ignore").decode("utf-8", errors="ignore")
        sample.text = garbled or "?" * 20
        return sample


@register("mismatched_pair")
class MismatchedPair(Contaminator):
    def apply(self, sample: Sample, ctx: Context) -> Sample:
        donor = ctx.pool[ctx.rng.randrange(len(ctx.pool))]
        sample.text = donor.text
        return sample


# ------- 文本语料污染器（V2 β：text_article 模态，全部 requires_image=False） -------

_BOILERPLATE_POOL = [
    "扫码关注公众号，回复关键词领取福利",
    "本文来自某某代理投稿，转载请注明出处",
    "点击链接 www.example-promo.cn 立即抢购",
    "免责声明：本站所有资源均收集于网络",
    "阅读原文，下载 APP 查看更多精彩内容",
]

_PII_POOL_TEMPLATES = [
    "联系人：138{d}，电话随时接通",
    "邮箱：user{d}@example-promo.cn",
    "证件号：1101011990{d}（示例）",
]


def _segments(text: str) -> list[str]:
    """按行/句切出可复制的正文片段。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 2:
        return lines
    return [seg + "。" for seg in text.split("。") if seg.strip()]


@register("paragraph_repeat")
class ParagraphRepeat(Contaminator):
    """随机正文段落整行复制 2-4 遍——行级重复率算子的靶子。
    必须按行复制（[seg]*n 后换行 join）而非行内拼接：line_repetition
    数的是重复行，行内重复它看不见（β 集成验收踩过的坑）。"""

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        segs = _segments(sample.text)
        if not segs:
            return sample
        pos = ctx.rng.randrange(len(segs))
        segs.insert(pos, "\n".join([segs[pos]] * ctx.rng.randint(2, 4)))
        sample.text = "\n".join(segs)
        return sample


@register("boilerplate_inject")
class BoilerplateInject(Contaminator):
    """注入广告/版权/导航模板句——boilerplate 算子的靶子。"""

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        lines = [
            _BOILERPLATE_POOL[ctx.rng.randrange(len(_BOILERPLATE_POOL))]
            for _ in range(ctx.rng.randint(1, 2))
        ]
        sample.text = "\n".join(lines) + "\n" + sample.text
        return sample


@register("pii_inject")
class PiiInject(Contaminator):
    """注入合成手机号/邮箱/证件号（无真实 PII）——pii_detect 算子的靶子。"""

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        tpl = _PII_POOL_TEMPLATES[ctx.rng.randrange(len(_PII_POOL_TEMPLATES))]
        payload = tpl.format(d="".join(str(ctx.rng.randint(0, 9)) for _ in range(8)))
        pos = ctx.rng.randint(0, len(sample.text))
        sample.text = sample.text[:pos] + " " + payload + " " + sample.text[pos:]
        return sample


@register("whitespace_pad")
class WhitespacePad(Contaminator):
    """正文片段被空白块替换（页面抽取坏损的典型形态）——doc_length 靶子。
    两个实现约束（β 集成验收踩过的坑）：必须替换而非插入（内部插空白
    经去空白后有效长度不减）；替换必须内部化——尾部空白会被下游算子
    的 strip() 无声剥离，污染等于没打。"""

    def apply(self, sample: Sample, ctx: Context) -> Sample:
        n = len(sample.text)
        if n < 3:
            return sample
        cut = ctx.rng.randint(max(1, n // 3), max(2, n // 2))
        pos = ctx.rng.randint(0, max(0, n - cut - 1))  # pad 后至少留 1 字符
        sample.text = sample.text[:pos] + "\n" * 4 + "\u3000" * 12 + sample.text[pos + cut :]
        return sample


@dataclass
class ContaminationPlan:
    """注入计划：inject_rate 相对干净集比例；kinds 为构成（自动归一化）。"""

    inject_rate: float
    seed: int
    kinds: dict[str, float]
    params: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.kinds) - set(_REGISTRY)
        if unknown:
            raise ValueError(f"未注册的污染类型: {sorted(unknown)}，可用: {sorted(_REGISTRY)}")
        if not self.kinds:
            raise ValueError("kinds 为空")

    def run(self, samples: list[Sample], images_out: str | Path):
        if not samples:
            raise ValueError("干净样本集为空")
        image_kinds = [k for k in self.kinds if _REGISTRY[k].requires_image]
        if image_kinds and not any(s.image_path is not None for s in samples):
            raise ValueError(f"污染类型 {image_kinds} 需要带图样本，但语料无图")
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
            impl = _REGISTRY[kind](**self.params.get(kind, {}))
            eligible = [s for s in samples if not (impl.requires_image and s.image_path is None)]
            src = eligible[rng.randrange(len(eligible))]
            dirty = copy.deepcopy(src)
            dirty.id = f"{src.id}::{kind}{i}"
            dirty.labels = {"dirty": kind}
            impl.apply(dirty, ctx)
            injected.append(dirty)
            counts[kind] = counts.get(kind, 0) + 1
        manifest = {
            "seed": self.seed,
            "n_clean": len(samples),
            "n_injected": n_inject,
            "counts": dict(sorted(counts.items())),
        }
        return samples + injected, manifest
