# 数据平台轨：从「指标计算脚本」到「有运行概念的数据系统」

> 2026-09-27。**S0–S6** 六批的落地记录（2026-09-30 收敛：原`DATA_SYSTEM_TRACK.md` 已删，本文即其唯一幸存载体）。
>
> **一句话**：旧仓库轨（`curation.duckdb`）回答的是「数据洗成什么样」；
> 平台轨（`data/lake/**` + `platform.duckdb`）回答的是
> 「**这是第几次运行、跑到哪了、上次为什么失败、这一版被谁消费了**」。
>
> **判据纪律**：本文所有数字都在本机实点（命令附在每节末尾），
> 不引用文档自述。凡是没做到的，单列在第七节。

---

## 一、为什么要另起一条轨，而不是改旧轨

旧轨的判据（2026-09-22 实点）：
`src/mm_curation/warehouse/` 下 `batch_date` / `watermark` / `run_id` / `checkpoint` /
`resume` / `partition` / `parquet` 命中 **0**。

命中 0 的原因不是缺模块，而是**缺一条维度**：旧轨是无状态函数 `jsonl → 产物`，
每次都 `CREATE OR REPLACE` 全量重算，只保留「当前这一份」。
数据系统的定义特征是**有状态**——有批次、有水位线、有依赖、有失败面、有产物版本。

两条轨并存，不是重复：

| | 旧仓库轨 | 平台轨 |
|---|---|---|
| 入口 | `scripts/mmc.py build/sql/metrics/scorecard/lineage/contracts` | `python -m mm_curation.cli <子命令>` |
| 产物 | `data/warehouse/curation.duckdb`（单文件，5.78 MB） | `data/lake/**`（Parquet + Hive 分区）+ `data/warehouse/platform.duckdb` |
| 计算模式 | 每次全量重算 | 按事件日分区，支持增量跳过 |
| 有没有「运行」 | 没有 | `job_runs` / `task_runs` / `watermarks` |
| 对外形态 | CLI 打印 + 静态报告 | CLI + **只读数据服务**（RBAC/限流/契约闸门）+ Prometheus |

> **把平台轨塞进旧轨的子命令里，会让人以为它们是同一套东西**——
> 而它们最关键的差别恰恰是「有没有运行这个概念」。

**为什么台账单独一个库文件**：`platform.duckdb` 与 `curation.duckdb` 分开，
且台账表与仓表共库但**重建仓表不该抹掉运行历史**——
**历史本身就是这一层要产出的资产**。

---

## 二、链路与分层

```
ods ──▶ dims ──▶ dwd ──▶ dws ──▶ ads ──▶ views ──▶ contracts ──▶ obs ──▶ metrics
落湖    SCD-2   明细事实  聚合    宽表   登记视图   契约闸门     观测快照   Prometheus
```

| 阶段 | 职责 | 落地形态 | 实点产出 |
|---|---|---|---|
| `ods` | 源 jsonl → ODS 列存，按 `dataset × event_date` 分区 | `data/lake/ods/ods_samples/dataset=X/event_date=Y/*.parquet` | 281 个分区文件 / 3.25 MB |
| `dims` | 维度 SCD-2：属性变更时封旧版本、开新版本，代理键随版本变 | `dim_device`（+ `dim_device_changes`） | 4479 个版本 |
| `dwd` | 明细事实：窗级事实 + 窗×算子事实，按 `valid_from/valid_to` 关联维版本 | `dwd_window` / `dwd_op_score` | 322 个分区文件 / 1.89 MB |
| `dws` | 聚合：`dataset × event_date`（+ `× op`），覆盖率与丢脏率**分列** | `dws_dataset_day` / `dws_op_day` | 322 个分区文件 / 0.45 MB |
| `ads` | 应用宽表：数据集健康（体量/新鲜度/覆盖/未匹配） | `ads_dataset_health` | 8 个数据集 |
| `views` | 湖上 Parquet → SQL 视图（**ads 之后**重建，保证视图不比数据旧） | 11 个视图 | — |
| `contracts` | **闸门**：`severity=error` 断言失败 → 阶段抛错 → 运行记 `FAILED`、下游不跑 | `configs/contracts_platform/` | 10 条断言全过 |
| `obs` | 新鲜度 / 行数稳健限 / 管道健康 / 维表未匹配 → 告警收敛 | `runs/obs/<run_id>.json` | 23 信号 → 9 告警 |
| `metrics` | Prometheus 文本落盘（**可归档的历史指标**，不只是当前值） | `runs/obs/<run_id>.prom` | 34 条序列 |

三个刻意的设计选择（都写在代码里，不只是文档里）：

1. **`contracts` 是闸门，不是报告。** 契约只要能「看过但不拦」，它就一定会变成「没人看」。
   warn 级只记录不阻断、进 `n_fail` 计数。
2. **`views` 单独立成一个阶段**，且必须在 `ads` 之后：
   混进任何别的阶段都会出现「视图比数据旧一版」的静默错。
3. **`metrics` 落在 DAG 内**：每次批量运行都留下一份可归档的指标快照，
   「当时是什么状态」有据可查，而不是只有「现在是什么状态」。

