"""9+1 类脏数据注入器实现。

每类脏数据对应一个清洗算子的「靶子」（见 ROADMAP 污染策略表）：
- 去重组：exact_duplicate / near_duplicate_image / near_duplicate_text / semantic_duplicate
- 图像质量组：low_resolution / blur
- 图文相关组：mismatched_pair（CLIP 对齐分的靶子）
- 文本质量组：low_quality_text
- 安全组：watermark / nsfw_placeholder（合规占位，不引入真实违规内容）
"""

from __future__ import annotations

from ..operators.base import Sample
from .base import ContaminationContext, Contaminator, register_contaminator

_NOISE_CHARS = "的地得和与及或在是被有了一个"


def _safe_name(sample_id: str) -> str:
    """Windows 文件名不允许 ':'，注入 id 里的 '::' 需清洗。"""
    return sample_id.replace("::", "--")


def _load(sample: Sample):
    from PIL import Image

    return Image.open(sample.image_path).convert("RGB")


def _save_jpeg(img, sample: Sample, ctx: ContaminationContext, quality: int = 88) -> None:
    ctx.images_out.mkdir(parents=True, exist_ok=True)
    dest = ctx.images_out / f"{_safe_name(sample.id)}.jpg"
    img.save(dest, "JPEG", quality=quality)
    sample.image_path = str(dest)


# ---------------- 去重组 ----------------


@register_contaminator("exact_duplicate")
class ExactDuplicate(Contaminator):
    """逐字节复制（图与 caption 均不变）——md5 去重的靶子。"""

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        return source  # image_path/caption 原样引用


@register_contaminator("near_duplicate_image")
class NearDuplicateImage(Contaminator):
    """轻度裁剪(4~10%) + 低质量重编码——字节级不同、视觉相同，pHash 的靶子。

    裁剪强度经真实数据校准（ROADMAP 2026-08-20）：每边 >10% 的裁剪会把
    pHash 海明距离推出安全阈值区（<12），落到与自然相似照片不可区分的区间。
    """

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        img = _load(source)
        w, h = img.size
        fx = ctx.rng.uniform(0.90, 0.96)
        fy = ctx.rng.uniform(0.90, 0.96)
        x0 = int(w * (1 - fx) * ctx.rng.random())
        y0 = int(h * (1 - fy) * ctx.rng.random())
        img = img.crop((x0, y0, x0 + int(w * fx), y0 + int(h * fy)))
        _save_jpeg(img, source, ctx, quality=ctx.rng.randint(35, 50))
        return source


@register_contaminator("near_duplicate_text")
class NearDuplicateText(Contaminator):
    """caption 局部扰动（删 5% 字符 + 重复一个 8-gram）——MinHash-LSH 的靶子。

    扰动强度经真实数据校准：目标是与原文 3-gram Jaccard 落在 0.75 附近，
    在 LSH 阈值 0.7 下可稳定召回，且与"模板句自然相似"（J~0.5-0.6）拉开
    距离——扰动再轻抓不到，再重就变成 low_quality_text 了。
    """

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        text = list(source.text)
        # 删字仅对长文本生效：短 caption（<20 字）删 1 个字就毁掉 ~30% 的
        # 3-gram，会把 J 压到 LSH 阈值以下（真实数据校准结论）
        if len(text) >= 20:
            for i in ctx.rng.sample(range(len(text)), k=max(1, int(len(text) * 0.03))):
                text[i] = ""
        kept = "".join(text)
        if len(kept) >= 8:
            start = ctx.rng.randrange(len(kept) - 7)
            gram = kept[start : start + 8]
            kept = kept[:start] + gram * 2 + kept[start:]
        source.text = kept
        return source


@register_contaminator("semantic_duplicate")
class SemanticDuplicate(Contaminator):
    """轻微裁剪 + caption 同义改写（tags 重组）——哈希类全部失效，embedding 的靶子。"""

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        img = _load(source)
        w, h = img.size
        m = int(min(w, h) * 0.04)
        img = img.crop((m, m, w - m, h - m))
        _save_jpeg(img, source, ctx)
        tags = source.meta.get("tags") or []
        if tags:
            source.text = f"一张包含{'、'.join(tags[:6])}等内容的照片"
        else:
            source.text = "这张图片展示了" + "，".join(
                source.text[i : i + 4] for i in range(0, len(source.text), 4)
            )
        return source


