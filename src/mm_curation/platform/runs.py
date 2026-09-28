"""运行台账：给数据链路装上「运行」这个概念（S1，地基）。

## 为什么这是地基

`Warehouse.build()` 是无状态函数：读 jsonl → 产出「当前这一份」。
缺的不是功能，是一条**贯穿维度**——于是下面这些问题一个都答不上来：

- 这是第几次运行？跑到哪个阶段了？（→ 补数只能从头全量）
- 上次为什么失败？（→ 无法复盘，只能重跑看运气）
- 同批次重跑，数据会不会变？（→ 幂等性只能口头声明）
- 某个源已经处理到哪了？（→ 增量无从判断"新增了什么"）

本模块补的就是这条维度。三张表即可覆盖：

| 表 | 粒度 | 语义对齐 |
|---|---|---|
| `job_runs` | 一次作业运行 | Airflow `dag_run` / OpenLineage `RunEvent` |
| `task_runs` | 一个阶段的一次执行 | Airflow `task_instance` |
| `watermarks` | 一个源的高水位 | 流批一体的 checkpoint / offset |

## 幂等性怎么变成"可验证"而不是"声明"

`fingerprint_rows()` 对一批行取**与顺序无关**的内容指纹
（逐行规范化 JSON 的 md5，再对指纹集合取 md5）。
同批次重跑 → 指纹相同 → 幂等成立；指纹不同 → 立刻定位到漂移的批次。
这是本项目「数字必须能复核」那条纪律在作业层的延续。

## 为什么台账单独一个库文件

`data/warehouse/platform.duckdb` 与 `curation.duckdb` 分开：
重建数仓（`CREATE OR REPLACE`）不该抹掉运行历史——
**历史本身就是本层要产出的资产**。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# 作业/阶段状态机（与 Airflow 的 state 命名保持可对照）
PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"
SKIPPED = "SKIPPED"

TERMINAL = (SUCCESS, FAILED, TIMEOUT, SKIPPED)

_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    run_id      VARCHAR PRIMARY KEY,
    job_name    VARCHAR,
    batch_date  DATE,
    status      VARCHAR,
    started_at  TIMESTAMP,
    ended_at    TIMESTAMP,
    duration_s  DOUBLE,
    git_sha     VARCHAR,
    params      VARCHAR,
    error       VARCHAR
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id        VARCHAR,
    task_name     VARCHAR,
    seq           INTEGER,
    status        VARCHAR,
    attempt       INTEGER,
    started_at    TIMESTAMP,
    ended_at      TIMESTAMP,
    duration_s    DOUBLE,
    n_in          BIGINT,
    n_out         BIGINT,
    bytes_scanned BIGINT,
    bytes_written BIGINT,
    fingerprint   VARCHAR,
    metrics       VARCHAR,
    error         VARCHAR,
    PRIMARY KEY (run_id, task_name)
);

CREATE TABLE IF NOT EXISTS watermarks (
    source         VARCHAR,
    partition_col  VARCHAR,
    high_watermark VARCHAR,
    updated_at     TIMESTAMP,
    run_id         VARCHAR,
    PRIMARY KEY (source, partition_col)
);
"""


def utcnow() -> datetime:
    """UTC 当前时间（去掉 tzinfo，DuckDB TIMESTAMP 是 naive 的）。

    统一用 UTC 而不是本地时间：台账会跨机器/跨 CI 读取，
    本地时间会制造"同一批数据两个时间戳"的假象。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def git_sha(repo_root: str | Path = ".") -> str:
    """当前提交号，作为"这批数据是哪版代码产出的"的溯源字段。

    取不到就给空串——**不伪造**。（`git rev-parse HEAD` 只读 refs，
    在对象库损坏时会给出乐观答案；这里只用于溯源记录，不用于完整性判断。）
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - 溯源字段取不到不该让作业失败
        return ""


def _canonical(row: Any) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def fingerprint_rows(rows: Iterable[Any]) -> str:
    """与顺序无关的内容指纹：先逐行 md5，再对**排序后**的指纹集合取 md5。

    为什么不直接 md5(整批字符串)：那样顺序一变指纹就变，
    而"同一批数据换了个顺序"不该算漂移。逐行 + 排序消除了顺序敏感性。
    """
    per_row = sorted(hashlib.md5(_canonical(r).encode("utf-8")).hexdigest() for r in rows)
    joined = "\n".join(per_row).encode("utf-8")
    return hashlib.md5(joined).hexdigest()


@dataclass
class TaskRecord:
    run_id: str
    task_name: str
    seq: int
    status: str
    attempt: int
    duration_s: float
    n_in: int
    n_out: int
    bytes_scanned: int
    bytes_written: int
    fingerprint: str
    error: str

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "TaskRecord":
        return cls(*row)  # type: ignore[arg-type]


