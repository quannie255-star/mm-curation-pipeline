# RUNBOOK：完整跑一遍（Windows / Git Bash，无需 make）

> 两个本机踩过的坑先说清楚：
> 1. **`.venv` 不可搬迁**：venv 创建时把当时的绝对路径烧进启动器，项目目录
>    改名（本项目曾从中文名目录迁来）后 `.venv` 里的 pytest.exe 等全部失效，
>    报 `Fatal error in launcher ... ??????`。本机可用环境一直是**系统 Python
>    （3.11）+ 用户目录 site-packages**——先 `deactivate`，直接用 `python`。
> 2. **Git Bash 没有 make**：所有命令给等价 python 直跑版（Linux/macOS 用
>    Makefile 目标等价替换即可）。

## 0. 环境（一次性）

```bash
deactivate 2>/dev/null          # 退出坏的 venv（如有）
python -V                       # Python 3.11.x
python -m pytest -q             # 健康检查：105 passed
```

依赖缺失时：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`
GPU 依赖：`pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121`

## 1. 完整复现（按管道顺序，每步有验收数字）

| 步骤 | 命令（make-free） | 耗时 | 验收 |
|---|---|---|---|
| 数据准备 | `python scripts/download_dataset.py` 然后 `python scripts/contaminate.py --config configs/contamination.default.yaml` | ~2 分钟 | contaminated = 2,106 条（1,620+486） |
| 清洗漏斗 | `python -X utf8 scripts/run_pipeline.py` | ~3 分钟 GPU | 2106→1585；召回 100%/误杀 2.16% |
| 双索引 | `python scripts/build_index.py --name clean_v2 --input data/processed/cn_flickr_curation_v2/cleaned.jsonl`<br>`python scripts/build_index.py --name dirty_raw --input data/interim/contaminated/samples.jsonl` | ~3 分钟 GPU | 两个 manifest 生成 |
| 灵魂实验 | `python -X utf8 scripts/eval_retrieval.py` | ~2 分钟 | R@1 0.459→0.556（+21%） |
| 算子 P/R | `python scripts/eval_operators.py` | ~4 分钟 | data/reports/operator_pr.md |
| 阈值扫描 | `python scripts/threshold_scan.py` | ~5 分钟 | 5 张 PNG 曲线 |
| 采样对比 | `python scripts/eval_sampling.py` | ~2 分钟 | budget=1000 R@1 +24% |
| 消融 | `python scripts/eval_ablation.py` | ~3 分钟 | 去重组 R@1 -0.017 |
| 检测器 | `python -X utf8 scripts/train_detector.py` | ~4 分钟 GPU | testA 98.2%/testB 87.3% |
| CLIP 微调 | `python -X utf8 scripts/finetune_clip.py` | ~20 分钟 GPU | clean_ft 0.688 vs dirty_ft 0.636 |
| 成本核算 | `python -X utf8 scripts/cost_model.py` | ~2 分钟 | cost_model.md 四维表 |

> Windows 注意：产出中文的脚本加 `-X utf8`；指标随污染 seed 有 ±1pp 正常浮动。
> 模型权重在 `models/`（gitignore）：新机器先跑任意 CLIP 命令触发下载，
> 再 `python scripts/convert_clip_weights.py`（详见 FAQ）。

## 2. 演示（10 分钟，面试/展示）

```bash
# 终端 1：检索服务
python -m uvicorn mm_curation.serving.api:app --app-dir src --host 127.0.0.1 --port 8000
# 终端 2：四 tab 界面
python -m streamlit run scripts/streamlit_app.py
```

必演示三件事（http://localhost:8000/docs Swagger 直接点）：
1. `/api/search` 同一查询"一只狗在草地上奔跑"切换 clean_v2 / dirty_raw——
   脏索引 top3 里 2 条脏数据、模糊图排第一
2. `/api/ingest` 同图发两次 → 第二次 `is_duplicate: true`；低质图看 `quality.flags`
3. `/metrics` → Prometheus 计数 + 延迟直方图 + 质量门漏斗

## 3. 清洁 venv（可选，彻底修复搬迁问题）

```bash
python -m venv .venv --clear
source .venv/Scripts/activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```
（约 3GB 下载；不重建则继续用系统 Python，本项目全部功能等价。）
