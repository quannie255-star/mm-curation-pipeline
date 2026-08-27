"""T1 数据层测试：参数化渲染、风格组错开、生成清单、确定性。

防循环论证的前提在数据层就必须成立：A/B 组参数四维（布局/透明度/字体/文本池）
全部错开，且 B 组文本不在 A 组出现。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from PIL import Image

from mm_curation.detector.synth import (
    STYLE_A,
    STYLE_B,
    WATERMARK_TEXTS_A,
    WATERMARK_TEXTS_B,
    generate_dataset,
    render_ad,
    render_watermark,
)


@pytest.fixture
def base_img():
    return Image.new("RGB", (320, 240), (100, 150, 90))


def _wm_params(style, rng, width=320):
    p = style.sample(rng, width)
    p["font_family"] = style.font_family
    return p


def test_style_groups_disjoint_on_style_params():
    """泛化协议（三轮迭代定稿，见 synth.py 注释）：布局模板允许共享
    （训练组须含模板多样性），风格参数三维（字体/透明度/文本池）必须错开。"""
    assert set(STYLE_A.font_family).isdisjoint(STYLE_B.font_family)
    assert STYLE_A.alpha[0] > STYLE_B.alpha[1]  # 叠加强度区间不重叠
    assert set(WATERMARK_TEXTS_A).isdisjoint(WATERMARK_TEXTS_B)
    assert "banner" in STYLE_A.layouts and "banner" in STYLE_B.layouts  # 共享的布局家族


def test_render_watermark_layouts_change_pixels(base_img):
    rng = random.Random(1)
    for style in (STYLE_A, STYLE_B):
        for layout in ("tiled", "corner", "banner"):
            params = _wm_params(style, rng)
            params["layout"] = layout
            out = render_watermark(base_img, params)
            assert out.size == base_img.size
            assert out.tobytes() != base_img.tobytes()  # 确实叠加了内容
    params = _wm_params(STYLE_A, rng)
    params["layout"] = "unknown"
    with pytest.raises(ValueError, match="布局"):
        render_watermark(base_img, params)


def test_render_watermark_deterministic(base_img):
    p1 = _wm_params(STYLE_B, random.Random(7))
    p2 = _wm_params(STYLE_B, random.Random(7))
    assert render_watermark(base_img, p1).tobytes() == render_watermark(base_img, p2).tobytes()


def test_render_ad_styles_differ():
    a = render_ad((256, 256), style="blocks", rng=random.Random(3))
    b = render_ad((256, 256), style="gradient", rng=random.Random(3))
    assert a.size == b.size == (256, 256)
    assert a.tobytes() != b.tobytes()
    with pytest.raises(ValueError, match="版式"):
        render_ad((64, 64), style="nope")


def test_generate_dataset_manifest(tmp_path):
    imgs = []
    for i in range(8):
        p = tmp_path / f"base{i}.png"
        Image.new("RGB", (160, 120), (i * 25, 60, 120)).save(p)
        imgs.append(str(p))

    rows = generate_dataset(imgs, tmp_path / "gen", n_per_class=5, group="B", seed=9)
    by_label = {0: [], 1: [], 2: []}
    for r in rows:
        by_label[r.label].append(r)
    assert [len(by_label[k]) for k in (0, 1, 2)] == [5, 5, 5]
    assert all(r.style_group == "B" for r in rows)
    assert all(Path(r.image_path).exists() for r in rows)
    assert by_label[0][0].image_path in imgs  # clean 引用原图不复制
    assert by_label[2][0].gen_params["style"] == "gradient"  # B 组广告用渐变版式
    manifest = tmp_path / "gen" / "B" / "manifest.jsonl"
    assert manifest.exists() and len(manifest.read_text("utf-8").splitlines()) == 15
    with pytest.raises(ValueError, match="底图"):
        generate_dataset([], tmp_path, 5)
