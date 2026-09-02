"""漏斗执行器测试：逐级数字、批量算子吃存活集、丢弃溯源、分数统计。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mm_curation.operators.base import Sample
from mm_curation.pipeline import OperatorSpec, PipelineConfig, run_funnel


def _config(*specs: OperatorSpec) -> PipelineConfig:
    return PipelineConfig(
        name="test_funnel",
        raw_jsonl=Path("unused"),
        output_dir=Path("unused"),
        operators=list(specs),
    )


def _samples(tmp_path: Path, captions: list[str], sid_prefix="s") -> list[Sample]:
    """每个样本一张独立小图（颜色各异）——批量去重算子依赖图像互不相同。"""
    from PIL import Image

    out = []
    for i, c in enumerate(captions):
        img = tmp_path / f"img{i}.jpg"
        Image.new("RGB", (64, 48), (i * 40 % 255, 80, 160)).save(img, "JPEG")
        out.append(Sample(id=f"{sid_prefix}{i}", image_path=str(img), text=c))
    return out


def test_funnel_stage_numbers_and_traceability(tmp_path):
    samples = _samples(
        tmp_path,
        [
            "一只猫坐在沙发上休息",  # 通过两级
            "两只狗",  # text_length(min=5) 通过（3字? -> 3 < 5 被扔）
            "都市夜景灯火辉煌",  # 通过
            "hello world",  # chinese_ratio 被扔
            "微风轻拂湖面泛起波纹",  # 通过
        ],
    )
    config = _config(
        OperatorSpec(op="text_length", params={"min": 5, "max": 100}),
        OperatorSpec(op="chinese_ratio", params={"min": 0.3}),
    )
    result = run_funnel(samples, config)

    assert [(s.op, s.n_in, s.n_out, s.dropped) for s in result.stats] == [
        ("text_length", 5, 4, 1),
        ("chinese_ratio", 4, 3, 1),
    ]
    assert len(result.kept) == 3
    # 丢弃溯源：每条能对上正确的算子
    drop_map = {s.id: op for op, s in result.dropped}
    assert drop_map == {"s1": "text_length", "s3": "chinese_ratio"}
    # 单样本算子记录了分数分布（text_length 分数即字符数；"hello world"=11
    # 在本stage只被打分不被扔，所以 max=11）
    s0 = result.stats[0]
    assert s0.score_min == 3 and s0.score_max == 11 and not s0.batch


def test_batch_op_receives_survivors_not_full_input(tmp_path):
    samples = _samples(
        tmp_path,
        [
            "一只猫坐在沙发上休息",
            "两只狗",  # 会被 text_length 扔掉，不应进入 md5 阶段
            "都市夜景灯火辉煌",
        ],
    )
    # 复制 s0 的图与 caption 构造精确重复。γ 起簇代表按 id 规范序选择
    # （"dup" < "s0"，见 sdk.run_batch_mixed_modality）——被扔的是 s0
    dup = Sample(id="dup", image_path=samples[0].image_path, text=samples[0].text)
    samples.append(dup)

    config = _config(
        OperatorSpec(op="text_length", params={"min": 5, "max": 100}),
        OperatorSpec(op="md5_exact"),
    )
    result = run_funnel(samples, config)

    s0, s1 = result.stats
    assert (s0.op, s0.n_in, s0.n_out) == ("text_length", 4, 3)
    # 关键断言：批量算子的 n_in 是上一级存活数 3，而不是原始 4
    assert (s1.op, s1.n_in, s1.n_out, s1.dropped, s1.batch) == ("md5_exact", 3, 2, 1, True)
    assert s1.score_min is None and s1.score_p50 is None  # 批量算子无分数
    assert [s.id for s in result.kept] == ["dup", "s2"]
    assert [(op, s.id) for op, s in result.dropped] == [("text_length", "s1"), ("md5_exact", "s0")]


def test_pass_rate_and_empty_stage(tmp_path):
    samples = _samples(tmp_path, ["一张图"])
    config = _config(OperatorSpec(op="text_length", params={"min": 5}))
    result = run_funnel(samples, config)
    assert result.stats[0].pass_rate == 0.0
    assert result.kept == []
    # 再跑一级空漏斗不崩（n_in=0 时 pass_rate 定义为 0）
    config2 = _config(
        OperatorSpec(op="text_length", params={"min": 5}),
        OperatorSpec(op="chinese_ratio", params={"min": 0.3}),
    )
    result2 = run_funnel(samples, config2)
    assert result2.stats[1].n_in == 0 and result2.stats[1].pass_rate == 0.0


CONTAMINATED = Path("data/interim/contaminated/samples.jsonl")


@pytest.mark.skipif(not CONTAMINATED.exists(), reason="需先 make data")
def test_real_data_smoke():
    """真实污染数据冒烟：漏斗不崩、每级 n_out <= n_in、总数守恒。"""
    samples = [
        Sample.from_dict(json.loads(line))
        for line in CONTAMINATED.read_text(encoding="utf-8").splitlines()[:100]
    ]
    config = PipelineConfig(
        name="smoke",
        raw_jsonl=CONTAMINATED,
        output_dir=Path("unused"),
        operators=[
            OperatorSpec(op="text_length", params={"min": 5, "max": 100}),
            OperatorSpec(op="chinese_ratio", params={"min": 0.3}),
            OperatorSpec(op="md5_exact"),
        ],
    )
    result = run_funnel(samples, config)
    prev_out = len(samples)
    for stat in result.stats:
        assert stat.n_in == prev_out
        assert stat.n_out <= stat.n_in
        prev_out = stat.n_out
    assert len(result.kept) + len(result.dropped) == len(samples)  # 总数守恒
