# 多模态图文数据清洗与向量检索 Pipeline

[![CI](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)

面向中文多模态大模型训练数据场景的端到端数据管道：**脏数据进 → 漏斗式多算子清洗 →
质量可量化 → 向量索引 → 检索服务 → 清洗收益可证明**。

> 状态：✅ 主线（Week 1-4）+ Phase 2（P1-P4）完成。路线图见 [docs/ROADMAP.md](docs/ROADMAP.md)。
> 面试叙事见 [docs/INTERVIEW.md](docs/INTERVIEW.md)。

## 核心结果（所有数字来自真实实验，可一键复现）

| 实验 | 指标 | 数值 |
|---|---|---|
| **清洗收益**（脏 vs 净索引对比） | Recall@1 | **0.459 → 0.556（+21%）** |
| | MRR | 0.599 → 0.670 |
| | Recall@10 | 0.874 → 0.901 |
| **配比收益**（分层 vs 随机采样，budget=1000） | Recall@1 | **0.353 → 0.437（+24%）** |
| **漏斗**（11 级算子，2106 → 1585） | 脏数据召回 / 误杀 | **100% / 2.16%** |
| **消融归因**（分组消融） | 去重组贡献 | **R@1 -0.017**（唯一显著组） |
| **算子级评测**（独立评测口径） | phash_near 主靶 recall / 误杀 | 84% / 0.24% |
| | clip_alignment 主靶 recall / 误杀 | 96% / 0.19% |
| **Phase2 · 自训检测器**（防循环论证） | testB 泛化 / 主靶召回 / 误杀 | **87.3% / 100% / 0.8%** |
| **Phase2 · CLIP 微调对比**（训练级证据） | clean_ft vs dirty_ft R@1 | **0.688 vs 0.636（差 5.2pp）** |
| **工程** | 单元测试 | 94 passing |

> 灵魂叙事：**脏数据 → 11 级漏斗 → 干净集（R@1 +21%）→ 分层采样（再 +18~24%）**
> → Phase 2 把"代理指标"升级为"训练证据"（脏集微调 CLIP 比 clean 低 5.2pp R@1）。
> 全链路收益可证、可复现、可归因。

## 架构总览

```
                    ┌────────────────────────────────────────────────┐
                    │                Airflow DAG 编排                 │
                    └────────────────────────────────────────────────┘
 raw 图文对 ──► 污染器(9类脏数据+标注) ──► 清洗漏斗(算子可配置) ──► 质量报告
                                              │                       ▲
                                              ▼                       │
                                   Chinese-CLIP 编码 ──► FAISS 索引   算子级 P/R 评测 +
                                              │                        阈值敏感性曲线
                                              ▼                        (data/reports/)
                                       FastAPI 检索服务 ◄──── 评测闭环(Recall@K/MRR,
                                              │              脏索引 vs 净索引对比)
                                              ▼
                                       Streamlit Demo
```

## 快速开始

```bash
# 1. 虚拟环境（Windows / Python 3.11）
python -m venv .venv
.venv\Scripts\activate          # Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt

# 2. 一键产出带标注的脏数据集（下载种子集 + 注入 10 类脏数据）
make data                        # COCO-CN 种子（~1.6k 对，图像来自 HF 镜像）
# 国内网络自动走 hf-mirror.com 镜像（见 docs/ROADMAP.md 数据源实测结论）

# 3. 启动 Airflow（编排层）
docker compose up -d            # http://localhost:8080 (airflow/airflow)

# 4. 运行测试
pytest

# 5. 算子级评测（可选，全量脏集上跑 ~30 秒，包含 GPU CLIP 编码）
make eval-op                          # data/reports/operator_pr.{json,md}
make threshold-scan                   # data/reports/threshold_scan.{json,md,png}

# 6. Phase 2（可选，需 GPU）
make train-detector                   # 自训水印/NSFW 检测器 → models/detector/
make finetune-clip                    # 干净/脏集 CLIP 微调对比 → data/reports/finetune_eval.{json,md}
```

## 目录结构

```
configs/          # 清洗漏斗 / 污染计划 YAML 配置（算子组合、阈值、比例）
dags/             # Airflow DAG
scripts/          # 数据下载 / 污染器 / 实验脚本
src/mm_curation/  # 核心包
  data/           # 数据获取：镜像适配、断点续传、COCO-CN join、格式统一
  contamination/  # 程序化污染器（10 类脏数据 + ground truth 标注）
  operators/      # 清洗算子（一算子一文件，注册表模式）
  pipeline/       # 漏斗执行器与配置解析
  quality/        # 质量指标与报告
  embedding/      # Chinese-CLIP 编码
  index/          # FAISS 索引
  serving/        # FastAPI 检索服务
  eval/           # 检索评测 + 算子评测
tests/            # pytest
data/             # raw / interim / processed / reports（git 忽略，DVC 管理）
```

## 设计文档

- [项目路线图](docs/ROADMAP.md) — 周计划 + 进度记录 + 阈值校准
- [岗位 JD 调研与能力映射](docs/JD_RESEARCH.md)
- [面试叙事（STAR + 预想追问）](docs/INTERVIEW.md)
- [工程笔记（环境踩坑）](docs/ENGINEERING_NOTES.md)

## License

[MIT](LICENSE)
