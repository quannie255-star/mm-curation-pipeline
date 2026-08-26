"""算子级 P/R 评测测试（D4）：独立评测口径、目标映射、recall/precision 计算。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mm_curation.eval.operator_pr import (
    OPERATOR_TARGETS,
    evaluate_all,
    evaluate_operator,
    render_pr_markdown,
    run_operator,
)
from mm_curation.operators.base import Sample
from mm_curation.operators.registry import build_operator
from mm_curation.pipeline import OperatorSpec


def _img(tmp_path: Path, name: str, color=(120, 80, 60)) -> str:
    from PIL import Image

    p = tmp_path / f"{name}.jpg"
    Image.new("RGB", (64, 64), color).save(p, "JPEG")
    return str(p)


def _clean(tmp_path, n=3) -> list[Sample]:
    # 每张图颜色各异，避免 md5 误判干净样本互为重复
    return [
        Sample(
            id=f"c{i}",
            image_path=_img(tmp_path, f"c{i}", (i * 40 % 255, 80, 160)),
            caption=f"这是第{i}张干净的图",
        )
        for i in range(n)
    ]


def _labeled(samples: list[Sample], dirty_type: str, ids: list[str]) -> list[Sample]:
    """给指定 id 的样本打上 dirty 标签（模拟污染器注入）。"""
    by_id = {s.id: s for s in samples}
    out = []
    for i, sid in enumerate(ids):
        s = by_id[sid]
        out.append(
            Sample(
                id=f"{sid}::{dirty_type}{i}",
                image_path=s.image_path,
                caption=s.caption,
                labels={"dirty": dirty_type},
            )
        )
    return out


# ---------- 精确重复：md5_exact 召回 100%、误杀 0 ----------


def test_md5_exact_full_recall_zero_falsekill(tmp_path):
    cleans = _clean(tmp_path, 3)
    # exact_duplicate = 同图同 caption，排在原样本之后
    dups = _labeled(cleans, "exact_duplicate", ["c0", "c1", "c2"])
    samples = cleans + dups
    spec = OperatorSpec(op="md5_exact")
    r = evaluate_operator(spec, samples)
    assert r.n_dropped == 3
    assert r.clean_killed == 0
    assert r.precision == 1.0
    assert r.recall_of("exact_duplicate", 3) == 1.0


def test_md5_independent_vs_funnel(tmp_path):
    """关键差异：独立评测口径下 md5 只扔字节相同的注入，不漏。
    漏斗串联时若上游已扔部分样本，下游的 n_in 会缩小——
    独立评测固定在全量，recall 分母恒定。"""
    cleans = _clean(tmp_path, 2)
    dups = _labeled(cleans, "exact_duplicate", ["c0"])
    samples = cleans + dups
    r = evaluate_operator(OperatorSpec(op="md5_exact"), samples)
    assert r.n_in == 3  # 独立评测：全集进入，不被上游裁剪
    assert r.recall_of("exact_duplicate", 1) == 1.0


# ---------- text_length：阈值过滤的 precision/recall ----------


def test_text_length_recall_on_low_quality(tmp_path):
    cleans = [
        Sample(id="c0", image_path=_img(tmp_path, "c0"), caption="一只猫坐在沙发上看电视"),
        Sample(id="c1", image_path=_img(tmp_path, "c1"), caption="两个孩子在公园里玩耍"),
    ]
    # low_quality_text: 截断（2字 < min=5）
    dirty = [
        Sample(
            id="d0::low_quality_text0",
            image_path=_img(tmp_path, "d0"),
            caption="哈" * 30,  # 刷字变体
            labels={"dirty": "low_quality_text"},
        ),
        Sample(
            id="d1::low_quality_text1",
            image_path=_img(tmp_path, "d1"),
            caption="好",
            labels={"dirty": "low_quality_text"},
        ),
    ]
    samples = cleans + dirty
    # char_repetition 抓"刷字"（哈哈哈…）, text_length 抓截断；两算子互补
    r_rep = evaluate_operator(OperatorSpec(op="char_repetition", params={"min": 0.8}), samples)
    r_len = evaluate_operator(OperatorSpec(op="text_length", params={"min": 5}), samples)
    # char_repetition 召回刷字样本 d0
    assert r_rep.dirty_caught.get("low_quality_text", 0) >= 1
    # text_length 召回截断样本 d1（1 字 < 5）
    assert r_len.dirty_caught.get("low_quality_text", 0) >= 1
    # 干净样本都不被这两个算子误杀
    assert r_rep.clean_killed == 0
    assert r_len.clean_killed == 0


# ---------- evaluate_all：全集分母与 recall 矩阵 ----------


def test_evaluate_all_dirty_totals_and_matrix(tmp_path):
    cleans = _clean(tmp_path, 4)
    dups = _labeled(cleans, "exact_duplicate", ["c0", "c1"])  # 2 个
    dirty = [
        Sample(
            id="d0::blur0",
            image_path=_img(tmp_path, "blur0"),
            caption=cleans[0].caption,
            labels={"dirty": "blur"},
        )
    ]
    samples = cleans + dups + dirty
    results, totals, n_clean = evaluate_all([OperatorSpec(op="md5_exact")], samples)
    assert totals == {"exact_duplicate": 2, "blur": 1}
    assert n_clean == 4
    assert results[0].recall_of("exact_duplicate", 2) == 1.0
    assert results[0].recall_of("blur", 1) == 0.0  # md5 抓不住 blur


# ---------- 渲染：Markdown 不崩 ----------


def test_render_markdown_runs(tmp_path):
    cleans = _clean(tmp_path, 2)
    dups = _labeled(cleans, "exact_duplicate", ["c0"])
    samples = cleans + dups
    results, totals, n_clean = evaluate_all([OperatorSpec(op="md5_exact")], samples)
    md = render_pr_markdown(results, totals, n_clean, "test")
    assert "精确重复" in md or "exact_duplicate" in md
    assert "md5_exact" in md


# ---------- 目标映射完备性 ----------


def test_all_registered_operators_have_target_entry():
    from mm_curation.operators.registry import available_operators

    missing = [op for op in available_operators() if op not in OPERATOR_TARGETS]
    # 未登记主靶的算子需显式登记（哪怕空 list），避免报告里「主靶」列无据
    assert not missing, f"OPERATOR_TARGETS 缺少: {missing}"


# ---------- run_operator：单样本 vs 批量分支 ----------


def test_run_operator_single_and_batch_branch(tmp_path):
    samples = _clean(tmp_path, 3)
    dup = Sample(id="dup", image_path=samples[0].image_path, caption=samples[0].caption)
    samples = samples + [dup]
    # 单样本算子走 __call__ 分支
    op_s = build_operator({"op": "text_length", "params": {"min": 5}})
    kept, dropped = run_operator(op_s, samples)
    assert len(kept) + len(dropped) == len(samples)
    # 批量算子走 run_batch 分支
    op_b = build_operator({"op": "md5_exact"})
    kept, dropped = run_operator(op_b, samples)
    assert len(dropped) == 1 and dropped[0].id == "dup"


# ---------- 真实数据冒烟 ----------


CONTAMINATED = Path("data/interim/contaminated/samples.jsonl")


@pytest.mark.skipif(not CONTAMINATED.exists(), reason="需先 make data")
def test_real_data_independent_pr_smoke():
    """真实数据冒烟：每个算子独立跑不崩，recall 在 [0,1]。"""
    samples = [
        Sample.from_dict(json.loads(line))
        for line in CONTAMINATED.read_text(encoding="utf-8").splitlines()
    ]
    results, totals, n_clean = evaluate_all(
        [OperatorSpec(op="text_length", params={"min": 5, "max": 100})], samples
    )
    r = results[0]
    assert r.n_in == len(samples)
    assert n_clean == 1620
    for t, total in totals.items():
        rec = r.recall_of(t, total)
        assert rec is None or 0.0 <= rec <= 1.0
