# 数据系统赛道：数仓 / 数据开发 / 数据平台 / 数据工程 的后续开发清单

> 2026-09-22。**回答一个问题：要让这个项目也能投数仓、数据开发、数据平台、数据工程，
> 「数据系统」这一核心还缺什么、按什么顺序补。**
>
> 与另两篇的关系：`docs/JD_RESEARCH.md` 打「大模型数据链路」，
> `docs/DS_DA_TRACK.md` 打「DS/DA + 数据治理产物」。本篇打**这四个岗位的共同底座——
> 数据系统本身**。三篇共用同一套仓库实点，不重复。
>
> **口径声明**：第一节的岗位要求在文末附来源（2026-09-27 实抓）；
> 第二节之后每一条「现状」都附**可复核的判据**（文件 / 命令），不引用文档自述。
>
> **设计门**：本篇只出计划，**未动任何代码**。第四节的批次需逐个进
> `docs/design_tables.md` 并经确认后才实施（`AGENTS.md` / `AI_CODING_PROTOCOL.md`）。

---

## 零、先说清这一篇的判断（一句话）

**现在的「数仓层」不是数据系统，是一个指标计算脚本。**

差别不在功能多少，在一条定义性特征：**数据系统有「运行」这个概念，脚本没有。**

一个数据系统要能回答：这是第几次运行？跑到哪了？上次为什么失败？补数会不会重复计数？
这一版数据被谁消费了？能不能回滚到上一批？

本项目的 `mmc build` 一次都答不上来——它**每次全量重算，只保留「当前这一份」**。
判据（实点，见第二节）：`src/mm_curation/warehouse/` 目录下
`batch_date` / `watermark` / `run_id` / `checkpoint` / `resume` / `partition` / `parquet`
命中 **0**；`pipeline/runner.py` 里 `checkpoint` / `resume` / `quarantine` / `yield` 命中 **0**。

**这个判断与本项目已发生过的两次是同一种：缺的不是模块，是一条维度的通道。**
- W1 的发现：清洗缺**改写通道**，归一化塞成算子会让前级在脏文本上打分；
- B1 的发现：合成需要 **1→N** 通道，而现有通道只能 1→1；
- 本篇的发现：数据系统缺**「批次 / 时间」这条贯穿全链路的维度**，现有一切都是无状态快照。

---

## 一、调研：这四类岗位在「数据系统」上要什么

把 2026-09-27 抓到的 10 份岗位（社招 6 + 实习 4）拆成**岗位实际要求的能力面**，
可以归成五面。四类岗位的差别主要在偏重，**底座是同一台机器**。

### 1.1 五面（按出现频次排序）

| 面 | JD 原话（摘） | 出现 |
|---|---|---|
| **A 模型面**<br>怎么组织数据 | 「ODS/DWD/DWS/ADS 分层架构设计、主题域划分」「维度建模（Kimball）/ 范式建模（Inmon）」「星型/雪花」「**事实表、维度表、SCD 缓慢变化维**」「一致性维度 / 事实对齐」「总线矩阵」「指标口径统一、指标管理体系、数据字典」 | 8/10 |
| **B 作业面**<br>怎么把数据算出来 | 「ETL/ELT 全流程开发」「**增量同步 / 全量同步 / CDC 变更数据捕获原理**」「分区裁剪、执行计划分析、**数据倾斜**处理、资源浪费」「UDF/UDAF」「Hive / Spark / Flink / MaxCompute」「**湖仓一体 Iceberg / Hudi / Paimon / Delta**」 | 8/10 |
| **C 调度面**<br>什么时候算、失败怎么办 | 「调度工具 Airflow / DolphinScheduler / Azkaban / DataWorks」「**任务依赖编排与异常恢复机制**」「调度周期配置」「**任务运行记录**、问题复盘」「补数 / backfill」「SLA 达标」 | 7/10 |
| **D 服务面**<br>算完谁怎么用 | 「数据集、数据视图、**数据服务接口**，支撑 BI 报表取数」「版本化 API + 契约 + SLA」「**RBAC / 权限分级 / 敏感数据脱敏**」「metadata-driven 管道」「平台服务与共享库」 | 6/10 |
| **E 运维面**<br>怎么知道它坏了 | 「数据质量监控体系 DQC」「**管道健康 / SLA 达成 / 文件到达 / 成本趋势**仪表盘」「告警阈值与**噪音收敛**」「on-call / incident / 根因分析 / postmortem」「Runbook」 | 6/10 |

**横切面**：元数据 / 血缘（表级 + **字段级**）、数据资产目录、分类分级。
这半面**本项目已经做了**（见 2.1），是四篇文档里唯一不用补的一面。

### 1.2 实习档的口径（对用户当前阶段更重要）

四份实习 JD（广州大数据开发实习、成都神州数码、众安保险、金融保险数仓实习）实际要求：

