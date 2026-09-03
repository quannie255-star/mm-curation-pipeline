# 多模态图文数据清洗与向量检索 Pipeline

[![CI](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/ci.yml)
[![Data CI](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/data-ci.yml/badge.svg)](https://github.com/quannie255-star/mm-curation-pipeline/actions/workflows/data-ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)

面向中文多模态大模型训练数据场景的端到端数据管道：**脏数据进 → 漏斗式多算子清洗 →
质量可量化 → 向量索引 → 检索服务 → 清洗收益可证明**。

> 状态：✅ 主线（Week 1-4）+ Phase 2（P1-P10）+ **V2 全阶段完成（α 协议 / β 文本语料 / γ Ray 双运行时 / δ LLM-judge / ε 数据 CI）** + **V3 ζ 收官（域专属判官 κ +0.560 达标）**。路线图见 [docs/ROADMAP.md](docs/ROADMAP.md)。
> 面试叙事见 [docs/INTERVIEW.md](docs/INTERVIEW.md)。
>
> **V2 定位**：从「一条多模态清洗管道」升级为「模态可插拔的数据质量框架」。
> 协议与算子 SDK 收口进 `curation-eval` 包，图文管道与纯文本语料管道是它的两个
> 实例——共享同一注册表、同一执行器、同一评测协议，**零框架特例**。
> 设计见 [docs/ARCHITECTURE_V2.md](docs/ARCHITECTURE_V2.md)。

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
| **Phase2 · 实时质量门** | POST /api/ingest | 质量评分 + 三层增量判重 + accept 一次返回 |
| **Phase2 · 生产化切片** | 跨集去污染 / PSI 漂移监控 / 成本核算 | 召回 94.4% / 换源批 0.36-0.66 告警 / 四维成本表 |
| **V2 β · 文本去重基准**（10 万档） | exact 召回 / near 召回 / 耗时 | **1.0 / 0.9714 / 21.1s**（30 万档 60.4s，近线性） |
| **V2 β · GPT-2 zh 微调对比**（文本版训练证据） | clean_ft vs dirty_ft held-out ppl | **7.16 vs 7.70（脏语料 +7.5%，超 5% 验收线）** |
| **V2 β · 文本全量漏斗**（30.2 万篇中文维基） | 保留率 | **302,002 → 181,980（60.3%）** |
| **V3 ζ · 域专属判官**（judge_news_v1 冻结 benchmark） | 通用 κ → LoRA 微调 κ | **-0.024 → +0.560**（P=0.706 / R=0.960 / 解析率 100%，验收线 ≥0.5） |
| **工程** | 单元测试 | **157 + 40**（主仓库 + curation-eval 包） |

> 灵魂叙事：**脏数据 → 11 级漏斗 → 干净集（R@1 +21%）→ 分层采样（再 +18~24%）**
> → Phase 2 把"代理指标"升级为"训练证据"（脏集微调 CLIP 比 clean 低 5.2pp R@1）。
> 全链路收益可证、可复现、可归因。
>
> **V2 把同一个闭环推广到第二个模态**：同一套协议零特例接入 30.2 万篇中文维基语料，
> 文本侧同样拿到训练级证据（脏语料微调困惑度 +7.5%）——证明这不是一条管道，
> 而是一个框架。
>
> **V3 把框架长成平台**：δ 的阴性结果（通用 0.5B 判官 κ≈0）催生「个人微调平台」
> ——自己的数据 → 自己的 benchmark（judge_news_v1，300 条版本冻结 + 防污染）
> → 自己的模型（LoRA + Qwen2.5-0.5B，本机 8GB）。锚点任务达标：**通用 κ=-0.024
> → 微调 κ=+0.560**——「通用不行，微调自己的就行」有全链路证据。

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

同一套协议支撑两个模态实例（V2 β 起）：

| 实例 | 模态 | 数据规模 | 算子 | 去重 | 下游证据 |
|---|---|---|---|---|---|
| 图文管道 | `image_caption` | 2,106 对（COCO-CN 1,620 + 注入 486） | 12 个（L1 规则 / 去重四件套 / CLIP 对齐 / 自训检测器） | md5 + pHash + MinHash + 语义 kNN | CLIP 微调 R@1 0.688 vs 0.636 |
| 文本语料管道 | `text_article` | 302,002 篇（中文维基） | 8 个（长度 / 中文占比 / 复读 / 模板句 / PII / 困惑度） | 向量化 MinHash-LSH（80 perm / 8 band） | GPT-2 zh 困惑度 7.16 vs 7.70 |

两者共用 `curation-eval` 的 Sample 协议、算子注册表、Executor 与 P/R 评测——
文本模态接入时**没有新增任何框架特例**。

## 快速开始

```bash
# 1. 虚拟环境（Windows / Python 3.11）
python -m venv .venv
.venv\Scripts\activate          # Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt

# 2. 一键产出带标注的脏数据集（下载种子集 + 注入 10 类脏数据）
make data                        # COCO-CN 种子（~1.6k 对，图像来自 HF 镜像）
# 国内网络自动走 hf-mirror.com 镜像（见 docs/ROADMAP.md 数据源实测结论）
# Windows/Git Bash 无 make？全部等价 python 命令见 docs/RUNBOOK.md（含完整复现步骤与验收数字）

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

# 7. V2 β：文本语料实例（make-free 等价命令见 docs/RUNBOOK.md 第 1.5 节）
python -X utf8 scripts/download_text_corpus.py        # 30.2 万篇中文维基
python -X utf8 scripts/text_dedup_benchmark.py        # 去重吞吐/召回基准
python -X utf8 scripts/run_pipeline.py --config configs/text_funnel.yaml
python -X utf8 scripts/finetune_gpt2.py               # 干净/脏语料训练对比（需 GPU）

# 8. V3 ζ：个人微调平台·专属数据判官（四步闭环，详见 docs/PRD.md + RUNBOOK 1.10）
make fetch-news                       # ① 原始数据获取（爬虫，robots 合规/幂等）
make build-benchmark                  # ② 构建自己的 benchmark（300 条版本冻结+防污染）
make finetune-judge                   # ③ LoRA 微调自己的模型（8GB 本机 ~70 分钟）
make eval-judge                       # ④ 冻结 benchmark 出钱表：通用 κ-0.024 → 微调 κ+0.560
```

> Windows 注意：产出中文的脚本加 `-X utf8`。`.venv` 若因目录搬迁失效，
> 直接用系统 Python（详见 [RUNBOOK 第 0 节](docs/RUNBOOK.md)）。

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
  benchmarks/     # V3：benchmark 构建器（版本冻结 + 防污染 + 泄漏检查）
  tuning/         # V3：LoRA 判官微调（SFT 数据生成 + 训练对隔离）
benchmarks/       # V3 产物：冻结评测集（items.jsonl + manifest，入库资产）
runs/             # V3：实验 ledger（配置/loss/评测数字追加式）
tests/            # pytest
data/             # raw / interim / processed / reports（git 忽略，DVC 管理）
```

## 独立评测包：curation-eval（V2 起是协议与 SDK 的单一来源）

[`packages/curation-eval/`](packages/curation-eval/) — 数据清洗的
**ground-truth 评测框架**（pip 可装）。定位：Data-Juicer 等清洗系统提供算子，
本包回答「算子好不好」。

0.2.0 起它同时承载**协议层**：泛化 `Sample` schema（模态可插拔）、算子注册表
（带模态/成本档/依赖字段元数据）、Executor 抽象、污染器协议、P/R 与检索指标。
主仓库反向消费本包——**自己产品的第一個用户**，这是"可复用"最硬的证明。

```bash
pip install -e packages/curation-eval
python -m pytest packages/curation-eval/tests   # 40 项协议测试
```

协议约定、五分钟上手示例与变更记录见 [包内 README](packages/curation-eval/README.md)。

## 设计文档

- [系统架构 V2（模态可插拔框架：八决策 + 六阶段路线）](docs/ARCHITECTURE_V2.md)
- [系统架构（数据流 Mermaid + FMEA + 替换成本）](docs/ARCHITECTURE.md)
- [完整跑法 RUNBOOK（make-free，含 venv 踩坑）](docs/RUNBOOK.md)
- [服务实测性能与降级矩阵（SLA_README）](docs/SLA_README.md)
- [FAQ：真实踩坑与评测口径](docs/FAQ.md)
- [项目路线图](docs/ROADMAP.md) — 周计划 + Phase2 + V2 六阶段 + 进度记录 + 阈值校准
- [岗位 JD 调研与能力映射](docs/JD_RESEARCH.md)
- [面试叙事（STAR + 预想追问）](docs/INTERVIEW.md)
- [工程发现日志 59 条（面试弹药库）](docs/ENGINEERING_NOTES.md)

## License

[MIT](LICENSE)
