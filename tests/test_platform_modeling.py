"""建模层测试：ODS → 维（SCD-2）→ DWD → DWS → ADS → 视图。

每个断言都对准一条**真实踩过的坑**，不是"跑通即可"：
  * 事件时间未知的行必须是 NULL 分区，不能是字符串 `"unknown"`；
  * 有通道却没维版本的传感器行必须是 `n_unmatched_sensor`（真问题），
    与"本来就没有通道"的 `n_no_channel` 分开上报；
  * SCD-2 变更必须**先封旧版本、再开新版本**，且代理键随版本变。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from mm_curation.platform import modeling  # noqa: E402
from mm_curation.platform.lake import Lake  # noqa: E402


def _lake(root: Path) -> Lake:
    return Lake(root / "data" / "lake")


def _con():
    return duckdb.connect()


def _write_sensor(root: Path, rows: list[dict]) -> None:
    p = root / "data" / "raw" / "real" / "metropt3" / "windows.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    p.write_text(text, encoding="utf-8")


def test_build_ods_parses_event_time_from_meta_and_url(lake_root: Path):
    lake = _lake(lake_root)
    rep = modeling.build_ods(lake, lake_root)
    assert rep["n_rows"] == 4 + 3
    assert rep["per_dataset"]["metropt3"] == 4
    assert rep["per_dataset"]["news_corpus"] == 3
    # 只造了 2 个源；其余 10 个必须走 SKIPPED 而不是被静默忽略
    assert len(rep["sources_used"]) == 2
    assert len(rep["sources_skipped"]) == 10


def test_build_ods_incremental_skip_dates_does_not_rewrite(lake_root: Path):
    """增量的**真实语义**：按分区值跳过已有分区。

    两处必须如实记录的边界：
    1. 读取侧收益为**零**——jsonl 是平面文件，没有分区目录可下推，
       "跳过"只发生在解析之后（`modeling.build_ods` 的文档写了这条）。
       所以这里断言的是"没有把旧分区重写一遍"，不是"少读了字节"。
    2. **未标事件时间的行无法被跳过**：它的分区目录名是
       `__HIVE_DEFAULT_PARTITION__`，不是某个日期值，`partition_values()` 按设计
       不返回它（由 `has_undated()` 单独回答）。所以这一行总会被重写——
       需要完全跳过它必须显式 `keep_undated=False`。
    """
    lake = _lake(lake_root)
    modeling.build_ods(lake, lake_root)
    before = {p for p in (lake_root / "data" / "lake" / "ods").rglob("*.parquet")}
    skip = tuple(sorted(lake.partition_values("ods", "ods_samples")))
    assert len(skip) == 3, f"3 个日期分区（04-01/04-02/2026-09-06），实际 {skip}"

    rep = modeling.build_ods(lake, lake_root, skip_dates=skip)
    after = {p for p in (lake_root / "data" / "lake" / "ods").rglob("*.parquet")}
    assert rep["n_rows"] == 1, "只剩那 1 条未标日期的记录被写入"
    assert rep["n_skipped_records"] == 6
    assert after == before, "重写的分区文件集合不变（幂等）"

    rep2 = modeling.build_ods(lake, lake_root, skip_dates=skip, keep_undated=False)
    assert rep2["n_rows"] == 0
    assert rep2["n_skipped_records"] == 7


def test_dim_scd2_row_per_natural_key_and_no_row_without_channel(lake_root: Path):
    lake = _lake(lake_root)
    modeling.build_ods(lake, lake_root)
    con = _con()
    try:
        rep = modeling.build_dims(con, lake)
        # 3 个 (device_id, channel)：compressor_01/TP2、compressor_01/Reservoirs、compressor_02/TP2
        # mtp3_0004 有 device_id 没有 channel → 按设计不建维行
        assert rep["n_observed_keys"] == 3
        assert rep["n_current"] == 3
        assert rep["n_versions"] == 3
        assert rep["n_changes"] == 0
        # 落湖后要 `refresh_views` 才能在库里查到（#97：维表不再是 BASE TABLE）
        modeling.refresh_views(con, lake)
        # 代理键必须唯一且非空（SCD-2 的主键约束）
        bad = con.execute("SELECT count(*) FROM dim_device WHERE device_sk IS NULL").fetchone()[0]
        assert bad == 0
    finally:
        con.close()


def test_scd2_closes_old_version_and_opens_new_one(lake_root: Path, metropt3_rows):
    lake = _lake(lake_root)
    con = _con()
    try:
        modeling.build_ods(lake, lake_root, datasets=("metropt3",))
        r1 = modeling.build_dims(con, lake)
        assert r1["n_versions"] == 3

        # 第二批：compressor_01/TP2 的单位改为 K，且**该键在本批只剩新观测**。
        #
        # 为什么必须这样构造：`build_dims` 用 `arg_min(attr, event_date)` 取"本批首见属性"
        # （见其 docstring 的边界说明）。若同一批次里旧观测（04-01, degC）与新观测
        # （04-03, K）同时存在，`arg_min` 会取到最早那条 → **变化被本实现静默忽略**。
        # 这是真限制，不是测试取巧：真实场景里"属性从某天起变了"就是按天各见一次的。
        rows = [dict(r) for r in metropt3_rows if r["id"] != "mtp3_0001"]
        changed = json.loads(json.dumps(metropt3_rows[0]))
        changed["meta"]["unit"] = "K"
        changed["meta"]["window_start"] = "2020-04-03T00:00:00"
        changed["id"] = "mtp3_0005"
        _write_sensor(lake_root, [*rows, changed])
        # 04-01 分区被重写：TP2 那条已从源里移除，只剩 Reservoirs
        modeling.build_ods(lake, lake_root, datasets=("metropt3",))

        r2 = modeling.build_dims(con, lake)
        assert r2["n_versions"] == 4, "属性变化必须产生新版本"
        assert r2["n_current"] == 3, "同一自然键只能有一个当前版本"
        assert r2["n_changes"] >= 1

        # 落湖后要 `refresh_views` 才能在库里查到（#97）
        modeling.refresh_views(con, lake)
        old = con.execute(
            "SELECT valid_to, is_current FROM dim_device "
            "WHERE device_id='compressor_01' AND channel='TP2' AND version=1"
        ).fetchone()
        assert old[1] is False
        assert str(old[0]) == "2020-04-03", f"旧版本应封口到变化发生那天，实际 {old[0]}"
        # 新版本存在且是当前版本
        new = con.execute(
            "SELECT unit, valid_from, is_current FROM dim_device "
            "WHERE device_id='compressor_01' AND channel='TP2' AND version=2"
        ).fetchone()
        assert new == ("K", _dt.date(2020, 4, 3), True)
    finally:
        con.close()


def test_dwd_separates_no_channel_from_unmatched_sensor(lake_root: Path):
    """DWD 的两个未匹配指标**不许合并**：一个描述事实，一个描述故障。"""
    lake = _lake(lake_root)
    con = _con()
    try:
        modeling.build_ods(lake, lake_root)
        modeling.build_dims(con, lake)
        rep = modeling.build_dwd(con, lake)
        assert rep["n_in"] == 7
        assert rep["n_out"] == 7  # DWD 是 ODS 的一一映射，join 不许吞行
        assert rep["n_no_channel"] == 4, "3 条文本 + mtp3_0004（有 device 无 channel）"
        assert rep["n_unmatched_sensor"] == 0, "有通道的行必须全部关联上维版本"
        assert rep["n_scores"] == 4, "4 条记录带 score: 前缀的分数，展开成 4 行"
    finally:
        con.close()


def test_dwd_event_date_join_tolerates_undated_rows(lake_root: Path):
    """含 NULL 事件日的批次里，SCD-2 按区间关联不能因类型比较炸掉。

    这是那个类型陷阱的端到端回归：只要有任何一条把 `"unknown"` 写进分区，
    这一句就会报 `Cannot compare VARCHAR and DATE`。
    """
    lake = _lake(lake_root)
    con = _con()
    try:
        modeling.build_ods(lake, lake_root)  # news 有 1 条未标日期
        assert lake.has_undated("ods", "ods_samples") is True
        modeling.build_dims(con, lake)
        rep = modeling.build_dwd(con, lake)
        assert rep["n_out"] == 7
    finally:
        con.close()


def test_dws_and_ads_aggregate_and_report_undated_partitions(lake_root: Path):
    lake = _lake(lake_root)
    con = _con()
    try:
        modeling.build_ods(lake, lake_root)
        modeling.build_dims(con, lake)
        modeling.build_dwd(con, lake)
        dws = modeling.build_dws(con, lake)
        assert dws["n_out"] >= 3
        ads = modeling.build_ads(con, lake)
        assert ads["n_out"] == 2  # metropt3 + news_corpus
        # `ads_dataset_health` 是**湖上 parquet 的视图**（不是表），要先登记视图。
        # 这正是 jobs 把 views 单独立成一个阶段的原因：视图必须先于任何 SQL 消费者。
        modeling.refresh_views(con, lake)

        rows = dict(
            (r[0], r[1:])
            for r in con.execute(
                "SELECT dataset, last_event_date, n_total, n_undated_partitions, freshness_days "
                "FROM ads_dataset_health ORDER BY dataset"
            ).fetchall()
        )
        # news_corpus 里 1 条没有事件时间 → ADS 必须**如实上报**未标日期分区数，
        # 而不许把它当成"今天"（那是把"不知道"伪装成"知道"）
        assert rows["news_corpus"][2] == 1
        assert rows["news_corpus"][0] == _dt.date(2026, 9, 6)
        assert rows["metropt3"][0] == _dt.date(2020, 4, 2)
    finally:
        con.close()


def test_refresh_views_makes_all_layers_queryable(lake_root: Path):
    lake = _lake(lake_root)
    con = _con()
    try:
        modeling.build_ods(lake, lake_root)
        modeling.build_dims(con, lake)
        modeling.build_dwd(con, lake)
        modeling.build_dws(con, lake)
        modeling.build_ads(con, lake)
        made = modeling.refresh_views(con, lake)
        assert {"ods_all", "dwd_window", "ads_dataset_health"} <= set(made)
        for v in ("ods_samples", "dwd_samples", "dwd_scores", "dws_dataset_profile", "ads_metrics"):
            assert con.execute(f"SELECT count(*) FROM {v}").fetchone()[0] >= 0
    finally:
        con.close()


def test_ads_avg_survives_all_null_varchar_partition():
    """回归（路线 D 实测）：全 NULL 数值列被 lake 兜底落成 VARCHAR，
    ADS 的 avg() 在 binder 抛异常致 run FAILED（realdata__0002/0003 同因）。
    修复 = avg(try_cast(... AS DOUBLE))。
    """
    import duckdb

    con = duckdb.connect()
    con.execute(
        "create table dws_dataset_day(score_coverage varchar, avg_len double, n_total bigint)"
    )
    con.execute("insert into dws_dataset_day values (NULL, 12.5, 10), (NULL, 7.5, 5)")
    # 旧写法在这里就抛 BinderException；修复后的写法必须返回一行 NULL 均值
    row = con.sql(
        "select avg(try_cast(score_coverage as double)) as m, avg(avg_len) as l "
        "from dws_dataset_day"
    ).fetchone()
    assert row[0] is None and row[1] == 10.0
    # 有值时 try_cast 不改变语义
    con.execute("update dws_dataset_day set score_coverage = '0.5' where n_total = 10")
    row = con.sql("select avg(try_cast(score_coverage as double)) from dws_dataset_day").fetchone()
    assert abs(row[0] - 0.5) < 1e-9  # 只有一行非 NULL
