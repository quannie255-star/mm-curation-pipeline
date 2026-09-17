"""工业算子测试：每算子覆盖 正常通过/正常拒绝/边界值/错误输入 + 批量确定性。"""

from __future__ import annotations

import copy

from curation_eval import Sample, SensorSample

from mm_curation.data.sensor_synth import SENTINEL, generate_corpus
from mm_curation.operators.industrial_quality import (
    FaultVsMaintenanceOp,
    SensorDriftOp,
    SensorRangeOp,
    SensorStuckOp,
    UnitConsistencyOp,
)

MIN = {"min": 1.0}
CORPUS = generate_corpus(seed=42, scale=0.1)  # 10 槽/通道 ≈ 120 窗 + 6 事件


def _window(**overrides):
    p = {
        "record_type": "reading_window",
        "device_id": "pump99",
        "device_type": "pump",
        "channel": "pressure",
        "unit": "MPa",
        "sampling_hz": 1,
        "operating_mode": "run",
        "window_start": "2026-06-01T00:00:00+00:00",
        "window_end": "2026-06-01T00:04:16+00:00",
        "readings": [1.2 + 0.001 * (i % 7) for i in range(256)],
    }
    p.update(overrides)
    return p


def _win(**overrides):
    return SensorSample.from_payload(_window(**overrides))


# --- sensor_stuck（单样本）---------------------------------------------------


def test_stuck_normal_window_passes():
    s = SensorStuckOp(**MIN)(_win())
    assert s is not None and s.meta["score:sensor_stuck"] == 1.0


def test_stuck_flat_non_idle_dropped():
    flat = _window(operating_mode="run")
    flat["readings"] = [1.2] * 256
    assert SensorStuckOp(**MIN)(_win(**{"operating_mode": "run", "readings": [1.2] * 256})) is None


def test_stuck_idle_flat_passes():
    """边界：idle 工况的平坦窗是合法停机——形态相同、结论相反。"""
    s = SensorStuckOp(**MIN)(_win(operating_mode="idle", readings=[0.1] * 256))
    assert s is not None and s.meta["score:sensor_stuck"] == 1.0


def test_stuck_near_flat_not_exact_passes():
    """边界：极差 1e-6 > 1e-9，是真实信号不是卡死。"""
    s = SensorStuckOp(**MIN)(_win(readings=[1.0, 1.0 + 1e-6] * 128))
    assert s is not None and s.meta["score:sensor_stuck"] == 1.0


def test_stuck_event_and_non_sensor_inputs():
    op = SensorStuckOp(**MIN)
    ev = SensorSample.from_payload({**_window(), "record_type": "maintenance_event"})
    assert op(ev) is not None
    s = op(Sample(id="t1", text="普通文本"))
    assert s is not None and s.meta["score:sensor_stuck"] is None


# --- sensor_range（单样本）---------------------------------------------------


def test_range_clean_passes():
    s = SensorRangeOp(**MIN)(_win())
    assert s is not None and s.meta["score:sensor_range"] == 1.0


def test_range_partial_violation_dropped():
    bad = _window()
    bad["readings"][0] = 99.0  # pump pressure 上限 2.5
    assert SensorRangeOp(**MIN)(_win(**bad)) is None


def test_range_unknown_channel_not_judged():
    s = SensorRangeOp(**MIN)(_win(channel="torque"))
    assert s is not None and s.meta["score:sensor_range"] == 1.0


def test_range_non_sensor_scores_none_kept():
    s = SensorRangeOp(**MIN)(Sample(id="t2", text="普通文本"))
    assert s is not None and s.meta["score:sensor_range"] is None


# --- sensor_drift（批量）-----------------------------------------------------


def _run_drift(samples, **params):
    op = SensorDriftOp(min=1.0, **params)
    survivors = op.run_batch(copy.deepcopy(samples))
    return op, {s.id: s for s in survivors}


def test_drift_clean_corpus_all_pass():
    """干净语料零误杀：AR(1) 平稳序列 + 同工况比较，changeover 偏移不跨组。"""
    _, kept = _run_drift(CORPUS)
    assert len(kept) == len(CORPUS)


def test_drift_cal_offset_dropped():
    """校准漂移：注入 +5σ 偏移的窗（基线之后）被判漂移。"""
    from mm_curation.data.sensor_synth import SENTINEL as _S  # noqa: F401  占位防误删
    from mm_curation.operators.industrial_quality import SensorDriftOp as _D  # noqa: F401

    corpus = sorted(CORPUS, key=lambda s: s.id)
    wins = [
        s for s in corpus
        if s.meta["sensor_record_type"] == "reading_window"
        and s.meta["device_id"] == "pump01"
        and s.meta["channel"] == "flow"
        and s.meta["operating_mode"] == "run"
    ]
    assert len(wins) > 6, "小规模语料需保证 drift 组内基线后仍有窗口"
    import json

    target_p = json.loads(wins[-1].text)
    target_p["readings"] = [v + 5.0 for v in target_p["readings"]]  # flow σ=1 → 偏移 5σ
    injected = SensorSample.from_payload(target_p)
    _, kept = _run_drift([s for s in CORPUS if s.id != injected.id] + [injected])
    assert injected.id not in kept


