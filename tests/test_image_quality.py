"""L1 图像质量算子测试：score 语义（越高越好）、None 语义、阈值过滤。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from mm_curation.operators import build_operator
from mm_curation.operators.base import Sample


def _save(img: Image.Image, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=92)
    return str(path)


@pytest.fixture
def paths(tmp_path):
    """三类合成图：高纹理（清晰）、其模糊版、纯色小图。"""
    rng = np.random.default_rng(0)
    noise = Image.fromarray((rng.random((120, 160, 3)) * 255).astype("uint8"))
    return {
        "noise": _save(noise, tmp_path / "noise.jpg"),
        "blurred": _save(noise.filter(ImageFilter.GaussianBlur(5)), tmp_path / "blurred.jpg"),
        "small_solid": _save(Image.new("RGB", (100, 50), (30, 60, 90)), tmp_path / "solid.jpg"),
        "square": _save(Image.new("RGB", (80, 80), (1, 2, 3)), tmp_path / "square.jpg"),
    }


def _sample(path: str, sid: str = "s1") -> Sample:
    return Sample(id=sid, image_path=path, caption="一只猫坐在沙发上")


def test_resolution_score_and_threshold(paths):
    op = build_operator({"op": "resolution", "params": {"min": 60}})
    small = _sample(paths["small_solid"])  # 100x50 -> 短边 50
    assert op.score(small) == 50
    assert op(small) is None  # 低于阈值被丢弃
    assert small.meta["score:resolution"] == 50  # 分数仍写入 meta（漏斗报告依赖）
    sharp = _sample(paths["noise"])  # 160x120 -> 短边 120
    assert op(sharp) is sharp


def test_aspect_ratio(paths):
    op = build_operator({"op": "aspect_ratio"})
    assert op.score(_sample(paths["small_solid"])) == pytest.approx(0.5)
    assert op.score(_sample(paths["square"])) == pytest.approx(1.0)


def test_blur_ranks_sharp_above_blurred(paths):
    op = build_operator({"op": "blur"})
    sharp = op.score(_sample(paths["noise"]))
    blurred = op.score(_sample(paths["blurred"]))
    assert sharp > blurred * 10  # 数量级差距（Laplacian 方差）
    assert blurred < 100  # 常用模糊阈值的量级参考


def test_unreadable_image_returns_none(tmp_path):
    op = build_operator({"op": "resolution"})
    missing = _sample(str(tmp_path / "nonexistent.jpg"))
    assert op.score(missing) is None
    assert op(missing) is missing  # None 分数语义：保留并记录缺失，不误杀
    assert missing.meta["score:resolution"] is None
