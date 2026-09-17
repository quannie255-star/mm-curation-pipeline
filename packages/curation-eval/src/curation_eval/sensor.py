"""工业传感器窗口 ↔ Sample 双向适配器（V5 α：第四模态零特例接入）。

扁平化约定（设计表 V5 α 决策点 1）：一个 Sample = 一个 通道×时间窗，窗口
payload 规范化序列化（sort_keys、ensure_ascii=False）进 sample.text——单一
事实源；记录类型/设备/通道/单位/采样率/工况/窗口边界提升为 meta 键。

两类 record_type 进同一模态样本流（镜像 FHIR 多资源类型先例）：
- reading_window：读数窗（readings 数组必备）
- maintenance_event：计划检修事件窗（fault_vs_maintenance 判别的业务事件源，
  计划窗口内的数据静默=合法由此佐证）

roundtrip 保真：from_payload(to_payload(s)) 与 s 逐字段相等——id/meta 从
payload 确定性推导，序列化键序固定。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .schema import Sample

MODALITY = "industrial_sensor"

READING_WINDOW = "reading_window"
MAINTENANCE_EVENT = "maintenance_event"
KNOWN_RECORD_TYPES = frozenset({READING_WINDOW, MAINTENANCE_EVENT})

_COMPACT = re.compile(r"[-:+.]")


class SensorSample:
    """工业传感器窗口 payload 与扁平 Sample 之间的双向映射（纯函数式）。"""

    modality = MODALITY

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> Sample:
        if not isinstance(payload, dict):
            raise ValueError(f"传感器 payload 应为 dict，得到 {type(payload).__name__}")
        record_type = payload.get("record_type")
        if record_type not in KNOWN_RECORD_TYPES:
            raise ValueError(
                f"未知或缺失 record_type: {record_type!r}，已知: {sorted(KNOWN_RECORD_TYPES)}"
            )
        device_id = payload.get("device_id")
        if not device_id:
            raise ValueError(f"{record_type} 缺少 device_id")
        start = payload.get("window_start")
        end = payload.get("window_end")
        if not start or not end:
            raise ValueError(f"{record_type} 缺少 window_start/window_end")
        meta: dict[str, Any] = {
            "sensor_record_type": record_type,
            "device_id": device_id,
            "device_type": payload.get("device_type", ""),
            "window_start": start,
            "window_end": end,
        }
        if record_type == READING_WINDOW:
            readings = payload.get("readings")
            if not isinstance(readings, list) or not readings:
                raise ValueError("reading_window 必须携带非空 readings 数组")
            if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in readings):
                raise ValueError("readings 必须为数值数组")
            if not payload.get("channel") or not payload.get("unit"):
                raise ValueError("reading_window 缺少 channel/unit")
            if not payload.get("sampling_hz"):
                raise ValueError("reading_window 缺少 sampling_hz")
            if not payload.get("operating_mode"):
                raise ValueError("reading_window 缺少 operating_mode")
            meta.update(
                {
                    "channel": payload["channel"],
                    "unit": payload["unit"],
                    "sampling_hz": payload["sampling_hz"],
                    "operating_mode": payload["operating_mode"],
                }
            )
            sample_id = (
                f"win_{device_id}_{payload['channel']}_{_COMPACT.sub('', start)}"
            )
        else:
            sample_id = f"maint_{device_id}_{_COMPACT.sub('', start)}"
        return Sample(
            id=sample_id,
            text=json.dumps(payload, sort_keys=True, ensure_ascii=False),
            modality=MODALITY,
            meta=meta,
        )

    @staticmethod
    def to_payload(sample: Sample) -> dict[str, Any]:
        if sample.modality != MODALITY:
            raise ValueError(f"modality={sample.modality!r} 不是 {MODALITY}，拒绝解析")
        payload = json.loads(sample.text)
        if not isinstance(payload, dict):
            raise ValueError("sample.text 不是传感器窗口 payload JSON 对象")
        return payload

    @staticmethod
    def parse(sample: Sample) -> dict[str, Any]:
        """算子侧别名：语义同 to_payload（解析失败抛 ValueError）。"""
        return SensorSample.to_payload(sample)
