"""作业装配测试：DAG 形状、端到端跑通、契约闸门、增量、续跑、CLI、Airflow 生成物同步。"""

from __future__ import annotations

from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
yaml = pytest.importorskip("yaml")

from mm_curation.platform import jobs  # noqa: E402
from mm_curation.platform.dag import to_airflow_source  # noqa: E402
from mm_curation.platform.runs import SKIPPED, SUCCESS, RunLedger, default_ledger  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# DAG 形状（纯定义，不执行）
# ---------------------------------------------------------------------------


def test_platform_dag_shape_and_chain():
    dag = jobs.build_dag()
    assert [t.name for t in dag.order()] == [
        jobs.ODS,
        jobs.DIMS,
        jobs.DWD,
        jobs.DWS,
        jobs.ADS,
        jobs.VIEWS,
        jobs.CONTRACTS,
        jobs.OBS,
        jobs.METRICS,
    ]
    assert dag.task(jobs.ODS).deps == ()
    assert dag.task(jobs.CONTRACTS).deps == (jobs.VIEWS,)
    # 契约闸门必须排在观测之前：闸门没过就不该产出观测快照
    assert dag.task(jobs.OBS).deps == (jobs.CONTRACTS,)
    assert dag.task(jobs.ODS).retries >= 1
    # 每个阶段都要有描述（Airflow 的 doc_md 直接用它）
    assert all(t.description for t in dag.tasks)


def test_platform_datasets_matches_registry():
    from mm_curation.platform.registry import datasets

    assert jobs.platform_datasets() == tuple(datasets())
    assert len(jobs.platform_datasets()) == len(set(jobs.platform_datasets()))


def test_make_context_resolves_paths_and_defaults(lake_root):
    ctx = jobs.make_context(lake_root, datasets=("metropt3",))
    try:
        assert ctx.root.is_absolute()
        assert ctx.batch_date  # 默认今天
        assert ctx.run_id.startswith("platform__")
        assert ctx.datasets == ("metropt3",)
        assert ctx.lake.root.is_absolute(), "相对 glob 会随 cwd 漂移，必须钉成绝对路径"
    finally:
        ctx.con.close()


# ---------------------------------------------------------------------------
# 端到端（7 行样本，跑真链路）
# ---------------------------------------------------------------------------


def test_run_platform_end_to_end_records_ledger_and_artifacts(lake_root):
    res = jobs.run_platform(
        lake_root, datasets=("metropt3", "news_corpus"), verbose=False, batch_date="2026-09-22"
    )
    assert res["status"] == SUCCESS, res
    assert set(res["tasks"]) == {
        jobs.ODS,
        jobs.DIMS,
        jobs.DWD,
        jobs.DWS,
        jobs.ADS,
        jobs.VIEWS,
        jobs.CONTRACTS,
        jobs.OBS,
        jobs.METRICS,
    }
    # 台账：1 个作业行 + 9 个阶段行
    led = default_ledger(lake_root)
    detail = led.run_detail(res["run_id"])
    assert detail["job"]["status"] == SUCCESS
    assert len(detail["tasks"]) == 9
    assert all(t["status"] == SUCCESS for t in detail["tasks"])
    # 每层的产物都在
    from mm_curation.platform.lake import default_lake

    lake = default_lake(lake_root)
    for layer, table in (
        ("ods", "ods_samples"),
        ("dwd", "dwd_window"),
        ("dwd", "dwd_op_score"),
        ("dws", "dws_dataset_day"),
        ("dws", "dws_op_day"),
        ("ads", "ads_dataset_health"),
    ):
        assert lake.exists(layer, table), f"{layer}/{table} 没有产物"
    # 观测快照与 Prometheus 文本都落了盘（可归档，不只是"当前值"）
    obs_path = lake_root / "runs" / "obs" / f"{res['run_id']}.json"
    prom_path = lake_root / "runs" / "obs" / f"{res['run_id']}.prom"
    assert obs_path.exists() and prom_path.exists()
    assert "mm_dataset_rows" in prom_path.read_text(encoding="utf-8")