---

## 三、快速上手

```bash
# 两条入口指同一套实现；下面用短的
python -m mm_curation.cli run                      # 跑整条链路（全量）
python -m mm_curation.cli run --incremental        # 跳过湖上已有分区
python -m mm_curation.cli run --limit 500          # 每源最多 500 条，快速迭代
python -m mm_curation.cli run --datasets metropt3  # 只跑某个数据集
python -m mm_curation.cli runs                     # 运行台账
python -m mm_curation.cli runs --detail <run_id>   # 某次运行的阶段明细
python -m mm_curation.cli obs --write runs/obs/x.json   # 观测快照（不跑链路）
python -m mm_curation.cli obs --prometheus         # Prometheus 文本
python -m mm_curation.cli contracts                # 平台轨契约校验（失败 exit 1）
python -m mm_curation.cli prune dwd metropt3 2020-05-01   # 分区裁剪的**字节级**证据
python -m mm_curation.cli dag --mermaid            # 打印链路
python -m mm_curation.cli dag --export-airflow dags/generated_platform_dag.py
python -m mm_curation.cli serve --dry-run          # 只跑契约闸门，不起服务
python -m mm_curation.cli serve --host 127.0.0.1 --port 8080
```

等价的旧入口写法：`python scripts/mmc.py platform <同上子命令>`。

**依赖**：`duckdb`（湖与仓）、`pyarrow`（Parquet schema 推断）、`pyyaml`（配置）、
`fastapi`+`uvicorn`（仅服务层需要）。`uvicorn` 未装时 `serve --dry-run` 仍可用——
它只跑契约闸门，不起 HTTP。

**Airflow 轨**：`dags/generated_platform_dag.py` 是**生成物入库**，
每个 `BashOperator` 调 `--only <阶段>`（独立进程，这是「超时可强杀、重试可隔离」的前提）。
它必须与 `src/mm_curation/platform/jobs.py` 的定义**逐字节一致**，漂移即红：
单测里有一条断言，`ci.yml` 里另有一条 `diff` 门禁（让漂移在 CI 页面上一眼可见）。

---

## 四、真实数据实点

### 4.1 跑批（8 个真实数据集，全量首跑）

| 阶段 | 结果 | n_in | n_out | 指纹 | 耗时 |
|---|---|---|---|---|---|
| `ods` | OK | 82264 | 82264 | `aafbc7c3ff42` | 12.847s |
| `dims` | OK | 4479 | 4479 | `dim:v4479` | 0.504s |
| `dwd` | OK | 82264 | 82264 | `9d2c2316de14` | 10.906s |
| `dws` | OK | 281 | 281 | `65d3f9b556db` | 8.962s |
| `ads` | OK | 8 | 8 | `936b00d58079` | 0.590s |
| `views` | OK | 0 | 11 | — | 1.491s |
| `contracts` | OK | 10 | 10 | `contracts:1::` | 2.080s |
| `obs` | OK | 23 | 9 | — | 0.462s |
| `metrics` | OK | 23 | 34 | — | 0.015s |
| **合计** | **SUCCESS** | | | | **38.75s** |

### 4.2 仓表行数

| 表 | 行数 | 表 | 行数 |
|---|---|---|---|
| `ods_samples` / `ods_all` | 82264 | `dim_device` | 4479 |
| `dwd_samples` / `dwd_window` | 82264 | `dim_device_changes` | 0 |
| `dwd_op_score` / `dwd_scores` | 30048 | `dws_dataset_day` | 281 |
| `dws_op_day` | 312 | `dws_dataset_profile` | 8 |
| `ads_metrics` | 8 | `ads_dataset_health` | 8 |

> `dim_device_changes = 0` 是**如实结果，不是缺陷**：SCD-2 的变更表只在同一
> 业务键的属性*真的变了*时才有行。全量首跑时每个键只观察到一次，
> 所以没有变更——这正是 SCD-2 该有的表现（`dims` 的 `n_versions == n_observed_keys`）。

### 4.3 湖上分区

| 层/表 | 数据集 | 事件日 | Parquet 文件 |
|---|---|---|---|
| `ods/ods_samples` | 8 | 266 | 281 |
| `dwd/dwd_window` | 8 | 266 | 323（含 `dwd_op_score`） |
| `dws/dws_dataset_day` | 8 | 266 | 323（含 `dws_op_day`） |
| `ads/ads_dataset_health` | 8 | 1 | 8 |
| **合计** | | | **935 个文件 / 5.60 MB** |

「事件日 266 但文件 281」的差 **15**，全部来自两个**无事件时间**的数据集
（`fhir_funnel` / `image_funnel`）——它们的记录落在
`event_date=__HIVE_DEFAULT_PARTITION__`。这条差异是设计要暴露的，
不是记账误差（见 §6.2「未标日期」）。

### 4.4 台账（含一次真实故障注入）

```
platform__realdata__0001  SUCCESS  2026-09-27  38.747s  bee5c45c
platform__realdata__0002  FAILED   2026-09-27  48.527s  bee5c45c
```

