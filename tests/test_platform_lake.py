"""湖层（`platform/lake.py`）测试：分区、类型、幂等、裁剪证据。"""

from __future__ import annotations

import datetime as _dt

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from mm_curation.platform.lake import (  # noqa: E402
    HIVE_DEFAULT_PARTITION,
    Lake,
    hive_value,
)


def _rows() -> list[dict]:
    return [
        {"dataset": "d1", "event_date": "2026-01-01", "v": 1, "flag": True, "f": 0.5},
        {"dataset": "d1", "event_date": "2026-01-02", "v": 2, "flag": False, "f": 1.5},
        {"dataset": "d2", "event_date": "2026-01-01", "v": 3, "flag": True, "f": 2.5},
    ]


def test_hive_value_maps_none_to_default_partition():
    assert hive_value(None) == HIVE_DEFAULT_PARTITION
    assert hive_value("2026-01-01") == "2026-01-01"


def test_write_creates_hive_partition_directories(tmp_path):
    lake = Lake(tmp_path / "lake")
    rep = lake.write("ods", _rows(), table="t")
    assert rep["n_rows"] == 3
    # 3 个 (dataset, event_date) 组合 → 3 个分区目录
    assert len(rep["partitions"]) == 3
    dirs = {p.name for p in (tmp_path / "lake" / "ods" / "t").rglob("*") if p.is_dir()}
    assert {"dataset=d1", "dataset=d2", "event_date=2026-01-01"} <= dirs


def test_scan_sql_exposes_partition_columns_and_types(tmp_path):
    lake = Lake(tmp_path / "lake")
    lake.write("ods", _rows(), table="t")
    con = duckdb.connect()
    try:
        sql = lake.scan_sql(
            "ods", table="t", where="dataset = 'd1'", columns="dataset, event_date, v"
        )
        got = con.execute(sql).fetchall()
        assert got == [
            ("d1", _dt.date(2026, 1, 1), 1),
            ("d1", _dt.date(2026, 1, 2), 2),
        ]
        # 分区列的类型由目录值推断 —— 这正是"写错一个字符串就毁掉整列"的地方
        types = {
            d[0]: d[1]
            for d in con.execute(
                "DESCRIBE SELECT * FROM read_parquet("
                f"'{lake.parquet_glob('ods', 't')}', hive_partitioning=true)"
            ).fetchall()
        }
        assert types["event_date"] == "DATE"
        assert types["flag"] == "BOOLEAN"
    finally:
        con.close()


def test_undated_partition_keeps_date_type(tmp_path):
    """核心回归：事件时间未知的行**不能**写成字符串 'unknown'。

    写成 `event_date="unknown"` 会把整列推断成 VARCHAR，之后再拿它和
    `dim_device.valid_from`（DATE）比较就是 `Cannot compare VARCHAR and DATE`。
    正确做法是写 NULL → Hive 默认分区目录 → 列类型仍是 DATE。
    """
    lake = Lake(tmp_path / "lake")
    rows = _rows() + [{"dataset": "d1", "event_date": None, "v": 9, "flag": False, "f": 0.1}]
    lake.write("ods", rows, table="t")
    part = tmp_path / "lake" / "ods" / "t" / "dataset=d1"
    assert (part / f"event_date={HIVE_DEFAULT_PARTITION}").exists()
    con = duckdb.connect()
    try:
        sql = lake.scan_sql("ods", table="t")
        ty = con.execute(f"DESCRIBE SELECT * FROM ({sql})").fetchall()
        col_types = {d[0]: d[1] for d in ty}
        assert col_types["event_date"] == "DATE", col_types
        # 未标日期的行能被 try_cast 安全处理（DWD 的 SCD-2 join 就靠这一条）
        n = con.execute(
            f"SELECT count(*) FROM ({sql}) WHERE try_cast(event_date AS DATE) IS NULL"
        ).fetchone()[0]
        assert n == 1
    finally:
        con.close()


def test_write_replace_is_idempotent(tmp_path):
    """同一批输入重写 → 文件数不翻倍、产出指纹一致。"""
    lake = Lake(tmp_path / "lake")
    r1 = lake.write("ods", _rows(), table="t")
    r2 = lake.write("ods", _rows(), table="t")
    assert r1["n_files"] == r2["n_files"]
    assert r1["bytes"] == r2["bytes"]
    assert r1["partitions"] == r2["partitions"]


def test_partition_values_and_has_undated(tmp_path):
    lake = Lake(tmp_path / "lake")
    extra = {"dataset": "d1", "event_date": None, "v": 9, "flag": False, "f": 0.0}
    lake.write("ods", [*_rows(), extra], table="t")
    vals = lake.partition_values("ods", "t")
    assert "2026-01-01" in vals
    assert lake.has_undated("ods", "t") is True
    assert lake.has_undated("ods", "nonexistent") is False


def test_prune_report_gives_byte_level_evidence(tmp_path):
    """分区裁剪要说的是"少扫了多少**字节**"，不是"少开了几个文件"。"""
    lake = Lake(tmp_path / "lake")
    rows = _rows() + [
        {"dataset": f"d{i}", "event_date": f"2026-02-{i:02d}", "v": i, "flag": True, "f": float(i)}
        for i in range(1, 11)
    ]
    lake.write("ods", rows, table="t")
    rep = lake.prune_report("ods", "d1", "2026-01-01", table="t")
    assert rep["matched_files"] == 1
    assert rep["layer_files"] > rep["matched_files"]
    assert 0 < rep["prune_ratio"] < 1
    assert rep["hint"] == ""


def test_prune_report_with_zero_match_refuses_to_report_a_ratio(tmp_path):
    """0 命中**不许**报成「少扫 100%」。

    `1 - 0/layer_bytes == 1.0` 会把「这个过滤条件什么都没匹配到」
    （实际用法里几乎总是 dataset / event_date 写错）渲染成一个极漂亮的性能数字——
    最该报错的时候给了最好的读数。所以没命中就返回 `None` + 提示，
    与 `obs.pipeline_health` 在分母为 0 时返回 `None` 是同一条纪律。
    """
    lake = Lake(tmp_path / "lake")
    lake.write("ods", _rows(), table="t")
    rep = lake.prune_report("ods", "no_such_dataset", "2026-01-01", table="t")
    assert rep["matched_files"] == 0
    assert rep["matched_bytes"] == 0
    assert rep["layer_bytes"] > 0, "整层有数据，只是过滤条件没命中"
    assert rep["prune_ratio"] is None, "0 命中 ≠ 少扫 100%"
    assert "没有匹配到任何分区" in rep["hint"]


def test_unknown_layer_raises(tmp_path):
    lake = Lake(tmp_path / "lake")
    with pytest.raises(ValueError, match="未知层"):
        lake.write("nosuchlayer", _rows())


def test_write_empty_rows_is_noop(tmp_path):
    lake = Lake(tmp_path / "lake")
    rep = lake.write("ods", [], table="t")
    assert rep["n_rows"] == 0 and rep["n_files"] == 0
