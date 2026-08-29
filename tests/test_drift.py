"""漂移监控测试：PSI 数学正确性、等级判定、对照实验语义。"""

from __future__ import annotations

import pytest

from mm_curation.monitoring.drift import drift_report, psi


def test_psi_identical_distributions():
    ref = [float(i % 100) for i in range(1000)]
    assert psi(ref, list(ref)) == pytest.approx(0.0, abs=1e-9)


def test_psi_shifted_distribution_fires():
    ref = [float(i % 100) for i in range(1000)]
    shifted = [float(i % 100) + 100 for i in range(1000)]  # 同形状整体平移
    assert psi(ref, shifted) > 0.25


def test_psi_empty_inputs_safe():
    assert psi([], []) == 0.0
    assert psi([1.0, 2.0], []) == 0.0


def _batch(scores, n_copies=50):
    return [{"meta": {"score:op_a": v}} for v in scores] * 1


def test_drift_report_levels_and_control():
    stable_scores = [float(i % 50) for i in range(500)]
    ref = _batch(stable_scores)
    control = _batch([float((i + 3) % 50) for i in range(500)])  # 同分布微扰
    drifted = _batch([float(i % 50) + 100 for i in range(500)])  # 整体平移

    ok = drift_report(ref, control, ops=["op_a"])
    assert ok["checks"][0]["level"] in ("stable", "moderate") and not ok["alert"]
    bad = drift_report(ref, drifted, ops=["op_a"])
    assert bad["checks"][0]["level"] == "significant" and bad["alert"]

    md = bad.get("markdown")  # 渲染由 render_markdown 单独负责，报告保持纯数据
    assert md is None
