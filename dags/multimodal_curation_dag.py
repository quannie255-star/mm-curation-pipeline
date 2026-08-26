"""端到端 DAG：下载/准备 → 污染 → 清洗漏斗 → 建索引 → 评测 → 质量报告。

各阶段随 ROADMAP 周计划逐步落地为真实命令；当前为骨架，
未实现的阶段以 SCAFFOLD 标记的占位任务呈现（成功退出、输出说明文字），
保证 Day1 起 DAG 结构可见、依赖关系可讲。
"""
from __future__ import annotations

import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="multimodal_curation",
    description="中文图文数据清洗与向量检索端到端管道",
    start_date=datetime.datetime(2026, 8, 20, tzinfo=datetime.timezone.utc),
    schedule=None,  # 手动/数据触发；接入数据源后可改 schedule
    catchup=False,
    tags=["multimodal", "curation"],
) as dag:
    bootstrap = BashOperator(
        task_id="bootstrap",
        bash_command="python -c \"import sys; print('python', sys.version); "
        "import mm_curation; print('mm_curation', mm_curation.__version__)\"",
    )

    data_prepare = BashOperator(
        task_id="data_prepare",
        bash_command=(
            'if [ -f /opt/airflow/scripts/download_dataset.py ]; then '
            "python /opt/airflow/scripts/download_dataset.py "
            "--out /opt/airflow/data/raw/samples.jsonl; "
            'else echo "[SCAFFOLD] 数据下载脚本将在 Week1 D3 实现"; fi'
        ),
    )

    contaminate = BashOperator(
        task_id="contaminate",
        bash_command=(
            'if [ -f /opt/airflow/scripts/contaminate.py ]; then '
            "python /opt/airflow/scripts/contaminate.py "
            "--in /opt/airflow/data/raw/samples.jsonl "
            "--out /opt/airflow/data/interim/contaminated.jsonl; "
            'else echo "[SCAFFOLD] 污染器将在 Week1 D4 实现"; fi'
        ),
    )

    clean = BashOperator(
        task_id="clean_funnel",
        bash_command=(
            'if [ -f /opt/airflow/src/mm_curation/pipeline/runner.py ]; then '
            "python -m mm_curation.pipeline.runner "
            "--config /opt/airflow/configs/pipeline.example.yaml; "
            'else echo "[SCAFFOLD] 漏斗执行器将在 Week2 D5 实现"; fi'
        ),
    )

    build_index = BashOperator(
        task_id="build_index",
        bash_command='echo "[SCAFFOLD] FAISS 索引构建将在 Week3 D1 实现"',
    )

    evaluate = BashOperator(
        task_id="evaluate",
        bash_command='echo "[SCAFFOLD] 检索评测与清洗收益对比将在 Week3 D3-4 实现"',
    )

    quality_report = BashOperator(
        task_id="quality_report",
        bash_command='echo "[SCAFFOLD] 质量报告生成将在 Week2 D5 实现"',
    )

    bootstrap >> data_prepare >> contaminate >> clean >> build_index >> evaluate
    clean >> quality_report