- **不看规模**：「了解 Hadoop/Hive/Spark/Kafka **之一**」「有课程项目或实习经历优先」；
- **看基础**：「熟练 SQL，能独立完成**多表关联、聚合、窗口函数**」「**了解**数仓分层思想、维度建模」；
- **看流程**：「**负责 ETL 任务调度配置、运行监控、异常排查及数据质量校验**」「协助整理数据开发文档、
  字段说明、**任务运行记录**和问题复盘」；
- **加分项**：「有 SQL 性能优化、数据质量治理、**任务调度或数据血缘**相关实践经验」
  「有个人技术项目、**GitHub 项目**」。

> **结论**：实习档的门槛落在 **C 调度面 + E 运维面**（"任务运行记录""异常排查""调度配置"），
> 而这两面恰好是本项目**最空白**的地方——A/B 两面反而已经超出实习要求。
> 这修正了 `DS_DA_TRACK.md` 的判断（那篇说入口缺口是 SQL），**对实习岗而言真正的入口缺口是"没有运行记录"**。

### 1.3 一条必须诚实对待的落差

社招 JD 里写的是 **TB/PB 级、Hive/Spark/Flink、Iceberg、K8s**。
本项目**不假装做过**（`INDUSTRY_BENCHMARK.md` / `JD_RESEARCH.md` 已确立这条纪律）。
补齐的正确姿势是：

> **把「同一件事在小规模上的正确做法」做对做透，并明确标注回本条件**——
> 而不是把 DuckDB 换名字叫数仓、把 Ray 说成 Spark。

这条纪律在第四节每一批都单列「诚实边界」。

---

## 二、现状实点（逐条可复核）

### 2.1 已经能直接对上 JD 的（**不要重建**）

| JD 要求 | 已有产物 | 判据 |
|---|---|---|
| 数据质量监控体系（**DQC**） | 29 算子四模态、每算子 P/R、污染器 ground truth、数据 CI 门禁 + **劣化注入自证会红** | `data/reports/operator_pr*.json`、`.github/workflows/data-ci.yml` |
| 数据质量规则**四维**（完整性/准确性/一致性/时效性/唯一性） | 质量记分卡 + SLO + **覆盖率一等公民** | `configs/quality_slo.yaml`、`src/mm_curation/quality/scorecard.py` |
| 元数据 / **血缘** / 影响分析 | 记录级判决书 PROV-O 三元组 + 血缘图（对齐 OpenLineage 语义子集）+ 4 份数据契约 | `src/mm_curation/lineage/{graph,contract}.py`、`configs/contracts/` |
| **指标口径统一 / 数据字典** | 指标字典（每条含定义 + 分母 + SQL）+ 44 条冻结基线 + 漂移即 exit 1 | `configs/metrics.yaml`、`configs/metrics_baseline.json` |
| 调度工具 | Airflow DAG + `docker/Dockerfile.airflow` + compose（LocalExecutor） | `dags/multimodal_curation_dag.py`、`docker-compose.yaml` |
| 分布式（诚实版） | local / Ray 双运行时，10 万~100 万档三口径逐 id 零差异，**如实报告单机无回本点** | `data/reports/scale_crossover.json` |
| 数据库 / SQL | DuckDB 四层 + 别名视图 + `mmc sql` / `mmc metrics` | `scripts/mmc.py`、`src/mm_curation/warehouse/model.py` |
| 数据脱敏 / PII / PHI | `pii_detect` / `phi_residual` / `wm_nsfw_cnn` | operators |
| 技术方案 / 文档 / 规范 | 文档厚度超规格 | `docs/` |
| 运维飞轮（部分） | 采集→巡检→清洗→日报，单源失败不阻塞 | `data/reports/daily/` |

> **这是本篇最重要的一个前提**：这不是绿地项目。10 行里有 10 行能对上 JD。
> 所以本节的其余内容**不是"再加功能"，是"把已有能力装进系统的形状里"**。

### 2.2 五面的现状与缺口

#### A 模型面

| 项 | 现状 | 判据 |
|---|---|---|
| 分层 | 🔴 **名义分层**：`ods_samples` 只是 `raw_samples` 的视图别名，`dwd_samples` 只是 `stg_samples` 的别名——**同一张物理表两个名字** | `model.py:141` `_ALIAS_DDL` |
| 维度建模 / 事实表 / 维度表 | ❌ 无 | `src/` 下 `dim_` / `fact_` / `surrogate` / `scd` / `valid_from` 命中 0 |
| SCD | ❌ 无 | 同上 |
| 主题域 / 总线矩阵 | ❌ 无 | — |
| 指标口径 / 数据字典 | ✅ 强 | `configs/metrics.yaml` |

