# 多模态图文数据清洗与向量检索 Pipeline

[![CI](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/ci.yml)

面向中文多模态大模型训练数据场景的端到端数据管道：**脏数据进 → 漏斗式多算子清洗 →
质量可量化 → 向量索引 → 检索服务 → 清洗收益可证明**。

> 状态：✅ 主线完成（Week 1-4）。路线图见 [docs/ROADMAP.md](docs/ROADMAP.md)。

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

- [项目路线图](docs/ROADMAP.md)
- [岗位 JD 调研与能力映射](docs/JD_RESEARCH.md)
