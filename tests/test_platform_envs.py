"""环境（`platform/envs.py`）：解析、dev/prod 物理隔离、DEV→PROD 晋升的闸门与对账。

这个文件测的是 S6 里最容易被"写在文档上就算做到"的那部分：
**"有 dev/prod 双环境"和"参数化晋升"**都可以只靠一份 README 宣称，
所以每一条都必须落在一个能失败的断言上——

- 隔离：跑完 prod，dev 的湖指纹**逐位没变**；
- 闸门：最近一次终态是 FAILED 时**必须拒绝**（而不是打个警告继续搬）；
- 对账：目标树指纹必须等于源树指纹，且这个指纹**明确不是内容哈希**
  （第 12 条测试就是在钉死这个边界，免得后来者把它当校验和用）；
- 无副作用：`--dry-run` 与"因陈旧分区被拒绝"这两条路径**都不许改文件**。

`tmp_path` 在系统 Temp 下，不占工作区的删除额度；测试之间互不影响。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from mm_curation.platform import envs, jobs  # noqa: E402
from mm_curation.platform.dag import Context  # noqa: E402
from mm_curation.platform.envs import (  # noqa: E402
    ENV_DEV,
    ENV_PROD,
    sync_tree,
    tree_fingerprint,
)
from mm_curation.platform.runs import RunLedger  # noqa: E402

DATASETS = ("metropt3", "news_corpus")


def _run(repo: Path, **kw):
    return jobs.run_platform(repo, datasets=DATASETS, verbose=False, **kw)


def _ledger_status(repo: Path, env: str) -> list[str]:
    led = RunLedger(envs.resolve(repo, env).warehouse_db)
    return [r["status"] for r in led.list_runs(limit=10)]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_resolve_dev_store_is_the_repo_root(lake_root):
    """dev 的 store 就是仓库根——这是**兼容性断言**，不是实现细节。

    既有文档、脚本、历史产物全按 `data/lake/`、`data/warehouse/`、`runs/obs/` 写；
    一旦 dev 也搬到 `data/envs/dev/`，所有老路径同时失效，而收益为零。
    """
    spec = envs.resolve(lake_root, ENV_DEV)
    assert spec.store == Path(lake_root).resolve()
    assert spec.lake_dir == Path(lake_root).resolve() / "data" / "lake"
    assert spec.warehouse_db == Path(lake_root).resolve() / "data" / "warehouse" / "platform.duckdb"
    assert spec.obs_dir == Path(lake_root).resolve() / "runs" / "obs"
    assert spec.as_dict()["isolated"] is False


def test_resolve_prod_store_is_isolated(lake_root):
    spec = envs.resolve(lake_root, ENV_PROD)
    dev = envs.resolve(lake_root, ENV_DEV)
    assert spec.store == dev.store / "data" / "envs" / "prod"
    assert spec.lake_dir != dev.lake_dir
    assert spec.repo == dev.repo  # 代码/源/配置**共用同一份**
    assert spec.as_dict()["isolated"] is True


def test_resolve_rejects_unknown_env_instead_of_falling_back(lake_root):
    """拼错的环境名必须抛错。

    回落到 dev 是最坏的处理方式：`--env prd` 会把"生产发布"静默写进 dev，
    而日志、退出码、产物路径全都是正常的。
    """
    with pytest.raises(ValueError, match="未知环境"):
        envs.resolve(lake_root, "prd")


def test_context_store_defaults_to_root_so_old_callers_keep_working(lake_root):
    """不给 `store` 的 `Context` 仍然按 `root` 落产物（老调用点零改动）。"""
    ctx = Context(
        run_id="r",
        batch_date="2026-01-01",
        root=lake_root,
        ledger=None,
        con=None,
        lake=None,
    )
    assert ctx.store is None
    assert ctx.store_root() == lake_root


# ---------------------------------------------------------------------------
# 物理隔离
# ---------------------------------------------------------------------------


def test_running_prod_does_not_change_dev(lake_root):
    """跑一次 prod，dev 的湖与台账必须**逐位没变**。"""
    _run(lake_root)
    dev_lake = tree_fingerprint(envs.resolve(lake_root, ENV_DEV).lake_dir)
    dev_db = envs.resolve(lake_root, ENV_DEV).warehouse_db.read_bytes()

    res = _run(lake_root, env=ENV_PROD)
    assert res["status"] == "SUCCESS"
    assert res["env"] == ENV_PROD

    assert tree_fingerprint(envs.resolve(lake_root, ENV_DEV).lake_dir) == dev_lake
    assert envs.resolve(lake_root, ENV_DEV).warehouse_db.read_bytes() == dev_db

    prod = envs.resolve(lake_root, ENV_PROD)
    assert tree_fingerprint(prod.lake_dir)["n_files"] > 0
    assert prod.warehouse_db.exists()
    # 两次运行各自记账，互不覆盖
    assert _ledger_status(lake_root, ENV_DEV) == ["SUCCESS"]
    assert _ledger_status(lake_root, ENV_PROD) == ["SUCCESS"]


def test_prod_obs_snapshot_lands_in_prod_store_not_repo_runs(lake_root):
    """观测快照与指标文件也必须按环境隔离——它们在 `runs/obs/`，最容易漏。"""
    _run(lake_root, env=ENV_PROD)
    prod = envs.resolve(lake_root, ENV_PROD)
    proms = list(prod.obs_dir.glob("*.prom"))
    assert proms, "prod 的 obs 目录应有运行指标文件"
    assert not list((Path(lake_root) / "runs" / "obs").glob("*.prom")), (
        "dev 的 runs/obs 不该收到 prod 的文件"
    )


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------


def test_gate_refuses_when_there_is_no_ledger(lake_root):
    gate = envs.promotion_gate(lake_root, from_env=ENV_DEV)
    assert gate["ok"] is False
    assert "没有台账" in gate["reason"]


def test_gate_refuses_when_latest_terminal_run_failed(lake_root):
    _run(lake_root)
    led = RunLedger(envs.resolve(lake_root, ENV_DEV).warehouse_db)
    led.start_run("platform__t__0002", "platform", "2026-09-22", {})
    led.finish_run("platform__t__0002", "FAILED", "boom")

    gate = envs.promotion_gate(lake_root, from_env=ENV_DEV)
    assert gate["ok"] is False
    assert gate["status"] == "FAILED"
    assert "platform__t__0002" in gate["reason"]


def test_gate_ignores_inflight_running_run(lake_root):
    """在途的 RUNNING 不算结论（与 `obs.pipeline_health` 的口径**故意一致**）。"""
    _run(lake_root)
    led = RunLedger(envs.resolve(lake_root, ENV_DEV).warehouse_db)
    led.start_run("platform__t__0003", "platform", "2026-09-22", {})  # 不 finish → RUNNING

    gate = envs.promotion_gate(lake_root, from_env=ENV_DEV)
    assert gate["ok"] is True
    assert gate["run_id"] != "platform__t__0003"


def test_gate_passes_on_success_and_says_when_the_alert_gate_did_not_apply(lake_root):
    """拿不到观测快照时如实记 missing，**不假装告警闸门过了**。"""
    _run(lake_root)
    led = RunLedger(envs.resolve(lake_root, ENV_DEV).warehouse_db)
    run_id = led.list_runs(limit=1)[0]["run_id"]

    gate = envs.promotion_gate(lake_root, from_env=ENV_DEV)
    assert gate["ok"] is True
    snap = envs.resolve(lake_root, ENV_DEV).obs_dir / f"{run_id}.json"
    assert snap.exists(), "run_platform 应当留下观测快照"
    assert gate["obs"] in ("clean", "crit")
    assert "告警闸门" not in gate["reason"]

    # 把快照删掉 → 闸门仍然放行，但必须说明告警闸门没生效
    snap.unlink()
    gate2 = envs.promotion_gate(lake_root, from_env=ENV_DEV)
    assert gate2["ok"] is True
    assert gate2["obs"] == "missing"
    assert "没有观测快照" in gate2["reason"]


def test_gate_refuses_when_snapshot_has_crit_alerts(lake_root):
    _run(lake_root)
    dev = envs.resolve(lake_root, ENV_DEV)
    led = RunLedger(dev.warehouse_db)
    run_id = led.list_runs(limit=1)[0]["run_id"]
    snap_path = dev.obs_dir / f"{run_id}.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    snap["alerts"] = [
        {"fingerprint": "crit::demo", "severity": "crit", "n_signals": 3, "sample": "x"}
    ]
    snap_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    gate = envs.promotion_gate(lake_root, from_env=ENV_DEV)
    assert gate["ok"] is False
    assert gate["crit"] == ["crit::demo"]


# ---------------------------------------------------------------------------
# 晋升
# ---------------------------------------------------------------------------


def test_promote_rejects_same_source_and_target(lake_root):
    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_DEV)
    assert rep["ok"] is False
    assert "相同" in rep["reason"]


def test_promote_dry_run_changes_nothing(lake_root):
    """`--dry-run` 必须**连一个文件都不写**。

    这条测试抓过一个真 bug：第一版 `promote(dry_run=True)` 先调了一次真正的
    `sync_tree` 再判断 dry-run，于是"只报告不改动"的模式把整棵树复制过去了，
    报告还写着 dry-run。无副作用的路径只能由它自己的参数保证。
    """
    _run(lake_root)
    prod = envs.resolve(lake_root, ENV_PROD)
    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD, dry_run=True)
    assert rep["ok"] is True
    assert "dry-run" in rep["reason"]
    assert rep["gate"]["plan"]["copied"] > 0, "dry-run 仍应如实报出会写多少文件"
    assert not prod.lake_dir.exists()
    assert not prod.warehouse_db.exists()
    assert not prod.store.exists(), "dry-run 不该建出目标环境的任何目录"


def test_promote_copies_the_tree_and_records_a_run_in_the_target_ledger(lake_root):
    _run(lake_root)
    dev, prod = envs.resolve(lake_root, ENV_DEV), envs.resolve(lake_root, ENV_PROD)
    dev_fp = tree_fingerprint(dev.lake_dir)

    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert rep["ok"] is True, rep["reason"]
    assert rep["dest"]["digest"] == rep["source"]["digest"] == dev_fp["digest"]

    led = RunLedger(prod.warehouse_db)
    runs = led.list_runs(limit=5)
    assert runs[0]["run_id"] == rep["to_run"]
    assert runs[0]["status"] == "SUCCESS"
    detail = led.run_detail(rep["to_run"])
    assert detail["job"]["job_name"] == "promote_prod"
    params = json.loads(detail["job"]["params"])
    assert params["promoted_from_env"] == ENV_DEV
    assert params["n_files"] == dev_fp["n_files"]

    # prod 的视图必须能查到晋升过来的数据（不只是文件到位）
    con = duckdb.connect(str(prod.warehouse_db))
    try:
        n = con.execute("SELECT count(*) FROM ads_dataset_health").fetchone()[0]
    finally:
        con.close()
    assert n > 0


def test_promote_leaves_every_relation_the_service_queries_readable_in_prod(lake_root):
    """**晋升后服务会打的每个端点都还能查** —— 2026-10-05 真实 prod 库上 500 的回归门禁。

    发现的原始缺陷：`dim_device` 曾是 DuckDB 里的 **BASE TABLE**，不在湖上 Parquet 上，
    而 `promote` 只搬湖上产物 → prod 库少了这两张表，`/api/datasets/{ds}/dims` 直接 500。
    **契约闸门 10/10 全绿却没抓到**，因为它只查 `ods/dwd/dws/ads` 四个视图层，
    维表既不在湖上、也不在契约范围里。

    判据**从 service 源码现取**（扫 `FROM <名字>`），不是硬编码一张表清单——
    硬编码的清单会随新端点一起腐烂，而"清单没更新"与"端点坏了"看起来一模一样。
    扫不出来时直接 `assert` 失败，不跳过：跳过的门禁等于没有门禁，
    而且它会报出一个漂亮的绿灯。
    """
    import re as _re

    from mm_curation.platform import service as service_mod

    src = Path(service_mod.__file__).read_text(encoding="utf-8")
    tables = sorted(set(_re.findall(r"FROM\s+([a-z_][a-z0-9_]*)", src)))
    assert tables, "扫不到 service 查询的任何表名——判据本身失效了，不许静默跳过"

    _run(lake_root)
    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert rep["ok"] is True, rep["reason"]

    prod = envs.resolve(lake_root, ENV_PROD)
    con = duckdb.connect(str(prod.warehouse_db))
    try:
        missing, empty = [], []
        for t in tables:
            try:
                con.execute(f"SELECT * FROM {t} LIMIT 1").fetchall()
            except duckdb.Error as e:  # 表不存在 / 湖上分区缺失
                missing.append(f"{t}: {type(e).__name__}")
                continue
            # 端点还要能返回**列**（`SELECT * ... LIMIT 0` 也要能拿到 description），
            # 否则 `/dims` 会在 `rows[0].keys()` 上炸。
            con.execute(f"SELECT * FROM {t} LIMIT 0").fetchall()
            if con.description is None:
                empty.append(t)
        # 台账表也必须在（`/api/runs` 读它）
        for t in ("job_runs",):
            con.execute(f"SELECT * FROM {t} LIMIT 1").fetchall()
    finally:
        con.close()

    assert not missing, f"晋升后 prod 库查不了这些关系（服务对应端点会 500）：{missing}"
    assert not empty, f"这些关系查不出列：{empty}"


def test_promote_second_time_is_a_no_op(lake_root):
    """幂等：第二次晋升 0 新增、0 删除、指纹不变。老实现每次都 `rmtree` + 全量重写。"""
    _run(lake_root)
    first = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert first["ok"] is True
    second = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert second["ok"] is True
    assert second["gate"]["plan"]["copied"] == 0
    assert second["gate"]["plan"]["pruned"] == 0
    assert second["dest"]["digest"] == first["dest"]["digest"]


def test_promote_refuses_stale_files_without_prune_and_touches_nothing(lake_root):
    """目标环境有源里没有的分区 → **在动任何文件之前**拒绝。"""
    _run(lake_root)
    produce = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert produce["ok"] is True

    prod = envs.resolve(lake_root, ENV_PROD)
    stale = prod.lake_dir / "ods" / "ods_samples" / "dataset=ghost" / "event_date=1999-01-01"
    stale.mkdir(parents=True)
    (stale / "part.parquet").write_bytes(b"not-really-parquet")
    before = tree_fingerprint(prod.lake_dir)

    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert rep["ok"] is False
    assert "陈旧分区" in rep["reason"]
    assert tree_fingerprint(prod.lake_dir) == before, "被拒绝时不许改动目标树"


def test_promote_prune_stale_removes_extra_partitions(lake_root):
    _run(lake_root)
    prod = envs.resolve(lake_root, ENV_PROD)
    stale = prod.lake_dir / "ods" / "ods_samples" / "dataset=ghost" / "event_date=1999-01-01"
    stale.mkdir(parents=True)
    (stale / "part.parquet").write_bytes(b"not-really-parquet")

    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD, prune_stale=True)
    assert rep["ok"] is True, rep["reason"]
    assert rep["gate"]["plan"]["pruned"] == 1
    # 文件与**空目录**都要清掉：Hive 语义下目录即分区，
    # 留一个空的 `dataset=ghost/` 下次仍会被 `partitions()` 数进去
    assert not (stale / "part.parquet").exists()
    assert not (prod.lake_dir / "ods" / "ods_samples" / "dataset=ghost").exists()


def test_promote_force_skips_the_gate_but_not_the_reconciliation(lake_root):
    """`--force` 只跳过闸门；对账（第 3 道）**任何情况下都在跑**。"""
    _run(lake_root)
    led = RunLedger(envs.resolve(lake_root, ENV_DEV).warehouse_db)
    led.start_run("platform__t__0009", "platform", "2026-09-22", {})
    led.finish_run("platform__t__0009", "FAILED", "boom")

    blocked = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert blocked["ok"] is False
    forced = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD, force=True)
    assert forced["ok"] is True, forced["reason"]
    assert forced["dest"]["digest"] == forced["source"]["digest"]
    assert forced["gate"]["ok"] is False  # 闸门本身仍然记着没过


def test_promote_refuses_when_source_lake_is_empty(lake_root):
    """空源不该被当成"晋升了 0 个文件"的成功。"""
    dev = envs.resolve(lake_root, ENV_DEV)
    dev.warehouse_db.parent.mkdir(parents=True, exist_ok=True)
    led = RunLedger(dev.warehouse_db)
    led.start_run("platform__t__0001", "platform", "2026-09-22", {})
    led.finish_run("platform__t__0001", "SUCCESS")

    rep = envs.promote(lake_root, from_env=ENV_DEV, to_env=ENV_PROD)
    assert rep["ok"] is False
    assert "湖是空的" in rep["reason"]


# ---------------------------------------------------------------------------
# 指纹与同步原语
# ---------------------------------------------------------------------------


def test_tree_fingerprint_is_path_plus_size_not_a_content_hash(tmp_path):
    """**钉死这条边界**：指纹口径是 `(相对路径, 字节)`，不是内容哈希。

    这既是它的优点（935 文件不必读满 5.6 MB），也是它的局限——
    等长改写一个分区文件不会被发现。把它当校验和用是过度信任，
    所以这条测试存在的意义是：**谁想改口径，得先改掉这条断言并解释**。
    """
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "p").mkdir(parents=True)
        (d / "p" / "x.parquet").write_bytes(b"AAAA")
    assert tree_fingerprint(a) == tree_fingerprint(b)  # 等长、内容不同 → 同指纹

    (b / "p" / "x.parquet").write_bytes(b"BBBBB")
    assert tree_fingerprint(a)["digest"] != tree_fingerprint(b)["digest"]  # 长度不同 → 能发现
    assert tree_fingerprint(a)["kind"] == "path+size"


def test_tree_fingerprint_of_missing_tree_is_empty_not_an_error(tmp_path):
    fp = tree_fingerprint(tmp_path / "nope")
    assert fp["n_files"] == 0 and fp["bytes"] == 0


def test_sync_tree_reports_stale_before_copying_anything(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "keep.parquet").parent.mkdir(parents=True)
    (src / "keep.parquet").write_bytes(b"new")
    (dst / "keep.parquet").parent.mkdir(parents=True)
    (dst / "keep.parquet").write_bytes(b"old!")
    (dst / "extra.parquet").write_bytes(b"stale")

    blocked = sync_tree(src, dst, prune_stale=False)
    assert blocked["ok"] is False
    assert blocked["stale"] == ["extra.parquet"]
    assert (dst / "keep.parquet").read_bytes() == b"old!", "拒绝时连更新都不许做"

    done = sync_tree(src, dst, prune_stale=True)
    assert done["ok"] is True
    assert (done["copied"], done["pruned"]) == (1, 1)
    assert (dst / "keep.parquet").read_bytes() == b"new"
    assert not (dst / "extra.parquet").exists()


def test_sync_tree_skips_same_size_files(tmp_path):
    """等长同路径视为未变 → 不重写（这是"第二次晋升是 no-op"的底层依据）。"""
    src, dst = tmp_path / "src", tmp_path / "dst"
    for d in (src, dst):
        (d / "x.parquet").parent.mkdir(parents=True)
        (d / "x.parquet").write_bytes(b"same")
    rep = sync_tree(src, dst, prune_stale=False)
    assert (rep["copied"], rep["unchanged"]) == (0, 1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exposes_env_on_subcommands_and_the_promote_command():
    from mm_curation.cli import build_parser

    p = build_parser()
    assert p.parse_args(["runs"]).env == "dev"
    assert p.parse_args(["runs", "--env", "prod"]).env == "prod"
    assert p.parse_args(["obs", "--env", "prod"]).env == "prod"
    pm = p.parse_args(["promote", "--dry-run", "--from-env", "dev", "--to-env", "prod"])
    assert pm.fn.__name__ == "cmd_promote"
    assert pm.dry_run is True and pm.to_env == "prod"
    # `promote` 刻意不继承 `--env`（它有 --from-env/--to-env 两个明确参数）
    assert not hasattr(pm, "env")
