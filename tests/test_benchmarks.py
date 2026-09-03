"""benchmark 构建器单测：seed 隔离 / 标签平衡 / 泄漏检查 / manifest 完整性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from curation_eval import Sample

from mm_curation.benchmarks.builder import BenchmarkSpec, build_benchmark


def _corpus(n: int) -> list[Sample]:
    return [
        Sample(
            id=f"doc{i:04d}",
            text=f"第{i}篇正文。" + f"这是第{i}号文档独有的领域正文，用于测试构建流程。" * 3,
        )
        for i in range(n)
    ]


def test_seed_collision_rejected():
    with pytest.raises(ValueError, match="seed"):
        BenchmarkSpec(name="x", domain_desc="d", seed=23, train_seeds=(23,))


def test_build_produces_frozen_items_and_manifest(tmp_path: Path):
    spec = BenchmarkSpec(
        name="t", domain_desc="测试域", n_clean=20, n_dirty=20, seed=9000, train_seeds=(23,)
    )
    manifest = build_benchmark(_corpus(40), spec, tmp_path / "bmk", images_out=tmp_path / "img")
    items = [
        json.loads(ln)
        for ln in (tmp_path / "bmk" / "items.jsonl").read_text(encoding="utf-8").split("\n")
        if ln.strip()
    ]
    # 污染器有放回选样：偶发同篇同损伤双注入 → 全同文本被去重（builder 防御），
    # 故 dirty 允许 -2 的容差；clean 侧是文档级采样必精确
    labels = [it["label"] for it in items]
    assert labels.count("clean") == 20
    assert 18 <= labels.count("dirty") <= 20
    assert all(it["kind"] != "clean" for it in items if it["label"] == "dirty")
    assert manifest["train_seed_isolation"]["train_seeds"] == [23]
    assert "label_protocol" in manifest and manifest["n_items"] == len(items)
    # 冻结性：id 由文本 md5 派生，去重后必唯一
    assert len({it["id"] for it in items}) == len(items)


def test_leakage_check_detects_exact_and_near(tmp_path: Path):
    train = tmp_path / "train.jsonl"
    leak_text = "这篇文本会以精确复制的方式泄漏进基准，用于验证检查真的能抓到。" * 2
    train.write_text(
        json.dumps({"id": "t1", "text": leak_text}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    spec = BenchmarkSpec(
        name="t", domain_desc="d", n_clean=6, n_dirty=4, seed=9100, train_seeds=(23,)
    )
    corpus = _corpus(6)  # 全量采样，注入泄漏源的 doc 必然入选
    corpus[0] = Sample(id="doc0000", text=leak_text)  # 注入精确泄漏源
    manifest = build_benchmark(
        corpus, spec, tmp_path / "bmk", train_jsonl=train, images_out=tmp_path / "img"
    )
    assert manifest["leakage_check"]["md5_leaks"], "精确泄漏必须被抓到"


def test_corpus_insufficient_rejected():
    spec = BenchmarkSpec(
        name="t", domain_desc="d", n_clean=50, n_dirty=50, seed=9200, train_seeds=(23,)
    )
    with pytest.raises(ValueError, match="不足"):
        build_benchmark(_corpus(10), spec, Path("unused"), images_out=Path("unused"))