> 代码 docstring 自己写了「**名字多不是模型多**」（`model.py:13`）——这句话是诚实的，
> 但也正说明分层是名义上的。**分层这个词的实质是「每层有独立的物化、独立的更新节奏、
> 独立的保留策略」**，而这三点现在一条都没有。

#### B 作业面

| 项 | 现状 | 判据 |
|---|---|---|
| 加工方式 | 🔴 **每次全量重算**（`CREATE OR REPLACE TABLE` + 重读全部 jsonl） | `model.py:_DDL` 里 6 处 `CREATE OR REPLACE TABLE` |
| 增量 / 水位线 | ❌ 无 | `warehouse/` 下 `watermark` / `batch_date` 命中 0 |
| CDC / merge / upsert | ❌ 无 | `merge` / `upsert` 命中 0 |
| 存储格式 | 🔴 源是 jsonl、目标是**单个 duckdb 文件**；无列存落盘、无分区 | `data/warehouse/curation.duckdb` |
| 分区裁剪 / 执行计划 | ❌ 无 | `EXPLAIN` 命中 0 |
| 性能证据 | 🟡 有 Ray 规模对照，**无数仓侧性能对照** | — |
| 流式 / 断点续跑 / 失败隔离 | ❌ 全内存批处理 | `runner.py` 只有 `def run_funnel`，`checkpoint`/`resume`/`quarantine`/`yield` 命中 0 |

#### C 调度面

| 项 | 现状 | 判据 |
|---|---|---|
| DAG | 🟡 存在，但 `schedule=None`（手动触发），硬编码容器路径 `/opt/airflow/scripts/*.py`，**不调 `mmc build`**——即**编排的是旧脚本，不是现在的数仓链路** | `dags/multimodal_curation_dag.py:22` |
| 任务运行记录 | ❌ 无 run / task 元数据表 | — |
| 重试 / 超时 / SLA 回调 | ❌ 无 | DAG 里无 `retries` / `sla` / `on_failure_callback` |
| 补数 / backfill | ❌ 无 | — |
| 幂等性证明 | ❌ 无 | 无「同批次重跑产物逐行一致」的对账 |

#### D 服务面

| 项 | 现状 | 判据 |
|---|---|---|
| 数据服务 | 🟡 FastAPI 只服务**检索**，**不服务数仓 marts** | `serving/api.py` |
| 鉴权 / 配额 / 限流 | 🔴 完全没有 | `auth`/`token`/`rate`/`limit`/`Depends` 命中 0（GAP_AUDIT P1-1，09-22 复核实点为"仍开"） |
| 版本化 API / 契约挂服务 | ❌ 无 | 契约只在 CLI 层校验 |
| 行/列级权限 | ❌ 无（有 PII 算子，但**不是查询期权限**） | — |
| 环境隔离 dev/prod | ❌ 无 | 单文件 duckdb |
| 交付镜像 / lock / 版本发布 | 🔴 均无 | 无应用侧 Dockerfile；24 条依赖 0 条精确锁；`pyproject.toml` `version = "0.1.0"`（项目已到 V6）；无 tag / CHANGELOG |

#### E 运维面

| 项 | 现状 | 判据 |
|---|---|---|
| 质量侧监控 | ✅ 强（记分卡 + SLO + 数据 CI） | `configs/quality_slo.yaml` |
| 管道侧监控（成功率/时延/失败 task） | ❌ 无 | 无 runs 台账 |
| **数据新鲜度（freshness）** | ❌ 无 | 无 `now - max(batch_date)` 这类指标 |
| 行数/延迟异常告警 | 🟡 有 PSI 漂移（模型侧），**无管道侧** | `monitoring/drift.py` |
| 告警收敛 | ❌ 无 | — |
| 故障 Runbook | 🟡 RUNBOOK 很厚，但**全是工具与数据踩坑**，无"数据系统故障篇" | `docs/RUNBOOK.md` |

---

## 三、判断：三个结构性缺口

### 缺口 1 · 缺「批次」这条维度（最致命，且是地基）

现在整条链路是**无状态函数**：`jsonl → 产物`。数据系统的定义是**有状态的机器**：
有批次、有水位线、有依赖、有失败面、有产物版本。

**判据**：`batch_date|watermark|run_id|checkpoint|resume|partition|parquet|quarantine` 在
`src/mm_curation/warehouse/` 命中 **0**。

**后果**（这不是"少个功能"，是整类能力不存在）：
- 无法回答「昨天那批跑到哪了」→ 补数只能从头全量；
- 无法证明幂等 → 重跑一次数据变没变，答不上来；
- 无法做增量 → 数据量线性增长时，每次运行成本也线性增长；
- 无法留运行记录 → **实习 JD 明确要的「任务运行记录」直接是空的**。

### 缺口 2 · 「分层」是名义的，没有物化

`ods_samples` / `dwd_samples` 是**同一张物理表的两个视图名**。
分层要成立，必须每层**独立落盘 + 独立更新节奏 + 独立保留策略**。
现在三者皆无。

