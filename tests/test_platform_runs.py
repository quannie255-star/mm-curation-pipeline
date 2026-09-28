"""台账层测试：内容指纹、作业/阶段生命周期、水位线、断点续跑的依据。"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")

from mm_curation.platform.runs import (  # noqa: E402
    FAILED,
    RUNNING,
    SKIPPED,
    SUCCESS,
    RunLedger,
    fingerprint_rows,
)


def test_fingerprint_is_order_independent():
    a = [{"id": 1, "v": "x"}, {"id": 2, "v": "y"}]
    b = [{"id": 2, "v": "y"}, {"id": 1, "v": "x"}]
    assert fingerprint_rows(a) == fingerprint_rows(b)


def test_fingerprint_changes_when_content_changes():
    a = [{"id": 1, "v": "x"}, {"id": 2, "v": "y"}]
    b = [{"id": 1, "v": "x"}, {"id": 2, "v": "Z"}]
    assert fingerprint_rows(a) != fingerprint_rows(b)


def test_fingerprint_is_stable_across_key_order_and_empty():
    a = [{"id": 1, "v": "x"}]
    b = [{"v": "x", "id": 1}]
    assert fingerprint_rows(a) == fingerprint_rows(b)
    assert fingerprint_rows([]) == fingerprint_rows([])
    assert fingerprint_rows([]) != fingerprint_rows(a)


def test_run_id_has_job_batch_and_random_suffix(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    rid = RunLedger.new_run_id("platform", "2026-09-22")
    assert rid.startswith("platform__2026-09-22__")
    # 同一批次两次生成必须是**不同**的 id：重跑要留下两条记录，
    # "重跑结果一致"才验得了（见 runs.new_run_id 的文档）
    assert RunLedger.new_run_id("platform", "2026-09-22") != rid
    _ = led


def test_ledger_run_and_task_lifecycle(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    rid = led.start_run("r1", "platform", "2026-09-22", {"datasets": ["d1"]})
    assert rid == "r1"

    led.start_task(rid, "ods", 1)
    led.finish_task(rid, "ods", SUCCESS, n_in=10, n_out=10, fingerprint="fp1")
    led.start_task(rid, "dwd", 2)
    led.finish_task(rid, "dwd", FAILED, error="boom")
    led.finish_run(rid, FAILED, "dwd 失败")

    states = led.task_states(rid)
    assert states == {"ods": SUCCESS, "dwd": FAILED}

    detail = led.run_detail(rid)
    assert detail["job"]["status"] == FAILED
    assert [t["task_name"] for t in detail["tasks"]] == ["ods", "dwd"]
    assert detail["tasks"][0]["fingerprint"] == "fp1"


def test_task_upsert_does_not_duplicate_on_retry(tmp_path):
    """同一 (run_id, task_name) 重试必须**覆盖**而不是插两行。"""
    led = RunLedger(tmp_path / "l.duckdb")
    rid = led.start_run("r1", "platform", "2026-09-22")
    led.start_task(rid, "ods", 1, attempt=1)
    led.finish_task(rid, "ods", FAILED, attempt=1, error="第一次")
    led.start_task(rid, "ods", 1, attempt=2)
    led.finish_task(rid, "ods", SUCCESS, attempt=2, n_out=5)
    con = led.connect()
    try:
        rows = con.execute(
            "SELECT status, attempt, error FROM task_runs WHERE run_id='r1'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    # `finish_task` 对空错误落的是空串（列是 VARCHAR，不是 NULL）；
    # 关键断言是"重试成功后不再残留上一次的错误文本"
    assert rows[0][0] == SUCCESS and rows[0][1] == 2 and not rows[0][2]


def test_resume_source_is_task_states(tmp_path):
    """`--resume` 只信台账：已 SUCCESS 的跳过，FAILED 的重跑。"""
    led = RunLedger(tmp_path / "l.duckdb")
    rid = led.start_run("r1", "platform", "2026-09-22")
    for i, (name, st) in enumerate(
        [("ods", SUCCESS), ("dims", SUCCESS), ("dwd", FAILED), ("dws", SKIPPED)], start=1
    ):
        led.start_task(rid, name, i)
        led.finish_task(rid, name, st)
    already = led.task_states(rid)
    assert [n for n, s in already.items() if s == SUCCESS] == ["ods", "dims"]


def test_watermark_upsert_and_read(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    led.set_watermark("metropt3_windows", "event_date", "2020-09-01", "r1")
    assert led.get_watermark("metropt3_windows", "event_date") == "2020-09-01"
    led.set_watermark("metropt3_windows", "event_date", "2020-09-02", "r2")
    assert led.get_watermark("metropt3_windows", "event_date") == "2020-09-02"
    assert len(led.all_watermarks()) == 1
    assert led.get_watermark("nope", "event_date") is None


def test_batch_fingerprints_joins_task_and_job(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    for rid in ("r1", "r2"):
        led.start_run(rid, "platform", "2026-09-22")
        led.start_task(rid, "ods", 1)
        led.finish_task(rid, "ods", SUCCESS, fingerprint="same")
        led.finish_run(rid, SUCCESS)
    rows = led.batch_fingerprints("platform", "2026-09-22")
    assert {r["run_id"] for r in rows} == {"r1", "r2"}
    assert {r["fingerprint"] for r in rows} == {"same"}


def test_ledger_is_separate_file_from_warehouse(tmp_path):
    """台账另存一个库：重建数仓不该抹掉运行历史。"""
    led = RunLedger(tmp_path / "platform.duckdb")
    led.start_run("r1", "platform", "2026-09-22")
    assert (tmp_path / "platform.duckdb").exists()
    con = led.connect()
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    finally:
        con.close()
    assert {"job_runs", "task_runs", "watermarks"} <= tables


def test_running_status_is_not_terminal(tmp_path):
    led = RunLedger(tmp_path / "l.duckdb")
    led.start_run("r1", "platform", "2026-09-22")
    runs = led.list_runs()
    assert runs[0]["status"] == RUNNING
