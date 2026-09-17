"""合成工业传感器语料测试：确定性逐字节、构成与计划缺席、干净语料零哨兵。"""

from __future__ import annotations

import json
from datetime import datetime

from mm_curation.data.sensor_synth import DEVICES, SENTINEL, generate_corpus


def _payloads(samples):
    return [json.loads(s.text) for s in samples]


def test_seed_determinism_byte_identical():
    a = [s.to_dict() for s in generate_corpus(seed=7, scale=0.1)]
    b = [s.to_dict() for s in generate_corpus(seed=7, scale=0.1)]
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)
    c = [s.to_dict() for s in generate_corpus(seed=8, scale=0.1)]
    assert json.dumps(a, ensure_ascii=False) != json.dumps(c, ensure_ascii=False)


def test_composition():
    samples = generate_corpus(seed=42)
    windows = [s for s in samples if s.meta["sensor_record_type"] == "reading_window"]
    events = [s for s in samples if s.meta["sensor_record_type"] == "maintenance_event"]
    assert len(events) == len(DEVICES) * 8  # 每设备 8 次计划检修
    assert len(windows) >= 1000  # 任务书 ≥1000 读数窗
    # 每通道窗口数一致（100 槽 - 16 检修缺席槽 = 84）
    from collections import Counter

    per_channel = Counter((s.meta["device_id"], s.meta["channel"]) for s in windows)
    assert len(per_channel) == 12 and set(per_channel.values()) == {84}


def test_planned_maintenance_absent_and_covered():
    """计划检修槽位的窗口真实缺席，且事件窗覆盖这些槽位。"""
    samples = generate_corpus(seed=42)
    starts = {
        (s.meta["device_id"], s.meta["window_start"])
        for s in samples
        if s.meta["sensor_record_type"] == "reading_window"
    }
    events = [
        s for s in samples if s.meta["sensor_record_type"] == "maintenance_event"
    ]
    assert events
    for ev in events:
        device = ev.meta["device_id"]
        span = ev.meta["window_end"]
        # 事件窗 [start, end] 内不应有任何该设备的读数窗
        inside = [
            (d, t)
            for (d, t) in starts
            if d == device
            and ev.meta["window_start"] <= t <= span
        ]
        assert not inside, f"计划检修窗内不应有读数: {device} @ {ev.meta['window_start']}"


def test_clean_corpus_zero_sentinel_and_baseline_run():
    samples = generate_corpus(seed=42)
    baseline_checked = 0
    for s in samples:
        p = json.loads(s.text)
        if p["record_type"] != "reading_window":
            continue
        assert SENTINEL not in p["readings"], "干净语料不得出现哨兵值"
        if p["window_start"] <= "2026-01-01T04:59:00+00:00":
            assert p["operating_mode"] == "run"  # 前 5 槽保持 run（漂移基线）
            baseline_checked += 1
    assert baseline_checked == 12 * 5  # 12 通道 × 5 基线窗


def test_baseline_group_has_earlier_slots():
    """同组（设备×通道×工况）漂移基线可构建：每通道前 5 窗为 run 且时间递增。"""
    samples = generate_corpus(seed=42)
    by_channel: dict[tuple, list[str]] = {}
    for s in samples:
        if s.meta["sensor_record_type"] != "reading_window":
            continue
        by_channel.setdefault((s.meta["device_id"], s.meta["channel"]), []).append(
            s.meta["window_start"]
        )
    for starts in by_channel.values():
        assert starts == sorted(starts)
        first = datetime.fromisoformat(starts[0])
        assert all(
            datetime.fromisoformat(t) > first for t in starts[1:]
        )