第二次运行在 `ods` 阶段被**本机工具链的批量删除护栏**中断（重写无日期分区需先删旧目录）。
这一次中断恰好构成一次**真实的故障注入**，所以保留它，用作 S5 的验收证据：

- 台账记 `FAILED`（进程确实没跑完；`finish_run` 走的是执行器失败时的同一条路径）；
- 观测层随之**恰好报出一条** `crit` 告警，且指名道姓：
  `pipeline_failure:*  n=1  最近一次已结束的运行 platform__realdata__0002 终态 FAILED`；
- **同一次快照里，`success_rate` 是 `0.5`**（1 成功 / 2 终态）——不是 0，也不是 1。

> `platform__realdata__0001.json` 是**修复前**的旧快照，保留下来作对照：
> 它的告警正文是「最近一次运行 … 终态 **RUNNING**」——
> 那条假告警的签名（详见 §6.3）。

### 4.5 契约闸门

`configs/contracts_platform/lake_core.yaml`：**1 份契约 / 10 条断言 / 0 fail / 0 error**。

| 断言 | 级别 | 校验什么 |
|---|---|---|
| `dwd_matches_ods` | error | DWD 明细行数 == ODS 行数（不丢行） |
| `profile_reconciliation` | error | DWS 日汇总与 `dws_dataset_profile` 对账 |
| `no_orphan_score` | error | 不存在没有对应窗的算子评分行 |
| `attribution_complete` | error | 每条判决都能归因到算子 |
| `dim_unmatched_sensor` | error | 有通道的传感器行必须关联到维版本 |
| `ads_undated_visible` | warn | 无日期分区必须被 ADS 如实计数 |

### 4.6 分区裁剪：字节级证据（不是「文件数」）

```bash
$ python -m mm_curation.cli prune dwd metropt3 2020-05-01
命中 2736 B / 整层 1885232 B → 少扫 99.85%
```

**为什么要报字节而不是文件数**：代价由字节数决定，不由文件数决定。
「扫了 1/323 个文件」听起来一样好，但如果那 1 个文件是整层的 80%，
结论就完全反了。

而且这里有一条**刻意的拒绝**：过滤条件**什么都没匹配到**时，
`prune_ratio` 返回 `None`、CLI 非零退出，**不返回 `1.0`**。
因为 `1 - 0/layer_bytes == 1.0` 会把「dataset 写错了」渲染成「少扫 100%」——
**最该报错的时候给了最好的读数**。这与 `obs.pipeline_health` 在分母为 0 时返回
`None` 是同一条纪律：**没有分母 / 没有命中时，返回 `None`，不返回一个好看的数**。

---

## 五、数据服务层（S4）

`ServiceCore`（框架无关的逻辑）+ `create_app()`（薄 FastAPI 适配）。
HTTP 层只做三件事：取 header、调 `dispatch()`、把状态码写回去——
于是**同一套权限/限流/脱敏逻辑可以被单测直接覆盖，不需要起服务器**。

> ⚠️ 但「薄壳不用测」是错的：**薄壳欠了一次整层级的债**（`ENGINEERING_NOTES` #85）。
> 上面那条设计理由（不起服务器就能单测）曾把所有测试都挡在 `dispatch` 层，
> 于是 HTTP 适配层从未被执行过一次——而它当时除 `/metrics` 外**全部返回 422**
> （注解解析失败）。现在 `test_platform_service.py` 里有一节
> **HTTP 适配层**（用 `fastapi.testclient`，进程内 ASGI，不需要 socket）：
> ① 每个端点不许是 422；② `/healthz` 的 503 穿过 HTTP 层；③ 行级授权穿层后仍生效；
> ④ **两层状态码必须一致**（防"以后只改一层"）。

### 5.1 端点

| 方法与路径 | 鉴权 | 说明 |
|---|---|---|
| `GET /api/health` | 否 | 存活 + 契约闸门状态 + **当前环境**（dev/prod 的表名相同，只能从这里区分） |
| `GET /healthz` | 否 | **容器探针专用**：就绪（契约闸门过了、真能供数）返回 `200`，否则 `503` |
| `GET /metrics` | 否 | 服务侧 + 数据侧（S5）指标合并 |
| `GET /api/datasets` | 是 | 可见数据集清单（含被脱敏列名） |
| `GET /api/runs?limit=N` | 是 | 运行台账 |
| `GET /api/datasets/{ds}/health` | 是 | 数据集健康宽表 |
| `GET /api/datasets/{ds}/daily?limit&from&to` | 是 | 日粒度：体量/保留率/设备数 |
| `GET /api/datasets/{ds}/ops?event_date&limit` | 是 | 算子粒度：评分覆盖率、丢脏率 |
| `GET /api/datasets/{ds}/dims?limit` | 是 | 维表版本（含 SCD-2 字段） |

`/healthz` 与 `/api/health` **刻意是两个端点**：前者的语义是「能供数」（探针用），
后者是「进程活着、把状态如实报出来」（永远 200，状态在 body 里，诊断用）。
混成一个的坏结果二选一：探针在闸门没过时报 healthy，或者老诊断入口开始返回 503。

### 5.2 状态码语义（不是随手挑的）

