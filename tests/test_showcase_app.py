"""演示门户纯函数单测：报告加载容错 / 门禁卡提取 / P-R 表构造 / 降级。

零 streamlit 交互（唯一例外是末尾一条 AppTest 真渲染冒烟，缺 streamlit 自动跳过）。
"""

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "src", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from showcase_app import (  # noqa: E402
    ablation_rows,
    current_threshold,
    dedup_rows,
    ft_rows,
    gate_cards,
    load_report,
    load_verdicts,
    pr_rows,
    recommend_threshold,
    score_histogram,
    score_values,
    verdict_table,
    waterfall_rows,
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


# --- V6 清洗过程页签（判决台账 + 阈值沙盘）--------------------------------


def _vrow(op, decision, score=None, *, seq=1, threshold=None,
          rule="within_threshold", fp=None):
    """一条判决书记录（形状对齐 mm_curation.verdict.build_verdict 的落盘结果）。"""
    return {
        "v": 1,
        "run_id": "t",
        "seq": seq,
        "op": op,
        "decision": decision,
        "score": score,
        "threshold": {} if threshold is None else threshold,
        "rule": rule,
        "evidence": {},
        "input_fingerprint": fp or ("sha256:" + "ab" * 32),
        "transform": None,
        "review": None,
        "prov": {},
    }


def test_load_verdicts_skips_blank_and_corrupt_lines(tmp_path, monkeypatch):
    import showcase_app as app

    monkeypatch.setattr(app, "REPORTS", tmp_path)
    assert load_verdicts("nope/verdict.jsonl") == []  # 报告缺失 → 降级为空表

    (tmp_path / "v.jsonl").write_text(
        '{"op": "a", "decision": "drop"}\n'
        "\n"                      # 空行跳过
        "{bad json\n"             # 半行跳过（不因一条坏数据让整页空转）
        '{"op": "b", "decision": "keep"}\n',
        encoding="utf-8",
    )
    assert [r["op"] for r in load_verdicts("v.jsonl")] == ["a", "b"]


def test_waterfall_rows_degrades_and_shapes():
    assert waterfall_rows(None) == []
    assert waterfall_rows({}) == []
    rep = {
        "comparison": {
            "per_op": [
                {"op": "text_minhash", "dropped_A": 29, "dropped_B": 30,
                 "delta_dropped": 1, "n_in_A": 2066, "n_in_B": 2066},
            ]
        }
    }
    assert waterfall_rows(rep) == [
        {"滤级": "text_minhash", "原样·拦截": 29, "归一化·拦截": 30, "Δ": 1}
    ]


def test_verdict_table_filter_limit_and_blank_threshold():
    rows = [
        _vrow("chinese_ratio", "drop", 0.123456, seq=1,
              threshold={"min": 0.3}, rule="score_below_min"),
        _vrow("chinese_ratio", "keep", 0.8154, seq=1, threshold={"min": 0.3}),
        _vrow("text_minhash", "drop", None, seq=7),  # 批量算子：无分数、无门限
    ]
    t = verdict_table(rows, op="chinese_ratio")
    assert [r["判决"] for r in t] == ["删", "留"]
    assert t[0]["分数"] == 0.1235  # 四位小数
    assert t[0]["门限"] == "min=0.3"
    assert t[0]["判据"] == "score_below_min"
    assert t[0]["指纹"] == "abababababab"  # 剥掉 "sha256:" 前缀，取 12 位

    assert len(verdict_table(rows, decision="drop")) == 2
    assert verdict_table(rows, decision="keep")[0]["滤芯"] == "chinese_ratio"

    only = verdict_table(rows, op="text_minhash")
    assert only[0]["分数"] is None and only[0]["门限"] == "—"
    assert verdict_table(rows, op="text_minhash")[0]["级"] == 7

    assert len(verdict_table(rows, limit=1)) == 1  # 截断（页面上限）


def test_score_values_and_histogram():
    rows = [
        _vrow("a", "keep", 0.0),
        _vrow("a", "keep", 1.0),
        _vrow("a", "keep", 0.5),
        _vrow("a", "drop", None),  # 无分数不入分布
        _vrow("b", "keep", 9.9),   # 别的滤级不入
    ]
    assert score_values(rows, "a") == [0.0, 1.0, 0.5]
    assert score_values(rows, "nope") == []

    assert score_histogram([]) == []
    assert score_histogram([0.7, 0.7, 0.7]) == [{"区间": "0.7", "样本数": 3}]  # 退化为单桶

    h = score_histogram([i / 99 for i in range(100)], bins=10)
    assert len(h) == 10
    assert sum(r["样本数"] for r in h) == 100  # 计数守恒：不许有样本掉出桶外


def test_recommend_threshold_from_drop_budget():
    assert recommend_threshold([], side="min", max_drop_rate=0.1) is None

    scores = [float(i) for i in range(100)]
    # 契约：反推出来的删除率必须贴住预算。门限落在某个样本值上，所以误差上限
    # 是一个样本的宽度（n=100 → 1%），不是浮点意义上的"相等"。
    tol = 2 / len(scores)

    lo = recommend_threshold(scores, side="min", max_drop_rate=0.1)
    assert lo["n"] == 100 and lo["score_min"] == 0.0 and lo["score_max"] == 99.0
    assert lo["threshold"] == 10.0           # k = round(0.1 × 100)
    assert abs(lo["drop_rate"] - 0.10) <= tol

    hi = recommend_threshold(scores, side="max", max_drop_rate=0.1)
    assert hi["threshold"] == 90.0
    assert abs(hi["drop_rate"] - 0.10) <= tol

    # 预算 0：两级都不许删任何东西
    assert recommend_threshold(scores, side="min", max_drop_rate=0.0)["drop_rate"] == 0.0
    assert recommend_threshold(scores, side="max", max_drop_rate=0.0)["drop_rate"] == 0.0


def test_current_threshold_picks_only_matching_op_with_threshold():
    rows = [_vrow("a", "drop", 0.1), _vrow("b", "drop", 7.0, threshold={"max": 6.0})]
    assert current_threshold(rows, "a") == {}      # 无门限字段 → 空
    assert current_threshold(rows, "b") == {"max": 6.0}
    assert current_threshold(rows, "missing") == {}


def test_showcase_app_renders_all_eight_tabs():
    """真渲染冒烟：脚本要能跑到底，报告缺失时走降级分支而不是抛异常。

    CI 里 `data/reports/` 不随仓库分发（.gitignore 第 25 行），所以这条在 CI 上
    覆盖的正是「报告全缺失」路径；本机则额外覆盖两个新页签有数据的路径。
    """
    st_testing = pytest.importorskip("streamlit.testing.v1")
    at = st_testing.AppTest.from_file(str(REPO / "scripts" / "showcase_app.py"))
    at.run(timeout=120)
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == [
        "总览", "图文数据", "文本数据", "医疗数据",
        "工业传感器", "效果证据", "清洗过程", "阈值沙盘",
    ]


def test_showcase_app_renders_with_no_reports_at_all(tmp_path):
    """CI 路径：`data/reports/` 一个文件都没有时，必须走降级分支而不是崩。

    做法是把脚本复制到临时目录再跑——`REPO` 取自脚本所在位置的上两级，
    于是 `REPORTS` 指向一个不存在的目录，等价于 CI 里的裸仓库。
    这条是「本地绿 ≠ CI 绿」的本地复现器：报告在 CI 不随仓库分发，
    所以走的是「报告全缺失」分支（每个页签显示生成命令），本机反而测不到。
    """
    st_testing = pytest.importorskip("streamlit.testing.v1")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(
        REPO / "scripts" / "showcase_app.py", tmp_path / "scripts" / "showcase_app.py"
    )
    at = st_testing.AppTest.from_file(str(tmp_path / "scripts" / "showcase_app.py"))
    at.run(timeout=90)
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) == 8
    assert not (tmp_path / "data" / "reports").exists()  # 确认真的什么都没读到


def test_threshold_tab_survives_batch_operator_selection():
    """批量算子没有逐样本分数：沙盘显示「不适用」而不是崩。

    针对一个真实存在过的 bug：指标卡渲染在 `if rec:` 之外，选到批量算子时
    `rec` 为 None → `c1` 从未赋值 → NameError。默认选中的恰好是第一个**有分数**
    的算子，所以普通渲染冒烟看不出来——必须显式切到批量算子才能复现。
    """
    st_testing = pytest.importorskip("streamlit.testing.v1")
    import showcase_app as app

    rows = load_verdicts(app.VERDICT_RELPATH)
    at = st_testing.AppTest.from_file(str(REPO / "scripts" / "showcase_app.py"))
    at.run(timeout=120)

    boxes = [b for b in at.selectbox if b.key == "cal_op"]
    if not boxes:
        pytest.skip("判决台账缺失（CI 路径），沙盘无控件可切换")

    target = next((o for o in boxes[0].options if not app.score_values(rows, o)), None)
    if target is None:
        pytest.skip("当前台账里每一级滤芯都有逐样本分数，无处复现该分支")

    boxes[0].select(target).run(timeout=120)
    assert not at.exception, [e.value for e in at.exception]