### 缺口 3 · 缺「平台」的那一半——别人能用

GAP_AUDIT 的 P1-1~P1-9 里，09-22 复核实点是 **1 条半修、8 条原封不动**。
数据平台岗考的就是"你有没有做过**别人能用**的东西"，而现在全部是"作者自己能跑的东西"。

**最强的证据是 N-1：CI 从建起来那天就是红的，pytest 步骤从未执行过。**
连作者本人都没让 CI 跑通——这条比"缺鉴权"更能说明"平台"这半边的状态。

---

## 四、开发清单（按**依赖**排序，不按兴趣）

原则沿用 V6：**先让证据可信 → 再让决策可审计 → 再让架构完整 → 最后让外部能用。**

### S0 · 先修真门禁（**前置，不修则后面全白做**）

| 项 | 内容 |
|---|---|
| 做什么 | ① **N-1**：协作方在途改动落库后全量 `ruff format src tests scripts dags packages`（56 文件，其中 43 个历史遗留），并把 CI 的 **pytest 步骤排到格式检查之前**——最低价值的检查不该否决最高价值的检查<br>② **P3-1**：把测试基线等门面数字纳入 `claims.json` + `verify_claims.py`，让数字腐烂在 CI 阶段被拦 |
| 对上哪句 JD | 「CI/CD」「自动化测试」「production readiness practices」 |
| 验收 | `git archive HEAD` 导出干净副本跑 CI 三条门禁全绿；**能证明 pytest 在 CI 里真的被执行过**（不是靠本地跑过） |
| 工时 | 小（1 天，但受协作顺序约束） |
| 诚实边界 | 本机无法验容器 → 以 CI 为唯一权威；**修好之前，任何"有 CI"的表述都不能写进简历** |

> 为什么排第一：数据平台岗的第一道面试题就是"你们怎么保证质量"，而这里**卖的门禁是假的**。
> 先有真门禁，再谈别的。

### S1 · 引入「批次」维度：把一次性重算改成可重复的作业（**地基**）

| 项 | 内容 |
|---|---|
| 做什么 | ① 加三张运行元数据表：`job_runs`（run_id / 逻辑日期 batch_date / 参数快照 / **git sha** / 起止 / 状态）、`task_runs`（每个阶段的状态与耗时）、`watermarks`（每个源的水位线）<br>② **让 `run_id` + `batch_date` 成为贯穿列**：所有产物表都带这两列（这就是那条缺失的维度）<br>③ CLI：`mmc build --batch-date` / `--run-id` / `--resume`；`mmc runs list` / `mmc runs show <id>`<br>④ 幂等可验证：同 batch_date 跑两次 → **产物逐行 hash 相同**（对账输出，不是声明）<br>⑤ 失败隔离：坏样本进 `quarantine.jsonl`（`dropped_by = "quarantine:json_decode"`），不阻断整批 |
| 对上哪句 JD | 「**任务依赖编排与异常恢复机制**」「**任务运行记录**」「调度周期」「数据作业调度体系的设计、开发与运维」「问题复盘」 |
| 验收 | ① 同批次重跑，产物 hash 逐行一致；② 第 3 阶段强制中断 → `--resume` 从 checkpoint 续跑，**已完成阶段不重算**（用耗时与扫描量双证）；③ 一条坏样本不阻断整批，且进 quarantine 可查 |
| 工时 | 中（协议改动 + 3 表 + CLI + 测试；**不改 `Sample` / `Operator` 签名**，只加包装层） |
| 简历句 | 「把全量重算的脚本改造成有批次概念的作业：引入 run/task/水位线元数据，支持**幂等重跑与断点续跑**，同批次重跑产物逐行 hash 一致，坏样本进隔离区不阻断整批。」 |
| 诚实边界 | checkpoint 粒度按阶段（不是逐行）；对 10 万档收益有限，**收益随数据量增长**——如实说明 |

> 这一批单独拎出来，因为**它决定了后面所有批次能不能做**：
> 没有批次就做不了增量（S2 无从判断"新增什么"）、做不了补数（S3 没有补数对象）、
> 做不了新鲜度（S5 没有时间轴）。

### S2 · 真分层 + 增量 + 列存分区（A 面 + B 面）