| 码 | 触发 |
|---|---|
| `401` | 没带 token |
| `403` | token 有效但角色无权（含**行级**：数据集不在白名单） |
| `404` | 路径或数据集不存在 |
| `429` | 令牌桶空（响应里带 `retry_after_s`） |
| `503` | **契约闸门未过** → 服务拒绝服务（`ready=False`），不是 500 |
| `500` | 未预期异常（要变成响应，不能崩服务） |

### 5.3 RBAC 与限流（`configs/rbac.yaml`）

| 角色 | 隐藏列（列级脱敏） | 可见数据集（行级） | 稳态 qps | 突发 burst |
|---|---|---|---|---|
| `admin` | — | `*` | 100 | 200 |
| `steward` | — | `*` | 50 | 100 |
| `analyst` | `device_id` | `*` | 10 | 20 |
| `finance_reader` | `device_id`, `channel` | `finance_funnel`, `text_funnel` | 5 | 10 |

**演示 token**（本地用；生产应从密钥管理注入）：

| token | 角色 |
|---|---|
| `mmc-admin-demo` | `admin` |
| `mmc-steward-demo` | `steward` |
| `mmc-analyst-demo` | `analyst` |
| `mmc-finance-demo` | `finance_reader` |

```bash
curl -s -H 'X-API-Token: mmc-analyst-demo' \
     'http://127.0.0.1:8080/api/datasets/metropt3/dims?limit=2'
```

```bash
# 服务侧权限/限流/脱敏不需要起服务也能自证（33 条单测走的就是这条路径；
# 另有 5 条走真实 ASGI 覆盖 HTTP 适配层，见 5.1 的说明）
python -m pytest tests/test_platform_service.py -q
```

三条**口径**上的选择：

1. **配置里不存明文 token，只存 sha256。** 配置泄露 ≠ 凭据泄露。
2. **列级脱敏在 SQL 里做，不是返回后再删。** 返回后再删等于数据已经出去过一道，
   日志与中间态都已经泄露。
3. **用经典令牌桶，不用固定窗口。** 固定窗口在边界上能放过 2× 稳态速率
   （窗口末尾 + 下一窗口开头），这是限流最常见的假实现。
4. **`analyst` 看得到代理键 `device_sk`、看不到业务自然键 `device_id`**——
   这正是 SCD-2 代理键的用途之一：让分析不必暴露业务键。

---

## 六、可观测性与告警收敛（S5）

### 6.1 四类信号

| 信号 | 判据 | 来源 | 实点 |
|---|---|---|---|
| 数据新鲜度 | `today - max(event_date) > SLO` | `ads_dataset_health` | 破线 3/8 |
| 行数异常（涨/跌） | 超出**稳健限**：中位数 ± margin × 1.4826×MAD | `dws_dataset_day` | 17 个分区 |
| 管道失败 | 最近一次**已终态**运行的终态不是 SUCCESS | `job_runs` | 1 条（故障注入） |
| 维表未匹配 | 有通道的传感器行没关联上维版本（真问题） | `dwd_window` | 0 条 |

### 6.2 三个刻意的口径

1. **用中位数 + MAD，不用均值 + 3σ。** 日窗数本身右偏，均值与标准差会被极端日拉走，
   阈值最后宽到什么都抓不到。口径直接复用 `operators/robust`（唯一真相源）。
2. **告警必须收敛后再报。** 原始信号按天产生，把几十条丢给人看，
   接受者的实际反应是「关掉通知」，那等于没有告警。
   所以按 `(类型, 数据集)` 指纹收敛成一条，带 `n_signals` / `first` / `last`。
   本次 23 条信号 → 9 条告警，收敛率 60.9%。
3. **「在途」不是一个结论。** `RUNNING` 既不是成功也不是失败，是「还不知道」。
   而 `obs` 阶段**必然**在一次运行进行中执行——把它算进成功率或报成失败，
   等于让这条指标每次运行都把自己判一遍罪。两处都按同一集合
   （`SUCCESS/FAILED/TIMEOUT/SKIPPED`）取终态，`RUNNING` 只在 `n_running` 里如实计数。
   **分母为 0 时返回 `None` 而不是 `0`**：`0.0` 是「全都失败了」，`None` 是「还没有结论」，
   这两件事不能共用一个值。

**新鲜度为什么要按数据集给 SLO**（`configs/platform.yaml`）：
`cmapss`(2000) / `metropt3`(2020) / `skab_w64`(2020) 是**已停产的公开数据集**，
事件时间不会再前进——用统一的 7 天 SLO 去卡它们，得到的是 6/8 破线这种
**没有信息量**的结果（「源停了 20 年」不是运维事件）。所以给两类不同 SLO：
归档集 `36500` 天，活数据集保持 `7` 天。**好阈值必须区分
「源停了（历史集：正常）」与「源停了（活集：故障）」**；混在一个阈值下，
两种情况都报警，也就两种都忽略。

### 6.3 一个「修了一半」的 bug（值得单独记）

`obs` 曾报一条 `crit pipeline_failure` —— 而九个阶段全绿。
根因是同一句话在**两个地方**各写了一遍，只修了一处：

