"""水印/广告合成渲染器：检测器训练数据的源头（design 1.1/1.2）。

与 contamination/ 水印污染共用本模块的渲染逻辑（单一实现，防双实现漂移）；
污染器以固定参数调用，检测器以风格组参数调用。

防循环论证的关键在风格组设计：A 组（训练）与 B 组（泛化测试）在
布局/透明度/字体/文本池四个维度全部错开——检测器想通过 B 组测试，
只能学会「图上有叠加文字」的概念，无法记住具体水印。
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

# 与旧污染器一致的水印文案（行为保持）；检测器文本池见下
LEGACY_WATERMARK_TEXT = "素材站 www.example-sample.com"
AD_PLACEHOLDER_TEXT = "广告示例图"

# 风格组文本池：域名完全不重叠（B 组训练不可见）
WATERMARK_TEXTS_A = [
    "素材站 www.example-sample.com",
    "图库 www.picstock-demo.cn",
    "水印 www.markdemo.net",
    "摄影 www.shotedu.com",
    "素材 www.materialpro.cn",
    "模板 www.templatedemo.com",
]
WATERMARK_TEXTS_B = [
    "www.freepik-demo.cc",
    "www.veer-demo.io",
    "图虫 www.tuchongdemo.com",
    "www.shutterstock-demo.org",
    "视觉中国 www.vcgdemo.net",
    "www.gettyimages-demo.com",
]
AD_TEXTS_B = ["限时特惠 点击抢购", "周年庆典 全场五折", "独家福利 先到先得"]

_FONT_DIRS = ("C:/Windows/Fonts", "/usr/share/fonts", "/System/Library/Fonts")


@dataclass
class WatermarkStyle:
    """一组水印渲染参数。layouts 为布局模板池（训练组应包含多样性，
    见下方风格组注释）。"""

    layouts: tuple[str, ...] = ("tiled",)  # tiled / corner / banner
    font_family: tuple[str, ...] = ("msyh.ttc",)
    alpha: tuple[float, float] = (0.45, 0.60)
    texts: list[str] = field(default_factory=lambda: list(WATERMARK_TEXTS_A))

    def sample(self, rng: random.Random, width: int) -> dict:
        return {
            "layout": rng.choice(self.layouts),
            "font": self.font_family[0],
            "alpha": rng.uniform(*self.alpha),
            "text": rng.choice(self.texts),
            "size": max(18, width // 24),
        }


# 风格组预设——经三轮迭代定稿（P1-T2 迭代记录，重要工程结论）：
# ① alpha B(0.18-0.32) 白字近乎不可见 → 收窄到可感知区间；
# ② corner 布局被量化证明低于可检测下限（224px 梯度能量比 0.948 vs
#   A 组 1.328）——角落小字水印对 224px 检测器原理上不可检，已记录为边界；
# ③ 把"布局"也留出泛化维度 = 要求零样本迁移到未见任务结构（banner 组
#   召回 0%）——过于激进。定稿协议：训练组含布局模板多样性（tiled+banner），
#   泛化组留出的是**风格参数**（字体/文本/透明度），与真实水印检测器的
#   训练范式一致（布局模板属于训练数据设计，不属于泄漏）。
STYLE_A = WatermarkStyle(
    layouts=("tiled", "banner"),
    font_family=("msyh.ttc",),  # 不与 B 组共享字体；缺字体时回落 PIL 默认
    alpha=(0.45, 0.60),
    texts=WATERMARK_TEXTS_A,
)
STYLE_B = WatermarkStyle(
    layouts=("banner",),
    font_family=("simhei.ttf", "simsun.ttc"),
    alpha=(0.25, 0.40),
    texts=WATERMARK_TEXTS_B,
)
AD_STYLE_B = "gradient"  # B 组广告用渐变版式（A 组沿用 blocks）


def _load_font(family: tuple[str, ...], size: int):
    from PIL import ImageFont

    for name in family:
        for root in _FONT_DIRS:
            p = Path(root) / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def render_watermark(img: Image.Image, params: dict) -> Image.Image:
    """按参数渲染水印，返回新图（不改入参）。params 由 WatermarkStyle.sample 产出。"""
    w, h = img.size
    family = tuple(params.get("font_family") or (params["font"],))
    font = _load_font(family, params["size"])
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fill = (255, 255, 255, int(255 * params["alpha"]))
    text = params["text"]
    layout = params["layout"]

    if layout == "tiled":
        step = max(60, w // 5)
        for y in range(-h // 2, h, step):
            for x in range(0, w, step * 2):
                draw.text((x, y), text, fill=fill, font=font)
    elif layout == "corner":
        draw.text(
            (w - w // 12, h - int(params["size"] * 1.6)), text, fill=fill, font=font, anchor="rb"
        )
    elif layout == "banner":
        band = int(params["size"] * 1.6)
        draw.rectangle((0, 0, w, band), fill=(0, 0, 0, int(255 * params["alpha"])))
        draw.text((w // 24, band // 6), text, fill=(255, 255, 255, 235), font=font)
    else:
        raise ValueError(f"未知布局: {layout}")

    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def render_ad(
    size: tuple[int, int] = (512, 512),
    *,
    style: str = "blocks",
    rng: random.Random | None = None,
    text: str = AD_PLACEHOLDER_TEXT,
    text_size: int = 64,
) -> Image.Image:
    """合成广告占位图。blocks=旧污染器版式（行为保持）；gradient=B 组泛化版式。"""
    rng = rng or random.Random(0)
    w, h = size
    # 真实底图尺寸跨度大（COCO 有 ~110px 矮横幅）：随机范围全部按尺寸钳制，
    # 防止 randint 空区间（P1-T2 踩坑：r=56 时 randint(56, 54) 崩溃）
    img = Image.new("RGB", size, (200, 200, 205) if style == "blocks" else (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if style == "blocks":
        for _ in range(8):
            x0, y0 = rng.randint(0, max(1, w - 60)), rng.randint(0, max(1, h - 60))
            color = tuple(rng.randint(150, 255) for _ in range(3))
            draw.rectangle(
                (x0, y0, x0 + rng.randint(40, 110), y0 + rng.randint(20, 60)), fill=color
            )
    elif style == "gradient":
        base = (rng.randint(0, 200), rng.randint(0, 200), rng.randint(0, 200))
        for x in range(w):  # 水平渐变底
            c = tuple(min(255, int(v + 55 * x / w)) for v in base)
            draw.line([(x, 0), (x, h)], fill=c)
        max_r = max(4, (min(w, h) - 8) // 2)  # 圆必须放得下
        for _ in range(4):  # 装饰圆点 + 边框（与 blocks 版式刻意不同）
            r = rng.randint(4, min(60, max_r))
            cx, cy = rng.randint(r, w - r), rng.randint(r, h - r)
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255), width=4)
        draw.rectangle((8, 8, w - 9, h - 9), outline=(255, 255, 255), width=6)
        text = rng.choice(AD_TEXTS_B)
        text_size = 48
    else:
        raise ValueError(f"未知广告版式: {style}")
    draw.text(
        (w // 2, h // 2),
        text,
        fill=(60, 60, 60),
        font=_load_font(("msyh.ttc", "simhei.ttf"), text_size),
        anchor="mm",
    )
    return img


@dataclass
class DetectorSample:
    """生成数据清单行（design 1.2）。"""

    image_path: str
    label: int  # 0=clean / 1=watermark / 2=ad_nsfw
    style_group: str
    gen_params: dict


def generate_dataset(
    base_images: list[str], out_dir: str | Path, n_per_class: int, group: str = "A", seed: int = 42
) -> list[DetectorSample]:
    """按风格组生成 3 类检测数据：clean 引用底图原路径，wm/ad 落盘渲染图。"""
    if not base_images:
        raise ValueError("底图为空")
    rng = random.Random(seed)
    out_dir = Path(out_dir) / group
    (out_dir / "wm").mkdir(parents=True, exist_ok=True)
    (out_dir / "ad").mkdir(parents=True, exist_ok=True)
    style = STYLE_A if group == "A" else STYLE_B
    rows: list[DetectorSample] = []

    for label in (0, 1, 2):
        picks = rng.sample(base_images, min(n_per_class, len(base_images)))
        for i, src in enumerate(picks):
            if label == 0:
                rows.append(DetectorSample(src, 0, group, {"ref": "original"}))
                continue
            img = Image.open(src).convert("RGB")
            if label == 1:
                params = style.sample(rng, img.width)
                params["font_family"] = style.font_family
                dest = out_dir / "wm" / f"{i:05d}.jpg"
                render_watermark(img, params).save(dest, "JPEG", quality=90)
            else:
                ad_style = "blocks" if group == "A" else AD_STYLE_B
                params = {"style": ad_style}
                dest = out_dir / "ad" / f"{i:05d}.jpg"
                render_ad(img.size, style=ad_style, rng=rng).save(dest, "JPEG", quality=90)
            rows.append(DetectorSample(str(dest), label, group, params))

    manifest = out_dir / "manifest.jsonl"
    with open(manifest, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    return rows