| 项 | 内容 |
|---|---|
| 做什么 | ① **每层独立物化**：ODS = append-only 全量历史；DWD = 明细 + 决策；DWS = 聚合（按分区可重算）；ADS = 面向应用的宽表。**废掉别名视图的"骗术"**（保留别名兼容，但底层真分层）<br>② **最小维度建模**：至少一对 `dim_dataset` / `dim_source`（带代理键 + **SCD-2 字段** `valid_from`/`valid_to`/`is_current`）+ `fact_curation_decision`（每样本每算子的裁决事实）。**口径诚实**：主题域用「数据集 / 源 / 算子 / 批次」，**不假装有 GMV、留存**<br>③ **列存 + Hive 风格分区落盘**：`marts/…/dataset=X/batch_date=Y/part.parquet`，DuckDB 退化为查询引擎（只读 parquet）<br>④ **增量**：源侧按 batch_date 分区 + 水位线；DWD 用 `MERGE`/upsert；DWS 按分区重算<br>⑤ **性能证据**：同份数据「jsonl 直扫 vs parquet 分区扫描」耗时与扫描字节对照 + `EXPLAIN ANALYZE` 输出入库 |
| 对上哪句 JD | 「ODS/DWD/DWS/ADS 分层架构设计」「维度建模 / **SCD 缓慢变化维** / 事实表维度表」「**分区裁剪**、执行计划分析」「增量同步原理」「ETL/ELT 全流程」 |
| 验收 | ① 新增一天数据 → **只有该分区被重扫**（用"扫描字节数"证明，不是声明）；② SCD-2 演示：某维属性变更 → 旧行 `valid_to` 封口、新行 `is_current=1`，历史可回溯；③ 分区扫描 vs 全量 jsonl 直扫出可比耗时 |
| 工时 | 中大（这是最重的一批） |
| 简历句 | 「按 ODS/DWD/DWS/ADS 落地**物理分层**（Parquet + Hive 分区），用维度建模设计事实表与含 **SCD-2** 的维度表；DWD 走增量 upsert、DWS 按分区重算，并通过分区裁剪把日增量作业的扫描量从 X 降到 Y（实测）。」 |
| 诚实边界 | **不声称 TB/PB**；只说单机实测 + 复杂度分析 + 回本条件。Iceberg/Hudi **不接**（见第五节），只做「开放格式 + 分区」这一层可验证的事 |

### S3 · 调度与依赖：让系统自己跑，并留下记录（C 面）

| 项 | 内容 |
|---|---|
| 做什么 | ① **重写 DAG**：task 调 `mmc`（不再硬编码旧脚本），形成真实依赖图 `raw → stg → marts → metrics → scorecard → contracts`<br>② 每个 task 传同一 `run_id` / `batch_date`（与 S1 打通）<br>③ `retries` + `execution_timeout` + `sla` + `on_failure_callback`（回调落本地台账即可，**不声称发了钉钉**）<br>④ **补数演示**：`airflow dags backfill -s <start> -e <end>` 补一个失败区间，并证明**不重复计数**<br>⑤ `schedule` 从 `None` 改为真实调度（或保留手动 + 演示 catchup 语义） |
| 对上哪句 JD | 「调度工具 Airflow / DolphinScheduler」「**任务依赖编排与异常恢复**」「**补数**」「SLA 达标」「调度周期配置」 |
| 验收 | **人为让第 4 步失败** → 下游不跑、上游按 `retries` 重试、失败入台账、backfill 后补齐且**行数不重复** |
| 工时 | 中 |
| 简历句 | 「用 Airflow 编排整条数仓链路（7 任务真实依赖），配重试/超时/SLA 回调与运行台账；做**补数**演练：某日任务人为失败后 backfill 补齐且不重复计数。」 |
| 诚实边界 | **本机 Docker 拉不到 `registry-1.docker.io`（已记录）** → 容器化调度只能靠 CI 或本地 LocalExecutor 验；这一点必须写在文档里，不能含糊 |

### S4 · 数据服务化：把 marts 变成别人能调的东西（D 面）

| 项 | 内容 |
|---|---|
| 做什么 | ① `mmc serve` → FastAPI 暴露 **ADS 层只读接口**（分页 + `batch_date` 参数化 + 版本化 `/v1/`）<br>② **契约前置**：启动时校验 `configs/contracts/*.yaml`，不过就**拒绝启动**（把"破坏性变更 CI 阻断"升级为"服务侧也阻断"）<br>③ **最小 RBAC**：token → role → 表/列可见性；**列级**用已有的 `pii_detect` 标注结果屏蔽 PII/PHI 列<br>④ **SLA 指标**：`/metrics` 暴露接口 P95 延迟 + **数据新鲜度**（`now - max(batch_date)`） |
| 对上哪句 JD | 「数据集、数据视图、**数据服务接口**」「版本化 API + 契约 + SLA」「**RBAC / 权限分级 / 敏感数据脱敏**」 |
| 验收 | ① 无 token → 401；② 低权 token 查询 PHI 列 → 403 或脱敏值；③ 人为把契约改坏 → 服务拒启；④ `/metrics` 能读出新鲜度 |
| 工时 | 中 |
| 简历句 | 「把数仓 ADS 层封装成**版本化数据服务**（契约校验 + 行/列级权限），并暴露数据新鲜度与接口 P95 延迟指标。」 |
| 诚实边界 | 是**单 token + 角色**级别的权限，不是企业级用户体系 / SSO |