class RunLedger:
    """运行台账句柄。所有写操作都走这里，CLI 与执行器不直接碰表。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- 连接 ---------------------------------------------------------------

    def connect(self):
        """DuckDB 是可选依赖：缺了就明确报错，不静默退化（同 `warehouse/model.py`）。"""
        try:
            import duckdb
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("平台层需要 duckdb：`pip install duckdb`") from e
        con = duckdb.connect(str(self.path))
        con.execute(_DDL)
        return con

    # -- 作业 ---------------------------------------------------------------

    @staticmethod
    def new_run_id(job_name: str, batch_date: str) -> str:
        """`<job>__<batch_date>__<短随机>`。

        带随机后缀而不是纯确定性 id：**同一批次重跑是一条新的运行记录**，
        两条记录的指纹都应该被留下（"重跑结果一致"是个可检验的断言，
        只有两条记录都在，才验得了）。
        """
        return f"{job_name}__{batch_date}__{uuid.uuid4().hex[:8]}"

    def start_run(
        self, run_id: str, job_name: str, batch_date: str, params: dict[str, Any] | None = None
    ) -> str:
        con = self.connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO job_runs "
                "(run_id, job_name, batch_date, status, started_at, git_sha, params) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    job_name,
                    batch_date,
                    RUNNING,
                    utcnow(),
                    git_sha(),
                    _canonical(params or {}),
                ],
            )
        finally:
            con.close()
        return run_id

    def finish_run(self, run_id: str, status: str, error: str = "") -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE job_runs SET status=?, ended_at=?, error=?, duration_s="
                "  date_diff('millisecond', started_at, ?) / 1000.0 "
                "WHERE run_id=?",
                [status, utcnow(), error, utcnow(), run_id],
            )
        finally:
            con.close()

    def list_runs(self, limit: int = 20, job_name: str = "") -> list[dict[str, Any]]:
        con = self.connect()
        try:
            sql = (
                "SELECT run_id, job_name, batch_date, status, duration_s, git_sha, error "
                "FROM job_runs"
            )
            args: list[Any] = []
            if job_name:
                sql += " WHERE job_name=?"
                args.append(job_name)
            sql += " ORDER BY started_at DESC LIMIT ?"
            args.append(limit)
            cur = con.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()

    def run_detail(self, run_id: str) -> dict[str, Any]:
        con = self.connect()
        try:
            cur = con.execute("SELECT * FROM job_runs WHERE run_id=?", [run_id])
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            job = dict(zip(cols, row)) if row else None
            cur = con.execute(
                "SELECT task_name, seq, status, attempt, duration_s, n_in, n_out, "
                "       bytes_scanned, bytes_written, fingerprint, error "
                "FROM task_runs WHERE run_id=? ORDER BY seq",
                [run_id],
            )
            tcols = [d[0] for d in cur.description]
            tasks = [dict(zip(tcols, r)) for r in cur.fetchall()]
            return {"job": job, "tasks": tasks}
        finally:
            con.close()

    # -- 阶段 ---------------------------------------------------------------

    def start_task(self, run_id: str, task_name: str, seq: int, attempt: int = 1) -> None:
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO task_runs (run_id, task_name, seq, status, attempt, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, task_name) DO UPDATE SET "
                "  status=excluded.status, attempt=excluded.attempt, "
                "  started_at=excluded.started_at, ended_at=NULL, error=NULL",
                [run_id, task_name, seq, RUNNING, attempt, utcnow()],
            )
        finally:
            con.close()

    def finish_task(
        self,
        run_id: str,
        task_name: str,
        status: str,
        *,
        attempt: int = 1,
        n_in: int = 0,
        n_out: int = 0,
        bytes_scanned: int = 0,
        bytes_written: int = 0,
        fingerprint: str = "",
        metrics: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        con = self.connect()
        try:
            con.execute(
                "UPDATE task_runs SET status=?, ended_at=?, error=?, attempt=?, "
                "  n_in=?, n_out=?, bytes_scanned=?, bytes_written=?, fingerprint=?, metrics=?, "
                "  duration_s = date_diff('millisecond', started_at, ?) / 1000.0 "
                "WHERE run_id=? AND task_name=?",
                [
                    status,
                    utcnow(),
                    error[:2000],
                    attempt,
                    n_in,
                    n_out,
                    bytes_scanned,
                    bytes_written,
                    fingerprint,
                    _canonical(metrics or {}),
                    utcnow(),
                    run_id,
                    task_name,
                ],
            )
        finally:
            con.close()

    def task_states(self, run_id: str) -> dict[str, str]:
        """`{task_name: status}`——`--resume` 靠它决定跳过哪些阶段。"""
        con = self.connect()
        try:
            cur = con.execute(
                "SELECT task_name, status FROM task_runs WHERE run_id=? ORDER BY seq", [run_id]
            )
            return {r[0]: r[1] for r in cur.fetchall()}
        finally:
            con.close()

    def batch_fingerprints(self, job_name: str, batch_date: str) -> list[dict[str, Any]]:
        """同一 (job, batch_date) 的历次运行与终态指纹——**幂等性的对账入口**。"""
        con = self.connect()
        try:
            cur = con.execute(
                "SELECT t.run_id, t.task_name, t.fingerprint, t.status, t.duration_s "
                "FROM task_runs t JOIN job_runs j USING (run_id) "
                "WHERE j.job_name=? AND j.batch_date=? ORDER BY t.seq, t.run_id",
                [job_name, batch_date],
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()

    # -- 水位线 -------------------------------------------------------------

    def set_watermark(
        self, source: str, partition_col: str, high_watermark: str, run_id: str
    ) -> None:
        con = self.connect()
        try:
            con.execute(
                "INSERT INTO watermarks VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (source, partition_col) DO UPDATE SET "
                "  high_watermark=excluded.high_watermark, updated_at=excluded.updated_at, "
                "  run_id=excluded.run_id",
                [source, partition_col, high_watermark, utcnow(), run_id],
            )
        finally:
            con.close()

    def get_watermark(self, source: str, partition_col: str) -> str | None:
        con = self.connect()
        try:
            row = con.execute(
                "SELECT high_watermark FROM watermarks WHERE source=? AND partition_col=?",
                [source, partition_col],
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def all_watermarks(self) -> list[dict[str, Any]]:
        con = self.connect()
        try:
            cur = con.execute(
                "SELECT source, partition_col, high_watermark, updated_at, run_id "
                "FROM watermarks ORDER BY source"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()


def default_ledger(repo_root: str | Path) -> RunLedger:
    return RunLedger(Path(repo_root) / "data" / "warehouse" / "platform.duckdb")


def today_batch() -> str:
    return date.today().isoformat()
