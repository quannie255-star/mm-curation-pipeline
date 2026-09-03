"""判官微调数据生成单测：SFT 格式契约 / 与 benchmark 的配比隔离 / 平衡性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from curation_eval import Sample

from mm_curation.operators.llm_judge import _JUDGE_PROMPT
from mm_curation.tuning.judge_data import TRAIN_KINDS, TRAIN_SEED, build_sft_rows


def _corpus(n: int) -> list[Sample]:
    return [
        Sample(
            id=f"doc{i:04d}",
            text=f"第{i}篇正文。" + f"这是第{i}号文档独有的领域正文，用于测试训练对生成。" * 3,
        )
        for i in range(n)
    ]


def test_sft_rows_match_judge_protocol(tmp_path: Path):
    rows = build_sft_rows(_corpus(30), n_clean=20, n_dirty=20, images_out=tmp_path / "img")
    assert len(rows) == 40
    for r in rows:
        assert r["prompt"].startswith(_JUDGE_PROMPT)  # 与 LlmJudgeOp 同一协议位
        obj = json.loads(r["completion"])
        assert isinstance(obj["score"], int) and 0 <= obj["score"] <= 10
        assert obj["reason"]
    assert sum(1 for r in rows if r["label"] == "dirty") == 20


def test_train_kinds_differ_from_benchmark_defaults():
    # 独立性原则 2：训练配比必须与 benchmark 默认配比不同
    from mm_curation.benchmarks.builder import BenchmarkSpec

    bench_kinds = BenchmarkSpec(name="x", domain_desc="d").kinds
    assert TRAIN_KINDS != bench_kinds
    assert set(TRAIN_KINDS) - set(bench_kinds)  # 含 benchmark 没有的损伤类型
    assert TRAIN_SEED != 9000  # 与 benchmark seed 族隔离


def test_corpus_insufficient_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="不足"):
        build_sft_rows(_corpus(5), n_clean=50, n_dirty=50, images_out=tmp_path / "img")
