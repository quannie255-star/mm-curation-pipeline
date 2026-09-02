"""curation-eval 包测试：协议自洽（Sample -> 污染 -> 丢弃 -> P/R 全链路）。"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from curation_eval import (
    ContaminationPlan,
    Sample,
    available_kinds,
    mrr,
    pr_from_drops,
    recall_at_k,
)
from PIL import Image


@pytest.fixture
def samples(tmp_path):
    out = []
    rng = random.Random(0)
    for i in range(10):
        p = tmp_path / f"img{i}.png"
        Image.new("RGB", (96, 72), (rng.randint(0, 255), 100, 150)).save(p)
        out.append(
            Sample(
                id=f"s{i}",
                image_path=str(p),
                text=f"第{i}张测试图片的内容描述",
            )
        )
    return out


def test_plan_labels_and_manifest(samples, tmp_path):
    mixed, manifest = ContaminationPlan(
        inject_rate=1.0,
        seed=7,
        kinds={"truncate_text": 0.5, "exact_duplicate": 0.5},
    ).run(samples, tmp_path / "dirty")
    assert len(mixed) == 20
    assert manifest["n_injected"] == 10 and sum(manifest["counts"].values()) == 10
    injected = mixed[10:]
    assert all(s.labels.get("dirty") for s in injected)
    assert all(not s.labels for s in mixed[:10])
    assert len({s.id for s in mixed}) == 20


def test_watermark_writes_new_file(samples, tmp_path):
    mixed, _ = ContaminationPlan(
        inject_rate=0.2,
        seed=3,
        kinds={"watermark": 1.0},
    ).run(samples, tmp_path / "dirty")
    wm = next(s for s in mixed if s.labels)
    orig = next(s for s in samples if wm.id.startswith(s.id))
    assert Path(wm.image_path).exists()
    assert wm.image_path != orig.image_path  # 渲染新图，不改原图


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="未注册"):
        ContaminationPlan(inject_rate=0.1, seed=1, kinds={"nope": 1.0})


def test_image_kind_requires_image_corpus(tmp_path):
    text_only = [Sample(id=f"t{i}", text=f"纯文本语料第{i}段") for i in range(4)]
    with pytest.raises(ValueError, match="带图样本"):
        ContaminationPlan(inject_rate=0.5, seed=1, kinds={"blur": 1.0}).run(text_only, tmp_path)
    # 文本类污染在纯文本语料上正常工作
    mixed, _ = ContaminationPlan(inject_rate=0.5, seed=1, kinds={"mojibake": 1.0}).run(
        text_only, tmp_path
    )
    assert sum(1 for s in mixed if s.labels) == 2


def test_pr_from_drops_full_loop(samples, tmp_path):
    mixed, _ = ContaminationPlan(
        inject_rate=1.0,
        seed=7,
        kinds={"truncate_text": 0.5, "exact_duplicate": 0.5},
    ).run(samples, tmp_path / "dirty")
    dirty_ids = [s.id for s in mixed if s.labels]
    # 模拟清洗系统：完美抓住全部脏数据、误杀 1 条干净
    pr = pr_from_drops(dirty_ids + ["s0"], mixed)
    assert pr["n_dirty"] == 10
    assert pr["precision"] == round(10 / 11, 4)
    assert pr["recall"] == 1.0
    assert pr["clean_killed"] == 1 and pr["dirty_missed"] == 0


def test_retrieval_metrics():
    assert recall_at_k([1, None, 3, 7], 5) == pytest.approx(0.5)
    assert mrr([1, None, 3, 7]) == pytest.approx((1 + 1 / 3 + 1 / 7) / 4)


def test_registry_documented_kinds():
    for kind in [
        "watermark",
        "blur",
        "low_resolution",
        "exact_duplicate",
        "truncate_text",
        "mojibake",
        "mismatched_pair",
    ]:
        assert kind in available_kinds()
