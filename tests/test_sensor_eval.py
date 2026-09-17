"""eval_industrial 测试：门禁函数数学 + 小规模真跑全链路过门禁。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "eval_industrial", _ROOT / "scripts" / "eval_industrial.py"
)
eval_industrial = importlib.util.module_from_spec(_spec)
sys.modules["eval_industrial"] = eval_industrial
_spec.loader.exec_module(eval_industrial)


def test_gate_metrics_math():
    class S:
        def __init__(self, id_, dirty):
            self.id, self.labels = id_, ({"dirty": dirty} if dirty else {})

    dropped = [
        ("op", S("d1", "sensor_cal_offset")),
        ("op", S("d2", "sensor_flatline")),
        ("op", S("c1", "")),
    ]
    m = eval_industrial.funnel_gate_metrics(dropped, n_dirty=4, n_clean=50)
    assert m["recall"] == 0.5 and m["false_kill_rate"] == 0.02
    assert m["n_dirty_caught"] == 2 and m["n_clean_killed"] == 1


def test_evaluate_gate_thresholds():
    assert eval_industrial.evaluate_gate({"recall": 0.95, "false_kill_rate": 0.0})
    assert not eval_industrial.evaluate_gate({"recall": 0.85, "false_kill_rate": 0.0})
    assert not eval_industrial.evaluate_gate({"recall": 0.99, "false_kill_rate": 0.06})


def test_smoke_run_end_to_end(tmp_path, monkeypatch):
    out = tmp_path / "operator_pr_industrial.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_industrial.py", "--scale", "0.1", "--seed", "7", "--out", str(out)],
    )
    rc = eval_industrial.main()
    assert rc == 0, "小规模合成语料上五算子门禁应通过"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["pipeline"] == "industrial_sensor_quality"
    assert len(report["operators"]) == 5
    assert set(report["dirty_totals"]) == set(eval_industrial.SENSOR_KINDS)
    assert report["funnel_gate"]["passed"] is True
    assert (tmp_path / "operator_pr_industrial.md").exists()