| | 原来的写法 | 后果 |
|---|---|---|
| `pipeline_health.success_rate` | 分母含 `RUNNING` | 每次运行把自己算成失败 → 实测 `0.0` |
| `collect_signals` 的管道失败信号 | `recent` 里任何一条非 SUCCESS 就报 | 每次运行都报一条 crit 假告警 |

第一次修复只动了成功率那一半（`success_rate` 改成只对终态算），
**信号那一半留了下来，换了个出口继续响**。
第二次修复（本次）把信号侧也改成「只看最近一条**已终态**运行」，
顺带修掉第二个缺陷：原实现遍历 `recent` 的 10 条，把历史上每一次失败都补一条信号，
而消息里写着「最近一次运行」——名实不符，失败越多报得越多。

修完的分工很干净：**指标管历史（成功率把历史失败都算进去），告警管当下（只报最近一次终态）**。
回归测试覆盖了三种形态：只有在途 → 不报；最新终态是 SUCCESS → 不报；
最新终态是 FAILED → **恰好报一条**（见 `tests/test_platform_obs.py`）。

---

## 七、环境与交付：从「我本机能跑」到「能交付」（S6）

前面六节解决的是「这套东西对不对」。这一节解决的是另一个问题：
**换一台机器、换一个人，能不能拿到同样的结果**。三件事，都是可核对的。

### 7.1 dev / prod 双环境：拆的是「仓库根」与「产物根」

在这之前整个平台轨只有**一个** `root`，它同时承担读源（`data/raw/**`）、
读配置（`configs/**`）、写产物（`data/lake/**`、`data/warehouse/*.duckdb`、`runs/obs/**`）
三件事。于是「换一个环境」无处可放——只能靠复制整个仓库来模拟，
而那会把**代码**也复制一份，于是「prod 上跑的是哪份代码」（台账的核心字段 `git_sha`）
就答不上来了。

现在拆成两个概念（实现在 `src/mm_curation/platform/envs.py`）：

| 概念 | 内容 | 每个环境几份 |
|---|---|---|
| **repo** | 代码、`data/raw/**`、`configs/**` | **一份**（同一份工作区、同一个 git_sha） |
| **store** | 湖、数仓、台账、观测快照 | **每环境一份** |

- `dev` 的 store **就是仓库根**（路径仍是 `data/lake/`、`data/warehouse/`、`runs/obs/`）。
  这不是偷懒：既有文档、脚本、历史产物全按这三个路径写，把它们整体搬走
  会让「引入环境概念」这次改动波及一切，而隔离目标（prod 不污染 dev）已经达成。
  **回归测试钉住了这一点**（`test_resolve_dev_store_is_the_repo_root`）。
- `prod` 的 store 是 `data/envs/prod/`，与 dev 物理隔离。
- 环境名**非法时抛错**，不回落到 dev：`--env prd` 如果静默当 dev，
  那次「生产发布」就会把数据写进 dev，而日志、退出码、产物路径全都正常。

`--env` 挂在**所有子命令**上（不是只挂 `run`）：环境是「这次操作针对哪份产物」的属性，
写的时候能选环境、读的时候不能，就会造出「查 prod 的告警只能去翻 dev 的湖」。
`promote` 例外——它有 `--from-env`/`--to-env` 两个明确参数，
刻意**不继承**一个不生效的 `--env`（留一个无效开关比没有更糟）。

### 7.2 参数化晋升：把「验过的那份」搬过去，而不是重跑一遍

`python -m mm_curation.cli promote` 的语义是
**把 dev 上已验证的那份产物快照搬到 prod，并留下可追溯的记录**。
它不是「重跑一遍」——重跑会得到另一份数据，那样 prod 就没有
「这份数是 dev 上验过的那份」这个性质了。

三道闸门，任何一道不过就不搬：

| # | 闸门 | 判据 |
|---|---|---|
| 1 | 源环境最近一次**已终态**运行是 `SUCCESS` | 在途 `RUNNING` 不算结论（与 `obs` 口径**故意一致**） |
| 2 | 该次运行的观测快照里**没有 crit 告警** | 快照缺失时如实记 `skipped`，**不假装通过** |
| 3 | 搬完**按「路径 + 字节」对账** | 目标树指纹必须等于源树指纹，不等记 `FAILED` |

外加一道**前置拒绝**（发生在动任何文件之前）：目标环境存在源里没有的
**陈旧分区**时默认拒绝，提示加 `--prune-stale`。

**实测（本机真实数据，935 个分区文件）**：

```
# 第一次晋升（prod 为空）
$ python -m mm_curation.cli promote --force
OK    dev → prod
  晋升完成：935 个分区文件 / 5601381 B（新增更新 935、未变 0、删除陈旧 0），
  视图 11 个，契约 1/1 通过，prod 台账记为 promote_prod__2026-09-27__2a221810
  源：935 文件 / 5601381 B（指纹 0cc9d2907c26…，口径 path+size）
  目标：935 文件 / 5601381 B（指纹 0cc9d2907c26…）

# 第二次晋升（幂等：0 新增、0 删除）
$ python -m mm_curation.cli promote --force
  晋升完成：935 个分区文件 / 5601381 B（新增更新 0、未变 935、删除陈旧 0），…
  耗时 3.60s（首次 4.04s）

# 闸门确实会拦
$ python -m mm_curation.cli promote --dry-run
FAIL  dev → prod
  dev 最近一次已结束的运行 platform__realdata__0003 终态是 FAILED，
  只有 SUCCESS 的运行才允许晋升
```

