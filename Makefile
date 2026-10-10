# ── 解释器探测（2026-10-07 加）────────────────────────────────────
# 起因：`make repro` 自我描述是「一键复现证明链」，但本机裸 `python`
# 解析到 3.13 —— 没有 numpy、没有 mm_curation，一跑就 ModuleNotFoundError。
# **「一键复现」在交付机器上跑不起来，等于这个卖点在现场崩掉。**
#
# 更隐蔽的一层：项目里有个 `.venv/`，**有 python.exe 但依赖全缺**——
# 只按「路径上有没有 python」去选，会精准选中这个空壳，换个姿势照样崩。
#
# 所以判定标准是「**能不能 import 本项目依赖**」，不是「有没有 python」。
# 探测逻辑放在 scripts/find_python.py（实测：make 的多行 `$(shell ...)` 续行
# 在 mingw32-make 上直接 missing separator，写在 Makefile 里不可靠）。
#
# 想手动指定：make PYTHON=/path/to/python repro（命令行变量优先级最高）
#
# 用 `python`（PATH 上的任意一个）来**启动**探测脚本是刻意的：探测脚本
# 只用标准库，谁都能跑它；它再去挨个验谁有依赖。
#
# 刻意**不写 `2>/dev/null`**：Windows 的 make 走 msys `sh.exe`，那里
# `/dev/null` 会被解析成 `C:\msys64\dev\null`（不存在）→ 重定向失败 →
# shell 丢掉整条 stdout，还顺手打印一行乱码。实测踩过：`$(shell)` 拿到空串，
# 于是 `PYTHON` 为空，`-X utf8` 被当成可执行文件名，make 报「系统找不到
# 指定的路径」后**继续跑完并 rc=0**。
# 不重定向反而是对的：`$(shell)` 只收 stdout，脚本成功时本就安静，
# 失败时诊断信息直接透传到 make 的 stderr —— 看得见，才修得了。
PYTHON := $(strip $(shell python scripts/find_python.py))

# 探测不到就**在解析期响亮地红**，绝不兜底成空串。
# 实测踩过的坑：空串会让 recipe 变成 `-X utf8 scripts/xxx.py`——
# `-X` 被当可执行文件名，make 报「系统找不到指定的路径」然后
# **继续跑下一个目标并最终 rc=0**。也就是说「一键复现」静默地什么也没做。
# 静默失效比报错危险得多：缺门禁你知道它缺，坏门禁告诉你「已跑通」。
ifeq ($(strip $(PYTHON)),)
$(error 找不到装齐本项目依赖的 Python 解释器。诊断：python scripts/find_python.py  （会列出试过的候选与修复命令）｜ 或手动指定：make PYTHON=/path/to/python <目标>)
endif
export PYTHON

.PHONY: doctor
doctor: ## 体检：解释器对吗、依赖齐吗、门禁绿吗（面试前跑一次）
	@$(PYTHON) -X utf8 scripts/doctor.py

.PHONY: help venv install install-gpu test lint fmt data data-download data-contaminate funnel eval-op threshold-scan eval-sampling airflow-build airflow-up airflow-down airflow-logs

help: ## 显示本帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## 创建 .venv (Python 3.11)
	$(PYTHON) -m venv .venv

install: ## 安装依赖（CPU 足够的部分）
	pip install -r requirements.txt
	pip install -e packages/curation-eval  # 协议与算子 SDK（V2 单一来源）
	pip install -e .

install-gpu: ## 安装 CUDA 版 torch（Windows + cu121）
	pip install torch --index-url https://download.pytorch.org/whl/cu121
	$(PYTHON) -c "import torch; assert torch.cuda.is_available(), 'CUDA 不可用，请检查驱动'"

data: data-download data-contaminate ## 一键产出带标注脏数据集（COCO-CN 种子 + 注入 10 类脏数据）

data-download: ## 下载种子集：COCO-CN 标注 + 镜像原始 JPEG（--limit 可冒烟）
	$(PYTHON) scripts/download_dataset.py

