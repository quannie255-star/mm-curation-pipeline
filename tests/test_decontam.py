"""去污染模块测试：图像/文本双层重叠检测 + P/R 对账。"""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageDraw

from mm_curation.eval.decontam import detect_overlap, evaluate_decontam
from mm_curation.operators.base import Sample

CAPTIONS = [
    "一只金毛犬在夕阳下的海滩上奔跑",
    "城市夜景中的霓虹灯广告牌特写",
    "厨房料理台上摆满了新鲜的蔬菜水果",
    "雪山脚下一栋木屋冒出袅袅炊烟",
]


def _structured(path, seed):
    rng = random.Random(seed)
    img = Image.new("RGB", (160, 120))
    draw = ImageDraw.Draw(img)
    base = (seed * 37 % 255, seed * 61 % 255, seed * 89 % 255)
    for x in range(160):
        draw.line([(x, 0), (x, 120)], fill=tuple(int(c * x / 160) for c in base))
    for _ in range(3):
        x0, y0 = rng.randint(0, 120), rng.randint(0, 80)
        draw.rectangle(
            (x0, y0, x0 + rng.randint(20, 40), y0 + rng.randint(15, 30)),
            fill=tuple(rng.randint(0, 255) for _ in range(3)),
        )
    img.save(path)
    return str(path)


@pytest.fixture
def corpus(tmp_path):
    return [
        Sample(
            id=f"c{i}",
            image_path=_structured(tmp_path / f"c{i}.png", i + 1),
            caption=CAPTIONS[i % 4],
        )
        for i in range(4)
    ]


def test_image_overlap_detected(corpus, tmp_path):
    dup = Sample(id="dup", image_path=corpus[0].image_path, caption="完全无关的文本内容")
    fresh = Sample(
        id="fresh", image_path=_structured(tmp_path / "new.png", 99), caption="全新样本的描述文字"
    )
    hits = detect_overlap(corpus, [dup, fresh])
    assert [(h.sample_id, h.method) for h in hits] == [("dup", "phash_image")]
    assert hits[0].overlap_with == "c0"


def test_text_overlap_detected(corpus, tmp_path):
    near = CAPTIONS[0] + "，一只金毛犬在夕阳下的海滩上奔跑"  # Jaccard 高
    suspicious = Sample(id="txt", image_path=_structured(tmp_path / "t.png", 77), caption=near)
    hits = detect_overlap(corpus, [suspicious])
    assert hits and hits[0].method == "minhash_text"


def test_evaluate_against_ground_truth(corpus, tmp_path):
    injected = [  # 已知污染：同图引用 + 近似文本
        Sample(
            id="d1",
            image_path=corpus[1].image_path,
            caption="随便什么文案内容甲",
            labels={"dirty": "image_leak"},
        ),
        Sample(
            id="d2",
            image_path=_structured(tmp_path / "d2.png", 55),
            caption=CAPTIONS[2] + "，厨房料理台上摆满了新鲜的蔬菜水果",
            labels={"dirty": "text_leak"},
        ),
    ]
    clean = Sample(
        id="ok", image_path=_structured(tmp_path / "ok.png", 88), caption="独立的全新描述句子"
    )
    hits = detect_overlap(corpus, injected + [clean])
    report = evaluate_decontam(hits, injected + [clean])
    assert report["n_flagged"] == 2 and report["n_incoming"] == 3
    assert report["precision"] == 1.0 and report["recall"] == 1.0
    assert report["clean_flagged"] == 0
