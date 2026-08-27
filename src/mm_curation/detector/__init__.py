"""detector 模块：水印/NSFW 合成数据自训检测器（Phase2 P1）。"""

from .synth import (
    STYLE_A,
    STYLE_B,
    DetectorSample,
    generate_dataset,
    render_ad,
    render_watermark,
)

__all__ = [
    "STYLE_A",
    "STYLE_B",
    "DetectorSample",
    "generate_dataset",
    "render_ad",
    "render_watermark",
]
