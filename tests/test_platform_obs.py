"""观测层测试：新鲜度、行数稳健限、管道健康、**告警收敛**、Prometheus 暴露。"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

duckdb = pytest.importorskip("duckdb")

from mm_curation.platform import obs  # noqa: E402
from mm_curation.platform.runs import FAILED, RUNNING, SUCCESS, RunLedger  # noqa: E402

DDL = """
CREATE TABLE ads_dataset_health (
    dataset VARCHAR, last_event_date DATE, freshness_days DOUBLE,
    n_total BIGINT, n_undated_partitions BIGINT
);
CREATE TABLE dws_dataset_day (dataset VARCHAR, event_date DATE, n_total BIGINT);
CREATE TABLE dwd_window (dataset VARCHAR, channel VARCHAR, device_sk BIGINT);
"""


def _con(ads=(), days=(), dwd=()):
    con = duckdb.connect()
    con.execute(DDL)
    for r in ads:
        con.execute("INSERT INTO ads_dataset_health VALUES (?,?,?,?,?)", list(r))
    for r in days:
        con.execute("INSERT INTO dws_dataset_day VALUES (?,?,?)", list(r))
    for r in dwd:
        con.execute("INSERT INTO dwd_window VALUES (?,?,?)", list(r))
    return con


# ---------------------------------------------------------------------------
# 新鲜度
# ---------------------------------------------------------------------------


def test_freshness_flags_breach_and_leaves_unevaluated_alone():
    con = _con(
        ads=[
            ("live", _dt.date(2026, 9, 20), 2.0, 100, 0),
            ("stale", _dt.date(2020, 1, 1), 2400.0, 50, 0),
            ("undated", None, None, 10, 3),
        ]
    )
    try:
        rows = {r["dataset"]: r for r in obs.freshness(con, slo_days=7.0)}
    finally:
        con.close()
    assert rows["live"]["ok"] is True
    assert rows["stale"]["ok"] is False
    # 没有事件时间 → **未评**（None），不是"合格"也不是"不合格"
    assert rows["undated"]["ok"] is None
    assert rows["undated"]["evaluated"] is False
    assert rows["undated"]["n_undated_partitions"] == 3


def test_freshness_per_dataset_slo_override():
    """归档数据集与活数据集不能共用一个 SLO——否则两种情况下都报警，也就两种都忽略。"""
    con = _con(
        ads=[
            ("archive", _dt.date(2000, 1, 1), 9741.0, 10, 0),
            ("live", _dt.date(2020, 1, 1), 2400.0, 10, 0),
        ]
    )
    try:
        rows = {
            r["dataset"]: r
            for r in obs.freshness(
                con, slo_days=7.0, per_dataset={"archive": {"freshness_days": 36500.0}}
            )
        }
    finally:
        con.close()
    assert rows["archive"]["ok"] is True, "归档数据集给宽限后不该破线"
    assert rows["archive"]["slo_days"] == 36500.0
    assert rows["live"]["ok"] is False, "没有覆盖的数据集仍按默认 7 天"
    # 原始滞后天数**始终照报**——被放宽的只是"破线"这个判断，不是数字本身
    assert rows["archive"]["freshness_days"] == 9741.0


# ---------------------------------------------------------------------------
# 行数异常（稳健限）
# ---------------------------------------------------------------------------


def _steady_days(dataset="d", n=20, base=100):
    return [
        (dataset, _dt.date(2026, 1, 1) + _dt.timedelta(days=i), base + (i % 3)) for i in range(n)
    ]


def test_volume_anomaly_detects_spike_and_drop():
    days = _steady_days()
    days[5] = ("d", days[5][1], 900)  # spike
    days[9] = ("d", days[9][1], 0)  # drop
    con = _con(days=days)
    try:
        got = obs.volume_anomalies(con, margin=3.0, min_days=8)
    finally:
        con.close()
    # `event_date` 在这里已经是字符串（`volume_anomalies` 为 JSON 友好做了 str()）
    directions = {g["event_date"]: g["direction"] for g in got}
    assert directions.get("2026-01-06") == "spike"
    assert directions.get("2026-01-10") == "drop"
    for g in got:
        # 每条都必须带得出限值，看到的人才能自己复核阈值
        assert g["limit_hi"] > g["limit_lo"]
        assert g["center"] > 0


def test_volume_anomaly_skips_datasets_below_min_days():
    """天数不够时**不定限**（宁可不报，也不报一个不可信的限）。"""
    days = _steady_days(n=5)
    days[2] = ("d", days[2][1], 500)  # 明显的尖峰
    con = _con(days=days)
    try:
        assert obs.volume_anomalies(con, min_days=8) == [], "5 天 < 8 天门槛 → 不评"
        got = obs.volume_anomalies(con, min_days=3)
        assert [g["direction"] for g in got] == ["spike"]
    finally:
        con.close()


def test_converge_dim_unmatched_signal_does_not_crash():
    """回归：`Signal(**d)` 会把计算属性 `fingerprint` 当构造参数 → TypeError。

    这条路径此前从未被执行过（真实数据里 `n_unmatched_sensor == 0`），
    也就是说**告警链路恰好在唯一需要报警的时候会崩**。必须有用例钉住。
    """
    s = obs.Signal("dim_unmatched", obs.SEV_CRIT, "3 条未匹配", dataset="d1", value=3, limit=0)
    d = s.to_dict()
    assert "fingerprint" in d
    raised = False
    try:
        obs.Signal(**d)
    except TypeError:
        raised = True
    assert raised, "确认 to_dict 的产物确实不能直接 ** 还原（这就是当初的坑）"

    # 正确的还原路径（collect_signals 用的就是它）
    sig = obs.collect_signals({"dim_unmatched": [d]})
    assert len(sig) == 1
    assert sig[0].fingerprint == "dim_unmatched:d1"
    assert sig[0].severity == obs.SEV_CRIT


def test_volume_anomaly_skips_zero_scale_series():
    """零离散度 → 定不出限 → 记"未评"而不是硬造一个阈值出来。"""
    days = [("d", _dt.date(2026, 1, 1) + _dt.timedelta(days=i), 100) for i in range(20)]
    con = _con(days=days)
    try:
        assert obs.volume_anomalies(con, margin=3.0, min_days=8) == []
    finally:
        con.close()


def test_volume_anomaly_ignores_undated_rows():
    days = _steady_days() + [("d", None, 9999)]
    con = _con(days=days)
    try:
        got = obs.volume_anomalies(con, min_days=8)
    finally:
        con.close()
    assert all(g["event_date"] is not None for g in got)


# ---------------------------------------------------------------------------
# 管道健康：**在途运行不算失败**
# ---------------------------------------------------------------------------


def test_pipeline_health_excludes_running_from_success_rate(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    for rid, st in (("r1", SUCCESS), ("r2", FAILED), ("r3", RUNNING)):
        led.start_run(rid, "platform", "2026-09-22")
        if st != RUNNING:
            led.finish_run(rid, st)
    ph = obs.pipeline_health(led)
    assert ph["n_runs"] == 3
    assert ph["n_running"] == 1
    # 分母只算已终态：1 成功 / 2 终态 = 0.5。
    # 若把 RUNNING 也算进分母就是 0.33，而且**每次运行都会把自己算成失败**
    assert ph["success_rate"] == 0.5


def test_pipeline_health_without_runs_returns_none_rate(tmp_path):
    ph = obs.pipeline_health(RunLedger(tmp_path / "l.duckdb"))
    assert ph["n_runs"] == 0 and ph["success_rate"] is None


# ---------------------------------------------------------------------------
# 管道失败信号：**在途 RUNNING 既不算失败，也不该被反复补账**
# ---------------------------------------------------------------------------


def _recent(*items):
    """构造 `pipeline.recent` 片段（顺序与 SQL 一致：started_at DESC）。"""
    return {
        "pipeline": {
            "recent": [
                {
                    "run_id": rid,
                    "status": st,
                    "batch_date": bd,
                    "duration_s": None if st == RUNNING else 1.0,
                }
                for rid, st, bd in items
            ]
        }
    }


def test_collect_signals_ignores_inflight_running_run():
    """在途 RUNNING **不是**管道失败。

    `obs` 阶段必然在一次运行**进行中**执行，所以"最近一次不是 SUCCESS"永远成立——
    原实现据此每次都报一条 crit，实测形态是"9 个阶段全绿却报管道失败"。
    """
    assert obs.collect_signals(_recent(("r1", RUNNING, "2026-09-22"))) == []


def test_collect_signals_reports_only_the_latest_terminal_run():
    """历史失败由**指标**承载（success_rate 全算进去）；告警只报最近一次终态。

    原实现遍历 `recent` 的 10 条，每条非 SUCCESS 都补一个信号，
    而消息里写着"最近一次运行"——失败越多报得越多，且名实不符。
    """
    sig = obs.collect_signals(
        _recent(
            ("cur", RUNNING, "2026-09-22"),
            ("old2", FAILED, "2026-09-21"),
            ("old1", FAILED, "2026-09-20"),
        )
    )
    assert len(sig) == 1, "3 条 recent 里最多只该报 1 条，不是每条失败各报一条"
    assert sig[0].kind == "pipeline_failure"
    assert "old2" in sig[0].message, "报的必须是**最近一条终态**，消息不能名实不符"


def test_collect_signals_silent_when_latest_terminal_is_success():
    """最新终态是 SUCCESS 就不报——历史那次 FAILED 归 success_rate 管。"""
    assert (
        obs.collect_signals(
            _recent(
                ("cur", RUNNING, "2026-09-22"),
                ("ok", SUCCESS, "2026-09-22"),
                ("bad", FAILED, "2026-09-21"),
            )
        )
        == []
    )


def test_snapshot_first_run_has_no_pipeline_failure_alert(tmp_path):
    """首跑真实形态：台账里只有**本次在途运行**时，快照不得含管道失败告警。

    这正是空台账上第一次跑全链路的处境——分母为 0 时 `success_rate` 是 `None`
    （"还没有结论"，不是 `0.0` 的"全都失败"），并且**不能**因此报一条 crit。
    """
    led = RunLedger(tmp_path / "l.duckdb")
    led.start_run("platform__r1", "platform", "2026-09-22")  # 故意不 finish：模拟在途
    con = _con()
    try:
        snap = obs.snapshot(con, led)
    finally:
        con.close()
    assert snap["pipeline"]["success_rate"] is None
    assert snap["pipeline"]["n_running"] == 1
    assert snap["pipeline"]["n_terminal"] == 0
    assert [a for a in snap["alerts"] if a["kind"] == "pipeline_failure"] == []


# ---------------------------------------------------------------------------
# 信号与收敛
# ---------------------------------------------------------------------------


def test_signal_fingerprint_groups_by_kind_and_dataset_only():
    a = obs.Signal("volume_spike", obs.SEV_WARN, "x", dataset="d1", at="2026-01-01")
    b = obs.Signal("volume_spike", obs.SEV_WARN, "y", dataset="d1", at="2026-01-09")
    c = obs.Signal("volume_spike", obs.SEV_WARN, "z", dataset="d2", at="2026-01-02")
    assert a.fingerprint == b.fingerprint != c.fingerprint


def test_converge_merges_same_cause_and_keeps_time_range():
    sig = [
        obs.Signal("volume_spike", obs.SEV_WARN, f"第{i}天", dataset="d1", at=f"2026-01-{i:02d}")
        for i in range(1, 9)
    ]
    alerts = obs.converge(sig)
    assert len(alerts) == 1, "同因的 8 条信号必须收敛成 1 条告警"
    a = alerts[0]
    assert a.n_signals == 8
    assert a.first == "2026-01-01" and a.last == "2026-01-08"
    assert len(a.values) == 8


def test_converge_escalates_severity_and_sorts_crit_first():
    sig = [
        obs.Signal("a", obs.SEV_INFO, "低", dataset="d1", at="2026-01-01"),
        obs.Signal("a", obs.SEV_CRIT, "高", dataset="d1", at="2026-01-02"),
        obs.Signal("b", obs.SEV_WARN, "中", dataset="d2", at="2026-01-01"),
    ]
    alerts = obs.converge(sig)
    assert [a.kind for a in alerts] == ["a", "b"]
    assert alerts[0].severity == obs.SEV_CRIT
    assert alerts[0].sample == "高", "样本消息应取最高严重度的那条"


# ---------------------------------------------------------------------------
# 快照 / 暴露
# ---------------------------------------------------------------------------


def _snapshot_fixture(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    led.start_run("r1", "platform", "2026-09-22")
    led.start_task("r1", "ods", 1)
    led.finish_task("r1", "ods", SUCCESS, n_out=10)
    led.start_task("r1", "dwd", 2)
    led.finish_task("r1", "dwd", SUCCESS, n_out=10)
    led.finish_run("r1", SUCCESS)
    days = _steady_days("live", n=20, base=100)
    # 放 3 个尖峰：它们 fingerprint 相同（`volume_spike:live`）→ 收敛成 1 条告警
    for i in (5, 12, 17):
        days[i] = ("live", days[i][1], 900)
    con = _con(
        ads=[
            ("live", _dt.date(2026, 1, 20), 300.0, 2000, 0),
            ("archive", _dt.date(2000, 1, 1), 9741.0, 30, 0),
            ("undated", None, None, 5, 2),
        ],
        days=days,
        dwd=[("live", "TP2", None)],  # 有通道但没维版本 → 真问题
    )
    return con, led


def test_snapshot_shape_and_convergence_ratio(tmp_path):
    con, led = _snapshot_fixture(tmp_path)
    try:
        snap = obs.snapshot(con, led, per_dataset={"archive": {"freshness_days": 36500.0}})
    finally:
        con.close()
    assert {"freshness", "volume_anomalies", "pipeline", "dim_unmatched", "alerts"} <= set(snap)
    conv = snap["convergence"]
    assert conv["raw_signals"] >= conv["converged_alerts"] >= 1
    # 3 个尖峰 → 同指纹 → 必须收敛，所以 ratio > 0
    assert conv["raw_signals"] > conv["converged_alerts"]
    assert 0.0 < conv["ratio"] < 1.0
    kinds = {a["kind"] for a in snap["alerts"]}
    assert "freshness_breach" in kinds  # live 停在 2026-01-20，滞后远超 7 天
    assert "dim_unmatched" in kinds  # 真问题必须报
    assert "undated_partition" in kinds  # 未标日期分区如实上报
    assert "volume_spike" in kinds
    # 归档数据集被按数据集放宽了 SLO（36500 天）→ 不该出现在破线告警里；
    # 而活数据集（默认 7 天）该出现。**放宽阈值不是把指标做好看，是区分两类数据集**。
    assert not any(a["dataset"] == "archive" for a in snap["alerts"])


def test_dim_signals_ignores_rows_without_channel():
    con = _con(dwd=[("live", "", None), ("live", "TP2", 7)])
    try:
        assert obs.dim_signals(con) == []
    finally:
        con.close()
    con = _con(dwd=[("live", "TP2", None)])
    try:
        got = obs.dim_signals(con)
        assert len(got) == 1 and got[0].severity == obs.SEV_CRIT and got[0].value == 1
    finally:
        con.close()


def test_prometheus_text_is_wellformed(tmp_path):
    con, led = _snapshot_fixture(tmp_path)
    try:
        snap = obs.snapshot(con, led, per_dataset={"archive": {"freshness_days": 36500.0}})
    finally:
        con.close()
    txt = obs.prometheus_text(snap)
    lines = txt.splitlines()
    assert txt.endswith("\n")
    helps = {ln.split()[2] for ln in lines if ln.startswith("# HELP")}
    assert {
        "mm_dataset_freshness_days",
        "mm_dataset_rows",
        "mm_pipeline_runs_total",
        "mm_alerts_active",
    } <= helps
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        # 每行必须是 `name{labels} value` 且 value 可解析为浮点
        assert ln.split(" ")[-1].replace(".", "").replace("-", "").isdigit() or ln.split(" ")[
            -1
        ] in ("0", "1")
    # 未评（None）的数据集不该出现"新鲜度"这条序列（宁缺勿造）
    for ln in lines:
        if ln.startswith("mm_dataset_freshness_days{") and 'dataset="undated"' in ln:
            raise AssertionError("未评数据集不该有新鲜度序列")


def test_write_snapshot_roundtrips(tmp_path):
    snap = {"a": 1, "b": {"c": _dt.date(2026, 1, 1)}}
    p = obs.write_snapshot(snap, tmp_path / "nested" / "o.json")
    assert p.exists()
    back = json.loads(p.read_text(encoding="utf-8"))
    assert back["b"]["c"] == "2026-01-01"


def test_load_platform_config_missing_file_returns_defaults(tmp_path):
    cfg = obs.load_platform_config(tmp_path / "nope.yaml")
    assert cfg["slo"] == obs.DEFAULT_SLO
    assert cfg["datasets"] == {}


def test_load_platform_config_merges_slo(tmp_path):
    p = tmp_path / "platform.yaml"
    p.write_text(
        "slo:\n  freshness_days: 30\n  volume_margin: 5\ndatasets:\n  x:\n    freshness_days: 1\n",
        encoding="utf-8",
    )
    cfg = obs.load_platform_config(p)
    assert cfg["slo"]["freshness_days"] == 30
    assert cfg["slo"]["volume_margin"] == 5
    assert cfg["slo"]["min_days_for_volume"] == obs.DEFAULT_SLO["min_days_for_volume"]
    assert cfg["datasets"]["x"]["freshness_days"] == 1


def test_summarize_is_single_line(tmp_path):
    con, led = _snapshot_fixture(tmp_path)
    try:
        s = obs.summarize(obs.snapshot(con, led))
    finally:
        con.close()
    assert "\n" not in s
    assert "收敛告警" in s
