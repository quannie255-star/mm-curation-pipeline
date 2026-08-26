"""分层采样器测试（D5）：预算守恒、分层配比、质量桶映射、可复现性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mm_curation.operators.base import Sample
from mm_curation.sampling import (
    RandomSampler,
    SamplingConfig,
    StratifiedSampler,
)
from mm_curation.sampling.sampler import _category, _quality_bucket


def _sample(sid: str, score: float | None, tags: list[str] | None = None) -> Sample:
    return Sample(
        id=sid,
        image_path=f"/tmp/{sid}.jpg",
        caption="测试样本",
        meta={"score:clip_alignment": score, "tags": tags or []},
    )


# ---------- 质量桶映射 ----------


def test_quality_bucket_boundaries():
    buckets = [("low", 0.0, 0.40), ("mid", 0.40, 0.48), ("high", 0.48, 1.01)]
    assert _quality_bucket(0.35, buckets) == "low"
    assert _quality_bucket(0.40, buckets) == "mid"  # 左闭右开
    assert _quality_bucket(0.479, buckets) == "mid"
    assert _quality_bucket(0.48, buckets) == "high"
    assert _quality_bucket(0.55, buckets) == "high"
    assert _quality_bucket(None, buckets) == "low"  # 缺分 = 低质量


def test_quality_bucket_out_of_range_falls_to_last():
    buckets = [("low", 0.0, 0.40), ("mid", 0.40, 0.48)]
    assert _quality_bucket(0.99, buckets) == "mid"  # 超出最后一个上界归末桶


# ---------- 类目 ----------


def test_category_first_tag_or_other():
    assert _category(_sample("a", 0.5, ["猫", "沙发"])) == "猫"
    assert _category(_sample("b", 0.5, [])) == "其他"
    assert _category(_sample("c", 0.5, None)) == "其他"


# ---------- 随机采样：预算守恒 + 不超量 ----------


def test_random_budget_capped():
    pool = [_sample(f"s{i}", 0.5, ["猫"]) for i in range(50)]
    recipe = RandomSampler().sample(pool, SamplingConfig(budget=20))
    assert recipe.n_sampled == 20
    assert len(recipe.sampled_ids) == 20
    assert len(set(recipe.sampled_ids)) == 20  # 无重复


def test_random_budget_exceeds_pool():
    pool = [_sample(f"s{i}", 0.5) for i in range(10)]
    recipe = RandomSampler().sample(pool, SamplingConfig(budget=100))
    assert recipe.n_sampled == 10  # 不超过池容量


def test_random_reproducible_with_seed():
    pool = [_sample(f"s{i}", 0.5, ["猫"]) for i in range(30)]
    r1 = RandomSampler().sample(pool, SamplingConfig(budget=10, seed=42))
    r2 = RandomSampler().sample(pool, SamplingConfig(budget=10, seed=42))
    assert r1.sampled_ids == r2.sampled_ids


# ---------- 分层采样：分层统计 + 高质量过采样 ----------


def test_stratified_strata_summary_populated():
    pool = [
        _sample("h0", 0.50, ["猫"]),
        _sample("h1", 0.50, ["猫"]),
        _sample("m0", 0.45, ["狗"]),
        _sample("l0", 0.35, ["猫"]),
    ]
    recipe = StratifiedSampler().sample(pool, SamplingConfig(budget=4))
    assert recipe.n_sampled == 4
    # strata_summary 应包含 quality/category 键
    assert any("high" in k for k in recipe.strata_summary)
    assert any("mid" in k for k in recipe.strata_summary)
    assert any("low" in k for k in recipe.strata_summary)


def test_stratified_oversamples_high_quality():
    # 10 低质 + 10 高质，预算 10，oversample=2.0 → 高质应多于低质
    pool = [_sample(f"l{i}", 0.30, ["猫"]) for i in range(10)] + [
        _sample(f"h{i}", 0.50, ["猫"]) for i in range(10)
    ]
    recipe = StratifiedSampler().sample(pool, SamplingConfig(budget=10, oversample_high=2.0))
    picked = {s.id for s in pool if s.id in set(recipe.sampled_ids)}
    high_picked = sum(1 for s in pool if s.id in picked and s.meta["score:clip_alignment"] >= 0.48)
    low_picked = sum(1 for s in pool if s.id in picked and s.meta["score:clip_alignment"] < 0.40)
    # 过采样应让 high >= low（2x 权重下 high 应明显更多）
    assert high_picked >= low_picked


def test_stratified_budget_capped_and_unique():
    pool = [_sample(f"s{i}", 0.5, [f"cat{i}"]) for i in range(30)]
    recipe = StratifiedSampler().sample(pool, SamplingConfig(budget=15))
    assert recipe.n_sampled == 15
    assert len(set(recipe.sampled_ids)) == 15


def test_stratified_no_budget_returns_all():
    pool = [_sample(f"s{i}", 0.5, ["猫"]) for i in range(5)]
    recipe = StratifiedSampler().sample(pool, SamplingConfig(budget=None))
    assert recipe.n_sampled == 5


# ---------- 真实数据冒烟 ----------


CLEANED = Path("data/processed/cn_flickr_curation_v2/cleaned.jsonl")


@pytest.mark.skipif(not CLEANED.exists(), reason="需先 make funnel")
def test_real_data_sampling_smoke():
    """真实漏斗产出冒烟：采样不崩、预算守恒、分层统计非空。"""
    pool = [
        Sample.from_dict(json.loads(line))
        for line in CLEANED.read_text(encoding="utf-8").splitlines()
    ]
    recipe_r = RandomSampler().sample(pool, SamplingConfig(budget=1000))
    recipe_s = StratifiedSampler().sample(pool, SamplingConfig(budget=1000))
    assert recipe_r.n_sampled == 1000
    assert recipe_s.n_sampled == 1000
    # 分层采样应产生多个分层（不坍缩成单层）
    assert len(recipe_s.strata_summary) > 1
