"""演示门户纯函数单测：报告加载容错 / 门禁卡提取 / P/R 表构造 / 降级。零 streamlit 交互。"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "src", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from showcase_app import (  # noqa: E402
    ablation_rows,
    dedup_rows,
    ft_rows,
    gate_cards,
    load_report,
    pr_rows,
)


def test_load_report_tolerates_missing_and_corrupt(tmp_path, monkeypatch):

    import showcase_app as app

    monkeypatch.setattr(app, "REPORTS", tmp_path)
    assert load_report("nope.json") is None
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
    assert load_report("bad.json") is None
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    assert load_report("ok.json") == {"a": 1}


def test_pr_rows_formats_primary_recall():
    report = {
        "operators": [
            {
                "op": "phi_residual",
                "n_dropped": 29,
                "clean_killed": 0,
                "precision": 1.0,
                "primary_target": ["fhir_phi_leak"],
                "primary_recall": {"fhir_phi_leak": 1.0},
            },
            {"op": "llm_judge", "n_dropped": 0, "clean_killed": 0,
             "precision": None, "primary_target": [], "primary_recall": {}},
        ]
    }
    rows = pr_rows(report)
    assert rows[0]["算子"] == "phi_residual" and rows[0]["precision"] == "100.0%"
    assert rows[0]["主靶recall"] == "100%" and rows[0]["主靶"] == "fhir_phi_leak"
    assert rows[1]["precision"] == "—" and rows[1]["主靶recall"] == "—"
    assert pr_rows(None) == []


def test_gate_cards_with_and_without_funnel_gate():
    gated = {
        "n_total": 650,
        "n_clean": 500,
        "funnel_gate": {"recall": 1.0, "false_kill_rate": 0.0, "passed": True, "n_dirty": 150},
    }
    g = gate_cards(gated)
    assert g and g["passed"] is True and g["recall"] == 1.0 and g["n_clean"] == 500
    assert gate_cards({"operators": []}) is None  # 图文老报告无漏斗门禁字段
    assert gate_cards(None) is None


def test_dedup_rows_and_ft_rows_and_ablation_rows():
    dedup = dedup_rows([{"scale": 10000, "n_total": 11000, "exact_recall": 1.0,
                         "near_recall": 0.97, "seconds_total": 21}])
    assert dedup[0]["规模档"] == 10000 and dedup[0]["near召回"] == 0.97
    assert dedup_rows(None) == []

    ft = ft_rows({"results": {"base": {"recall_at_k": {"1": 0.5558, "5": 0.824, "10": 0.9},
                                       "mrr": 0.675}}})
    assert ft[0]["配置"] == "基线（未微调）" and ft[0]["R@1"] == 0.556
    assert ft_rows(None) == []

    abl = ablation_rows({"ablations": [
        {"name": "no_b", "n_kept": 2, "recall_at_k": {"1": 0.55}, "delta_r1": -0.017},
        {"name": "no_a", "n_kept": 2, "recall_at_k": {"1": 0.56}, "delta_r1": 0.0},
    ]})
    assert [r["配置"] for r in abl] == ["no_b", "no_a"]  # 按影响排序（ΔR@1 升序）
    assert ablation_rows(None) == []
