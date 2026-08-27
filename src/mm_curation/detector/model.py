"""检测器模型加载（惰性单例，测试 monkeypatch 本入口）。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[3] / "models/detector/wm_nsfw_cnn.pt"

_MODEL = None


def load_detector(path: str | Path = DEFAULT_MODEL_PATH):
    """加载 MobileNetV3-Small 三分类检测器（clean/watermark/ad_nsfw）。"""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    import torch
    from torchvision import models

    if not Path(path).exists():
        raise FileNotFoundError(
            f"检测器权重不存在: {path}（先运行 python scripts/train_detector.py）"
        )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = models.mobilenet_v3_small(weights=None)
    import torch.nn as nn

    model.classifier[3] = nn.Linear(model.classifier[3].in_features, 3)
    ckpt = torch.load(path, map_location=device, weights_only=True)
    model.load_state_dict(ckpt["state_dict"])
    model = model.to(device).eval()
    logger.info("检测器就绪: %s (%s)", path, device)
    _MODEL = model
    return model


def reset_detector() -> None:
    """测试辅助：清空单例。"""
    global _MODEL
    _MODEL = None
