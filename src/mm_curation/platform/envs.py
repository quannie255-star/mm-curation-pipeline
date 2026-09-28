"""环境解析与 DEV→PROD 参数化晋升（S6 · 环境与交付）。

## 为什么要把「仓库根」和「产物根」拆成两个概念

在这之前，整个平台轨只有**一个** `root`，它同时承担了三件事：

1. 读源（`data/raw/**`）
2. 读配置（`configs/**`、契约）
3. 写产物（`data/lake/**`、`data/warehouse/*.duckdb`、`runs/obs/**`）

于是"换一个环境"这种需求无处可放——任何 `--env` 都只能靠"换一个仓库副本"来
模拟，而那会把**代码**也复制一份，于是"dev 上验过的代码晋升到 prod"就永远
无法与"dev 上验过的代码"是同一份。这不是洁癖：`git_sha` 是台账的核心字段，
如果 prod 跑的是另一份工作区，"prod 这次运行的代码是什么"就答不上来。

所以这里拆成：

| 概念 | 内容 | 每个环境几份 |
|---|---|---|
| **repo** | 代码、`data/raw/**`、`configs/**` | **一份**（同一份工作区、同一个 git_sha） |
| **store** | 湖、数仓、台账、观测快照 | **每环境一份** |

## dev 的 store 就是仓库根：这是刻意的

`dev` 的 `store == repo`（路径仍是 `data/lake/`、`data/warehouse/`、`runs/obs/`）。
**不是为了省事**，而是：既有文档、RUNBOOK、脚本、测试、历史产物全按这三个路径写，
把它们整体搬到 `data/envs/dev/` 会让「引入环境概念」这次改动波及一切，
而隔离目标（prod 不污染 dev）用"只给 prod 一个新目录"就已经达成。
新克隆的仓库与老文档因此都不需要改。

`prod` 的 store 是 `data/envs/prod/`，与 dev 完全物理隔离。

## 晋升（promote）的语义

晋升 = **把 dev 上已验证的那份产物快照搬到 prod，并留下可追溯的记录**。
它不是"重跑一遍"——重跑会得到另一份数据，那样 prod 就没有"这份数是 dev 上
验过的那份"这个性质了。

三道闸门，任何一道不过就不搬：

1. dev 台账的**最近一次终态运行**必须是 `SUCCESS`（在途不算结论，同 `obs` 的口径）；
2. 该次运行的观测快照里**没有 crit 告警**（拿不到快照时如实记 `skipped`，不假装通过）；
3. 搬完**按「路径 + 字节」对账**：目标树指纹必须与源一致，不一致记 `FAILED`。

第 3 条是对"复制成功"这句话的唯一硬证据。`shutil.copy2` 返回不等于成功，
"我执行了复制"也不等于"目标就是源"。

另有一道**前置**拒绝（不算闸门，因为它发生在动任何文件之前）：目标环境里存在
源里没有的**陈旧分区**时，默认拒绝并提示 `--prune-stale`。见 `sync_tree`。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_DEV = "dev"
ENV_PROD = "prod"
ENVS = (ENV_DEV, ENV_PROD)

# 非 dev 环境的产物根放在这里；dev 例外（就是仓库根，见模块文档）
_ENV_HOME = Path("data") / "envs"

__all__ = [
    "ENV_DEV",
    "ENV_PROD",
    "ENVS",
    "EnvSpec",
    "PromotionReport",
    "promote",
    "promotion_gate",
    "resolve",
    "sync_tree",
    "tree_fingerprint",
]


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvSpec:
    """一个环境的两个根。`repo` 全局唯一，`store` 每环境一份。"""

    name: str
    repo: Path
    store: Path

    # -- 产物路径（与既有约定逐字一致：dev 下 == 老的硬编码路径）-----------

    @property
    def lake_dir(self) -> Path:
        return self.store / "data" / "lake"

    @property
    def warehouse_db(self) -> Path:
        return self.store / "data" / "warehouse" / "platform.duckdb"

    @property
    def obs_dir(self) -> Path:
        return self.store / "runs" / "obs"

    def as_dict(self) -> dict[str, Any]:
        return {
            "env": self.name,
            "repo": str(self.repo),
            "store": str(self.store),
            "lake_dir": str(self.lake_dir),
            "warehouse_db": str(self.warehouse_db),
            "obs_dir": str(self.obs_dir),
            "isolated": self.store != self.repo,
        }


def resolve(repo_root: str | Path, name: str = ENV_DEV) -> EnvSpec:
    """环境名 → 两个根。名字非法时**抛错**而不是回落到 dev。

    回落是最容易出的事故：`--env prd`（拼错）如果静默当成 dev，
    那次"生产发布"就会把数据写进 dev，而日志上看一切正常。
    """
    repo = Path(repo_root).resolve()
    n = (name or ENV_DEV).strip().lower()
    if n not in ENVS:
        raise ValueError(f"未知环境 {name!r}；合法值 {ENVS}")
    store = repo if n == ENV_DEV else repo / _ENV_HOME / n
    return EnvSpec(name=n, repo=repo, store=store)


# ---------------------------------------------------------------------------
# 树指纹（晋升的"同一份产物"判据）
# ---------------------------------------------------------------------------


def tree_fingerprint(root: str | Path, pattern: str = "**/*.parquet") -> dict[str, Any]:
    """产物树的指纹：文件数 + 总字节 + 逐文件 `(相对路径, 字节)` 的 sha256。

    **为什么不逐文件算内容哈希**：一次全量是 935 个分区文件 / 5.6 MB，
    内容哈希要读满 5.6 MB 且随数据增长线性变慢；`(相对路径, 字节)` 已经能抓住
    "少了一个分区""某个分区被截断"这两类真实事故，而且它明确**不是**内容校验和
    ——把它当内容哈希用是过度信任，命名与文档里都必须说清这一点。
    """
    base = Path(root)
    files = sorted(p for p in base.rglob(pattern.split("**/")[-1]) if p.is_file())
    h = hashlib.sha256()
    n_bytes = 0
    for p in files:
        size = p.stat().st_size
        n_bytes += size
        h.update(f"{p.relative_to(base).as_posix()}\0{size}\n".encode("utf-8"))
    return {
        "n_files": len(files),
        "bytes": n_bytes,
        "digest": h.hexdigest(),
        "kind": "path+size",  # 明确不是内容哈希
    }


def _rel_files(root: Path, suffix: str = ".parquet") -> dict[str, int]:
    """`相对路径 → 字节数`。树为空/不存在时返回空字典（不是报错：空是合法起点）。"""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.stat().st_size
        for p in sorted(root.rglob(f"*{suffix}"))
        if p.is_file()
    }


def sync_tree(src: Path, dst: Path, *, prune_stale: bool, dry_run: bool = False) -> dict[str, Any]:
    """把 `src` 的 Parquet 增量同步到 `dst`。**先算陈旧集，再决定动不动手。**

    ## 为什么不是 `rmtree` + `copytree`

    第一版是"删掉目标树再整棵复制"。两个问题，第二个是真问题：

    1. **代价**：935 个文件全量重写，而两次晋升之间通常只差几个分区；
    2. **不可逆**：万一 `--from-env` 写错（把空环境当源），`rmtree` 已经把
       目标环境删干净了——**在检查源之前就删掉了目标**。顺序错了。

    所以现在是：先算「源有什么」「目标有什么」，把目标里**源没有的**分区
    （陈旧分区）列出来；只要存在陈旧分区且没给 `--prune-stale`，就**在动任何
    文件之前**拒绝。这样"拒绝"这个动作是**无副作用**的，可以放心地先跑
    `--dry-run` 看一遍。

    `prune_stale=True` 时才会删陈旧分区——那是唯一会产生删除的分支，
    因此也是唯一需要显式勾选的。

    ⚠️ `dry_run=True` 走的是**同一套计算**、只是不落盘。它必须存在，而不能靠
    "调用方自己别调"：第一版的 `promote(dry_run=True)` 先调了一次真正的
    `sync_tree` 再判断 dry-run，于是 `--dry-run` **真的把整棵树复制过去了**。
    一个声称"只报告不改动"的路径，只能由它自己的参数保证。

    删除陈旧分区后会**向上清理空目录**：Hive 分区是目录即分区，
    留下一个空的 `dataset=ghost/` 目录，下一次 `partitions()` 仍会把它数进去。
    """
    src_files = _rel_files(src)
    dst_files = _rel_files(dst)
    stale = sorted(set(dst_files) - set(src_files))
    plan = {
        "ok": True,
        "reason": "",
        "stale": stale,
        "copied": 0,
        "pruned": 0,
        "unchanged": 0,
    }
    if stale and not prune_stale:
        plan["ok"] = False
        plan["reason"] = (
            f"目标环境有 {len(stale)} 个源里不存在的分区（陈旧分区），"
            "拒绝同步以免删掉它们；确认后加 --prune-stale 重试"
        )
        return plan

    to_copy = [
        rel
        for rel, size in src_files.items()
        if not (dst_files.get(rel) == size and (dst / rel).exists())
    ]
    plan["unchanged"] = len(src_files) - len(to_copy)
    plan["copied"] = len(to_copy)
    plan["pruned"] = len(stale)
    if dry_run:
        plan["dry_run"] = True
        return plan

    for rel in to_copy:
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, d)
    for rel in stale:
        p = dst / rel
        p.unlink()
        _prune_empty_parents(p.parent, dst)
    return plan


def _prune_empty_parents(d: Path, stop: Path) -> None:
    """从 `d` 向上删掉空目录，直到 `stop`（含 stop 的下级，不含 stop 本身）。"""
    cur = d
    while cur != stop and cur.is_relative_to(stop):
        try:
            next(cur.iterdir())
        except StopIteration:
            cur.rmdir()
            cur = cur.parent
            continue
        break


# ---------------------------------------------------------------------------
# 晋升
# ---------------------------------------------------------------------------


@dataclass
class PromotionReport:
    ok: bool
    reason: str
    from_env: str
    to_env: str
    from_run: str = ""
    to_run: str = ""
    gate: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    dest: dict[str, Any] = field(default_factory=dict)
    contracts: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "from_env": self.from_env,
            "to_env": self.to_env,
            "from_run": self.from_run,
            "to_run": self.to_run,
            "dry_run": self.dry_run,
            "gate": self.gate,
            "source": self.source,
            "dest": self.dest,
            "contracts": self.contracts,
        }


def _latest_terminal(ledger) -> dict[str, Any] | None:
    from .runs import TERMINAL

    for r in ledger.list_runs(limit=50):
        if r.get("status") in TERMINAL:
            return r
    return None


def promotion_gate(
    repo_root: str | Path,
    *,
    from_env: str = ENV_DEV,
) -> dict[str, Any]:
    """晋升前必须过的闸门。**只读**，可在 dry-run 里单独调用。"""
    src = resolve(repo_root, from_env)
    from .runs import RunLedger

    out: dict[str, Any] = {"ok": False, "reason": "", "run_id": "", "obs": "skipped", "crit": []}
    if not src.warehouse_db.exists():
        out["reason"] = f"{from_env} 环境没有台账（{src.warehouse_db} 不存在），没有可晋升的运行"
        return out
    led = RunLedger(src.warehouse_db)
    run = _latest_terminal(led)
    if run is None:
        out["reason"] = f"{from_env} 台账里没有已结束的运行（只有在途或空台账）"
        return out
    out["run_id"] = run["run_id"]
    out["status"] = run["status"]
    if run.get("status") != "SUCCESS":
        out["reason"] = (
            f"{from_env} 最近一次已结束的运行 {run['run_id']} 终态是 {run['status']}，"
            "只有 SUCCESS 的运行才允许晋升"
        )
        return out

    # 观测快照：拿不到就如实记 skipped，**不假装通过**
    snap_path = src.obs_dir / f"{run['run_id']}.json"
    if not snap_path.exists():
        out["obs"] = "missing"
        out["ok"] = True
        out["reason"] = "闸门通过（但该次运行没有观测快照，告警闸门未生效）"
        return out
    import json

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    crit = [a["fingerprint"] for a in snap.get("alerts", []) if a.get("severity") == "crit"]
    out["obs"] = "crit" if crit else "clean"
    out["crit"] = crit
    if crit:
        out["reason"] = f"该次运行有 {len(crit)} 条 crit 告警，拒绝晋升：{crit}"
        return out
    out["ok"] = True
    out["reason"] = "闸门通过"
    return out


def promote(
    repo_root: str | Path,
    *,
    from_env: str = ENV_DEV,
    to_env: str = ENV_PROD,
    dry_run: bool = False,
    force: bool = False,
    prune_stale: bool = False,
) -> dict[str, Any]:
    """把 `from_env` 上已验证的产物快照晋升到 `to_env`。

    `force=True` 只跳过**闸门**（第 1、2 道），不跳过**对账**（第 3 道）——
    对账失败说明这次晋升的结果不可信，那不是"要不要拦"的问题。

    `prune_stale=True` 才允许删除目标环境里源没有的分区。默认不允许：
    删除是唯一不可逆的动作，必须显式勾选（见 `sync_tree`）。
    """
    from .lake import Lake
    from .modeling import refresh_views
    from .runs import RunLedger, git_sha

    src = resolve(repo_root, from_env)
    dst = resolve(repo_root, to_env)
    if src.name == dst.name:
        return PromotionReport(False, "源环境与目标环境相同", src.name, dst.name).as_dict()

    gate = promotion_gate(repo_root, from_env=from_env)
    rep = PromotionReport(
        ok=False,
        reason=gate["reason"],
        from_env=src.name,
        to_env=dst.name,
        from_run=gate.get("run_id", ""),
        gate=gate,
        dry_run=dry_run,
    )
    if not gate["ok"] and not force:
        return rep.as_dict()

    rep.source = tree_fingerprint(src.lake_dir)
    rep.dest = tree_fingerprint(dst.lake_dir)
    if rep.source["n_files"] == 0:
        rep.reason = f"{from_env} 的湖是空的（{src.lake_dir}），没有可晋升的分区"
        return rep.as_dict()

    # 先算陈旧集：先做一次**不落盘**的试算，存在陈旧分区且未勾选 prune_stale 时
    # **在动任何文件之前**拒绝。`dry_run=True` 保证"拒绝"与"报告"这两个动作
    # 都是无副作用的——第一版这里调了真正的 sync_tree，于是 `--dry-run` 会复制。
    plan = sync_tree(src.lake_dir, dst.lake_dir, prune_stale=prune_stale, dry_run=True)
    stale_n = len(plan["stale"])
    if stale_n and not prune_stale:
        rep.reason = (
            f"目标环境有 {stale_n} 个源里不存在的陈旧分区，拒绝同步（未改动任何文件）；"
            "确认后加 --prune-stale 重试"
        )
        rep.gate["stale_files"] = plan["stale"][:10]
        return rep.as_dict()

    if dry_run:
        rep.ok = True
        rep.reason = (
            f"dry-run：将同步 {rep.source['n_files']} 个分区文件"
            f"（新增/更新 {plan['copied']}，陈旧待删 {stale_n}），闸门 {gate['reason']}"
        )
        rep.gate["plan"] = {k: plan[k] for k in ("copied", "unchanged", "stale", "pruned")}
        return rep.as_dict()

    # -- 落盘（唯一会产生写入的分支）----------------------------------------
    dst.lake_dir.parent.mkdir(parents=True, exist_ok=True)
    plan = sync_tree(src.lake_dir, dst.lake_dir, prune_stale=prune_stale)
    rep.dest = tree_fingerprint(dst.lake_dir)

    dst.warehouse_db.parent.mkdir(parents=True, exist_ok=True)
    dst.obs_dir.mkdir(parents=True, exist_ok=True)
    led = RunLedger(dst.warehouse_db)
    run_id = RunLedger.new_run_id(f"promote_{dst.name}", _today())
    led.start_run(
        run_id,
        f"promote_{dst.name}",
        _today(),
        {
            "promoted_from_env": src.name,
            "promoted_from_run": rep.from_run,
            "n_files": rep.dest["n_files"],
            "bytes": rep.dest["bytes"],
            "copied": plan["copied"],
            "unchanged": plan["unchanged"],
            "pruned": plan["pruned"],
            "source_digest": rep.source["digest"],
            "git_sha": git_sha(src.repo),
        },
    )
    rep.to_run = run_id
    rep.gate["plan"] = {k: plan[k] for k in ("copied", "unchanged", "stale", "pruned")}

    import duckdb

    con = duckdb.connect(str(dst.warehouse_db))
    try:
        con.execute(f"SET home_directory='{dst.store.as_posix()}'")
        made = refresh_views(con, Lake(dst.lake_dir))

        # -- 对账：目标必须是源的一份精确副本（路径 + 字节）--
        if rep.dest["digest"] != rep.source["digest"]:
            led.finish_run(run_id, "FAILED", "晋升对账不一致：目标树与源树指纹不同")
            rep.ok = False
            rep.reason = (
                f"对账失败：源 {rep.source['n_files']} 文件/{rep.source['bytes']} B，"
                f"目标 {rep.dest['n_files']} 文件/{rep.dest['bytes']} B"
            )
            return rep.as_dict()

        # -- 目标环境自己跑一遍契约闸门：晋升过来的是数据，不是结论 --
        from ..lineage.contract import check_contract, load_contracts

        cdir = src.repo / "configs" / "contracts_platform"
        contracts = load_contracts(cdir)
        wh = _ConQuery(con)
        results = [check_contract(wh, c) for c in contracts]
        n_fail = sum(1 for r in results if not r["ok"])
        rep.contracts = {"n_contracts": len(results), "n_failed": n_fail}
        if n_fail:
            led.finish_run(run_id, "FAILED", f"目标环境契约闸门 {n_fail}/{len(results)} 未过")
            rep.ok = False
            rep.reason = f"晋升落盘了，但目标环境契约 {n_fail}/{len(results)} 未过 → 记 FAILED"
            return rep.as_dict()

        led.finish_run(run_id, "SUCCESS")
        rep.ok = True
        rep.reason = (
            f"晋升完成：{rep.dest['n_files']} 个分区文件 / {rep.dest['bytes']} B"
            f"（新增更新 {plan['copied']}、未变 {plan['unchanged']}、删除陈旧 {plan['pruned']}），"
            f"视图 {len(made)} 个，契约 {len(results)}/{len(results)} 通过，"
            f"{dst.name} 台账记为 {run_id}"
        )
        return rep.as_dict()
    finally:
        con.close()


class _ConQuery:
    """把 DuckDB 连接适配成 `check_contract` 认识的仓库句柄（同 `jobs._LakeWarehouse`）。"""

    def __init__(self, con):
        self.con = con

    def connect(self):
        return self.con

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()


def _today() -> str:
    import datetime as _dt

    return _dt.date.today().isoformat()
