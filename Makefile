.PHONY: help venv install install-gpu test lint fmt data data-download data-contaminate funnel eval-op threshold-scan eval-sampling airflow-build airflow-up airflow-down airflow-logs

help: ## 显示本帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## 创建 .venv (Python 3.11)
	python -m venv .venv

install: ## 安装依赖（CPU 足够的部分）
	pip install -r requirements.txt
	pip install -e packages/curation-eval  # 协议与算子 SDK（V2 单一来源）
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

eval-sampling: ## 采样策略对比（随机 vs 分层，固定预算下游检索指标）
	python scripts/eval_sampling.py

eval-ablation: ## 消融实验（逐个 + 分组移除算子，测检索指标变化）
	python scripts/eval_ablation.py

INDEXES := data/indexes

index-clean: ## 构建净索引（漏斗产出，~1.6k 条，GPU 编码）
	python scripts/build_index.py --name clean_v2 --input data/processed/cn_flickr_curation_v2/cleaned.jsonl --out $(INDEXES)

index-dirty: ## 构建脏索引（污染全集，~2.1k 条，对比实验用）
	python scripts/build_index.py --name dirty_raw --input data/interim/contaminated/samples.jsonl --out $(INDEXES)

serve: ## 启动检索服务 (http://localhost:8000/docs)
	uvicorn mm_curation.serving.api:app --app-dir src --host 0.0.0.0 --port 8000

train-detector: ## 训练水印/NSFW 检测器（合成数据，GPU，~3 分钟）
	python scripts/train_detector.py

finetune-clip: ## CLIP 干净/脏集微调对比实验（GPU，~20 分钟）
	python scripts/finetune_clip.py

demo: ## 启动 Streamlit Demo (http://localhost:8501)
	streamlit run scripts/streamlit_app.py

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

# ---- V3 ζ：个人微调平台·专属数据判官（详见 docs/RUNBOOK.md 1.10）----
fetch-news: ## 爬取新闻域语料（robots 合规/限速/幂等）
	python -X utf8 scripts/fetch_news_corpus.py --max-docs 2000
build-benchmark: ## 构建/更新冻结 benchmark（judge_news_v1）
	python -X utf8 scripts/build_judge_benchmark.py
finetune-judge: ## LoRA 微调专属判官（8GB 本机 ~70 分钟）
	PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python -X utf8 scripts/finetune_judge_lora.py --n-clean 500 --n-dirty 500 --epochs 3 --batch 4
eval-judge: ## 冻结 benchmark 上出钱表（--adapter 缺省=通用基线）
	python -X utf8 scripts/run_judge_benchmark.py --adapter models/judge_lora_v1
