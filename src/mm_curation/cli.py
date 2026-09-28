"""平台轨 CLI（S6）：`python -m mm_curation.cli <子命令>`。

入口有两种形态，都指同一套实现：
- `python -m mm_curation.cli run|dag|obs|contracts|prune|runs|watermarks|serve|promote`
- `python scripts/mmc.py platform <同上子命令>`（旧仓库轨 CLI 下的一个转发子命令）
  ⚠️ 这里**没有** `platform` 这一层——`scripts/mmc.py platform ...` 里的
  `platform` 是 mmc 自己的子命令名，转发时会把它剥掉。

`--env {dev,prod}` 决定**产物写/读哪个 store**（dev = 仓库根，保持既有路径；
prod = `data/envs/prod/`），而**源与配置始终读仓库根**——同一份代码、同一个 git_sha。
详见 `platform/envs` 模块文档。

与 `scripts/mmc.py` 的分工：那个 CLI 收口的是**旧仓库轨**（`curation.duckdb` 的
build/sql/metrics/scorecard/lineage/contracts）；本 CLI 收口的是**平台轨**
（`data/lake/**` 的湖仓链路 + 台账 + 服务 + 观测）。

两者并存不是重复。旧轨是"一次算完一份产物"，新轨是"有运行、有分区、有水位、
有服务"。**把新轨塞进旧轨的子命令里，会让人以为它们是同一套东西**——
而它们最关键的差别恰恰是"有没有运行这个概念"。

Airflow 生成物调用的就是本模块（`--only <阶段>` 单阶段执行）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _root(a) -> Path:
    """解析仓库根，三种写法都支持。

    ⚠️ 为什么这里有两个字段（`root` / `root_top`）：argparse 的
    `_SubParsersAction.__call__` 是**先在一个全新命名空间里解析子命令，然后把
    整个子命名空间 `setattr` 回父级**——不是"只补缺失的键"。于是子解析器里
    `--root` 的默认值 `""` 会把顶层已经解析出来的 `--root X` 覆盖成空串。
    （实测：`["--root", "/tmp/a", "dag"]` 得到 `root == ''`。）
    所以顶层与子命令用**两个不同的 dest**，读取时按优先级合并。
    """
    v = getattr(a, "root", "") or getattr(a, "root_top", "")
    return Path(v).resolve() if v else ROOT


def _csv(v: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (v or "").split(",") if x.strip())


def _env(a):
    """解析 `--env`：`repo`（代码/源/配置）与 `store`（产物）一起给出。

    环境名**非法时抛错**，不回落到 dev——拼错一个字符就把"生产发布"写进 dev，
    而日志上一切正常，是这类改动最容易引入的事故（见 `envs.resolve`）。
    """
    from mm_curation.platform.envs import ENV_DEV, resolve

    return resolve(_root(a), getattr(a, "env", "") or ENV_DEV)


def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(a) -> int:
    from mm_curation.platform import jobs

    res = jobs.run_platform(
        _root(a),
        run_id=a.run_id or "",
        batch_date=a.batch_date or "",
        datasets=_csv(a.datasets),
        event_dates=_csv(a.event_dates),
        incremental=a.incremental,
        limit=a.limit,
        resume=a.resume,
        only=_csv(a.only),
        contract_blocking=not a.no_contract_gate,
        verbose=not a.quiet,
        env=a.env,
    )
    if a.json:
        _dump(res)
    else:
        print(
            f"\n{'=' * 62}\nrun_id={res['run_id']}  status={res['status']}  "
            f"env={res.get('env', a.env)}  wall={res['duration_s']}s"
        )
        for name, r in res["tasks"].items():
            print(f"  {name:<10} {r.get('_status', '-'):<8} {r.get('_duration_s', '-')}s")
        o = res["tasks"].get("obs", {})
        if o.get("summary"):
            print(f"  观测：{o['summary']}")
    return 0 if res["status"] == "SUCCESS" else 1


# ---------------------------------------------------------------------------
# dag
# ---------------------------------------------------------------------------


def cmd_dag(a) -> int:
    from mm_curation.platform import jobs
    from mm_curation.platform.dag import to_airflow_source, validate_airflow_source

    dag = jobs.build_dag()
    if a.export_airflow:
        src = to_airflow_source(dag, dag_id=a.dag_id, schedule=a.schedule)
        problems = validate_airflow_source(src, dag)
        out = Path(a.export_airflow)
        out.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ `newline="\n"` 不能省：Windows 上文本模式会把 `\n` 翻译成 `\r\n`，
        # 于是同一份定义在 Windows 产出 CRLF、在 Linux 产出 LF。
        # `Path.read_text()` 会归一化换行，所以"读回来比对"的断言看不出差别——
        # 但 CI 里那条 `diff 已提交文件 重新生成的文件` 不会归一化，
        # 会在 Linux runner 上因**换行符**而红，与 DAG 内容毫无关系。
        # 生成物是要入库、要跨平台比对的，写入侧就必须钉死 LF。
        out.write_text(src, encoding="utf-8", newline="\n")
        print(f"已导出 {out}（{len(src)} 字符），静态校验问题 {len(problems)} 条")
        for p in problems:
            print("  !", p)
        return 1 if problems else 0
    if a.mermaid:
        print("flowchart LR")
        for t in dag.order():
            for d in t.deps:
                print(f"  {d} --> {t.name}")
        return 0
    _dump(dag.as_dict())
    return 0


# ---------------------------------------------------------------------------
# obs / contracts / prune
# ---------------------------------------------------------------------------


def cmd_obs(a) -> int:
    import duckdb

    from mm_curation.platform import obs
    from mm_curation.platform.lake import default_lake
    from mm_curation.platform.runs import default_ledger

    spec = _env(a)
    con = duckdb.connect()
    con.execute(f"SET home_directory='{spec.store.as_posix()}'")
    lake = default_lake(spec.store)
    from mm_curation.platform.modeling import refresh_views

    refresh_views(con, lake)
    # 配置读**仓库根**（两个环境共用同一套阈值口径：同一份 SLO 定义）
    cfg = obs.load_platform_config(spec.repo / "configs" / "platform.yaml")
    slo = cfg.get("slo") or {}
    snap = obs.snapshot(
        con,
        default_ledger(spec.store),
        slo_days=float(slo.get("freshness_days", obs.DEFAULT_SLO["freshness_days"])),
        per_dataset=cfg.get("datasets") or None,
        volume_margin=float(slo.get("volume_margin", obs.DEFAULT_SLO["volume_margin"])),
        min_days_for_volume=int(
            slo.get("min_days_for_volume", obs.DEFAULT_SLO["min_days_for_volume"])
        ),
    )
    if a.prometheus:
        print(obs.prometheus_text(snap), end="")
    elif a.json:
        _dump(snap)
    else:
        print(f"环境：{spec.name}（store={spec.store}）")
        print(obs.summarize(snap))
        print("\n数据集新鲜度：")
        for f in snap["freshness"]:
            flag = "未评" if f["ok"] is None else ("OK  " if f["ok"] else "破线")
            fd = "-" if f["freshness_days"] is None else f"{f['freshness_days']:.0f}天"
            print(
                f"  [{flag}] {f['dataset']:<16} last={str(f['last_event_date']):<12} "
                f"滞后={fd:<8} 行数={f['n_total']:<8} 未标日期分区={f['n_undated_partitions']}"
            )
        print(f"\n收敛：原始信号 {snap['n_signals']} → 告警 {snap['n_alerts']}")
        for al in snap["alerts"]:
            print(
                f"  {al['severity']:<5} {al['fingerprint']:<32} n={al['n_signals']:<3} "
                f"{al['sample'][:56]}"
            )
    if a.write:
        p = obs.write_snapshot(snap, Path(a.write))
        print(f"\n已写 {p}", file=sys.stderr)
    con.close()
    return 0


def cmd_contracts(a) -> int:
    from mm_curation.lineage.contract import check_contract, load_contracts
    from mm_curation.platform.service import ServiceCore, _CoreQuery

    spec = _env(a)
    core = ServiceCore(spec.repo, env=spec.name)
    wh = _CoreQuery(core)
    cdir = spec.repo / "configs" / "contracts_platform"
    contracts = load_contracts(cdir)
    if not contracts:
        print(f"(no platform contracts at {cdir})")
        return 0
    results = [check_contract(wh, c) for c in contracts]
    if a.json:
        _dump(results)
    else:
        for r in results:
            flag = "OK " if r["ok"] else "FAIL"
            print(
                f"[{flag}] {r['dataset']}@v{r['version']}  {r['n_checks']} 条断言, "
                f"{r['n_fail']} fail, {r['n_error']} error"
            )
            for c in r["checks"]:
                if c["status"] != "PASS":
                    print(
                        f"        {c['status']:5s} {c['name']}  "
                        f"actual={c['actual']} expect={c['expect']}"
                    )
    core.close()
    return 1 if any(not r["ok"] for r in results) else 0


def cmd_prune(a) -> int:
    """分区裁剪的**字节级证据**（不是"文件数"）。"""
    from mm_curation.platform.lake import default_lake

    lake = default_lake(_env(a).store)
    rep = lake.prune_report(a.layer, a.dataset, a.event_date, table=a.table)
    _dump(rep)
    if rep["prune_ratio"] is None:
        # 0 命中 → 非零退出码：这类调用几乎总是参数写错，
        # 静默返回 0 会让它混进脚本里当作"裁剪成功"。
        print(f"\n未给出裁剪率：{rep['hint']}", file=sys.stderr)
        return 1
    print(
        f"\n命中 {rep['matched_bytes']} B / 整层 {rep['layer_bytes']} B "
        f"→ 少扫 {rep['prune_ratio'] * 100:.2f}%"
    )
    return 0


# ---------------------------------------------------------------------------
# runs / watermarks
# ---------------------------------------------------------------------------


def cmd_runs(a) -> int:
    from mm_curation.platform.runs import default_ledger

    ledger = default_ledger(_env(a).store)
    if a.detail:
        _dump(ledger.run_detail(a.detail))
        return 0
    rows = ledger.list_runs(limit=a.limit)
    if a.json:
        _dump(rows)
    else:
        for r in rows:
            print(
                f"{r['run_id']:<40} {r['status']:<8} {str(r['batch_date']):<12} "
                f"{r['duration_s']}s  {(r.get('git_sha') or '')[:8]}"
            )
    return 0


def cmd_watermarks(a) -> int:
    from mm_curation.platform.runs import default_ledger

    _dump(default_ledger(_env(a).store).all_watermarks())
    return 0


# ---------------------------------------------------------------------------
# promote（DEV→PROD 参数化晋升）
# ---------------------------------------------------------------------------


def cmd_promote(a) -> int:
    from mm_curation.platform import envs

    spec = _env(a)
    rep = envs.promote(
        spec.repo,
        from_env=a.from_env,
        to_env=a.to_env,
        dry_run=a.dry_run,
        force=a.force,
        prune_stale=a.prune_stale,
    )
    if a.json:
        _dump(rep)
    else:
        print(f"{'OK  ' if rep['ok'] else 'FAIL'}  {a.from_env} → {a.to_env}")
        print(f"  {rep['reason']}")
        if rep["source"]:
            print(
                f"  源：{rep['source']['n_files']} 文件 / {rep['source']['bytes']} B"
                f"（指纹 {rep['source']['digest'][:12]}…，口径 {rep['source']['kind']}）"
            )
        if rep["dest"]:
            print(
                f"  目标：{rep['dest']['n_files']} 文件 / {rep['dest']['bytes']} B"
                f"（指纹 {rep['dest']['digest'][:12]}…）"
            )
        if rep["to_run"]:
            print(f"  目标环境运行：{rep['to_run']}")
    return 0 if rep["ok"] else 1


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(a) -> int:
    from mm_curation.platform.service import create_app

    app = create_app(
        _root(a),
        rbac_path=a.rbac,
        contract_dir=a.contracts,
        contract_blocking=not a.no_contract_gate,
        env=a.env,
    )
    if a.dry_run:
        core = app.state.core
        _dump(core.startup())
        print(f"\n环境={core.spec.name}  store={core.store}")
        print(f"就绪={core.ready}；演示 token 见 docs/PLATFORM.md")
        return 0 if core.ready else 1
    try:
        import uvicorn
    except ImportError:
        print("需要 uvicorn：`pip install uvicorn`（或加 --dry-run 只跑契约闸门）", file=sys.stderr)
        return 2
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # `--root` 同时支持写在**子命令前**与**子命令后**。
    # 两个位置用不同的 dest（`root_top` / `root`），原因见 `_root()` 的说明：
    # 子解析器会整体覆盖回父命名空间，同名 dest 会让顶层那次解析作废。
    top_common = argparse.ArgumentParser(add_help=False)
    top_common.add_argument("--root", dest="root_top", default="", help="仓库根（默认自动定位）")
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--root", dest="root", default="", help="仓库根（默认自动定位）")
    # `--env` 挂在**所有子命令**上（不是只挂 `run`）：环境是"这次操作针对哪份产物"
    # 的属性，`obs`/`prune`/`runs`/`serve` 全都需要它。只给 `run` 加会造出一个
    # 更容易出错的局面——写的时候能选环境，读的时候不能，于是查 prod 的告警
    # 只能去翻 dev 的湖。
    sub_common.add_argument(
        "--env",
        default="dev",
        choices=["dev", "prod"],
        help="产物环境：dev（仓库根，默认）/ prod（data/envs/prod/）",
    )
    kw = {"parents": [sub_common]}

    p = argparse.ArgumentParser(
        prog="python -m mm_curation.cli",
        description="mm-curation 平台轨 CLI",
        parents=[top_common],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑整条平台链路（ODS→…→metrics）", **kw)
    r.add_argument("--datasets", default="", help="逗号分隔，默认全部")
    r.add_argument("--event-dates", default="", help="逗号分隔，只处理这些事件日")
    r.add_argument("--incremental", action="store_true", help="跳过湖上已有分区")
    r.add_argument("--limit", type=int, default=0, help="每个源最多读 N 条（快速迭代用）")
    r.add_argument("--resume", action="store_true", help="跳过本次运行已成功的阶段")
    r.add_argument("--only", default="", help="只跑这些阶段（Airflow 单阶段调用）")
    r.add_argument("--run-id", default="")
    r.add_argument("--batch-date", default="")
    r.add_argument("--no-contract-gate", action="store_true", help="契约失败不阻断（仅调试用）")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("dag", help="打印 DAG 定义 / 导出 Airflow", **kw)
    d.add_argument("--export-airflow", default="", help="生成 Airflow DAG 文件路径")
    d.add_argument("--dag-id", default="")
    d.add_argument("--schedule", default="@daily")
    d.add_argument("--mermaid", action="store_true")
    d.set_defaults(fn=cmd_dag)

    o = sub.add_parser("obs", help="观测快照（新鲜度/行数异常/管道健康/告警收敛）", **kw)
    o.add_argument("--json", action="store_true")
    o.add_argument("--prometheus", action="store_true")
    o.add_argument("--write", default="")
    o.set_defaults(fn=cmd_obs)

    c = sub.add_parser("contracts", help="平台轨数据契约校验（失败 exit 1）", **kw)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_contracts)

    pr = sub.add_parser("prune", help="分区裁剪的字节级证据", **kw)
    pr.add_argument("layer")
    pr.add_argument("dataset")
    pr.add_argument("event_date")
    pr.add_argument("--table", default="")
    pr.set_defaults(fn=cmd_prune)

    rr = sub.add_parser("runs", help="运行台账", **kw)
    rr.add_argument("--limit", type=int, default=20)
    rr.add_argument("--detail", default="", help="打印某次运行的阶段明细")
    rr.add_argument("--json", action="store_true")
    rr.set_defaults(fn=cmd_runs)

    sub.add_parser("watermarks", help="水位线", **kw).set_defaults(fn=cmd_watermarks)

    # `promote` 刻意**不继承** `--env`：它有 `--from-env` / `--to-env` 两个明确的
    # 环境参数。留一个不生效的 `--env` 比没有更糟——有人会用它，而它什么也不做。
    root_only = argparse.ArgumentParser(add_help=False)
    root_only.add_argument("--root", dest="root", default="", help="仓库根")
    pm = sub.add_parser("promote", help="把已验证的产物从 dev 晋升到 prod", parents=[root_only])
    pm.add_argument("--from-env", default="dev", help="源环境（默认 dev）")
    pm.add_argument("--to-env", default="prod", help="目标环境（默认 prod）")
    pm.add_argument("--dry-run", action="store_true", help="只过闸门并报告，不落盘")
    pm.add_argument("--force", action="store_true", help="跳过闸门（**不跳过对账**）")
    pm.add_argument(
        "--prune-stale",
        action="store_true",
        help="删除目标环境里源没有的陈旧分区（唯一会产生删除的开关）",
    )
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(fn=cmd_promote)

    s = sub.add_parser("serve", help="只读数据服务（RBAC + 限流 + 契约闸门）", **kw)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--rbac", default="configs/rbac.yaml")
    s.add_argument("--contracts", default="configs/contracts_platform")
    s.add_argument("--no-contract-gate", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="只跑契约闸门，不起服务")
    s.set_defaults(fn=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