def test_drift_changeover_offset_not_flagged():
    """跨工况不比较：changeover 均值偏移是合法业务差异，即使幅度大。"""
    import json

    corpus = sorted(CORPUS, key=lambda s: s.id)
    wins = [
        s for s in corpus
        if s.meta["sensor_record_type"] == "reading_window"
        and s.meta["device_id"] == "pump01"
        and s.meta["channel"] == "flow"
    ]
    change = next(s for s in wins if s.meta["operating_mode"] == "changeover")
    p = json.loads(change.text)
    p["readings"] = [v + 50.0 for v in p["readings"]]
    shifted = SensorSample.from_payload(p)
    _, kept = _run_drift([s for s in CORPUS if s.id != shifted.id] + [shifted])
    assert shifted.id in kept


def test_drift_small_group_no_baseline_passes_and_shuffle_deterministic():
    import random as _random

    outs = []
    for seed in (0, 1):
        items = CORPUS.copy()
        _random.Random(seed).shuffle(items)
        _, kept = _run_drift(items)
        outs.append(sorted(kept))
    assert outs[0] == outs[1]


def test_drift_non_sensor_scores_none_kept():
    _, kept = _run_drift(CORPUS + [Sample(id="t3", text="文本")])
    assert kept["t3"].meta["score:sensor_drift"] is None


# --- unit_consistency（批量）-------------------------------------------------


def _run_unit(samples):
    op = UnitConsistencyOp(min=1.0)
    survivors = op.run_batch(copy.deepcopy(samples))
    return op, {s.id: s for s in survivors}


def test_unit_consistent_group_passes():
    _, kept = _run_unit(CORPUS)
    assert len(kept) == len(CORPUS)  # 干净语料同测点单位一致


def test_unit_swap_dropped():
    import json

    corpus = sorted(CORPUS, key=lambda s: s.id)
    target = next(
        s for s in corpus
        if s.meta["sensor_record_type"] == "reading_window"
        and s.meta["channel"] == "pressure"
        and s.meta["device_type"] == "pump"
    )
    p = json.loads(target.text)
    p["unit"] = "bar"  # 数值不换算，标签替换
    swapped = SensorSample.from_payload(p)
    _, kept = _run_unit([s for s in CORPUS if s.id != swapped.id] + [swapped])
    assert swapped.id not in kept


def test_unit_event_sample_passes():
    events = [
        s for s in CORPUS if s.meta["sensor_record_type"] == "maintenance_event"
    ]
    assert events
    _, kept = _run_unit(events)
    assert all(s.meta["score:unit_consistency"] == 1.0 for s in kept.values())


def test_unit_shuffle_deterministic():
    import random as _random

    outs = []
    for seed in (0, 1):
        items = CORPUS.copy()
        _random.Random(seed).shuffle(items)
        _, kept = _run_unit(items)
        outs.append(sorted(kept))
    assert outs[0] == outs[1]


# --- fault_vs_maintenance（批量）---------------------------------------------


def _run_fault(samples):
    op = FaultVsMaintenanceOp(min=1.0)
    survivors = op.run_batch(copy.deepcopy(samples))
    return op, {s.id: s for s in survivors}


def test_fault_clean_corpus_all_pass():
    _, kept = _run_fault(CORPUS)
    assert len(kept) == len(CORPUS)  # 干净语料无哨兵窗


def test_fault_unplanned_silence_dropped():
    """计划外静默（全哨兵）→ 真实链路故障，丢弃。"""
    import json

    corpus = sorted(CORPUS, key=lambda s: s.id)
    target = next(
        s for s in corpus
        if s.meta["sensor_record_type"] == "reading_window"
    )
    p = json.loads(target.text)
    p["readings"] = [SENTINEL] * len(p["readings"])
    silent = SensorSample.from_payload(p)
    _, kept = _run_fault([s for s in CORPUS if s.id != silent.id] + [silent])
    assert silent.id not in kept


def test_fault_in_plan_silence_passes():
    """计划内静默（哨兵窗落在检修事件窗内）→ 合法放行——归因架构的核心。"""
    ev = SensorSample.from_payload(
        {
            "record_type": "maintenance_event",
            "device_id": "pump99",
            "device_type": "pump",
            "window_start": "2026-06-01T00:00:00+00:00",
            "window_end": "2026-06-01T02:00:00+00:00",
        }
    )
    in_plan = _win(window_start="2026-06-01T01:00:00+00:00")
    in_plan_p = SensorSample.to_payload(in_plan)
    in_plan_p["readings"] = [SENTINEL] * len(in_plan_p["readings"])
    silent = SensorSample.from_payload(in_plan_p)
    outside = _win(window_start="2026-06-02T00:00:00+00:00")
    _, kept = _run_fault([ev, silent, outside])
    assert silent.id in kept  # 计划内哨兵 = 合法静默
    assert kept[silent.id].meta["score:fault_vs_maintenance"] == 1.0
    assert outside.id in kept


def test_fault_non_sensor_scores_none_kept():
    _, kept = _run_fault(CORPUS + [Sample(id="t4", text="文本")])
    assert kept["t4"].meta["score:fault_vs_maintenance"] is None
