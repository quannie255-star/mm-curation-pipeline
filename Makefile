.PHONY: help venv install install-gpu test lint fmt data data-download data-contaminate funnel eval-op threshold-scan airflow-build airflow-up airflow-down airflow-logs

help: ## 显示本帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## 创建 .venv (Python 3.11)
	python -m venv .venv

install: ## 安装依赖（CPU 足够的部分）
	pip install -r requirements.txt
	pip install -e .

install-gpu: ## 安装 CUDA 版 torch（Windows + cu121）
	pip install torch --index-url https://download.pytorch.org/whl/cu121
	python -c "import torch; assert torch.cuda.is_available(), 'CUDA 不可用，请检查驱动'"

data: data-download data-contaminate ## 一键产出带标注脏数据集（COCO-CN 种子 + 注入 10 类脏数据）

data-download: ## 下载种子集：COCO-CN 标注 + 镜像原始 JPEG（--limit 可冒烟）
	python scripts/download_dataset.py

data-contaminate: ## 注入 10 类可控脏数据（配置 configs/contamination.default.yaml）
	python scripts/contaminate.py --config configs/contamination.default.yaml

funnel: ## 运行清洗漏斗（CONFIG=configs/pipeline.example.yaml）
	python scripts/run_pipeline.py --config $(CONFIG)

eval-op: ## 算子级 P/R 独立评测（全量脏集，独立运行每个算子）
	python scripts/eval_operators.py

threshold-scan: ## 阈值敏感性扫描（含 matplotlib 图表）
	python scripts/threshold_scan.py

INDEXES := data/indexes

index-clean: ## 构建净索引（漏斗产出，~1.6k 条，GPU 编码）
	python scripts/build_index.py --name clean_v2 --input data/processed/cn_flickr_curation_v2/cleaned.jsonl --out $(INDEXES)

index-dirty: ## 构建脏索引（污染全集，~2.1k 条，对比实验用）
	python scripts/build_index.py --name dirty_raw --input data/interim/contaminated/samples.jsonl --out $(INDEXES)

serve: ## 启动检索服务 (http://localhost:8000/docs)
	uvicorn mm_curation.serving.api:app --app-dir src --host 0.0.0.0 --port 8000

test: ## 运行单元测试
	pytest

lint: ## ruff 静态检查
	ruff check src tests scripts dags

fmt: ## ruff 自动格式化 + 排序 import
	ruff check --fix --unsafe-fixes src tests scripts dags || true
	ruff format src tests scripts dags

airflow-build: ## 构建 Airflow 定制镜像
	docker compose build

airflow-up: ## 启动 Airflow (http://localhost:8080, airflow/airflow)
	docker compose up -d

airflow-down: ## 停止 Airflow
	docker compose down

airflow-logs: ## 查看 scheduler 日志
	docker compose logs -f airflow-scheduler
