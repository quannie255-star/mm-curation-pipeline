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
    MODALITY_COUNTS,
    ablation_rows,
    current_threshold,
    dedup_rows,
    ft_rows,
    gate_cards,
    load_report,
    load_verdicts,
    modality_phrase,
    pr_rows,
    real_applicability_rows,
    real_arm_stats,
    real_curve_point,
    real_curve_rows,
    real_dataset,
    real_datasets,
    real_kill_filters,
    real_kill_rows,
    real_op_rows,
    real_reachability_rows,
    real_unscored_total,
    real_window_mix,
    recommend_threshold,
    rows_to_csv,
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


def test_showcase_app_renders_all_nine_tabs():
    """真渲染冒烟：脚本要能跑到底，报告缺失时走降级分支而不是抛异常。

    CI 里 `data/reports/` 不随仓库分发（.gitignore 第 25 行），所以这条在 CI 上
    覆盖的正是「报告全缺失」路径；本机则额外覆盖后三个新页签有数据的路径。
    """
    st_testing = pytest.importorskip("streamlit.testing.v1")
    at = st_testing.AppTest.from_file(str(REPO / "scripts" / "showcase_app.py"))
    at.run(timeout=120)
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == [
        "总览", "图文数据", "文本数据", "医疗数据",
        "工业传感器", "效果证据", "清洗过程", "阈值沙盘", "真实数据",
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
    assert len(at.tabs) == 9
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


# --- F2 第 9 页签「真实数据」（消费 F1 预计算 JSON）--------------------------
# 这一页的全部数字都来自预计算产物，所以这里测的是**口径**而不是渲染：
# 误杀率的分母是不是干净窗、旧/新两档是不是真的读不同数组、未评是不是单列。


def _real_payload() -> dict:
    """最小可用的真实数据 payload（形状对齐 build_real_interactive.py 的产物）。

    精心挑过的一组数：A 通道 4 窗（2 干净 2 脏）、B 通道 2 窗（2 干净）。
    新判据丢 2 窗（1 干净 1 脏）→ 误杀率 1/4；旧判据丢 3 窗（2 干净 1 脏）→ 2/4。
    """
    return {
        "meta": {
            "honesty_note": ["误杀率是**上界**。"],
            "unscored_note": "「未评」既非通过也非失败。",
            "config_synth": "configs/funnel_industrial.yaml",
            "config_real": "configs/funnel_industrial_real.yaml",
            "generated_at": "2026-09-20T00:00:00+00:00",
            "columns": {"d": "新档裁决"},
        },
        "datasets": [
            {
                "id": "toy",
                "label": "玩具集",
                "blurb": "两通道",
                "n_total": 6,
                "n_clean": 4,
                "n_dirty": 2,
                "channels": {
                    "A": {"t": [10, 20, 30, 40], "lab": [0, 1, 0, 1],
                          "d": [0, 1, 0, 0], "d0": [1, 1, 1, 0], "u": [0, 0, 1, 2]},
                    "B": {"t": [10, 20], "lab": [0, 0],
                          "d": [1, 0], "d0": [0, 0], "u": [0, 1]},
                },
                "operators": [
                    {"op": "sensor_stuck", "n_in": 6, "n_dropped": 2, "clean_killed": 1,
                     "kill_rate": 0.25, "n_dirty_caught": 1, "n_unscored": 2,
                     "forms": {"machine_stop": 2, "scale_collapse": 1},
                     "old": {"n_dropped": 3, "clean_killed": 2, "kill_rate": 0.5,
                             "n_dirty_caught": 1}},
                    {"op": "sensor_range", "n_in": 6, "n_dropped": 0, "clean_killed": 0,
                     "kill_rate": 0.0, "n_dirty_caught": 0, "n_unscored": 6,
                     "forms": {}, "old": {"n_dropped": 0, "clean_killed": 0,
                                          "kill_rate": 0.0, "n_dirty_caught": 0}},
                ],
                "curves": {
                    "sensor_drift": [
                        {"label": "scale=mad · z=8", "recall": 0.10, "kill_rate": 0.02,
                         "n_dropped": 8, "params": {"scale": "mad", "z": 8}},
                        {"label": "scale=mad · z=4", "recall": 0.30, "kill_rate": 0.05,
                         "n_dropped": 20, "params": {"scale": "mad", "z": 4}},
                        {"label": "scale=pooled · z=8", "recall": 0.40, "kill_rate": 0.09,
                         "n_dropped": 40, "params": {"scale": "pooled", "z": 8}},
                        {"label": "scale=pooled · z=4", "recall": 0.50, "kill_rate": 0.15,
                         "n_dropped": 60, "params": {"scale": "pooled", "z": 4}},
                    ]
                },
                "applicability": [
                    {"op": "unit_consistency", "target": "多单位组", "n_target": 0,
                     "n_windows": 6, "applicable": False, "hint": "无区分度"},
                ],
                "reachability": [
                    {"op": "sensor_stuck", "group_by": "device×channel", "requirement": 10,
                     "n_groups": 2, "group_size_p50": 3, "group_size_max": 4,
                     "n_groups_shorter": 2, "reachable": False},
                ],
                "kills": [
                    {"op": "sensor_stuck", "device": "d1", "channel": "A", "t": 20,
                     "rule": "machine_stop", "label": "fault_x",
                     "reading_min": 1.0, "reading_max": 1.0, "reading_std": 0.0},
                    {"op": "sensor_stuck", "device": "d1", "channel": "B", "t": 10,
                     "rule": "scale_collapse", "label": "",
                     "reading_min": 2.0, "reading_max": 2.1, "reading_std": 0.01},
                    {"op": "sensor_range", "device": "d2", "channel": "A", "t": 30,
                     "rule": "", "label": "fault_y",
                     "reading_min": 3.0, "reading_max": 3.0, "reading_std": 0.0},
                ],
            }
        ],
    }


def test_real_datasets_degrades_and_looks_up():
    assert real_datasets(None) == []
    assert real_datasets({}) == []
    assert real_datasets({"datasets": [{"nope": 1}]}) == []  # 没有 id 的条目不算数据集
    payload = _real_payload()
    assert [d["id"] for d in real_datasets(payload)] == ["toy"]
    assert real_dataset(payload, "toy")["label"] == "玩具集"
    assert real_dataset(payload, "missing") is None


def test_real_arm_stats_switches_arm_and_uses_clean_denominator():
    """核心口径：**误杀率的分母是干净窗**，且新旧两档读的是不同数组。"""
    ds = _real_payload()["datasets"][0]

    new = real_arm_stats(ds, "new")
    assert (new["n"], new["n_clean"], new["n_dirty"]) == (6, 4, 2)
    assert new["n_dropped"] == 2 and new["clean_killed"] == 1 and new["dirty_caught"] == 1
    assert new["kill_rate"] == 1 / 4          # 分母是干净窗（4），不是总窗数（6）
    assert new["recall"] == 1 / 2
    assert abs(new["survival"] - 4 / 6) < 1e-12

    old = real_arm_stats(ds, "old")
    assert old["n_dropped"] == 3 and old["clean_killed"] == 2
    assert old["kill_rate"] == 2 / 4
    assert old["recall"] == 1 / 2             # 旧判据同样抓到那 1 条，但多杀了 1 条干净窗

    # 分母口径的反例守卫：拿总窗数当分母会算出 1/6，那是「假精确」
    assert new["kill_rate"] != 1 / 6


def test_real_arm_stats_without_dirty_or_clean_windows_is_none():
    """分母为 0 时返回 None（不可算），**不是 0**——0 会被读成「误杀为零，完美」。"""
    only_clean = {"channels": {"A": {"lab": [0, 0], "d": [0, 0], "d0": [0, 0]}}}
    assert real_arm_stats(only_clean, "new")["recall"] is None
    only_dirty = {"channels": {"A": {"lab": [1, 1], "d": [0, 0], "d0": [0, 0]}}}
    assert real_arm_stats(only_dirty, "new")["kill_rate"] is None
    assert real_arm_stats(None, "new")["n"] == 0


def test_real_window_mix_partitions_all_windows():
    """三分解之和必须等于总窗数——「未评」不许悄悄并进「通过」。"""
    ds = _real_payload()["datasets"][0]
    mix = real_window_mix(ds)
    counts = {r["状态"]: r["窗数"] for r in mix}
    assert counts["全算子都评过（真通过）"] == 1
    assert counts["通过但有算子未评"] == 3
    assert counts["被丢弃"] == 2
    assert sum(counts.values()) == 6 == real_arm_stats(ds, "new")["n"]


def test_real_op_rows_reports_percentage_points_and_forms():
    rows = real_op_rows(_real_payload()["datasets"][0])
    assert [r["算子"] for r in rows] == ["sensor_stuck", "sensor_range"]
    stuck = rows[0]
    assert stuck["旧·误杀率%"] == 50.0        # 分数 × 100（百分点），列名带 %
    assert stuck["新·误杀率%"] == 25.0
    assert stuck["命中的判据形态"] == "machine_stop×2 · scale_collapse×1"
    assert rows[1]["命中的判据形态"] == "—"   # 没有命中的形态别留空串
    assert real_op_rows(None) == []


def test_real_unscored_total_sums_sample_operator_pairs():
    ds = _real_payload()["datasets"][0]
    assert real_unscored_total(ds) == 8      # 2 + 6：每一对都是一格「没干活」
    assert real_unscored_total(None) == 0


def test_real_curve_rows_and_nearest_point():
    ds = _real_payload()["datasets"][0]
    rows = real_curve_rows(ds)
    assert len(rows) == 4
    assert {r["尺度"] for r in rows} == {"mad", "pooled"}
    assert real_curve_rows(None) == []
    assert real_curve_rows(ds, "no_such_op") == []

    # 网格外的 z 取最近档（并如实报告取到的是哪一档）
    assert real_curve_point(rows, "mad", 7.0)["z"] == 8
    assert real_curve_point(rows, "mad", 3.0)["z"] == 4
    assert real_curve_point(rows, "mad", 4.0)["z"] == 4
    assert real_curve_point(rows, "nope", 4.0) is None
    assert real_curve_point([], "mad", 4.0) is None


def test_real_applicability_and_reachability_rows():
    ds = _real_payload()["datasets"][0]
    app = real_applicability_rows(ds)
    assert app[0]["本数据集适用"] == "否（空转）" and app[0]["靶子数"] == 0
    assert real_applicability_rows(None) == []

    reach = real_reachability_rows(ds)
    assert reach[0]["要求"] == "≥10 窗" and reach[0]["可达"] == "否"
    assert real_reachability_rows(None) == []


def test_real_kill_rows_filters_and_limit():
    ds = _real_payload()["datasets"][0]
    assert len(real_kill_rows(ds)) == 3
    assert len(real_kill_rows(ds, op="sensor_stuck")) == 2
    assert len(real_kill_rows(ds, channel="A")) == 2
    assert [r["通道"] for r in real_kill_rows(ds, op="sensor_stuck", channel="A")] == ["A"]
    # 「只看已标脏」= 把「其实是真坏窗」的那部分摘出来，这是误杀率上界的复核动作
    labeled = real_kill_rows(ds, labeled_only=True)
    assert [r["数据集标签"] for r in labeled] == ["fault_x", "fault_y"]
    assert len(real_kill_rows(ds, limit=1)) == 1
    assert real_kill_rows(None) == []

    row = real_kill_rows(ds, channel="B")[0]
    assert row["数据集标签"] == "（无标签）"   # 无标签如实写出来，不留空
    assert row["窗口起始"].startswith("19")  # epoch 秒 → 可读时间（本机时区）


def test_real_kill_filters_read_from_data_not_hardcoded():
    ops, chans = real_kill_filters(_real_payload()["datasets"][0])
    assert ops == ["sensor_range", "sensor_stuck"]
    assert chans == ["A", "B"]
    assert real_kill_filters(None) == ([], [])


def test_rows_to_csv_has_header_and_rows():
    rows = [{"算子": "a", "丢弃": 1}, {"算子": "b", "丢弃": 2}]
    text = rows_to_csv(rows)
    assert text.splitlines()[0] == "算子,丢弃"
    assert len(text.splitlines()) == 3
    assert rows_to_csv([]) == ""   # 空表返回空串，调用方据此禁用下载按钮


def test_image_domain_blurb_uses_registry_count_not_a_stale_number():
    """回归：`DOMAINS["image"]["blurb"]` 曾写「**11 级滤芯**」——实点是 12。

    11 恰好是 `text_article` 模态的数量（两个数字串了档）。这条把「图文模态数」
    与注册表现算绑定：以后加/减算子，blurb 会自己跟上，串档则立刻红。
    """
    import showcase_app as app

    if not MODALITY_COUNTS:
        pytest.skip("注册表不可用（裸环境，import 失败时数字按设计降级）")

    from curation_eval.registry import available_operator_metas

    import mm_curation.operators  # noqa: F401  —— 注册靠导入触发

    metas = available_operator_metas()
    live = sum(1 for m in metas.values() if "image_caption" in (m.modalities or ()))
    assert live == MODALITY_COUNTS["image_caption"]
    assert f"{live} 级滤芯" in app.DOMAINS["image"]["blurb"]
    assert modality_phrase("image_caption") == f"{live} 级滤芯"
    assert modality_phrase("no_such_modality") == "多级滤芯"   # 取不到就不编数字
    assert app._modality_breakdown().count("/") == 3           # 四段：图文/文本/医疗/工业
    # 四个双模态算子被两边各算一次，所以四段之和 ≥ 算子总数（不是相等）
    assert all(v > 0 for v in MODALITY_COUNTS.values())
    assert sum(MODALITY_COUNTS.values()) >= len(metas)