> `--force` 只跳过**闸门**，**不跳过对账**。上面这次真实晋升用了 `--force`，
> 原因写在明处：唯一成功的那次运行（`0001`）已经不是最新的一次
> （`0002` 是刻意注入的故障、`0003` 被本机环境的批量删除护栏中断），
> 而闸门的规则是「最新一次终态必须成功」。**规则没有被绕过，它是被显式跳过的**，
> 且目标环境的契约闸门仍然独立跑了一遍。

服务容器读的就是这个 prod store：`serve --env prod` 实点
「契约 10 条断言 / 阻断 0 / 就绪 True / store=data/envs/prod」。

### 7.3 交付物：镜像 + 锁 + 容器冒烟

| 交付物 | 位置 | 门禁（会失败的那种） |
|---|---|---|
| 应用镜像（多阶段、非 root、CPU-only） | `docker/Dockerfile.app` | `COPY` 的源必须存在；不许装 `requirements.txt`（会把训练栈拖进服务镜像）；必须打 `/healthz`；必须 `USER ` + `0.0.0.0` |
| 服务侧依赖精确锁（21 个 pin） | `requirements.lock`（生成器 `scripts/gen_lock.py`） | 每条必须是纯 `==`；**每个直接依赖都必须在锁里**（漏了就红）；LF-only |
| 编排 | `docker-compose.yaml` 的 `app` 服务（8081→8080） | 端口不抢 Airflow 的 8080；必须挂 `./data` |
| 容器冒烟 | `.github/workflows/container-ci.yml` + `scripts/smoke_container.py` | 镜像必须构建成功；容器必须起；`/healthz` 必须 **200 且 ready=true**；带 token 取数必须成功；行级授权必须仍生效 |

关于 lock 的一句要紧的话：**它钉的是「构建可复现」，不是「实测版本可复现」。**
锁里写的是 pip 解析器当日给出的上游版本（`pyarrow==25.0.1`），
而本机 venv 实测用的是 `23.0.1`。两者都对，含义不同——这句话写在锁文件抬头，
因为它决定了别人拿到的镜像和你手上这套是不是同一套。

### 7.4 这一节暴露的四个真实缺陷

写交付物最大的风险不是「写不出来」，而是**写出来的东西看起来是对的**。
S6 期间被抓出四个，全部已修、全部有回归测试（详见 `ENGINEERING_NOTES.md` #85–#89）：

1. **HTTP 适配层整层不可用**：除 `/metrics` 外所有端点一律 **422**。
   根因是 `from __future__ import annotations` 让签名注解在运行时成了字符串，
   而 `Request` 只在 `create_app` 内部导入，FastAPI 用**模块全局**解析时找不到它，
   于是把它当成必填 query 参数。**所有单元测试都是直接调 `ServiceCore.dispatch` 的**
   （刻意的设计），于是「HTTP 层从未被跑过」这件事完全没有信号。
   → 已补 6 条走真实 ASGI 的测试 + 冒烟脚本对着真 socket 打。
2. **同一个适配层的第二个独立缺陷**：DuckDB 的 DATE 列是 `datetime.date`，
   直接 `JSONResponse` 会 **500**。→ 适配层用 `jsonable_encoder`。
3. **`--dry-run` 真的写了盘**：`sync_tree` 在 dry-run 判断之前就被调用，
   于是「只报告不改动」的模式把整棵树复制了过去。
   → 无副作用必须由参数保证，而不是靠调用方自律。
4. **`.gitignore` 用逐条列举覆盖 `data/raw/`，于是新目录漏网**：
   `data/raw/*.jsonl` 这类模式**不跨 `/`**，只吃直挂文件；子目录全靠一串列举，
   于是当时新出现的 `fhir_synth/`（44 KB）与 `finance_news/`（136 KB）谁都没覆盖
   → `git status` 里多一个 `??` → 任何人 `git add -A` 就把它提交上去。
   **这类缺陷不会让任何命令失败**，「名单漏了新目录」是唯一征兆。
   → 换成 `data/raw/*/` 一条覆盖（实测覆盖点目录与任意深度、不碰直挂文件，
   `.gitkeep` 仍在跟踪范围内）。补门禁时自己又踩了两个坑——untracked cache
   让第一版门禁**在规则已坏时仍是绿的**、`subprocess` 文本模式把 `\n` 翻成 `\r\n`
   让第二版**全红**——详见 #89。

---

## 八、诚实边界

**明确不做，以及为什么**：