def test_run_platform_obs_summary_mentions_convergence(lake_root):
    first = jobs.run_platform(lake_root, datasets=("metropt3", "news_corpus"), verbose=False)
    # 第一次运行时，台账里**本次这条还是 RUNNING**，没有任何已终态的运行 →
    # 成功率必须是 `None`（"算不出来"），不是 0（"全失败"）也不是 1（"全成功"）
    assert first["tasks"][jobs.OBS]["pipeline_success_rate"] is None

    res = jobs.run_platform(lake_root, datasets=("metropt3", "news_corpus"), verbose=False)
    o = res["tasks"][jobs.OBS]
    assert "收敛告警" in o["summary"]
    assert o["n_signals"] >= o["n_alerts"]
    # 第二次运行时，上一次已 SUCCESS 落成终态 → 分母 = 1 → 1.0
    assert o["pipeline_success_rate"] == 1.0


def test_run_platform_contract_gate_fails_the_run(lake_root):
    """契约闸门失败 → 整条运行 FAILED，且**下游 obs/metrics 不跑**。"""
    d = lake_root / "configs" / "contracts_platform"
    d.mkdir(parents=True, exist_ok=True)
    (d / "core.yaml").write_text(
        yaml.safe_dump(
            {
                "dataset": "text_funnel",
                "version": 1,
                "owner": "test",
                "table": "ods_samples",
                "checks": [
                    {"name": "must_fail", "sql": "SELECT 1", "expect": 0, "severity": "error"}
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    res = jobs.run_platform(lake_root, datasets=("metropt3",), verbose=False)
    assert res["status"] == "FAILED"
    assert res["failed_task"] == jobs.CONTRACTS
    assert jobs.OBS not in res["tasks"] and jobs.METRICS not in res["tasks"]
    assert not (lake_root / "runs" / "obs").exists() or not any(
        (lake_root / "runs" / "obs").glob("*.json")
    )
    led = default_ledger(lake_root)
    assert led.task_states(res["run_id"])[jobs.CONTRACTS] == "FAILED"


def test_run_platform_incremental_second_run_writes_almost_nothing(lake_root):
    first = jobs.run_platform(lake_root, datasets=("metropt3", "news_corpus"), verbose=False)
    assert first["tasks"][jobs.ODS]["n_out"] == 7
    second = jobs.run_platform(
        lake_root, datasets=("metropt3", "news_corpus"), verbose=False, incremental=True
    )
    assert second["status"] == SUCCESS
    # 只剩未标事件时间的那 1 条会被重写（它无法用分区值寻址）
    assert second["tasks"][jobs.ODS]["n_out"] == 1
    assert second["tasks"][jobs.ODS]["n_skipped_records"] == 6


def test_run_platform_resume_skips_everything(lake_root):
    first = jobs.run_platform(lake_root, datasets=("metropt3",), verbose=False)
    again = jobs.run_platform(
        lake_root, datasets=("metropt3",), verbose=False, run_id=first["run_id"], resume=True
    )
    assert again["status"] == SUCCESS
    assert all(r["_status"] == SKIPPED for r in again["tasks"].values())


def test_run_platform_only_runs_single_stage_without_touching_job_runs(lake_root):
    """`--only`（Airflow 单阶段）只写 task_runs，**不写 job_runs**。

    理由：一个 Airflow DAG run 的所有阶段共用同一个 run_id，各阶段是独立进程；
    若都去 INSERT OR REPLACE 那行，最后留下的是最后一个阶段的耗时与状态——那是假数。
    """
    res = jobs.run_platform(lake_root, datasets=("metropt3",), verbose=False, only=(jobs.ODS,))
    assert res["status"] == SUCCESS
    assert set(res["tasks"]) == {jobs.ODS}
    led = default_ledger(lake_root)
    assert led.list_runs() == [], "单阶段模式不该产生 job_runs 记录"
    assert led.task_states(res["run_id"]) == {jobs.ODS: SUCCESS}


def test_run_platform_events_dates_filter(lake_root):
    res = jobs.run_platform(
        lake_root, datasets=("metropt3",), event_dates=("2020-04-01",), verbose=False
    )
    assert res["tasks"][jobs.ODS]["n_out"] == 2


def test_run_platform_contract_blocking_disabled(lake_root):
    d = lake_root / "configs" / "contracts_platform"
    d.mkdir(parents=True, exist_ok=True)
    (d / "core.yaml").write_text(
        yaml.safe_dump(
            {
                "dataset": "text_funnel",
                "version": 1,
                "table": "ods_samples",
                "checks": [{"name": "f", "sql": "SELECT 1", "expect": 0, "severity": "error"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    res = jobs.run_platform(
        lake_root, datasets=("metropt3",), verbose=False, contract_blocking=False
    )
    assert res["status"] == SUCCESS, "关掉阻断后即使断言失败也要能跑完（仅供调试）"
    assert res["tasks"][jobs.CONTRACTS]["n_fail"] == 1


# ---------------------------------------------------------------------------
# Airflow 生成物与定义保持同步
# ---------------------------------------------------------------------------


def test_committed_airflow_dag_is_in_sync_with_definition():
    """仓库里那份生成物必须**逐字节等于**重新生成的结果。

    这是一条"产物被验证"而不是"产物被声明"的断言：手改生成的 DAG、
    或改了 jobs.py 忘了重新导出，都会在这里红。
    """
    committed = REPO / "dags" / "generated_platform_dag.py"
    assert committed.exists(), "生成物缺失；跑 `mmc platform dag --export-airflow ...`"
    expected = to_airflow_source(jobs.build_dag(), schedule="@daily")
    assert committed.read_text(encoding="utf-8") == expected


def test_airflow_dag_uses_only_stage_invocation():
    src = to_airflow_source(jobs.build_dag(), schedule="@daily")
    # 命令在生成物里是跨行拼接的，所以按片段断言而不是断言一整行
    assert "python -m mm_curation.cli platform run " in src
    assert '"--only " + _name' in src
    assert "{{ run_id }}" in src and "{{ ds }}" in src
    # 每个阶段都必须独立成进程（这是超时可强杀、重试可隔离的前提）
    assert "BashOperator" in src


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_parser_accepts_platform_subcommands():
    from mm_curation.cli import build_parser

    p = build_parser()
    a = p.parse_args(["run", "--datasets", "metropt3,cmapss", "--incremental"])
    assert a.cmd == "run" and a.datasets == "metropt3,cmapss" and a.incremental is True
    a2 = p.parse_args(["dag", "--export-airflow", "x.py", "--schedule", "@hourly"])
    assert a2.schedule == "@hourly"
    # `--root` 写在子命令**前**或**后**都要能用（argparse 的子命名空间会整体
    # 覆盖回父级，所以两个位置用了不同的 dest，读取统一走 `_root`）
    from mm_curation.cli import _root

    assert _root(p.parse_args(["--root", str(REPO), "obs", "--json"])).name == REPO.name
    assert _root(p.parse_args(["obs", "--root", str(REPO), "--json"])).name == REPO.name
    a3 = p.parse_args(["--root", str(REPO), "obs", "--json"])
    assert a3.json is True
    a4 = p.parse_args(["serve", "--port", "9000", "--dry-run"])
    assert a4.port == 9000 and a4.dry_run is True


def test_cli_cmd_dag_and_runs(tmp_path, capsys):
    from mm_curation.cli import main

    assert main(["--root", str(tmp_path), "dag", "--mermaid"]) == 0
    out = capsys.readouterr().out
    assert "ods --> dims" in out and "contracts --> obs" in out

    led = RunLedger(tmp_path / "data" / "warehouse" / "platform.duckdb")
    led.start_run("r1", "platform", "2026-09-22")
    led.finish_run("r1", SUCCESS)
    assert main(["--root", str(tmp_path), "runs", "--limit", "5"]) == 0
    assert "r1" in capsys.readouterr().out


def test_cli_space_mmc_forwards_platform_args(tmp_path):
    """`scripts/mmc.py platform ...` 只是转交，不重复实现一套子命令。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mmc_cli", REPO / "scripts" / "mmc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _A:
        platform_args = ["dag", "--mermaid"]

    assert mod.cmd_platform(_A()) == 0

    class _B:
        platform_args = []

    assert mod.cmd_platform(_B()) == 0  # 无参数时打印用法而不是崩


def test_platform_import_does_not_pull_operators():
    """导入边界（G2 容器首跑栽的坑）：platform 包不得连带导入 operators 包。

    operators/__init__ 会拉起 numpy/torch（clip/detector 算子），而服务容器
    的 requirements.lock 刻意不含它们——platform 链路只需要 robust 统计
    （已迁到包根）。此测试锁住这条边界，防止未来 import 又把它接回去。
    """
    import subprocess
    import sys

    src = str(Path(__file__).resolve().parents[1] / "src")
    code = "import sys, mm_curation.platform.jobs; print('mm_curation.operators' in sys.modules)"
    env = {**__import__("os").environ, "PYTHONPATH": src}
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("False"), (
        f"platform 导入拉起了 operators：{r.stdout.strip()}"
    )