# ---------------- 图像质量组 ----------------


@register_contaminator("low_resolution")
class LowResolution(Contaminator):
    """下采样到 ~48px 再放大回原尺寸（真实世界里的低分辨率扩图）。"""

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        img = _load(source)
        w, h = img.size
        small = ctx.rng.randint(40, 56)
        img = img.resize((small, max(1, round(h * small / w)))).resize((w, h))
        _save_jpeg(img, source, ctx)
        return source


@register_contaminator("blur")
class Blur(Contaminator):
    """高斯模糊（模拟失焦/运动模糊的低质图）。"""

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        from PIL import ImageFilter

        img = _load(source).filter(ImageFilter.GaussianBlur(ctx.rng.uniform(3.5, 6.0)))
        _save_jpeg(img, source, ctx)
        return source


# ---------------- 图文相关组 ----------------


@register_contaminator("mismatched_pair")
class MismatchedPair(Contaminator):
    """caption 来自另一张随机图——图文错配，Chinese-CLIP 对齐分的靶子。"""

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        donor = ctx.samples[ctx.rng.randrange(len(ctx.samples))]
        source.text = donor.text
        source.meta["mismatch_donor"] = donor.id
        return source


# ---------------- 文本质量组 ----------------


@register_contaminator("low_quality_text")
class LowQualityText(Contaminator):
    """四种低质文本变体：截断 / 刷字 / 乱码 / 噪声字。"""

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        text = source.text
        variant = ctx.rng.choice(["truncate", "repeat", "mojibake", "noise"])
        if variant == "truncate":
            source.text = text[:3]
        elif variant == "repeat":
            source.text = text[:8] + "哈" * 30
        elif variant == "mojibake":
            garbled = text.encode("gbk", errors="ignore").decode("utf-8", errors="ignore")
            source.text = garbled or "?" * 20
        else:
            source.text = "".join(ctx.rng.choice(_NOISE_CHARS) for _ in range(max(8, len(text))))
        source.meta["lqt_variant"] = variant
        return source


# ---------------- 安全组（合规占位，无真实违规内容） ----------------


@register_contaminator("watermark")
class Watermark(Contaminator):
    """程序化半透明斜向水印（真实业务中最常见的版权/广告污染）。

    渲染委托 detector/synth.py（与检测器训练数据共用一份实现，防双实现漂移）；
    此处固定为旧版参数：斜向平铺 + 素材站文案 + 0.45 叠加强度。
    """

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        from ..detector.synth import LEGACY_WATERMARK_TEXT, render_watermark

        img = _load(source)
        params = {
            "layout": "tiled",
            "font": "msyh.ttc",
            "font_family": ("msyh.ttc", "simhei.ttf"),
            "alpha": 0.45,
            "text": LEGACY_WATERMARK_TEXT,
            "size": max(18, img.width // 24),
        }
        _save_jpeg(render_watermark(img, params), source, ctx)
        return source


@register_contaminator("nsfw_placeholder")
class NsfwPlaceholder(Contaminator):
    """违规内容占位：以生成的「广告图」替换原图并标注。

    合规考量：不引入真实 NSFW 内容；检测框架与评测逻辑不变，
    真实部署时替换为线上检测器即可（ROADMAP 风险表 #3）。
    渲染委托 detector/synth.py 的 blocks 版式（与旧实现一致）。
    """

    def apply(self, source: Sample, index: int, ctx: ContaminationContext) -> Sample:
        from ..detector.synth import render_ad

        img = render_ad((512, 512), style="blocks", rng=ctx.rng, text="广告示例图", text_size=64)
        _save_jpeg(img, source, ctx)
        source.text = "点击领取优惠券，限时特价，马上抢购！"
        return source
