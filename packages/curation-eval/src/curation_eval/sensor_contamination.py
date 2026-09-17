"""工业传感器污染器（V5 α）：向合成时序语料注入「合规脏数据」。

供体重抽（fhir_contamination 同模式）：计划抽中的源样本未必满足靶算子的
检出前提（如 cal_offset 需要落在漂移基线窗之后的窗口、unplanned_silence
需要落在检修计划之外）。从 ctx.pool 重抽合适供体（走 ctx.rng，同 seed
确定性），plan 分配的 id/labels 不变，meta 随 payload 刷新。原始样本永远
不被修改（注入即复制）。

量程/单位换算表与主仓 mm_curation.data.sensor_synth 同源——本模块持同值
副本（包不反向依赖消费方），换表两边一起换。
"""

from __future__ import annotations

import json

from .contamination import Contaminator, Context, register

SENTINEL = -999.0
_BASELINE = 5  # 与 sensor_drift 的基线窗数同值（漂移注入需落在基线之后）

# (device_type, channel) -> (lo, hi)，与 sensor_synth.RANGE_TABLE 同值副本
_RANGE_TABLE = {
    ("pump", "flow"): (0.0, 120.0),
    ("pump", "pressure"): (0.0, 2.5),
    ("fan", "speed"): (0.0, 3600.0),
    ("fan", "vibration"): (0.0, 20.0),
    ("furnace", "temp"): (0.0, 1200.0),
    ("furnace", "pressure"): (0.0, 10.0),
}
_UNIT_SWAP = {"MPa": "bar"}  # 典型集成事故：单位标注替换但数值未换算


def _load(sample) -> dict:
    return json.loads(sample.text)


def _store(sample, payload: dict) -> None:
    sample.text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    meta = sample.meta
    meta["sensor_record_type"] = payload.get("record_type")
    meta["device_id"] = payload.get("device_id")
    meta["device_type"] = payload.get("device_type", "")
    meta["window_start"] = payload.get("window_start")
    meta["window_end"] = payload.get("window_end")
    if payload.get("record_type") == "reading_window":
        meta["channel"] = payload["channel"]
        meta["unit"] = payload["unit"]
        meta["sampling_hz"] = payload["sampling_hz"]
        meta["operating_mode"] = payload["operating_mode"]


def _draw_donor(ctx: Context, predicate) -> dict:
    pool = [s for s in ctx.pool if predicate(_load(s))]
    if not pool:
        raise ValueError("污染器供体池为空（语料不满足注入前提）")
    return _load(pool[ctx.rng.randrange(len(pool))])


def _is_reading(payload: dict) -> bool:
    return payload.get("record_type") == "reading_window"


def _beyond_baseline(payload: dict, pool_payloads: list[dict]) -> bool:
    """候选窗同组（设备×通道×工况）内，早于它的窗口数 ≥ 基线数——保证
    sensor_drift 有基线可比（否则注入检不出，白注入）。"""
    key = (payload["device_id"], payload["channel"], payload["operating_mode"])
    start = payload["window_start"]
    earlier = sum(
        1
        for p in pool_payloads
        if _is_reading(p)
        and (p["device_id"], p["channel"], p["operating_mode"]) == key
        and p["window_start"] < start
    )
    return earlier >= _BASELINE


@register("sensor_cal_offset")
class SensorCalOffset(Contaminator):
    """校准漂移：窗口读数整体加系统性偏移（偏移量 = 窗内 σ 的 5 倍，
    远超 drift 判定阈 3σ）。"""

    def apply(self, sample, ctx: Context):
        pool_payloads = [_load(s) for s in ctx.pool]

        def ok(p):
            return _is_reading(p) and _beyond_baseline(p, pool_payloads)

        payload = _load(sample)
        if not ok(payload):
            payload = _draw_donor(ctx, ok)
        readings = payload["readings"]
        mean = sum(readings) / len(readings)
        std = max(1e-9, (sum((v - mean) ** 2 for v in readings) / len(readings)) ** 0.5)
        offset = 5.0 * std + 1.0
        payload["readings"] = [round(v + offset, 4) for v in readings]
        _store(sample, payload)
        return sample


@register("sensor_flatline")
class SensorFlatline(Contaminator):
    """卡死：非 idle 工况窗塞恒定读数（max-min=0）。"""

    def apply(self, sample, ctx: Context):
        payload = _load(sample)

        def ok(p):
            return _is_reading(p) and p["operating_mode"] != "idle"

        if not ok(payload):
            payload = _draw_donor(ctx, ok)
        payload["readings"] = [payload["readings"][0]] * len(payload["readings"])
        _store(sample, payload)
        return sample


@register("sensor_out_of_range")
class SensorOutOfRange(Contaminator):
    """超量程：头两个读数改到量程外（hi + 10 + i），部分越界形态。"""

    def apply(self, sample, ctx: Context):
        payload = _load(sample)

        def ok(p):
            return _is_reading(p) and (p["device_type"], p["channel"]) in _RANGE_TABLE

        if not ok(payload):
            payload = _draw_donor(ctx, ok)
        hi = _RANGE_TABLE[(payload["device_type"], payload["channel"])][1]
        readings = payload["readings"]
        readings[0] = hi + 10.0
        readings[1] = hi + 11.0
        payload["readings"] = readings
        _store(sample, payload)
        return sample


@register("sensor_unit_swap")
class SensorUnitSwap(Contaminator):
    """单位不一致：MPa → bar（数值不换算，标签替换——混源集成事故形态）。"""

    def apply(self, sample, ctx: Context):
        payload = _load(sample)

        def ok(p):
            return _is_reading(p) and p["unit"] in _UNIT_SWAP

        if not ok(payload):
            payload = _draw_donor(ctx, ok)
        payload["unit"] = _UNIT_SWAP[payload["unit"]]
        _store(sample, payload)
        return sample


@register("sensor_unplanned_silence")
class SensorUnplannedSilence(Contaminator):
    """计划外静默：读数全改哨兵值 -999，且供体窗不落在任何检修计划窗内
    （计划内的静默是合法的，注入必须避开——否则 ground truth 不成立）。"""

    def apply(self, sample, ctx: Context):
        pool_payloads = [_load(s) for s in ctx.pool]
        plans = [
            (
                p["device_id"],
                p["window_start"],
                p["window_end"],
            )
            for p in pool_payloads
            if p.get("record_type") == "maintenance_event"
        ]

        def ok(p):
            if not _is_reading(p):
                return False
            return not any(
                device == p["device_id"] and win_start <= p["window_start"] <= win_end
                for device, win_start, win_end in plans
            )

        payload = _load(sample)
        if not ok(payload):
            payload = _draw_donor(ctx, ok)
        payload["readings"] = [SENTINEL] * len(payload["readings"])
        _store(sample, payload)
        return sample
