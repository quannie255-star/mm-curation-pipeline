"""工业算子测试：每算子覆盖 正常通过/正常拒绝/边界值/错误输入 + 批量确定性。

R1/R3 把 `sensor_stuck` 从单样本算子改成批量算子（判据需要通道内全量视角），
本文件对应段落的调用方式随之下沉到 `run_batch`；同时补上 R3 新增三档
（设备级停机 / 跨度缺口 / 停机放行）与 R5 新增两个真实形态靶子的验收。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

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
_EPOCH = datetime(2026, 6, 1)


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


def _seq(n, *, channel="pressure", device_id="pump99", readings=None,
         mode="run", offset_minutes=0):
    """同 `(device, channel)` 的 n 个连续窗（window_start 每分钟递进）。

    批量算子的判据全部建立在**组内多窗**之上，单窗测不出任何东西——这是 R1
    把 `sensor_stuck` 改成批量算子的直接后果，测试的构造方式也必须跟着变。
    """
    out = []
    for i in range(n):
        start = _EPOCH + timedelta(minutes=offset_minutes + i)
        base = _window(
            channel=channel, device_id=device_id, operating_mode=mode,
            window_start=start.isoformat(),
            window_end=(start + timedelta(seconds=256)).isoformat(),
        )
        if readings is not None:
            base["readings"] = list(readings)
        out.append(base)
    return out


def _payloads_to_samples(payloads):
    return [SensorSample.from_payload(p) for p in payloads]


def _scores(samples, op):
    """跑一遍批量算子，按输入顺序取每个样本的分数（跑完即读，含被丢弃的）。"""
    op.run_batch(samples)
    return [s.meta.get(f"score:{op.name}") for s in samples]


HEALTHY = [1.2 + 0.1 * (i % 7) for i in range(256)]  # σ≈0.21
TINY = [1.5 + 0.001 * (i % 2) for i in range(256)]  # σ≈5e-4 ≪ 0.2×0.21


# --- sensor_stuck（批量）-----------------------------------------------------


def test_stuck_normal_window_passes():
    ss = _payloads_to_samples(_seq(3))
    assert _scores(ss, SensorStuckOp(**MIN)) == [1.0] * 3


def test_stuck_flat_non_idle_dropped():
    """严格平坦（极差 == 0）→ 立即判卡死，**不需要连续性**。"""
    ss = _payloads_to_samples(_seq(3) + _seq(1, readings=[1.2] * 256, offset_minutes=3))
    scores = _scores(ss, SensorStuckOp(**MIN))
    assert scores[:3] == [1.0] * 3 and scores[3] == 0.0
    assert ss[3].meta["evidence:sensor_stuck"]["rule"] == "exact_flat"


def test_stuck_flat_alone_on_uninformative_channel_is_none():
    """边界：该通道**一个健康窗都没有** → 无信息通道豁免，记 None（不是卡死）。

    这是 R1 第 1 档与第 2 档的分界：形态完全一样（都是严格平坦），
    结论相反取决于「该通道本来测不测得到东西」。
    """
    ss = _payloads_to_samples(_seq(4, channel="torque", readings=[7.0] * 256))
    assert _scores(ss, SensorStuckOp(**MIN)) == [None] * 4


def test_stuck_device_stop_is_none_not_stuck():
    """R3 第 0 档：同 (device, window_start) 下**多通道同步恒定** = 设备停机。

    它与单通道卡死形态相同、含义相反——区别只在**同步性**。所以两个通道
    同时冻结 → `None`（判据不适用），而单通道冻结仍是 `exact_flat` → `0.0`。
    """
    frozen = [1.2] * 256
    payloads = (
        _seq(3, channel="pressure")
        + _seq(3, channel="flow", offset_minutes=10)
        + _seq(1, channel="pressure", readings=frozen, offset_minutes=3)
        + _seq(1, channel="flow", readings=[2.0] * 256, offset_minutes=3)
    )
    ss = _payloads_to_samples(payloads)
    scores = _scores(ss, SensorStuckOp(**MIN))
    assert scores[6] is None and scores[7] is None  # 停机：不判
    assert ss[6].meta["evidence:sensor_stuck"]["rule"] == "machine_stop"
    assert ss[6].meta["evidence:sensor_stuck"]["frozen_channels"] == 2


def test_stuck_device_stop_excludes_sentinel_form():
    """边界：全通道**哨兵**冻结是链路静默（合成靶子），不能被停机档豁免。

    否则 `fault_vs_maintenance` 的合成召回直接掉——这是 R3 的一条红线。
    """
    sentinel = [SENTINEL] * 256
    payloads = _seq(2, channel="pressure") + _seq(1, channel="pressure",
                                                  readings=sentinel, offset_minutes=3)
    ss = _payloads_to_samples(payloads)
    assert _scores(ss, SensorStuckOp(**MIN))[2] == 0.0  # 没有被豁免


def test_stuck_idle_flat_passes():
    """边界：idle 工况的平坦窗是合法停机——形态相同、结论相反。"""
    ss = _payloads_to_samples(
        _seq(3) + _seq(1, readings=[0.1] * 256, mode="idle", offset_minutes=3)
    )
    assert _scores(ss, SensorStuckOp(**MIN))[3] == 1.0


def test_stuck_near_flat_not_exact_passes():
    """边界：极差 1e-6 —— 不是严格平坦，且单窗塌陷不够连续性门槛。"""
    ss = _payloads_to_samples(
        _seq(3) + _seq(1, readings=[1.0, 1.0 + 1e-6] * 128, offset_minutes=3)
    )
    assert _scores(ss, SensorStuckOp(**MIN))[3] == 1.0


def test_stuck_scale_collapse_needs_consecutive_run():
    """R1 第 3 档：抖动极小的**持续**塌陷——前 9 窗不够门槛，第 10 窗起判卡死。

    ⚠️ 构造上的隐含前提：塌陷窗必须是**少数**（这里 12/22）。参考尺度取健康窗
    σ 的 p75，若同通道内**非零**塌陷窗超过 75%，参考尺度会被塌陷窗自己拉低，
    塌陷档随即失效——这是该档的已知上界，不是测试的取巧（见 ENGINEERING_NOTES）。
    """
    payloads = _seq(10, readings=HEALTHY) + _seq(12, readings=TINY, offset_minutes=10)
    ss = _payloads_to_samples(payloads)
    scores = _scores(ss, SensorStuckOp(**MIN))[10:]
    assert scores[:9] == [1.0] * 9 and scores[9:] == [0.0] * 3
    assert ss[10 + 9].meta["evidence:sensor_stuck"]["rule"] == "scale_collapse"
    assert ss[10 + 9].meta["evidence:sensor_stuck"]["run_length"] == 10


def test_stuck_reference_scale_collapses_when_most_windows_are_collapsed():
    """把上一条的隐含前提**钉成断言**：塌陷窗占多数（12/15）时参考尺度被拉低，
    塌陷档抓不到 → 全通过。知道失效边界在哪，比假装它不存在强。"""
    payloads = _seq(3, readings=HEALTHY) + _seq(12, readings=TINY, offset_minutes=5)
    ss = _payloads_to_samples(payloads)
    assert _scores(ss, SensorStuckOp(**MIN))[3:] == [1.0] * 12


def test_stuck_event_and_non_sensor_inputs():
    ev = SensorSample.from_payload({**_window(), "record_type": "maintenance_event"})
    txt = Sample(id="t1", text="普通文本")
    ss = [ev, txt]
    SensorStuckOp(**MIN).run_batch(ss)
    assert ev.meta["score:sensor_stuck"] == 1.0
    assert txt.meta["score:sensor_stuck"] is None


# --- sensor_range（单样本）---------------------------------------------------


def test_range_clean_passes():
    s = SensorRangeOp(**MIN)(_win())
    assert s is not None and s.meta["score:sensor_range"] == 1.0


def test_range_partial_violation_dropped():
    bad = _window()
    bad["readings"][0] = 99.0  # pump pressure 上限 2.5
    assert SensorRangeOp(**MIN)(_win(**bad)) is None


def test_range_unknown_channel_not_judged():
    """R2：量程表外通道记 `None`（**没评**），不再记 1.0（伪装成通过）。

    真实数据集的 (device_type, channel) 基本都落在内嵌表外——旧行为会让报告
    读起来像「量程全部通过」，实际一次都没评过。
    """
    s = SensorRangeOp(**MIN)(_win(channel="torque"))
    assert s is not None and s.meta["score:sensor_range"] is None
    assert s.meta["score:sensor_range"] != 1.0
    assert SensorRangeOp(**MIN).explain(_win(channel="torque"), None) == {
        "channel_in_range_table": False,
        "reason": "no_range_entry",
        "device_type": "pump",
        "channel": "torque",
    }


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


# --- R3：fault_vs_maintenance 的另外两种真实静默形态 --------------------------


def _fault_scores(samples):
    ss = copy.deepcopy(samples)
    FaultVsMaintenanceOp(min=1.0).run_batch(ss)
    return ss


def test_fault_span_gap_not_judged_but_kept():
    """R3 形态 ③：窗内时间跨度 6 倍 → 跨接两个运行时段 → 记 None，**保留**。

    裁决不是 0.0（不是脏）：读数不构成一段连续观测，「这静默是不是故障」
    在该窗上没有定义。真实数据依据见 docs/design_tables.md §8.2。
    """
    payloads = _seq(1)
    start = _EPOCH
    payloads[0]["window_end"] = (start + timedelta(seconds=256 * 6)).isoformat()
    ss = _fault_scores(_payloads_to_samples(payloads))
    assert ss[0].meta["score:fault_vs_maintenance"] is None
    ev = ss[0].meta["evidence:sensor_fault_vs_maintenance"]
    assert ev["rule"] == "span_gap" and ev["span_ratio"] > 5.0
    assert ev["missing_slots"] > 1000  # 缺席槽位可计算，不是一句「有问题」


def test_fault_normal_span_still_judged():
    """边界：正常跨度（≈1.004）不得被当成缺口——否则真实轨满屏未评。"""
    ss = _fault_scores(_payloads_to_samples(_seq(2)))
    assert [s.meta["score:fault_vs_maintenance"] for s in ss] == [1.0, 1.0]
    assert "evidence:sensor_fault_vs_maintenance" not in ss[0].meta


def test_fault_machine_stop_passes_with_evidence():
    """R3 形态 ②：多通道同步冻结 = 设备停机 → 放行（不是链路故障）。

    这条也就是「为什么不能把停机判成计划外静默」的回归：判 0.0 会在 MetroPT-3
    上白丢 1337 条合法记录（故障标签 0/191、计划内 0/191）。
    """
    frozen = [1.2] * 256
    ss = _fault_scores(_payloads_to_samples([
        _window(channel="pressure", readings=frozen),
        _window(channel="flow", readings=frozen),
    ]))
    assert [s.meta["score:fault_vs_maintenance"] for s in ss] == [1.0, 1.0]
    assert ss[0].meta["evidence:sensor_fault_vs_maintenance"]["rule"] == "machine_stop"


def test_fault_single_channel_freeze_is_not_machine_stop():
    """边界：只有**一个**通道冻结不是设备停机（那是卡死，归 sensor_stuck）→ 本算子照常通过。"""
    ss = _fault_scores(_payloads_to_samples([_window(channel="pressure", readings=[1.2] * 256)]))
    assert ss[0].meta["score:fault_vs_maintenance"] == 1.0
    assert "evidence:sensor_fault_vs_maintenance" not in ss[0].meta


# --- R2：外接量程表 ----------------------------------------------------------


def test_range_external_table_overrides_and_misses(tmp_path):
    """外接量程表覆盖内嵌表同名键；表外通道仍记 None（留空 = 没评，不是通过）。"""
    p = tmp_path / "ranges.yaml"
    p.write_text(
        "pump:\n  torque: [0.0, 500.0]\n", encoding="utf-8"
    )
    op = SensorRangeOp(min=1.0, ranges_path=str(p))
    ok = op(_win(channel="torque", readings=[10.0] * 256))
    assert ok is not None and ok.meta["score:sensor_range"] == 1.0
    bad = op(_win(channel="torque", readings=[999.0] * 256))
    assert bad is None  # 超出外接表上限
    assert op.explain(_win(channel="torque"), 1.0)["range"] == [0.0, 500.0]
    # 形态不对的表必须直接报错，不许「猜一个」
    bad_shape = tmp_path / "bad.yaml"
    bad_shape.write_text("pump:\n  torque: 5\n", encoding="utf-8")
    try:
        SensorRangeOp(min=1.0, ranges_path=str(bad_shape))
    except ValueError as exc:
        assert "torque" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("形状不对的量程表应当直接抛错")


# --- R5：真实形态靶子（污染器 → 算子端到端）-----------------------------------
#
# 这三档判据在**合成语料上原本不可达**（语料里只有「严格平坦」和「哨兵」两种
# 形态），等于「加了判据但没有任何合成回归能证明它工作」。以下测试补上靶子。

FORMS_CORPUS = generate_corpus(seed=42, scale=1.0)  # 100 槽/通道，容得下 min_run=10


def _inject(kind, rate, corpus=None, **params):
    from pathlib import Path

    from curation_eval import ContaminationPlan

    corpus = corpus if corpus is not None else FORMS_CORPUS
    plan = ContaminationPlan(
        inject_rate=rate, seed=42, kinds={kind: 1.0},
        params={kind: params} if params else {},
    )
    mixed, manifest = plan.run(corpus, Path("data/tmp_sensor_forms"))
    return mixed, mixed[len(corpus):], manifest


def _std_of(values):
    m = sum(values) / len(values)
    return max(0.0, sum((v - m) ** 2 for v in values) / len(values)) ** 0.5


def _channel_ref(corpus):
    """与算子同口径的通道尺度参考：健康窗 σ 的 p75。"""
    groups: dict[tuple, list[float]] = {}
    for s in corpus:
        p = SensorSample.to_payload(s)
        if p.get("record_type") != "reading_window":
            continue
        groups.setdefault((p["device_id"], p["channel"]), []).append(_std_of(p["readings"]))
    return {
        k: sorted(v)[int(0.75 * (len(v) - 1))]
        for k, v in groups.items()
    }


def test_r5_noisy_flatline_is_near_flat_but_not_exact():
    """靶子形态正确性：抖动极小的卡死**不是**严格平坦 → `exact_flat` 档抓不到。

    断言用**相对口径**（该通道自己的尺度参考），因为注入器要适配 12 个通道的
    不同 σ（flow≈1 到 speed≈5），绝对值写死就会只在某一个通道上成立。
    """
    _, injected, manifest = _inject("sensor_noisy_flatline", 0.3)
    assert set(manifest["counts"]) == {"sensor_noisy_flatline"}
    ref = _channel_ref(FORMS_CORPUS)
    for s in injected:
        p = SensorSample.to_payload(s)
        vals = p["readings"]
        assert max(vals) - min(vals) > 0.0, "严格平坦就不是本靶子了"
        key = (p["device_id"], p["channel"])
        assert _std_of(vals) < 0.1 * ref[key], "抖动必须显著小于该通道的健康尺度"


def test_r5_noisy_flatline_is_recognized_by_collapse_tier():
    """形态归属：微小抖动卡死落在**塌陷档**，而不是严格平坦档。

    把连续性门槛设成 1（`min_run=1`）把「游程够不够」这个合成轨表达不了的
    维度摘掉，单独验「形态对不对」：命中率应接近满、且干净窗零误杀。
    """
    mixed, injected, _ = _inject("sensor_noisy_flatline", 0.3)
    ss = copy.deepcopy(mixed)
    SensorStuckOp(min=1.0, min_run=1).run_batch(ss)
    scores = {s.id: s.meta.get("score:sensor_stuck") for s in ss}
    dirty = {s.id for s in injected}
    caught = [i for i in dirty if scores[i] == 0.0]
    assert len(caught) / len(dirty) >= 0.9
    assert all(scores[s.id] == 1.0 for s in mixed if s.id not in dirty), "干净窗被误杀"
    rules = {
        ss[i].meta["evidence:sensor_stuck"]["rule"]
        for i in range(len(ss))
        if ss[i].meta.get("score:sensor_stuck") == 0.0
    }
    assert rules == {"scale_collapse"}, f"应当只由塌陷档命中，实际 {rules}"


def test_r5_collapse_tier_structurally_unreachable_via_contamination():
    """把一条结构性事实钉成断言：**「注入即复制」的合成轨无法表达「连续 N 窗」**。

    三条事实合起来决定了这件事，缺一条都不成立：
    ① 污染器复制供体、**保留原窗**（`test_originals_never_modified` 是本包的硬不变式）；
    ② 塌陷档要求同通道内按时间**连续 ≥min_run 窗**都是塌陷窗；
    ③ 排序键是 `(window_start, id)`，而注入副本与原窗**共享 window_start**
       → 原窗必然插在同时间戳的副本之前，把游程重置。
    实测：12 个通道的最长塌陷游程只有 3~5，门槛是 10 → 合成污染轨上永不触发。

    所以这一档的验收只有两条路，**都不是合成污染轨**：
    ① 直接构造序列的单测（`test_stuck_scale_collapse_needs_consecutive_run`）；
    ② 真实轨（MetroPT-3 上 300 次真脏命中**全部**来自该档）。

    这条断言的作用是防回归：如果哪天污染器改成「替换原窗」，这里会立刻报红，
    提示「合成轨现在可以表达连续窗了，该给塌陷档补端到端门禁了」。
    """
    mixed, injected, _ = _inject("sensor_noisy_flatline", 0.7)
    ss = copy.deepcopy(mixed)
    SensorStuckOp(min=1.0).run_batch(ss)  # 默认 min_run=10
    scores = {s.id: s.meta.get("score:sensor_stuck") for s in ss}
    dirty = {s.id for s in injected}
    assert not any(scores[i] == 0.0 for i in dirty), (
        "塌陷档在合成污染轨上触发了——污染语义或排序键变了，请同步更新本断言与门禁"
    )


def test_r5_sampling_stall_is_not_judged_and_not_dropped():
    """端到端：采样停摆 → 跨时段窗 → 记 None（**未评**）而不是丢弃。

    它测的是「诚实计量」而不是召回：混进召回去分母会凭空压低召回，
    所以这个 kind **不进** `eval_industrial.py` 的默认注入构成。
    """
    mixed, injected, _ = _inject("sensor_sampling_stall", 0.3, ratio=6.0)
    ss = copy.deepcopy(mixed)
    op = FaultVsMaintenanceOp(min=1.0)
    survivors = op.run_batch(ss)
    scores = {s.id: s.meta.get("score:fault_vs_maintenance") for s in ss}
    dirty = {s.id for s in injected}
    assert dirty
    assert all(scores[i] is None for i in dirty), "停摆窗应当全部记 None"
    kept = {s.id for s in survivors}
    assert dirty <= kept, "未评必须保留（None 的语义就是「不判」，不是「丢弃」）"


def test_r5_sampling_stall_does_not_disturb_clean_windows():
    """采样停摆注入只改 window_end，不改干净窗的任何字段。"""
    corpus = generate_corpus(seed=42, scale=0.1)
    mixed, _, _ = _inject("sensor_sampling_stall", 0.3, corpus=corpus, ratio=6.0)
    assert [s.to_dict() for s in mixed[: len(corpus)]] == [s.to_dict() for s in corpus]

