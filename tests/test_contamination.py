"""污染器测试：每类注入后的标注正确性、可检测性、确定性与比例。

注意：Contaminator.apply 会原地修改传入样本（计划内部对 deepcopy 调用），
单测里比较「改写效果」时必须先快照不可变的原始值（caption 字符串）。
"""

from __future__ import annotations

import copy
import random

import pytest
from PIL import Image

from mm_curation.contamination import ContaminationPlan, available_contaminators
from mm_curation.contamination.base import ContaminationContext
from mm_curation.contamination.impl import (
    Blur,
    ExactDuplicate,
    LowQualityText,
    LowResolution,
    MismatchedPair,
    NearDuplicateImage,
    NearDuplicateText,
    NsfwPlaceholder,
    SemanticDuplicate,
    Watermark,
)
from mm_curation.operators.base import Sample

ALL_KINDS = [
    "exact_duplicate",
    "near_duplicate_image",
    "near_duplicate_text",
    "semantic_duplicate",
    "low_resolution",
    "blur",
    "mismatched_pair",
    "low_quality_text",
    "watermark",
    "nsfw_placeholder",
]


@pytest.fixture
def clean_samples(tmp_path):
    """构造 20 张小图 + 中文 caption 的合成干净集（不依赖网络与真实数据）。"""
    samples = []
    rng = random.Random(0)
    for i in range(20):
        img = Image.new("RGB", (128, 96), (rng.randint(0, 255), 128, 64))
        p = tmp_path / "clean" / f"img{i}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p, "JPEG")
        samples.append(
            Sample(
                id=f"COCO_train2014_{i:012d}",
                image_path=str(p),
                caption=f"一只可爱的动物在草地上玩耍编号{i}",
                meta={"tags": ["动物", "草地"], "split": "train"},
            )
        )
    return samples


def _ctx(samples, tmp_path):
    return ContaminationContext(samples, tmp_path / "dirty", random.Random(1))


def test_registry_covers_all_kinds():
    assert set(ALL_KINDS) <= set(available_contaminators())


def test_exact_duplicate_keeps_references(clean_samples, tmp_path):
    src = clean_samples[0]
    out = ExactDuplicate().apply(src, 0, _ctx(clean_samples, tmp_path))
    assert out.image_path == src.image_path and out.caption == src.caption


def test_near_duplicate_image_changes_bytes(clean_samples, tmp_path):
    src = clean_samples[0]
    src.id += "::near0"  # 模拟计划注入时的 id 形态
    before = open(src.image_path, "rb").read()
    out = NearDuplicateImage().apply(src, 0, _ctx(clean_samples, tmp_path))
    assert open(out.image_path, "rb").read() != before
    assert "--" in out.image_path  # id 中的 '::' 已按 Windows 规则清洗


def test_near_duplicate_text_still_similar(clean_samples, tmp_path):
    src = clean_samples[0]
    original = src.caption
    out = NearDuplicateText().apply(src, 0, _ctx(clean_samples, tmp_path))
    # 字符 3-gram Jaccard 应保持较高（MinHash-LSH 阈值 0.5 可召回）
    a = {original[i : i + 3] for i in range(len(original) - 2)}
    b = {out.caption[i : i + 3] for i in range(len(out.caption) - 2)}
    assert len(a & b) / len(a | b) > 0.5


def test_semantic_duplicate_rewrites_caption(clean_samples, tmp_path):
    src = clean_samples[0]
    original = src.caption
    out = SemanticDuplicate().apply(src, 0, _ctx(clean_samples, tmp_path))
    assert out.caption != original and "动物" in out.caption  # tags 改写


def test_image_quality_kinds_modify_pixels(clean_samples, tmp_path):
    ctx = _ctx(clean_samples, tmp_path)
    for src, op in zip(clean_samples[1:3], (LowResolution(), Blur())):
        original_path = src.image_path
        out = op.apply(src, 0, ctx)
        assert out.image_path != original_path
        assert Image.open(out.image_path).size == (128, 96)


def test_mismatched_pair_caption_from_donor(clean_samples, tmp_path):
    src = clean_samples[0]
    original = src.caption
    others = clean_samples[1:]  # 供体池排除自身，保证 caption 必然不同
    out = MismatchedPair().apply(src, 0, _ctx(others, tmp_path))
    assert out.caption != original
    assert out.meta["mismatch_donor"] in {s.id for s in others}


def test_low_quality_text_variants(clean_samples, tmp_path):
    ctx = _ctx(clean_samples, tmp_path)
    seen = set()
    for i in range(20):  # 覆盖四种变体
        src = clean_samples[2]
        out = LowQualityText().apply(src, i, ctx)
        seen.add(out.meta["lqt_variant"])
    assert seen == {"truncate", "repeat", "mojibake", "noise"}


def test_watermark_and_placeholder(clean_samples, tmp_path):
    ctx = _ctx(clean_samples, tmp_path)
    w = Watermark().apply(clean_samples[3], 0, ctx)
    assert Image.open(w.image_path).size == (128, 96)
    n = NsfwPlaceholder().apply(clean_samples[4], 0, ctx)
    assert Image.open(n.image_path).size == (512, 512)
    assert "优惠券" in n.caption  # 广告引流文案，模拟违规占位样本


def test_plan_labels_and_counts(clean_samples, tmp_path):
    kinds = {k: 1.0 for k in ALL_KINDS}
    plan = ContaminationPlan(inject_rate=1.0, seed=7, kinds=kinds)
    mixed, manifest = plan.run(clean_samples, tmp_path / "out" / "images")
    assert len(mixed) == 40  # 20 干净 + 20 注入
    assert sum(manifest["counts"].values()) == 20
    injected = mixed[len(clean_samples) :]
    assert all(s.labels.get("dirty") in ALL_KINDS for s in injected)
    assert all(not s.labels for s in mixed[: len(clean_samples)])
    # 注入 id 不与原始 id 冲突（去重「先到先保留」约定成立的前提）
    assert len({s.id for s in mixed}) == 40


def test_plan_deterministic(clean_samples, tmp_path):
    kinds = {k: 1.0 for k in ALL_KINDS}
    a, ma = ContaminationPlan(inject_rate=0.5, seed=7, kinds=kinds).run(
        clean_samples, tmp_path / "a"
    )
    b, mb = ContaminationPlan(inject_rate=0.5, seed=7, kinds=kinds).run(
        clean_samples, tmp_path / "b"
    )
    assert [s.id for s in a] == [s.id for s in b]
    assert ma["counts"] == mb["counts"]


def test_plan_rejects_unknown_kind(clean_samples, tmp_path):
    with pytest.raises(ValueError, match="未注册"):
        ContaminationPlan(inject_rate=0.1, seed=1, kinds={"nonexistent": 1.0}).run(
            clean_samples, tmp_path
        )


def test_originals_untouched_after_plan(clean_samples, tmp_path):
    """污染计划不得修改原始样本对象与文件（ground truth 干净性的前提）。"""
    before = copy.deepcopy([s.to_dict() for s in clean_samples])
    kinds = {k: 1.0 for k in ALL_KINDS}
    ContaminationPlan(inject_rate=1.0, seed=3, kinds=kinds).run(
        clean_samples, tmp_path / "out" / "images"
    )
    assert [s.to_dict() for s in clean_samples] == before
