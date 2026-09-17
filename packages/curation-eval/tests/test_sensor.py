"""SensorSample 适配器测试：roundtrip、模态登记、执行器零特例、错误输入。"""

from __future__ import annotations

import json

import pytest
from curation_eval import (
    MODALITY_FIELDS,
    BatchOperator,
    CostClass,
    LocalSequentialExecutor,
    Operator,
    OperatorMeta,
    Sample,
    SensorSample,
)

WINDOW = {
    "record_type": "reading_window",
    "device_id": "pump01",
    "device_type": "pump",
    "channel": "pressure",
    "unit": "MPa",
    "sampling_hz": 1,
    "operating_mode": "run",
    "window_start": "2026-01-01T00:00:00+00:00",
    "window_end": "2026-01-01T00:04:16+00:00",
    "readings": [1.0 + 0.001 * i for i in range(256)],
}

EVENT = {
    "record_type": "maintenance_event",
    "device_id": "pump01",
    "device_type": "pump",
    "window_start": "2026-01-02T00:00:00+00:00",
    "window_end": "2026-01-02T02:00:00+00:00",
}


def test_roundtrip_fidelity():
    s = SensorSample.from_payload(WINDOW)
    s2 = SensorSample.from_payload(SensorSample.to_payload(s))
    assert s2 == s
    assert json.loads(s.text)["readings"] == WINDOW["readings"]


def test_flat_mapping_meta_and_id():
    s = SensorSample.from_payload(WINDOW)
    assert s.modality == "industrial_sensor"
    assert s.id == "win_pump01_pressure_20260101T0000000000"
    assert s.meta["sensor_record_type"] == "reading_window"
    assert s.meta["channel"] == "pressure" and s.meta["unit"] == "MPa"
    assert s.meta["operating_mode"] == "run"
    ev = SensorSample.from_payload(EVENT)
    assert ev.id.startswith("maint_pump01_")
    assert ev.meta["sensor_record_type"] == "maintenance_event"
    assert MODALITY_FIELDS["industrial_sensor"] == frozenset({"text"})


def test_serialization_roundtrip_via_from_dict():
    s = SensorSample.from_payload(WINDOW)
    assert Sample.from_dict(s.to_dict()) == s


def test_unknown_modality_still_rejected():
    with pytest.raises(ValueError, match="未知 modality"):
        Sample(id="x", modality="plc_log")


def test_executor_skips_sensor_for_text_ops():
    """混合模态漏斗：文本算子对 industrial_sensor 样本保留不评判（计 skipped）。"""

    class _LenOp(Operator):
        name = "test_slen"
        meta = OperatorMeta(
            name="test_slen",
            modalities=frozenset({"text_article"}),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
        )

        def score(self, sample: Sample) -> float | None:
            return float(len(sample.text))

    win = SensorSample.from_payload(WINDOW)
    text = Sample(id="t1", text="正文")
    result = LocalSequentialExecutor().run([_LenOp()], [win, text])
    assert [s.id for s in result.kept] == [win.id, "t1"]
    assert result.stats[0].skipped == 1 and result.stats[0].dropped == 0


def test_batch_operator_id_ordering_determinism():
    """批量算子按 record_type 分组保留首窗——只依赖 id 序，与输入序无关。"""

    class _KeepFirstPerType(BatchOperator):
        name = "test_skeep"
        meta = OperatorMeta(
            name="test_skeep",
            modalities=frozenset({"industrial_sensor"}),
            required_fields=frozenset({"text"}),
            cost_class=CostClass.RULE,
            shardable=False,
        )

        def run_batch(self, samples: list[Sample]) -> list[Sample]:
            seen: set[str] = set()
            keep = []
            for s in samples:  # 执行器已按 id 规范化排序
                rtype = SensorSample.to_payload(s)["record_type"]
                if rtype not in seen:
                    seen.add(rtype)
                    keep.append(s)
            return keep

    b = SensorSample.from_payload({**WINDOW, "device_id": "b-pump"})
    a = SensorSample.from_payload({**WINDOW, "device_id": "a-pump"})
    result = LocalSequentialExecutor().run([_KeepFirstPerType()], [b, a])
    assert [s.id for s in result.kept] == [a.id]  # 字典序最小者胜


def test_error_inputs_rejected():
    with pytest.raises(ValueError, match="record_type"):
        SensorSample.from_payload({**WINDOW, "record_type": "alarm"})
    with pytest.raises(ValueError, match="readings"):
        SensorSample.from_payload({**WINDOW, "readings": []})
    with pytest.raises(ValueError, match="device_id"):
        SensorSample.from_payload({**WINDOW, "device_id": ""})
    with pytest.raises(ValueError, match="不是 industrial_sensor"):
        SensorSample.parse(Sample(id="t", text="正文"))