data-contaminate: ## 注入 10 类可控脏数据（配置 configs/contamination.default.yaml）
	$(PYTHON) scripts/contaminate.py --config configs/contamination.default.yaml

funnel: ## 运行清洗漏斗（CONFIG=configs/pipeline.example.yaml）
	$(PYTHON) scripts/run_pipeline.py --config $(CONFIG)

eval-op: ## 算子级 P/R 独立评测（全量脏集，独立运行每个算子）
	$(PYTHON) scripts/eval_operators.py

eval-fhir: ## 医疗 FHIR 模态 P/R + 漏斗门禁（合成语料，确定性 seed）
	$(PYTHON) -X utf8 scripts/eval_fhir.py

eval-industrial: ## 工业传感器模态 P/R + 漏斗门禁（合成语料，确定性 seed）
	$(PYTHON) -X utf8 scripts/eval_industrial.py

verify-claims: ## 简历/文档数字版本锁定校验（漂移 exit 1，可进 CI）
	$(PYTHON) -X utf8 scripts/verify_claims.py

repro: ## 一键复现证明链轻量数字（双领域多 seed 门禁 + 随机删 baseline + claims 校验）
	$(PYTHON) -X utf8 scripts/eval_fhir.py --seeds 42,7,2026
	$(PYTHON) -X utf8 scripts/eval_industrial.py --seeds 42,7,2026
	$(PYTHON) -X utf8 scripts/eval_random_drop_baseline.py --n-seeds 10
	$(PYTHON) -X utf8 scripts/verify_claims.py

threshold-scan: ## 阈值敏感性扫描（含 matplotlib 图表）
	$(PYTHON) scripts/threshold_scan.py

eval-sampling: ## 采样策略对比（随机 vs 分层，固定预算下游检索指标）
	$(PYTHON) scripts/eval_sampling.py

eval-ablation: ## 消融实验（逐个 + 分组移除算子，测检索指标变化）
	$(PYTHON) scripts/eval_ablation.py

INDEXES := data/indexes

index-clean: ## 构建净索引（漏斗产出，~1.6k 条，GPU 编码）
	$(PYTHON) scripts/build_index.py --name clean_v2 --input data/processed/cn_flickr_curation_v2/cleaned.jsonl --out $(INDEXES)

index-dirty: ## 构建脏索引（污染全集，~2.1k 条，对比实验用）
	$(PYTHON) scripts/build_index.py --name dirty_raw --input data/interim/contaminated/samples.jsonl --out $(INDEXES)

serve: ## 启动检索服务 (http://localhost:8000/docs)
	uvicorn mm_curation.serving.api:app --app-dir src --host 0.0.0.0 --port 8000

train-detector: ## 训练水印/NSFW 检测器（合成数据，GPU，~3 分钟）
	$(PYTHON) scripts/train_detector.py

finetune-clip: ## CLIP 干净/脏集微调对比实验（GPU，~20 分钟）
	$(PYTHON) scripts/finetune_clip.py

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
	$(PYTHON) -X utf8 scripts/fetch_news_corpus.py --max-docs 2000
build-benchmark: ## 构建/更新冻结 benchmark（judge_news_v1）
	$(PYTHON) -X utf8 scripts/build_judge_benchmark.py
finetune-judge: ## LoRA 微调专属判官（8GB 本机 ~70 分钟）
	PYTORCH_CUDA_ALLOC_CONFexpandable_segments:True$(PYTHON) -X utf8 scripts/finetune_judge_lora.py --n-clean 500 --n-dirty 500 --epochs 3 --batch 4
eval-judge: ## 冻结 benchmark 上出钱表（--adapter 缺省=通用基线）
	$(PYTHON) -X utf8 scripts/run_judge_benchmark.py --adapter models/judge_lora_v1
studio:
	streamlit run scripts/judge_studio.py

platform: ## 启动个人微调平台控制台（Streamlit）
	streamlit run scripts/platform_app.py
judge-cost: ## 判官成本核算报告（本机 vs API vs 人工）
	$(PYTHON) -X utf8 scripts/judge_cost_report.py
