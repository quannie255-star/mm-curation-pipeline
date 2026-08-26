"""端到端 DAG：下载 → 污染 → 清洗 → 建索引 → 评测 → 采样对比 → 报告。

D5 收官：全部阶段从 SCAFFOLD 占位换成真实 make 命令，一键复现整条管道。
LocalExecutor 单机跑通，重计算（CLIP 批量推理）在宿主机 GPU venv 完成；
容器负责 DAG 编排与 CPU 可承受的规则算子（算力分级，见 docs/ROADMAP.md）。

各阶段产物落 data/ 对应子目录（git 忽略，DVC 管理）：
  raw/ → interim/ → processed/ → indexes/ + reports/
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

    # 1. 下载种子集：COCO-CN 标注 + 镜像原始 JPEG（幂等+并发+UA 镜像）
    data_download = BashOperator(
        task_id="data_download",
        bash_command="python /opt/airflow/scripts/download_dataset.py",
    )

    # 2. 注入 10 类可控脏数据 + ground truth 标注
    contaminate = BashOperator(
        task_id="contaminate",
        bash_command=(
            "python /opt/airflow/scripts/contaminate.py "
            "--config /opt/airflow/configs/contamination.default.yaml"
        ),
    )

    # 3. 清洗漏斗：11 级算子（L1 规则 + 去重四件套 + L2 CLIP）
    clean_funnel = BashOperator(
        task_id="clean_funnel",
        bash_command=(
            "python /opt/airflow/scripts/run_pipeline.py "
            "--config /opt/airflow/configs/pipeline.example.yaml"
        ),
    )

    # 4. 构建净索引（漏斗产出）+ 脏索引（污染全集，对比用）
    build_clean_index = BashOperator(
        task_id="build_clean_index",
        bash_command=(
            "python /opt/airflow/scripts/build_index.py --name clean_v2 "
            "--input /opt/airflow/data/processed/cn_flickr_curation_v2/cleaned.jsonl "
            "--out /opt/airflow/data/indexes"
        ),
    )
    build_dirty_index = BashOperator(
        task_id="build_dirty_index",
        bash_command=(
            "python /opt/airflow/scripts/build_index.py --name dirty_raw "
            "--input /opt/airflow/data/interim/contaminated/samples.jsonl "
            "--out /opt/airflow/data/indexes"
        ),
    )

    # 5. 检索对比评测：脏索引 vs 净索引（清洗收益的核心证据）
    eval_retrieval = BashOperator(
        task_id="eval_retrieval",
        bash_command=(
            "python /opt/airflow/scripts/eval_retrieval.py "
            "--indexes clean_v2 dirty_raw --out /opt/airflow/data/reports/retrieval_eval.json"
        ),
    )

    # 6. 算子级 P/R 独立评测 + 阈值敏感性曲线
    eval_operators = BashOperator(
        task_id="eval_operators",
        bash_command=(
            "python /opt/airflow/scripts/eval_operators.py "
            "--config /opt/airflow/configs/pipeline.example.yaml "
            "--out /opt/airflow/data/reports/operator_pr.json"
        ),
    )
    threshold_scan = BashOperator(
        task_id="threshold_scan",
        bash_command=(
            "python /opt/airflow/scripts/threshold_scan.py "
            "--out /opt/airflow/data/reports/threshold_scan.json"
        ),
    )

    # 7. 采样策略对比：随机 vs 分层（配比采样的下游收益）
    eval_sampling = BashOperator(
        task_id="eval_sampling",
        bash_command=(
            "python /opt/airflow/scripts/eval_sampling.py "
            "--budgets 1200 1000 800 "
            "--out /opt/airflow/data/reports/sampling_eval.json"
        ),
    )

    bootstrap >> data_download >> contaminate >> clean_funnel
    clean_funnel >> build_clean_index >> eval_retrieval
    contaminate >> build_dirty_index >> eval_retrieval
    clean_funnel >> eval_operators >> threshold_scan
    clean_funnel >> eval_sampling
