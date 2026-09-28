"""作业装配（S3 的"具体有哪些阶段"）：把湖仓各层接成一张 DAG。

这个模块只做两件事：**声明阶段** 与 **把层函数包成阶段函数**。
它不含执行逻辑（在 `dag.LocalExecutor`）、不含建模逻辑（在 `modeling`）、
不含服务逻辑（在 `service`）——"谁依赖谁"和"怎么算"分开，是本层的全部意义。

## 链路

```
ods ──▶ dims ──▶ dwd ──▶ dws ──▶ ads ──▶ views ──▶ contracts ──▶ obs ──▶ metrics
（落湖）（SCD-2）（明细事实）（聚合）（宽表）（登记视图）（契约闸门）（观测快照）（Prometheus）
```

## 三个刻意的口径

1. **`contracts` 是闸门，不是报告**。`severity=error` 的断言失败 → 阶段抛异常 →
   整条运行记 `FAILED`，下游 `obs`/`metrics` 不跑。契约只要能"看过但不拦"，
   它就一定会变成"没人看"。warn 级只记录不阻断（留给 `n_warn` 计数）。
2. **`views` 单独立成一个阶段**。视图是"湖上 Parquet → 可 SQL 查询"的唯一桥，
   它由 `ads` 之后重建，保证视图指向的是刚写完的数据。混进任何别的阶段都会
   出现"视图比数据旧一版"的静默错。
3. **`metrics` 落在 DAG 内、而不是只由服务暴露**。这样每次批量运行都会留下
   一份可归档的指标快照（`runs/obs/*.prom`），"当时是什么状态"有据可查，
   而不是只有"现在是什么状态"。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from ..lineage.contract import check_contract, load_contracts
from . import modeling
from . import obs as obs_mod
from .dag import DAG, Task
from .envs import ENV_DEV
from .runs import RunLedger, default_ledger

__all__ = [
    "ODS",
    "DIMS",
    "DWD",
    "DWS",
    "ADS",
    "VIEWS",
    "CONTRACTS",
    "OBS",
    "METRICS",
    "PLATFORM_CONTRACT_DIR",
    "build_dag",
    "make_context",
    "platform_datasets",
    "run_platform",
]

# 阶段名做成常量：CLI / Airflow 导出 / 测试都引用它们，
# 字符串一旦散落各处，改名就是一次静默的断链。
ODS = "ods"
DIMS = "dims"
DWD = "dwd"
DWS = "dws"
ADS = "ads"
VIEWS = "views"
CONTRACTS = "contracts"
OBS = "obs"
METRICS = "metrics"

PLATFORM_CONTRACT_DIR = Path("configs") / "contracts_platform"


# ---------------------------------------------------------------------------
# 契约闸门：把 Lake 适配成 check_contract 认识的"仓库句柄"
# ---------------------------------------------------------------------------


class _LakeWarehouse:
    """`lineage.contract.check_contract` 只要求 `wh.query(sql) -> (cols, rows)`。

    这里不继承 `warehouse.Warehouse`，因为那个类绑定了 DuckDB 文件路径与
    建表逻辑；契约检查只用到 `query` 一个方法。**按需实现最小接口**，
    比为了复用而去动一个已被别处依赖的类安全（也不改任何既有签名）。
    """

    def __init__(self, con):
        self.con = con

    def connect(self):
        return self.con

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()


# ---------------------------------------------------------------------------
# 阶段函数
# ---------------------------------------------------------------------------


def _ok(rep: dict[str, Any]) -> dict[str, Any]:
    """给阶段报告补上执行器认识的三个公共字段（n_in / n_out / bytes_written）。"""
    rep = dict(rep)
    rep.setdefault("n_in", rep.get("n_rows", 0))
    rep.setdefault("n_out", rep.get("n_rows", 0))
    return rep


def task_ods(ctx) -> dict[str, Any]:
    """源 jsonl → ODS Parquet（按 dataset × event_date 分区）。

    `incremental=True` 时用**湖上已有分区**做 `skip_dates`。⚠️ 如实说明：
    jsonl 是平面文件、没有分区目录可下推，所以跳过只发生在解析之后，
    读取侧 I/O 收益为零，收益只在写入侧（不重写已有分区）——见
    `modeling.build_ods` 的文档。这条限制是源格式决定的，不是实现偷懒。
    """
    skip: tuple[str, ...] = ()
    if ctx.extra.get("incremental"):
        skip = tuple(sorted(ctx.lake.partition_values("ods", "ods_samples")))
        ctx.log(f"        增量：跳过湖上已有分区 {len(skip)} 个")
    rep = modeling.build_ods(
        ctx.lake,
        ctx.root,
        datasets=ctx.datasets,
        event_dates=ctx.event_dates,
        skip_dates=skip,
        limit=int(ctx.extra.get("limit") or 0),
    )
    return _ok(rep)


def task_dims(ctx) -> dict[str, Any]:
    rep = modeling.build_dims(ctx.con, ctx.lake, datasets=ctx.datasets, event_dates=ctx.event_dates)
    return {
        "n_in": rep["n_observed_keys"],
        "n_out": rep["n_current"],
        "fingerprint": f"dim:v{rep['n_versions']}",
        **rep,
    }


def task_dwd(ctx) -> dict[str, Any]:
    return _ok(
        modeling.build_dwd(ctx.con, ctx.lake, datasets=ctx.datasets, event_dates=ctx.event_dates)
    )


def task_dws(ctx) -> dict[str, Any]:
    return _ok(
        modeling.build_dws(ctx.con, ctx.lake, datasets=ctx.datasets, event_dates=ctx.event_dates)
    )


def task_ads(ctx) -> dict[str, Any]:
    rep = modeling.build_ads(ctx.con, ctx.lake, today=ctx.batch_date)
    return _ok(rep)


def task_views(ctx) -> dict[str, Any]:
    made = modeling.refresh_views(ctx.con, ctx.lake)
    return {"n_in": 0, "n_out": len(made), "views": made}


def task_contracts(ctx) -> dict[str, Any]:
    """契约闸门：error 级断言失败 → 抛异常 → 本次运行记 FAILED。"""
    cdir = ctx.root / PLATFORM_CONTRACT_DIR
    contracts = load_contracts(cdir)
    if not contracts:
        return {"n_in": 0, "n_out": 0, "n_contracts": 0, "note": f"无契约目录 {cdir}（未阻断）"}
    wh = _LakeWarehouse(ctx.con)
    reports = [check_contract(wh, c) for c in contracts]
    blocking = [f"{r['dataset']}@{r['version']}::{b}" for r in reports for b in r["blocking"]]
    n_fail = sum(r["n_fail"] for r in reports)
    n_err = sum(r["n_error"] for r in reports)
    n_checks = sum(r["n_checks"] for r in reports)
    rep = {
        "n_in": n_checks,
        "n_out": n_checks - n_fail - n_err,
        "n_contracts": len(reports),
        "n_checks": n_checks,
        "n_fail": n_fail,
        "n_error": n_err,
        "blocking": blocking,
        "fingerprint": f"contracts:{len(reports)}:{n_fail}:{n_err}",
        "reports": reports,
    }
    if blocking and ctx.extra.get("contract_blocking", True):
        raise RuntimeError("契约阻断（severity=error 断言失败）：" + "; ".join(blocking))
    return rep


def task_obs(ctx) -> dict[str, Any]:
    # 观测参数**只有一个来源**：`configs/platform.yaml`。
    # CLI 的 `mmc platform obs` 与 DAG 里的这一阶段读同一个文件——
    # 两边各有一套默认值，必然出现"手工查是这套口径、自动跑是那套口径"。
    slo = ctx.extra.get("slo") or obs_mod.load_platform_config(
        ctx.root / "configs" / "platform.yaml"
    )
    snap = obs_mod.snapshot(
        ctx.con,
        ctx.ledger,
        slo_days=float(slo.get("freshness_days", obs_mod.DEFAULT_SLO["freshness_days"])),
        per_dataset=slo.get("datasets") or None,
        volume_margin=float(slo.get("volume_margin", obs_mod.DEFAULT_SLO["volume_margin"])),
        min_days_for_volume=int(
            slo.get("min_days_for_volume", obs_mod.DEFAULT_SLO["min_days_for_volume"])
        ),
    )
    out = ctx.store_root() / "runs" / "obs" / f"{ctx.run_id}.json"
    obs_mod.write_snapshot(snap, out)
    ctx.extra["obs"] = snap
    ctx.log("        " + obs_mod.summarize(snap))
    return {
        "n_in": snap["n_signals"],
        "n_out": snap["n_alerts"],
        "n_signals": snap["n_signals"],
        "n_alerts": snap["n_alerts"],
        "summary": obs_mod.summarize(snap),
        "snapshot_path": str(out),
        "freshness_breaches": sum(1 for f in snap["freshness"] if f["ok"] is False),
        "volume_anomalies": len(snap["volume_anomalies"]),
        "pipeline_success_rate": snap["pipeline"].get("success_rate"),
    }


def task_metrics(ctx) -> dict[str, Any]:
    """把观测快照落成 Prometheus 文本文件（**可归档的历史指标**，不只是当前值）。"""
    snap = ctx.extra.get("obs")
    if snap is None:
        snap = obs_mod.snapshot(ctx.con, ctx.ledger)
    text = obs_mod.prometheus_text(snap)
    out = ctx.store_root() / "runs" / "obs" / f"{ctx.run_id}.prom"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    n_series = sum(1 for ln in text.splitlines() if ln and not ln.startswith("#"))
    return {
        "n_in": snap.get("n_signals", 0),
        "n_out": n_series,
        "n_series": n_series,
        "path": str(out),
        "bytes_written": len(text.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------


def build_dag() -> DAG:
    """构造平台链路 DAG（纯定义，不执行）。"""
    dag = DAG(name="mm_curation_platform")
    dag.tasks = [
        Task(
            ODS,
            task_ods,
            deps=(),
            retries=1,
            description="源 jsonl → ODS Parquet（Hive 分区：dataset × event_date）",
        ),
        Task(
            DIMS,
            task_dims,
            deps=(ODS,),
            retries=1,
            description="维度 SCD-2：封旧版本 + 开新版本（代理键随版本变）",
        ),
        Task(
            DWD,
            task_dwd,
            deps=(DIMS,),
            retries=1,
            description="明细事实：窗级事实 + 窗×算子事实，按 valid_from/valid_to 关联维版本",
        ),
        Task(
            DWS,
            task_dws,
            deps=(DWD,),
            retries=1,
            description="聚合：dataset × event_date（+ × op），覆盖率与丢脏率分列",
        ),
        Task(
            ADS,
            task_ads,
            deps=(DWS,),
            retries=1,
            description="应用宽表：数据集健康（体量/新鲜度/覆盖/未匹配）",
        ),
        Task(
            VIEWS,
            task_views,
            deps=(ADS,),
            retries=0,
            description="重建湖上 Parquet → SQL 视图（ads 之后，保证视图不比数据旧）",
        ),
        Task(
            CONTRACTS,
            task_contracts,
            deps=(VIEWS,),
            retries=0,
            timeout_s=120.0,
            description="数据契约闸门：error 级断言失败 → 本阶段抛错、下游不跑",
        ),
        Task(
            OBS,
            task_obs,
            deps=(CONTRACTS,),
            retries=0,
            timeout_s=120.0,
            description="观测快照：新鲜度 / 行数稳健限 / 管道健康 / 维表未匹配 → 告警收敛",
        ),
        Task(
            METRICS,
            task_metrics,
            deps=(OBS,),
            retries=0,
            description="Prometheus 文本指标落盘（runs/obs/*.prom，可归档）",
        ),
    ]
    return dag


# ---------------------------------------------------------------------------
# 组装上下文 / 一键运行
# ---------------------------------------------------------------------------


def platform_datasets() -> tuple[str, ...]:
    """平台链路默认处理的数据集（与 `registry.LAKE_SOURCES` 的 dataset 去重后一致）。"""
    from .registry import LAKE_SOURCES

    seen: list[str] = []
    for src in LAKE_SOURCES:
        if src.dataset not in seen:
            seen.append(src.dataset)
    return tuple(seen)


def make_context(
    root: str | Path,
    *,
    run_id: str = "",
    batch_date: str = "",
    ledger: RunLedger | None = None,
    datasets: tuple[str, ...] = (),
    event_dates: tuple[str, ...] = (),
    verbose: bool = True,
    env: str = ENV_DEV,
    **extra: Any,
) -> Any:
    """建一次运行需要的上下文（含 DuckDB 连接、Lake、台账）。

    路径全部转成绝对路径：`lake.scan_sql` 生成的是**相对 glob**，
    在进程 cwd 变化时会被解析到别处——这是"本机跑得通、换个目录就找不到数据"
    这类问题的常见来源，所以在这里一次性钉死。

    `env` 决定**产物写哪**（`dev` = 仓库根本身，保持既有路径；`prod` =
    `data/envs/prod/`），而**源与配置始终读仓库根**——同一份代码、同一个 git_sha。
    详见 `envs` 模块文档。
    """
    import duckdb

    from .dag import Context
    from .envs import resolve
    from .lake import default_lake

    root = Path(root).resolve()
    spec = resolve(root, env)
    store = spec.store
    lake = default_lake(store)
    # ⚠️ 目录必须先建：DuckDB 不会替我们建父目录，缺了它报的是
    # `IO Error: Cannot open file ... 系统找不到指定的路径`——
    # 一个完全指不到"是目录没建"的错。新克隆的仓库里 `data/warehouse/`
    # 必然不存在（数据产物不入库），所以这条一定会被踩到。
    db_path = spec.warehouse_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 同上：观测快照与指标文件写在 <store>/runs/obs/ 下
    spec.obs_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    ledger = ledger or default_ledger(store)
    batch_date = batch_date or _dt.date.today().isoformat()
    run_id = run_id or RunLedger.new_run_id("platform", batch_date)
    return Context(
        run_id=run_id,
        batch_date=batch_date,
        root=root,
        ledger=ledger,
        con=con,
        lake=lake,
        store=store,
        datasets=tuple(datasets),
        event_dates=tuple(event_dates),
        verbose=verbose,
        extra=dict(extra, env=spec.name),
    )


def run_platform(
    root: str | Path,
    *,
    run_id: str = "",
    batch_date: str = "",
    datasets: tuple[str, ...] = (),
    event_dates: tuple[str, ...] = (),
    incremental: bool = False,
    limit: int = 0,
    resume: bool = False,
    only: tuple[str, ...] = (),
    slo: dict[str, Any] | None = None,
    contract_blocking: bool = True,
    verbose: bool = True,
    env: str = ENV_DEV,
) -> dict[str, Any]:
    """跑整条平台链路（CLI 与本模块共用同一个入口）。"""
    from .dag import LocalExecutor
    from .envs import resolve

    store = resolve(root, env).store
    ledger = default_ledger(store)
    ctx = make_context(
        root,
        run_id=run_id,
        batch_date=batch_date,
        ledger=ledger,
        datasets=datasets,
        event_dates=event_dates,
        verbose=verbose,
        env=env,
        incremental=incremental,
        limit=limit,
        slo=slo or {},
        contract_blocking=contract_blocking,
    )
    dag = build_dag()
    if not only:
        # ⚠️ `--only`（Airflow 的单阶段调用）**不写 job_runs**：一个 Airflow DAG run
        # 的所有阶段共用同一个 run_id，各阶段是独立进程、若都去 INSERT OR REPLACE
        # 这条行，最终留下的是最后一个阶段的耗时与状态——那是一个假数。
        # 单阶段模式只写 task_runs（谁跑了、多久、产出什么），这是不撒谎的最小集合。
        ledger.start_run(ctx.run_id, "platform", ctx.batch_date, {"datasets": list(datasets)})
    ex = LocalExecutor(dag, ctx)
    try:
        result = ex.run(resume=resume, only=only)
    finally:
        ctx.con.close()
    result["datasets"] = list(datasets)
    result["batch_date"] = ctx.batch_date
    result["env"] = ctx.extra.get("env", env)
    return result


def dag_json(root: str | Path | None = None) -> str:  # pragma: no cover - 便捷函数
    return json.dumps(build_dag().as_dict(), ensure_ascii=False, indent=2)