> 这一批有个额外收益：它把 GAP_AUDIT **P1-1（零鉴权）从"缺口"变成"证据"**——
> 面试时能说"我做过权限分级，并且能证明它对 PHI 列生效"。

### S5 · 可观测性与运维闭环（E 面）

| 项 | 内容 |
|---|---|
| 做什么 | ① 运行台账 → **管道健康看板**（成功率 / 各阶段耗时趋势 / 失败 task Top N）<br>② **新鲜度 + 行数异常告警**：复用 `monitoring/drift.py` 的 PSI，对「每日行数」「批次到达时间」做 3σ / PSI 判据<br>③ **告警收敛**：同因合并 + 去重窗口 + 分级（这条本身就是 JD 原话："reduce alert noise through actionable thresholds"）<br>④ **RUNBOOK 增「数据系统故障篇」**：故障现象 → 判据 → 处置（对齐 JD 的 Runbook / incident / postmortem） |
| 对上哪句 JD | 「管道健康 / SLA 达成 / **文件到达**仪表盘」「告警阈值与**噪音收敛**」「on-call / incident / 根因分析」「Runbook」 |
| 验收 | 人为注入「上游延迟」与「行数骤降」→ 看板变红、**产生一条收敛后的告警**（不是 30 条）、runbook 有对应条目 |
| 工时 | 小中（复用已有漂移与前端预计算 JSON） |
| 简历句 | 「建数据系统运维面：管道健康与数据新鲜度看板、行数/到达时间异常告警（含**告警收敛**），并沉淀故障 Runbook。」 |

### S6 · 环境与交付：从"我本机能跑"到"能交付"（D 面收尾）

| 项 | 内容 |
|---|---|
| 做什么 | ① 应用侧多阶段 Dockerfile（含 `mmc` 传入口）；compose 把 app / warehouse / 服务加进去<br>② **dev / prod 双环境**（schema 前缀或目录隔离）+ 参数化晋升（DEV→PROD）<br>③ **lock 文件**（`requirements.lock.txt` 或迁 uv）——GAP_AUDIT P1-3 一次根治<br>④ 版本对齐 + git tag + CHANGELOG；`pyproject.toml` 加 `[project.scripts]` 让 `mmc` 成为真命令（P1-8 收尾）<br>⑤ CI 加「容器 build 成功 + `/healthz` 200」冒烟（P1-4 要求）<br>⑥ P1-9：补一条"**真实数据 + 纯 CPU + 十分钟**"的外人可验路径（含 S1 的批次概念） |
| 对上哪句 JD | 「DEV-to-UAT-to-PROD promotion with parameterization, approval gates, artifact versioning」「reproducible environments」「可复现构建」「对外可验证」 |
| 验收 | 干净环境 `docker build` + 起容器 + `/healthz` 200 + `mmc build` 跑通（CI 内验证）；外人照 QUICKSTART 路线 D 十分钟内看到真实数据结果 |
| 工时 | 中 |
| 简历句 | 「配 dev/prod 双环境与参数化晋升，用 lock 文件锁依赖、Dockerfile 交付镜像，CI 内置容器冒烟；提供纯 CPU 十分钟可验的真实数据路径。」 |
| 诚实边界 | 本机拉不到镜像 → 全靠 CI 验，如实标注 |

**S6 完成状态（2026-09-27 实点，逐条对照上表）**

| 项 | 状态 | 实点 / 证据 |
|---|---|---|
| ① 应用侧多阶段 Dockerfile + compose 加 app 服务 | ✅ 已写 | `docker/Dockerfile.app`（多阶段、非 root、CPU-only、含 `mmc` 入口）；`docker-compose.yaml` 的 `app`（8081→8080，不与 Airflow 抢端口）；`.dockerignore` 挡掉 383MB 真数据 / `.git` / `.venv` |
| ② dev/prod 双环境 + 参数化晋升 | ✅ 已做并实测 | `src/mm_curation/platform/envs.py`：repo/store 两个根；`--env` 挂所有子命令、拼错即抛错；`promote` 三道闸门 + 陈旧分区**前置**拒绝。真实数据实点：935 文件两侧指纹逐位一致，二次晋升 `copied=0/unchanged=935/pruned=0`（3.60s，首次 4.04s） |
| ③ lock 文件 | ✅ 已做 | `requirements-app.txt`（5 个直接依赖）→ `requirements.lock`（**21 个精确 pin**，含 `uvicorn[standard]` 的 extras 闭包）；生成器 `scripts/gen_lock.py`（可重放）；结构门 8 条 |
| ④ 版本对齐 + git tag + CHANGELOG + `[project.scripts]` | ⬜ **未做** | 如实标注：本批只做 ①②③⑤。tag / CHANGELOG 属发布动作，需要推送窗口（当前 `github.com:443` 仍不通） |
| ⑤ CI 容器冒烟（build + `/healthz` 200） | ✅ 已写，**尚未在 CI 跑过** | `.github/workflows/container-ci.yml`，刻意**不加** `continue-on-error`；断言逻辑 `scripts/smoke_container.py` **已在本机对宿主 `serve --env prod` 实测 PASS**（8 数据集可见、行级授权生效、404 正确），造数脚本有 4 条单测。剩下未验的只有「镜像能不能构建、容器能不能起」 |
| ⑥ 纯 CPU 十分钟可验路径（P1-9） | ⚠️ 部分 | 合成源路线（`scripts/ci_seed_sources.py`）已进 CI 冒烟；**"十分钟"是待实测的承诺，本文不写未测数字** |

