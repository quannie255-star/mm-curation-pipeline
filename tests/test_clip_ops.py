"""L2 CLIP 算子测试：用假编码器验证逻辑（真实模型的校准跑真实数据，不入单测）。

monkeypatch get_encoder() 注入确定性假向量：
- 对齐分：构造 cos=1（匹配）与 cos=0（错配）两种正交向量
- 语义去重：构造近共线（cos≈0.99）与正交的向量
"""

from __future__ import annotations

import numpy as np
import pytest

import mm_curation.embedding.clip_encoder as encoder_mod
from mm_curation.operators import build_operator
from mm_curation.operators.base import Sample


class FakeEncoder:
    """按样本 id 的确定性假编码器：img=id 向量，txt=给定向量。"""

    def __init__(self, text_vecs: dict[str, np.ndarray], img_vecs: dict[str, np.ndarray]):
        self.text_vecs = text_vecs
        self.img_vecs = img_vecs

    def encode_images(self, paths):
        return np.stack([self.img_vecs[p] for p in paths])

    def encode_texts(self, texts):
        return np.stack([self.text_vecs[t] for t in texts])


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


@pytest.fixture
def samples(tmp_path):
    from PIL import Image

    out = []
    for i in range(4):
        p = tmp_path / f"img{i}.jpg"
        Image.new("RGB", (32, 32), (i * 50, 100, 150)).save(p, "JPEG")
        out.append(Sample(id=f"s{i}", image_path=str(p), text=f"caption{i}"))
    return out


def test_clip_alignment_filters_mismatched(samples, monkeypatch):
    base = _unit([1, 0, 0, 0])
    orth = _unit([0, 1, 0, 0])
    monkeypatch.setattr(
        encoder_mod,
        "get_encoder",
        lambda: FakeEncoder(
            # s0/s2 文本与图像同向（cos=1），s1/s3 正交（cos=0，错配）
            text_vecs={f"caption{i}": (base if i % 2 == 0 else orth) for i in range(4)},
            img_vecs={s.image_path: base for s in samples},
        ),
    )
    op = build_operator({"op": "clip_alignment", "params": {"min": 0.5}})
    kept = op.run_batch(samples)
    assert [s.id for s in kept] == ["s0", "s2"]
    assert samples[1].meta["score:clip_alignment"] == pytest.approx(0.0)
    assert samples[0].meta["score:clip_alignment"] == pytest.approx(1.0)


def test_semantic_dedup_keep_first(samples, monkeypatch):
    a = _unit([1, 0, 0])
    near_a = _unit([1, 0.01, 0])  # cos ≈ 0.9999
    b = _unit([0, 1, 0])
    c = _unit([0, 0, 1])
    monkeypatch.setattr(
        encoder_mod,
        "get_encoder",
        lambda: FakeEncoder(
            text_vecs={},  # 语义去重不用文本
            img_vecs={
                samples[0].image_path: a,
                samples[1].image_path: b,
                samples[2].image_path: near_a,  # s2 ≈ s0 -> 被扔
                samples[3].image_path: c,
            },
        ),
    )
    op = build_operator({"op": "semantic_dedup", "params": {"threshold": 0.99}})
    kept = op.run_batch(samples)
    assert [s.id for s in kept] == ["s0", "s1", "s3"]
    assert samples[2].meta["dedup:semantic_dedup"]["duplicate_of"] == "s0"


def test_semantic_dedup_no_false_merge(samples, monkeypatch):
    """正交向量互不合并：全部保留。"""
    vecs = [_unit(v) for v in ([1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0.001])]
    monkeypatch.setattr(
        encoder_mod,
        "get_encoder",
        lambda: FakeEncoder(
            text_vecs={},
            img_vecs={s.image_path: v for s, v in zip(samples, vecs)},
        ),
    )
    op = build_operator({"op": "semantic_dedup", "params": {"threshold": 0.99}})
    assert len(op.run_batch(samples)) == 4
