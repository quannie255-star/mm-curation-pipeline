"""自动生成，请勿手改（说明见 docs/PLATFORM.md）。

生成物是**适配器**，不是第二个事实源：阶段、依赖、重试、超时全部来自
`src/mm_curation/platform/jobs.py`。改这里会在下次导出时被覆盖。

⚠️ 单阶段语义：每个 BashOperator 调 `--only <阶段>`，是**独立进程**。
进程级隔离带来的正是"超时可强杀、重试可隔离"这两件事；
代价是 `job_runs` 那行由非 `--only` 的运行写入，Airflow 轨只留 `task_runs` 明细
（`jobs.run_platform` 的注释解释了为什么不在这条路上写 job_runs）。
"""

from __future__ import annotations

import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

TASKS = {
    "__start__": {
        "deps": [],
        "description": "起点",
        "retries": 0,
        "timeout_s": 0
    },
    "ads": {
        "deps": [
            "dws"
        ],
        "description": "应用宽表：数据集健康（体量/新鲜度/覆盖/未匹配）",
        "retries": 1,
        "timeout_s": 0.0
    },
    "contracts": {
        "deps": [
            "views"
        ],
        "description": "数据契约闸门：error 级断言失败 → 本阶段抛错、下游不跑",
        "retries": 0,
        "timeout_s": 120.0
    },
    "dims": {
        "deps": [
            "ods"
        ],
        "description": "维度 SCD-2：封旧版本 + 开新版本（代理键随版本变）",
        "retries": 1,
        "timeout_s": 0.0
    },
    "dwd": {
        "deps": [
            "dims"
        ],
        "description": "明细事实：窗级事实 + 窗×算子事实，按 valid_from/valid_to 关联维版本",
        "retries": 1,
        "timeout_s": 0.0
    },
    "dws": {
        "deps": [
            "dwd"
        ],
        "description": "聚合：dataset × event_date（+ × op），覆盖率与丢脏率分列",
        "retries": 1,
        "timeout_s": 0.0
    },
    "metrics": {
        "deps": [
            "obs"
        ],
        "description": "Prometheus 文本指标落盘（runs/obs/*.prom，可归档）",
        "retries": 0,
        "timeout_s": 0.0
    },
    "obs": {
        "deps": [
            "contracts"
        ],
        "description": "观测快照：新鲜度 / 行数稳健限 / 管道健康 / 维表未匹配 → 告警收敛",
        "retries": 0,
        "timeout_s": 120.0
    },
    "ods": {
        "deps": [
            "__start__"
        ],
        "description": "源 jsonl → ODS Parquet（Hive 分区：dataset × event_date）",
        "retries": 1,
        "timeout_s": 0.0
    },
    "views": {
        "deps": [
            "ads"
        ],
        "description": "重建湖上 Parquet → SQL 视图（ads 之后，保证视图不比数据旧）",
        "retries": 0,
        "timeout_s": 0.0
    }
}

with DAG(
    dag_id='mm_curation_platform',
    description="mm-curation 数据平台链路（ODS→DIM→DWD→DWS→ADS→契约/观测）",
    start_date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    schedule='@daily',
    catchup=True,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["platform", "warehouse"],
) as dag:
    _ops = {}
    for _name, _spec in TASKS.items():
        _ops[_name] = BashOperator(
            task_id=_name,
            bash_command=(
                "python -m mm_curation.cli platform run "
                "--only " + _name + " --run-id {{ run_id }} --batch-date {{ ds }}"
            ),
            retries=_spec["retries"],
            execution_timeout=datetime.timedelta(seconds=_spec["timeout_s"])
            if _spec["timeout_s"] else None,
            doc_md=_spec["description"],
        )
    for _name, _spec in TASKS.items():
        for _dep in _spec["deps"]:
            _ops[_dep] >> _ops[_name]
