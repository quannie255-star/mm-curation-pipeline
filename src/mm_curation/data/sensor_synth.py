"""确定性合成工业传感器语料（V5 α）。

为工业算子/污染器提供带检修计划的合成时序语料：泵/风机/加热炉三类设备、
6 台设备 × 2 通道、每通道约 100 个窗口槽位——全部字段程序生成，不含任何
真实产线数据（报告固定脚注）。

结构约定（与设计表 V5 α 一致）：
- 一窗一 Sample：SensorSample.from_payload（canonical JSON 进 text）
- 计划检修窗**真实缺席**（读数流里没有该窗），另有 maintenance_event 样本
  佐证「静默=合法」——fault_vs_maintenance 的业务事件源
- 工况 schedule 确定性循环：run（绝大多数）/ changeover（每 20 槽 1 窗，
  合法的均值偏移）/ idle（每 33 槽 1 窗，合法的近平坦）
- 随机只走 random.Random(seed)（AR(1)+高斯噪声），同 seed 逐字节一致
- SENTINEL=-999.0 为缺失哨兵值：干净语料绝不出现（真实读数域远离它），
  由污染器注入后交 fault_vs_maintenance 裁决
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from curation_eval import Sample, SensorSample

DEVICE_TYPES: dict[str, tuple[str, ...]] = {
    "pump": ("flow", "pressure"),
    "fan": ("speed", "vibration"),
    "furnace": ("temp", "pressure"),
}
DEVICES: dict[str, str] = {
    "pump01": "pump",
    "pump02": "pump",
    "fan01": "fan",
    "fan02": "fan",
    "fur01": "furnace",
    "fur02": "furnace",
}

# 量程/单位表（单一事实源：算子 sensor_range 与污染器共用；包内污染器
# 持同值副本并注释同源，包不反向依赖主仓）
RANGE_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("pump", "flow"): (0.0, 120.0),
    ("pump", "pressure"): (0.0, 2.5),
    ("fan", "speed"): (0.0, 3600.0),
    ("fan", "vibration"): (0.0, 20.0),
    ("furnace", "temp"): (0.0, 1200.0),
    ("furnace", "pressure"): (0.0, 10.0),
}
CHANNEL_UNITS: dict[str, str] = {
    "flow": "m3/h",
    "pressure": "MPa",  # 泵侧；炉压通道在生成时覆盖为 kPa
    "speed": "rpm",
    "vibration": "mm/s",
    "temp": "℃",
}
CHANNEL_PARAMS: dict[str, tuple[float, float]] = {  # (run 均值, 噪声σ)
    "flow": (60.0, 1.0),
    "pressure": (1.2, 0.01),
    "speed": (2900.0, 5.0),
    "vibration": (4.0, 0.1),
    "temp": (850.0, 1.5),
}
FURNACE_PRESSURE = (5.0, 0.05)  # (run 均值, 噪声σ)，单位 kPa

SENTINEL = -999.0
SLOTS_PER_CHANNEL = 100
WINDOW_SECONDS = 256  # 256 读数 @ 1Hz
SLOT_SECONDS = 3600  # 窗口槽位间隔 1 小时
MAINT_EVENT_SLOTS = 8  # 每设备计划检修次数（每次占 2 个槽位）
MAINT_SPAN = 2
BASELINE_SLOTS = frozenset(range(0, 5))  # 前五槽保持 run，保证 drift 基线存在

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PHI = 0.9  # AR(1) 系数


def _mode_for_slot(slot: int) -> str:
    if slot in BASELINE_SLOTS:
        return "run"
    if slot % 20 == 5:
        return "changeover"
    if slot % 33 == 17:
        return "idle"
    return "run"


def _channel_params(device_type: str, channel: str) -> tuple[float, float, str]:
    if device_type == "furnace" and channel == "pressure":
        return FURNACE_PRESSURE[0], FURNACE_PRESSURE[1], "kPa"
    mu, sigma = CHANNEL_PARAMS[channel]
    return mu, sigma, CHANNEL_UNITS[channel]


def _readings(rng: random.Random, mu: float, sigma: float, prev: float | None) -> list[float]:
    out = []
    x = prev if prev is not None else mu
    for _ in range(WINDOW_SECONDS):
        x = mu + _PHI * (x - mu) + rng.gauss(0.0, sigma)
        out.append(round(x, 4))
    return out


def generate_corpus(seed: int = 42, scale: float = 1.0) -> list[Sample]:
    """生成合成工业传感器语料。scale 缩放窗口槽位（冒烟/测试用），同 seed 逐字节一致。"""
    n_slots = max(10, round(SLOTS_PER_CHANNEL * scale))
    rng = random.Random(seed)
    samples: list[Sample] = []

    for device_id, device_type in DEVICES.items():
        for channel in DEVICE_TYPES[device_type]:
            mu, sigma, unit = _channel_params(device_type, channel)
            prev: float | None = None
            last_mode: str | None = None
            for slot in range(n_slots):
                start = _EPOCH + timedelta(seconds=slot * SLOT_SECONDS)
                if _in_maintenance(slot):
                    prev = None  # 检修后重新起算
                    last_mode = None
                    continue  # 计划检修窗真实缺席
                mode = _mode_for_slot(slot)
                mode_mu = mu * (1.15 if mode == "changeover" else 0.1 if mode == "idle" else 1.0)
                if mode != last_mode:
                    prev = None  # 工况切换：过程从新工况稳态起算（合成语料不含启停瞬态，
                    last_mode = mode  # 真实切换瞬态属 V5 β 真实数据轨；drift 算子只裁稳态窗）
                readings = _readings(rng, mode_mu, sigma, prev)
                prev = readings[-1]
                samples.append(
                    SensorSample.from_payload(
                        {
                            "record_type": "reading_window",
                            "device_id": device_id,
                            "device_type": device_type,
                            "channel": channel,
                            "unit": unit,
                            "sampling_hz": 1,
                            "operating_mode": mode,
                            "window_start": start.isoformat(),
                            "window_end": (start + timedelta(seconds=WINDOW_SECONDS)).isoformat(),
                            "readings": readings,
                        }
                    )
                )

    # 检修计划事件（窗口缺席的业务佐证）
    for device_id, device_type in DEVICES.items():
        for i in range(max(1, round(MAINT_EVENT_SLOTS * scale))):
            first_slot = 10 + i * 11
            start = _EPOCH + timedelta(seconds=first_slot * SLOT_SECONDS)
            samples.append(
                SensorSample.from_payload(
                    {
                        "record_type": "maintenance_event",
                        "device_id": device_id,
                        "device_type": device_type,
                        "window_start": start.isoformat(),
                        # 收尾 -1s：事件窗为闭区间，恰好覆盖缺席槽位而不吞下一个在岗窗
                        "window_end": (
                            start + timedelta(seconds=MAINT_SPAN * SLOT_SECONDS - 1)
                        ).isoformat(),
                    }
                )
            )
    return samples


def _in_maintenance(slot: int) -> bool:
    for i in range(MAINT_EVENT_SLOTS):
        if 10 + i * 11 <= slot < 10 + i * 11 + MAINT_SPAN:
            return True
    return False
