"""图像质量算子（L1 规则层：纯 CPU、毫秒级，漏斗中放在最前）。

模糊度说明：Laplacian 方差是分辨率/内容相关的绝对量，阈值需按数据分布
校准（这正是 quality/report.py 输出分数分布的意义）；算子本身保持无状态。
"""

from __future__ import annotations

from typing import Optional

from .base import Operator, Sample
from .registry import register


def _image_size(sample: Sample) -> Optional[tuple[int, int]]:
    try:
        from PIL import Image

        with Image.open(sample.image_path) as img:
            return img.size
    except OSError:
        return None


@register("resolution")
class ResolutionOp(Operator):
    """短边像素数。太小的图对 CLIP 编码与训练几乎无贡献。score = min(w, h)。"""

    def score(self, sample: Sample) -> Optional[float]:
        size = _image_size(sample)
        return float(min(size)) if size else None


@register("aspect_ratio")
class AspectRatioOp(Operator):
    """长宽比均衡度。score = min(w/h, h/w) ∈ (0, 1]，1 为正方形。
    极端长宽比多为banner/拼接图。"""

    def score(self, sample: Sample) -> Optional[float]:
        size = _image_size(sample)
        if not size:
            return None
        w, h = size
        return min(w / h, h / w)


@register("blur")
class BlurOp(Operator):
    """清晰度 = Laplacian 方差（越低越模糊）。score 语义统一为越高越好，
    直接取方差原值，min 阈值按数据校准（典型量级见 configs/）。"""

    def score(self, sample: Sample) -> Optional[float]:
        try:
            import cv2
        except ImportError:  # pragma: no cover - 环境缺 cv2 时显式失败
            raise ImportError("blur 算子需要 opencv-python（pip install -r requirements.txt）")
        try:
            img = cv2.imread(sample.image_path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                return None
            return float(cv2.Laplacian(img, cv2.CV_64F).var())
        except OSError:
            return None
