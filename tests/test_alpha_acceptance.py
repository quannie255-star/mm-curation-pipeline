"""V2 α 阶段验收测试（docs/design_tables.md 验收标准 A1-A7 的主仓库侧落点）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mm_curation.operators.base import (
    BatchOperator,
    Executor,
    Operator,
    Sample,
    StageStat,
)
from mm_curation.pipeline import OperatorSpec, PipelineConfig, run_funnel

# A5 的守卫对象：协议类型不允许在主仓库本地定义
_PROTOCOL_PATTERN = r"^class (Sample|Operator|BatchOperator|Executor|StageStat|FunnelResult)\b"


def _img(tmp_path: Path, name: str, color) -> str:
    from PIL import Image

    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(p)
    return str(p)


def _mixed_ten(tmp_path: Path) -> list[Sample]:
    """验收样本集：5 图文（真实小图）+ 5 纯文本（design_tables A1/A2）。"""
    out = []
    for i in range(5):
        out.append(Sample(
            id=f"ic{i}", text=f"图文样本第{i}号的描述文字",
            image_path=_img(tmp_path, f"ic{i}.jpg", (i * 40, 100, 150)),
        ))
    for i in range(5):
        out.append(Sample(id=f"ta{i}", text=f"纯文本语料第{i}段的正文内容足够长"))
    return out


def test_a1_sample_round_trip_and_legacy(tmp_path):
    s = Sample(id="a1", text="一只猫", image_path="img/1.jpg",
               meta={"k": 1}, labels={"dirty": "blur"})
    assert Sample.from_dict(s.to_dict()) == s
    legacy = Sample.from_dict({"id": "a2", "image_path": "img/2.jpg",
                               "caption": "老格式", "meta": {}, "labels": {}})
    assert legacy.text == "老格式" and legacy.modality == "image_caption"


def test_a2_mixed_funnel_with_modality_skips(tmp_path):
    samples = _mixed_ten(tmp_path)
    config = PipelineConfig(
        name="alpha_acceptance",
        raw_jsonl=Path("unused"),
        output_dir=Path("unused"),
        operators=[
            OperatorSpec(op="text_length", params={"min": 5}),   # 双模态：10 条全评
            OperatorSpec(op="resolution", params={"min": 32}),   # 仅图文：评 5 跳 5
            OperatorSpec(op="md5_exact"),                        # 仅图文批量：评 5 跳 5
        ],
    )
    result = run_funnel(samples, config)

    tl, res, md5 = result.stats
    assert (tl.n_in, tl.n_out, tl.skipped) == (10, 10, 0)
    assert (res.n_in, res.n_out, res.skipped) == (10, 10, 5)
    assert (md5.n_in, md5.n_out, md5.skipped, md5.batch) == (10, 10, 5, True)
    # 顺序保持 + 全部存活（样本互不相同且长度达标）
    assert [s.id for s in result.kept] == [s.id for s in samples]
    # 图文样本有两级分数；纯文本只有 text_length 一级
    ic = next(s for s in result.kept if s.id == "ic0")
    ta = next(s for s in result.kept if s.id == "ta0")
    assert "score:resolution" in ic.meta and "score:resolution" not in ta.meta


def test_a2_config_fail_fast_on_disjoint_modality(tmp_path):
    samples = _mixed_ten(tmp_path)[:5]  # 只留图文
    text_only = [s for s in samples]
    for i, s in enumerate(text_only):
        s.image_path = None
        s.modality = "text_article"
    config = PipelineConfig(
        name="bad", raw_jsonl=Path("u"), output_dir=Path("u"),
        operators=[OperatorSpec(op="resolution", params={"min": 32})],
    )
    with pytest.raises(ValueError, match="完全不相交"):
        run_funnel(text_only, config)  # 图像算子配纯文本数据 → 启动即报错


def test_a3_registry_validation():
    from curation_eval import CostClass, OperatorMeta, register_operator

    with pytest.raises(ValueError, match="蕴含"):
        OperatorMeta(name="bad1", modalities=frozenset({"text_article"}),
                     required_fields=frozenset({"image_path"}),
                     cost_class=CostClass.RULE)
    from curation_eval import unregister

    @register_operator(name="alpha_probe", modalities=frozenset({"text_article"}),
                       required_fields=frozenset({"text"}), cost_class=CostClass.RULE)
    class First:
        pass

    with pytest.raises(ValueError, match="冲突"):
        @register_operator(name="alpha_probe", modalities=frozenset({"text_article"}),
                           required_fields=frozenset({"text"}), cost_class=CostClass.RULE)
        class Second:
            pass

    unregister("alpha_probe")  # 探针不进全局注册表（污染 guard 测试）


def test_a4_executor_placeholder():
    class _E(Executor):
        def run(self, ops, samples):  # pragma: no cover
            raise AssertionError

    with pytest.raises(NotImplementedError, match="二期"):
        _E().reduce([])


def test_a5_single_source_guard():
    """协议类型不允许在主仓库本地定义（A5 grep 守卫）。"""
    import re

    violations = []
    for p in Path("src/mm_curation").rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(_PROTOCOL_PATTERN, line):
                violations.append(f"{p}:{i}: {line.strip()}")
    assert not violations, f"协议类型本地定义违反单一来源: {violations}"
    base = Path("src/mm_curation/operators/base.py").read_text(encoding="utf-8")
    assert "from curation_eval import" in base  # 必须经包导入


BASELINE = {"n_kept": 1897, "dirty_recall": 0.43, "clean_killed": 0}
LEGACY = Path("data/interim/contaminated/samples.jsonl")


@pytest.mark.skipif(not LEGACY.exists(), reason="需先 make data")
def test_a6_legacy_reconciliation():
    """T11：迁移后轻量漏斗（4 确定性算子）在 legacy 数据上与基线对账 ≤1pp。"""
    rows = [json.loads(line) for line in LEGACY.read_text(encoding="utf-8").splitlines()]
    samples = [Sample.from_dict(r) for r in rows]  # from_dict 兼容 caption 键
    config = PipelineConfig(
        name="legacy_recon", raw_jsonl=LEGACY, output_dir=Path("unused"),
        operators=[
            OperatorSpec(op="text_length", params={"min": 5, "max": 100}),
            OperatorSpec(op="chinese_ratio", params={"min": 0.3}),
            OperatorSpec(op="char_repetition", params={"min": 0.8}),
            OperatorSpec(op="md5_exact"),
        ],
    )
    result = run_funnel(samples, config)
    kept_ids = {s.id for s in result.kept}
    n_dirty = sum(1 for r in rows if r["labels"])
    dirty_kept = sum(1 for r in rows if r["labels"] and r["id"] in kept_ids)
    clean_killed = sum(1 for r in rows if not r["labels"] and r["id"] not in kept_ids)

    assert len(result.kept) == BASELINE["n_kept"]  # 确定性算子应精确一致
    assert abs((n_dirty - dirty_kept) / n_dirty - BASELINE["dirty_recall"]) <= 0.01
    assert clean_killed == BASELINE["clean_killed"]