| 不做 | 理由 |
|---|---|
| Hive / Spark / Flink 集群 | 本机无环境；Ray 已在且已如实报告回本点。**换名字不产生能力** |
| Iceberg / Hudi / Paimon | 开放表格式的价值在「多引擎并发写 + 时间旅行」，单机单写者场景收益为零。只做「Parquet + 分区」这层可验证的事 |
| DataHub / OpenMetadata | 那是运维一个平台，不是证明会做数据工作；血缘/契约语义已对齐 |
| Kafka / Flink CDC 实时流 | 与项目定位（离线质量）不符。**但不装看不见**：这是主动的取舍 |
| K8s | 单机项目上 K8s 是纯装饰 |
| BI 工具（Tableau / Power BI） | 环境成本高，静态 HTML / Streamlit 等价 |
| 假装有 GMV / 留存 / 转化漏斗 | 口径不诚实。主题域用「数据集 / 源 / 算子 / 批次」 |
| 声称 TB / PB 级 | 个人无算力。定位是「单机跑通 + 一手真实边界 + 明确回本条件」 |

**已知限制（逐条，都能复现）**：

1. **增量的读取侧收益为零。** 源是平面 jsonl，没有分区目录可下推，
   所以「跳过已有分区」只发生在**解析之后**——收益只在写入侧。
   想拿读取侧收益，源本身就得是分区表。**这是源格式决定的，不是实现偷懒。**
2. **断点续跑是阶段粒度，不是逐行。** `--resume` 跳过的是「这次 `run_id` 下已
   `SUCCESS` 的阶段」；跨运行续跑需要参数指纹比对，本版未做。
3. **超时是「检测」不是「强杀」。** 本地执行器在**线程**里跑任务，线程无法被强杀，
   所以 `TIMEOUT` 只保证「记录并中断后续阶段」，不保证底层工作立刻停止。
   真正的强杀需要进程级隔离——Airflow 轨的每阶段 `BashOperator` 才是那个形态。
4. **`--only`（Airflow 单阶段调用）不写 `job_runs`。** 一个 Airflow DAG run 的所有阶段
   共用同一个 `run_id`，各阶段是独立进程，若都去 upsert 那条行，
   最终留下的是最后一个阶段的耗时与状态——那是一个假数。单阶段只写 `task_runs`。
5. **权限是「单 token + 角色」级别**，不是企业级用户体系 / SSO / OIDC。
6. **本机 Docker 拉不到 `registry-1.docker.io`**（无 HTTPS 代理），
   所以容器化路径只能靠 CI 验，不能本地复现。
   → 处置不是"加个 `continue-on-error` 让它别红"，而是**把可本地验证的部分全部移出 CI**：
   冒烟的断言逻辑在 `scripts/smoke_container.py`（可对着宿主 `serve` 先跑通，已实测 PASS）、
   造数在 `scripts/ci_seed_sources.py`（有 4 条单测覆盖）、
   Dockerfile 的 `COPY` 源与锁的覆盖度有 8 条结构门。
   于是 `container-ci.yml` 里剩下的未知量**只有「镜像能不能构建、容器能不能起」**——
   而这两件事确实只能在那里验，所以它**刻意不加 `continue-on-error`**。
7. **无日期分区每次都重写。** `partition_values` 刻意排除
   `__HIVE_DEFAULT_PARTITION__`（它不是一个「事件日」），
   于是无日期分区永远不在跳过集合里。代价是每次增量都要重写这 2 个分区——
   换取的是「事件时间未知 ≠ 今天」这条语义没有被悄悄抹平。
8. **`success_rate` 在第一次运行后是 `None`。** 这是设计，不是缺陷（见 §6.2 第 3 条）。

---

## 九、S0：先修真门禁

数据平台岗的第一道面试题是「你们怎么保证质量」，而这道门禁原先**是假的**。

`ci.yml` 的历史形态是 `Ruff lint → Ruff format check → pytest`，且格式检查没有容错。
它在**既有历史文件**上必然失败（实点：`ruff format --check src tests scripts dags packages`
报 **54 个文件**待格式化 —— `scripts` 25 / `tests` 14 / `src` 11 / `packages` 4），
于是它后面的两条 `pytest` **从未被执行过一次**——
工作流里写着「主仓与包侧测试」，执行层面却是零覆盖。

**这不是格式问题，是门禁语义问题：步骤顺序也是一种门禁语义。**

本次改动：

1. **测试步骤排到格式检查之前**（`lint → 主仓测试 → 包侧测试 → DAG 同步 → 格式检查`）；
   刻意**不加** `continue-on-error`——格式欠债是真欠债，它该红，但不许再挡在测试前面。
2. **新增 DAG 逐字节同步门禁**（生成到临时路径再 `diff`，**不是就地重写**——就地重写永远绿）。
3. 顺带修掉两个会让上面那条门禁**假红 / 永远不绿**的坑：
   - **换行符**：Windows 上文本模式会把 `\n` 写成 `\r\n`，同一份定义在 Windows 产出 CRLF、
     在 Linux 产出 LF。`Path.read_text()` 会归一化换行，所以「读回来比对」的断言看不出差别，
     而 CI 里那条 `diff` 不会——它会在 Linux runner 上因**换行符**而红，与 DAG 内容毫无关系。
     修法是写入侧显式 `newline="\n"`。
   - **格式门禁与同步门禁互相打架**：`ruff format` 会按自己的规则改写生成物，
     于是「格式合规」与「逐字节一致」永远无法同时满足。修法是把生成物排除出 ruff
     （`pyproject.toml` 的 `extend-exclude = ["dags/generated_*.py"]`）——
     **生成物不是人写的代码，就不该受人写代码的规则约束。**