另：S6 期间发现并修掉 **3 个真实缺陷**（HTTP 适配层整层 422、日期对象序列化 500、
`--dry-run` 真的写盘），全部有回归测试，见 `ENGINEERING_NOTES.md` #85–#88。

---

## 五、明确不做（防炫技）

| 不做 | 理由 |
|---|---|
| 接 Hive / Spark / Flink 集群 | 本机无环境；Ray 已在且已如实报告回本点不存在。**换名字不产生能力** |
| 部署 Iceberg / Hudi / Paimon | 开放表格式的价值在**多引擎并发写 + 时间旅行**，单机单写者场景下**收益为零**。只做「Parquet + 分区 + 时间分区」这层能验证的事 |
| 部署 DataHub / OpenMetadata / Polaris | 那是运维一个平台，不是证明我会做数据工作。血缘/契约**语义已对齐**，够了 |
| 实时流（Kafka / Flink / CDC） | 与项目定位（离线质量）不符。**但要在文档里写明为什么不做**——而不是装看不见（见 S2 荣誉条款：CDC 原理可做单文件回放的最小演示，若时间允许） |
| BI 工具（Tableau / Power BI / 帆软） | 环境成本高，Streamlit / 静态 HTML 等价；JD 里是加分项不是门槛 |
| 假装有真实业务主题域（GMV / 留存 / 转化漏斗） | **口径不诚实**。主题域改用「数据集 / 源 / 算子 / 批次」，面试时能自圆其说 |
| 声称 TB / PB 级处理 | 个人无算力。定位是「单机跑通 + 一手真实边界 + 明确回本条件」 |
| K8s | 单机项目上 K8s 是纯装饰 |

---

## 六、JD 可写性测试：补完之后能写出什么

沿用 `DS_DA_TRACK.md` 的验收方式——**写不出的就是没打中**。

| # | 补完之后能写的句子 | 对上哪句 JD 原话 | 属于 |
|---|---|---|---|
| 1 | 把全量重算脚本改造成**有批次概念的作业**：run/task/水位线元数据 + **幂等重跑 + 断点续跑**，同批次产物逐行 hash 一致 | 「任务运行记录」「异常恢复机制」 | S1 |
| 2 | 按 ODS/DWD/DWS/ADS 落地**物理分层**（Parquet + 分区），设计含 **SCD-2** 的维度表与事实表，DWD 增量 upsert，分区裁剪使扫描量降 X | 「数仓分层」「维度建模 / SCD」「分区裁剪」 | S2 |
| 3 | 用 Airflow 编排 7 任务真实依赖，配重试/超时/SLA 回调，**补数演练**证明不重复计数 | 「任务依赖编排与异常恢复」「补数」「SLA 达标」 | S3 |
| 4 | 把 ADS 层封装成**版本化数据服务**（契约 + 行/列级权限），暴露新鲜度与 P95 延迟 | 「数据服务接口」「RBAC / 脱敏」 | S4 |
| 5 | 建**管道健康与新鲜度看板 + 行数异常告警（含告警收敛）**，沉淀故障 Runbook | 「管道健康 / SLA 达成」「告警噪音收敛」 | S5 |
| 6 | dev/prod 双环境 + 参数化晋升 + lock + 镜像交付 + CI 容器冒烟 | 「DEV-to-PROD promotion」「reproducible environments」 | S6 |
| 7 | **（前置）** CI 三条门禁全绿，且能证明 pytest 在 CI 中真实执行 | 「CI/CD」「automated testing」 | S0 |

**判据**：第 1、3、5 条是**实习岗**的直接答案（"任务运行记录""异常排查""调度配置"）；
第 2、4、6 条是**社招数仓/平台岗**的答案。

---

## 七、如果只能做三件（优先级建议）

**第 1 件：S0 修门禁 + S1 引入批次。**
理由：S0 是"别卖假货"（CI 从第一天红，pytest 从未跑过）；
S1 是**唯一一件不做就做不了后面任何一件**的地基（增量 / 补数 / 新鲜度全依赖时间轴）。
而且 S1 的产物（run 台账 + 幂等对账）**恰好是实习 JD 明写、本项目最空白的那一项**。

