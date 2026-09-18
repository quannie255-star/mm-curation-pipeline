"""证明链实验纯函数单测：多 seed 门禁聚合 / 随机删 baseline 报告构造。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "scripts", _ROOT / "packages" / "curation-eval" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _name, _rel in (
    ("eval_fhir", "scripts/eval_fhir.py"),
    ("eval_random_drop_baseline", "scripts/eval_random_drop_baseline.py"),
):
    _spec = importlib.util.spec_from_file_location(_name, _ROOT / _rel)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)


def test_worst_case_gate_takes_pessimistic_aggregate():
    runs = [
        {"seed": 42, "recall": 1.0, "false_kill_rate": 0.0104, "passed": True,
         "n_dirty": 317, "n_dirty_caught": 317, "n_clean_killed": 11, "n_clean": 1056},
        {"seed": 7, "recall": 0.98, "false_kill_rate": 0.0123, "passed": True,
         "n_dirty": 300, "n_dirty_caught": 294, "n_clean_killed": 13, "n_clean": 1056},
    ]
    g = sys.modules["eval_fhir"].worst_case_gate(runs)
    assert g["recall"] == 0.98  # 最差召回
    assert g["false_kill_rate"] == 0.0123  # 最差误杀
    assert g["n_dirty_caught"] == 294 and g["n_clean_killed"] == 13
    assert g["passed"] is True and g["n_seeds"] == 2

    runs.append({**runs[0], "passed": False})
    assert sys.modules["eval_fhir"].worst_case_gate(runs)["passed"] is False  # 一票否决


def test_random_drop_report_verdict():
    funnel_m = {"n_input": 2106, "recall_at_k": {1: 0.575, 10: 0.9}}
    rows = [
        {"seed": 0, "recall_at_k": {1: 0.45, 10: 0.88}},
        {"seed": 1, "recall_at_k": {1: 0.44, 10: 0.87}},
    ]
    report = sys.modules["eval_random_drop_baseline"].build_report(funnel_m, rows, n_drop=521)
    assert report["random"]["random_r1_mean"] == 0.445
    assert report["verdict"]["margin_over_random"] == 0.13
    assert "净贡献" in report["verdict"]["interpretation"]
    # 反向场景：漏斗跑不赢随机删时如实记录
    bad = sys.modules["eval_random_drop_baseline"].build_report(
        {"n_input": 2106, "recall_at_k": {1: 0.40, 10: 0.9}}, rows, n_drop=521
    )
    assert "无净贡献" in bad["verdict"]["interpretation"]