> ⚠️ **未完成的欠债（实点，不粉饰）**：那 54 个文件仍未格式化——它们包含协作方在途的改动，
> 全量 `ruff format` 必须等那些改动落库后再做。
> **但本次平台轨新增的 17 个文件（9 个模块 + `cli.py` + 7 个测试文件）一个都不在那 54 个里，
> 全部双绿**（`ruff check` + `ruff format --check` 实点）。
> 在此之前格式检查仍会红；**但它已经不再挡住测试**。
> 表述纪律：在格式检查转绿之前，任何「CI 全绿」的说法都不能写进简历。

---

## 十、复现本文所有数字

```bash
# 1) 全量真实跑批（约 40s；需要 data/raw/ 下的真实数据）
python -m mm_curation.cli run --run-id platform__realdata__0001

# 2) 台账
python -m mm_curation.cli runs --json
python -m mm_curation.cli runs --detail platform__realdata__0001

# 3) 观测（不跑链路，只读）
python -m mm_curation.cli obs --write runs/obs/platform__after_failure.json
python -m mm_curation.cli obs --prometheus

# 4) 契约闸门
python -m mm_curation.cli contracts

# 5) 分区裁剪的字节级证据（不是「文件数」）
python -m mm_curation.cli prune dwd metropt3 2020-05-01
#   实测：命中 2736 B / 整层 1885232 B → 少扫 99.85%

# 6) 服务侧（不起服务也能自证）
python -m mm_curation.cli serve --dry-run --env prod
python -m pytest tests/test_platform_service.py -q

# 7) 环境与晋升（S6）
python -m mm_curation.cli runs                       # dev 台账
python -m mm_curation.cli promote --dry-run          # 只过闸门
python -m mm_curation.cli promote                     # 真晋升（幂等）
python -m mm_curation.cli runs --env prod            # prod 台账
python -m mm_curation.cli serve --env prod --dry-run  # 容器读的就是这个 store

# 8) 容器冒烟（**本机只能验断言逻辑，不能验 docker**）
#    起一个宿主服务，然后对着它打真 socket
python -m mm_curation.cli serve --env prod --port 18080 &
python scripts/smoke_container.py --base-url http://127.0.0.1:18080 --expect-env prod
#    镜像构建 + 容器运行只能在 CI 跑（本机拉不到 registry-1.docker.io）
#    见 .github/workflows/container-ci.yml

# 9) 平台轨全部测试
python -m pytest tests/test_platform_*.py tests/test_lock_file.py tests/test_ci_seed_sources.py -q
```

---

## 附：代码与测试规模（实点）

| 平台轨模块 | 行数 | | 测试 | 行数 | 例 |
|---|---|---|---|---|---|
| `service.py` | 718 | | `test_platform_service.py` | 572 | 38 |
| `modeling.py` | 680 | | `test_platform_obs.py` | 443 | 23 |
| `obs.py` | 518 | | `test_platform_envs.py` | 414 | 24 |
| `envs.py` | 505 | | `test_platform_jobs.py` | 303 | 16 |
| `jobs.py` | 454 | | `test_platform_dag.py` | 291 | 19 |
| `dag.py` | 398 | | `test_platform_modeling.py` | 223 | 8 |
| `runs.py` | 396 | | `test_platform_lake.py` | 158 | 10 |
| `lake.py` | 371 | | `test_lock_file.py` | 262 | 11 |
| `registry.py` | 199 | | `test_platform_runs.py` | 142 | 11 |
| `__init__.py` | 32 | | `test_ci_seed_sources.py` | 88 | 4 |
| **合计** | **4271** | | **合计** | **2896** | **164** |

（另计 `src/mm_curation/cli.py` 463 行；未计入测试文件的辅助模块与 fixture。）

**测试基线（2026-09-30 实点）**：主仓 `pytest -q tests` → **624 passed / 0 failed / 0 error / 0 skipped**
（本机装了 ray + duckdb 且已生成 `data/`；106.9s 为 616 档时点值）；包侧 `pytest -q packages/curation-eval/tests` → **67 passed**（48.7s）；
合计 **691 条全绿**。
其中平台轨与交付物门禁贡献 **164 条**（`--collect-only` 实点，逐文件见上表）。
演进：修观测层信号缺陷前 562 → 567（S6 平台侧修复）→ 613（S6 环境/晋升/交付门禁 +46）
→ **616**（`data/raw/` 忽略覆盖面门禁 +3，见 §7.4 与 `ENGINEERING_NOTES` #89）
→ **618**（glm R1 两条回归用例，未回写）→ **623**（M2 门面数字门禁 +5，见 `ENGINEERING_NOTES` #93）→ **624**（glm G2 终局修复补 1 条 store 搬迁回归测试，见 `docs/devlog/2026-09-29-glm.md`）。

参考：`docs/ENGINEERING_NOTES.md` 记现象与根因（本次新增 #85–#89）；
`docs/RUNBOOK.md` 记处置；本文各节即六批的落地记录。