**第 2 件：S2 真分层 + 增量 + 列存分区。**
理由：这是"数仓"这个词的实质内容（分层、维度建模、SCD、分区裁剪），
也是**唯一能让简历出现"数仓"二字而不心虚**的一批。同时它天然带一份性能对照证据
（jsonl 直扫 vs 分区扫描），补上数仓侧**完全没有性能数字**的空白。

**第 3 件：S3 调度与补数。**
理由：实习档真正的入口门槛。`dags/` 里那个 DAG 现在是**编排旧脚本的手动 DAG**——
改造成编排现在的数仓链路 + 补数演练，工作量不大，但对"数据开发"这个岗位名的针对性最强。

**S4–S6 放到最后**：它们是"有用户之后"的问题。
但注意与 09-18 的判断有出入——那时说"不建议现在做鉴权/Docker"，
现在**仍然不建议优先做**，但 S4 的成本已被 S3/S5 摊薄（服务化与看板共用运行台账），
可以在拿到第一段实习后作为第二阶段的叙事。

---

## 八、投递时的一句话定位（数据系统向）

> 「我做过一条**完整的数据系统**：从 ODS/DWD/DWS/ADS 物理分层、维度建模与 SCD，
> 到有批次概念的幂等作业与调度补数，再到数据服务与新鲜度告警。
> 规模是单机的，但**每一层的边界我都实测过**——包括它在哪里不成立。」

对齐到什么程度：**A/B/C/D/E 五面各至少有 1 条硬证据 + 1 处如实标注的边界。**
这个组合对一个无大厂实习、瞄准日常实习的候选人来说，比"参与过某大厂数仓"更可验证。

---

## 附：调研来源（2026-09-27 抓取）

**数仓 / 数据开发（社招）**
- RedotPay · Data Warehouse Engineer（中国香港）— ODS/DWD/DWS/ADS 建模、ETL/ELT、SLA、SQL 调优、数据倾斜、DQC、元数据血缘
- 大希地科技 · 数据仓库开发工程师（杭州）— 分层架构、维度建模/宽表、**SCD**、一致性维度事实对齐、数据标准、元数据管理规范、脱敏与权限分级
- 某公司 · 数据开发工程师（数据仓库方向）— **Kimball / Inmon、总线矩阵、主题域、SCD**、执行计划分析/分区裁剪/数据倾斜、Spark SQL/Hive/Flink SQL、调度工具与**异常恢复**、OLAP 引擎、UDF/UDAF、DataX/Canal/Flink CDC
- 信泰福建科技 · 资深数据仓库工程师（上海）— StarRocks/Hive/Spark、离线+实时、需求转指标口径
- 思绎科技 · 数据开发工程师（广州）— 数仓架构、数据源接入、ETL/ELT、**调度体系设计开发运维**

**数据平台（社招）**
- 某公司 · 数据平台工程师（济南）— 湖仓一体、Spark/Flink、Doris/StarRocks/ClickHouse、**表级/字段级血缘**、资产目录、分类分级、安全合规
- Notify · Data Platform Engineer（杭州）— 平台服务与共享库、**元数据驱动管道**、RBAC、**可观测性 logs/metrics/traces**、SLA、CI/CD、K8s、Iceberg/Parquet、catalog
- Smartly · Senior Data Platform Engineer — **契约驱动发布、schema 演进、GitOps、CDC runtime、DataHub、生命周期与成本归属**
- Snowflake · Data Platform Engineer — **DEV→UAT→PROD 晋升 + 参数化 + 审批门 + 版本化 + 回滚**、IaC、**管道健康/SLA达成/文件到达/成本趋势**看板、on-call/incident/postmortem、Runbook

**数据开发（实习档）**
- 广州 · 大数据开发实习生 — ETL/调度配置/运行监控/异常排查、SQL、Hive/Spark/ClickHouse、Airflow/DolphinScheduler、ODS/DWD/DWS/ADS
- 成都神州数码 · 大数据开发实习生 — SQL/Hive 查询与表结构、Spark/Hadoop/Kafka 任务调试、**任务运行记录与问题复盘**
- 众安保险 · 数据开发实习生（上海）— 数仓建模、ETL 脚本、调度与链路排查、**SQL 性能优化**、指标口径与数据血缘资产维护
- 恒生聚源 · 数据开发实习生（杭州）— ETL/调度/脚本、数仓监控报警与日常运维、Kimball 维度建模、Hadoop 生态

**开源生态**
- dbt 官方 materialization 最佳实践 — view → table → incremental 的「Golden Rule」
- dbt incremental models — `updated_at` 水位线 + merge/upsert 语义
