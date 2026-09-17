"""eval_fhir 测试：冒烟全链路（小规模真跑出报告过门禁）+ 门禁函数劣化判红。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("eval_fhir", _ROOT / "scripts" / "eval_fhir.py")
eval_fhir = importlib.util.module_from_spec(_spec)
sys.modules["eval_fhir"] = eval_fhir
_spec.loader.exec_module(eval_fhir)


def test_gate_metrics_math():
    class S:
        def __init__(self, id_, dirty):
            self.id, self.labels = id_, ({"dirty": dirty} if dirty else {})

    dropped = [
        ("op", S("d1", "fhir_phi_leak")),
        ("op", S("d2", "fhir_unit_off")),
        ("op", S("c1", "")),
    ]
    m = eval_fhir.funnel_gate_metrics(dropped, n_dirty=4, n_clean=50)
    assert m["recall"] == 0.5 and m["false_kill_rate"] == 0.02
    assert m["n_dirty_caught"] == 2 and m["n_clean_killed"] == 1


def test_evaluate_gate_thresholds():
    ok = {"recall": 0.95, "false_kill_rate": 0.0}
    assert eval_fhir.evaluate_gate(ok)
    low_recall = {"recall": 0.85, "false_kill_rate": 0.0}
    assert not eval_fhir.evaluate_gate(low_recall)
    kill = {"recall": 0.99, "false_kill_rate": 0.06}
    assert not eval_fhir.evaluate_gate(kill)


def test_smoke_run_end_to_end(tmp_path, monkeypatch):
    out = tmp_path / "operator_pr_fhir.json"
    monkeypatch.setattr(
        sys, "argv",
        [
            "eval_fhir.py",
            "--scale", "0.1",
            "--seed", "7",
            "--out", str(out),
        ],
    )
    rc = eval_fhir.main()
    assert rc == 0, "小规模合成语料上五算子门禁应通过"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["pipeline"] == "fhir_r4_quality"
    assert len(report["operators"]) == 5
    assert set(report["dirty_totals"]) == set(eval_fhir.FHIR_KINDS)
    assert report["funnel_gate"]["passed"] is True
    assert (tmp_path / "operator_pr_fhir.md").exists()
