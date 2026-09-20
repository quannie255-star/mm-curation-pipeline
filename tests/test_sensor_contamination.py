"""工业污染器测试：确定性、原始样本不可变、五类注入靶向命中对应算子、干净侧零误杀。"""

from __future__ import annotations

from pathlib import Path

from curation_eval import ContaminationPlan, Sample

from mm_curation.data.sensor_synth import generate_corpus
from mm_curation.operators.industrial_quality import (
    FaultVsMaintenanceOp,
    SensorDriftOp,
    SensorRangeOp,
    SensorStuckOp,
    UnitConsistencyOp,
)

KINDS = {
    "sensor_cal_offset": 1.0,
    "sensor_flatline": 1.0,
    "sensor_out_of_range": 1.0,
    "sensor_unit_swap": 1.0,
    "sensor_unplanned_silence": 1.0,
}
# 单样本算子：`sensor_range` 仍是 Operator。
# `sensor_stuck` 已下沉为 BatchOperator（R1：判据需要通道内全量视角），
# 所以它归到 BATCH_OPS —— 用 `op(sample)` 调它会直接抛 TypeError。
SINGLE_OPS = {
    "sensor_out_of_range": SensorRangeOp(min=1.0),
}
BATCH_OPS = {
    "sensor_flatline": SensorStuckOp(min=1.0),
    "sensor_cal_offset": SensorDriftOp(min=1.0),
    "sensor_unit_swap": UnitConsistencyOp(min=1.0),
    "sensor_unplanned_silence": FaultVsMaintenanceOp(min=1.0),
}
CORPUS = generate_corpus(seed=42, scale=0.1)


def _contaminate(seed: int):
    plan = ContaminationPlan(inject_rate=0.6, seed=seed, kinds=dict(KINDS))
    mixed, manifest = plan.run(CORPUS, Path("data/tmp_sensor_images"))
    return mixed, manifest


def test_plan_deterministic_same_seed():
    a, ma = _contaminate(42)
    b, mb = _contaminate(42)
    assert [s.to_dict() for s in a] == [s.to_dict() for s in b]
    assert ma == mb
    assert ma["counts"] and sum(ma["counts"].values()) == ma["n_injected"]


def test_originals_never_modified():
    mixed, _ = _contaminate(42)
    assert [s.to_dict() for s in mixed[: len(CORPUS)]] == [s.to_dict() for s in CORPUS]
    injected = mixed[len(CORPUS):]
    assert injected and all(s.labels.get("dirty") in KINDS for s in injected)


def test_five_kinds_present_and_hit_primary_operator():
    mixed, manifest = _contaminate(42)
    injected = mixed[len(CORPUS):]
    assert set(manifest["counts"]) == set(KINDS)  # inject_rate 足够大 + 等权 → 五类齐

    for kind, op in SINGLE_OPS.items():
        targets = [s for s in injected if s.labels["dirty"] == kind]
        assert targets, f"{kind} 无注入样本"
        missed = [s.id for s in targets if op(Sample.from_dict(s.to_dict())) is not None]
        assert not missed, f"{kind} 被主靶算子漏检: {missed}"

    for kind, op in BATCH_OPS.items():
        targets = [s for s in injected if s.labels["dirty"] == kind]
        kept = {s.id for s in op.run_batch(mixed)}
        missed = [s.id for s in targets if s.id in kept]
        assert not missed, f"{kind} 未被批量主靶拦截: {missed}"


def test_clean_corpus_zero_false_kill_all_ops():
    """干净语料在全部五个算子上零误杀（含时序平稳性与同工况比较的前提）。"""
    for op in SINGLE_OPS.values():
        assert all(op(Sample.from_dict(s.to_dict())) is not None for s in CORPUS)
    for op in BATCH_OPS.values():
        assert len(op.run_batch(CORPUS)) == len(CORPUS)
