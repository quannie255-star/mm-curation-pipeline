# 真实数据接入选型与下载清单

> 回答「项目还没跑过真实数据，该下载哪几类」。先纠正前提（真实数据已跑过三轮），
> 再给候选清单与**实测可达的下载命令**。生成日期：2026-09-18。
> 所有下载地址均已 curl 探活（结果标注在每条下方），速度为本机实测。
>
> **首跑结果见 [REAL_DATA_REPORT.md](REAL_DATA_REPORT.md)**（2026-09-18：**三个数据集已全部
> 跑通** —— SKAB / MetroPT-3 全量 / C-MAPSS FD001。合成 → 真实迁移后召回 24%~72%、
> 误杀 16%~28%，合成对照是 100%/1.23%；另发现「无标签 ≠ 干净」与「同形态多含义」两条方法论结论）。
>
> **镜像实测（2026-09-18）**：`raw.githubusercontent.com` 与 `codeload` 当前**完全不通**；
> `https://ghfast.top/<raw 完整 URL>` 代理**实测 ~300KB/s 可用**（首选）；
> `cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/<path>` 可用但**大文件返 200 + 0 字节**（别只看状态码）；
> `ghproxy.net` / `gh-proxy.com` / `raw.githack.com` / `mirror.ghproxy.com` 等均 0 字节。
> UCI 直链实测 ~7KB/s（大文件基本拿不动）。

## 一、先纠正前提：真实数据已经跑过三轮

本机落盘实点（`data/raw/`）：

| 数据 | 是什么 | 实际规模 | 落盘路径 | 跑了什么 |
|---|---|---|---|---|
| COCO-CN | 真实照片 + 人工中文描述（HF 镜像下载） | 1,620 对 | `data/raw/samples.jsonl`（492K） | 11 级漏斗 + 脏/净双索引 R@1 |
| 中文维基 | 真实语料，爬取原文 | **302,002 篇**（401M） | `data/raw/text_corpus.jsonl` | 8 级文本漏斗 302,002→181,980 |
| 金融新闻 | 真实线上采集（akshare，每日飞轮） | 214 条/日 | `data/raw/finance_news/` | 日报 + 巡检 + 漏斗（保留 76.2%） |

而且真实轨**已经抓出过真问题**，这正是它的价值：
- 维基真实语料暴露 `chinese_ratio` **空白膨胀误杀 773 条**（爬虫抽取缺陷导致空白占比中位 77.2%）
  → 修算子语义（去空白后计占比）→ 保留率 **61.5% → 98.3%**（工程笔记 #65）
- 金融新闻首日审计抓到 **46 条域误杀**（阈值-域不匹配），进 `docs/ops_backlog.md`

**所以真正空的不是"真实数据"，是这两件事：**

1. **医疗 / 工业两个域只有合成语料**——PROOF_CHAIN §五已如实降级声明；
2. **没有任何一个域拿到过"真实数据 + 自带脏标签"**——现有真实轨只能算**误杀与域校准**，
   算不了召回与收益（真实数据没有 ground truth，这是设计使然：GT 由污染器注入）。
   代价是「配方迁到真实分布后还剩多少」回答不了，而这是 PROOF_CHAIN §五自己承认的
   **最诚实的边界**。

## 二、真实数据按成色分三层（选型判据）

| 成色 | 能算什么 | 固有缺陷 |
|---|---|---|
| **A. 真实 + 自带异常/故障标签** | 召回 **和** 误杀（第一次能在真实底噪上算） | 标签粒度通常是**逐点**，而仓库算子是**一窗一样本** → 必须加"窗口化 + 标签聚合"前置 |
| B. 真实 + 弱质量分 | 只能当评审基线 | 弱分 ≠ 脏标签，不能当 GT |
| C. 真实但干净 | 只验误杀 / 域校准 | 算不了召回、证明不了收益 |

**结论：优先 A 类。** 下面四条按"能补上多少 PROOF_CHAIN 的缺口"排序。

---

## 三、候选清单（按优先级）

### 第 1 优先 · MetroPT-3 —— 唯一能同时给「故障窗口」和「计划检修时间戳」

| 项 | 值 |
|---|---|
| 领域 | `industrial_sensor`（第四模态，已有五算子） |
| 许可 | **CC BY 4.0**（UCI 数据集 791，可自由使用/分发，注明出处） |
| 规模 | 1,516,948 行 × 15 列，**单 CSV** |
| 探活 | `archive.ics.uci.edu/static/public/791/metropt+3+dataset.zip` → **200 OK**（本机实测 ~7 KB/s，慢，挂后台下） |

```bash
# ✅ 实测可用路径（UCI 直链 ~7KB/s 拿不动，改走社区镜像 + ghfast 代理）
curl -sL --max-time 400 -o data/raw/real/metropt3/labelled_df.csv.zip \
  "https://ghfast.top/https://raw.githubusercontent.com/Masa-Tantawy/MetroPT-3-Predictive-Maintenance/main/labelled_df.csv.zip"
python -X utf8 scripts/ingest_real_sensor.py --source metropt3 --unzip
```

> 实测结果：**1,516,948 行 / 15 列 / 2020-02-01 → 2020-09-01（213 天）**，与官方口径一致；
> 附带的社区派生标签 `Airleak`（29,954 行）与官方 4 段故障窗口**逐行 100% 吻合**，
> 且 3 个计划检修时刻附近**全部标 0**（合法静默没被误标为故障）→ **标签可用**。
> 另一个候选 `harveyphm/MetroPT-3-Anomaly-Detection` 的 `Group_14_Clean_Data.csv` 只有
> **59,445 行**（分组子集），**别当全量用**（接入脚本已显式优选全量文件）。
> **采样率实测修正**：UCI 页面自己写了两处矛盾口径（0.1Hz / 1Hz），**实测中位间隔 10 秒
> ≈ 0.1Hz**，1Hz 是错的——按 1Hz 算「256 读数窗」会把时长从 42.7 分钟错成 4.3 分钟。

**为什么排第一。** PROOF_CHAIN §九第 10 条自己写着：

> 工业核心判别（检修 vs 故障）**仅在合成语料验证**；C-MAPSS 计划覆盖不了该 claim，
> 需带检修事件源的数据集。

MetroPT-3 的官方失败报告表**同时给了两种事件**：
- **故障窗口**：4/18、5/29–5/30、6/5–6/7、7/15（空气泄漏 / 油泄漏）
- **计划检修时刻**：`Maintenance on 30Apr at 12:00`、`Maintenance on 8Jun at 16:00`、`Maintenance on 16Jul at 00:00`

这正是 `fault_vs_maintenance` 算子缺的**阴性面**——检修造成的合法静默，算子**不该**报故障。
现在这个 claim 只有合成语料背书，接上它才有真实样本。

- **能验证**：`drift` / `range` / `stuck` / `unit` 在真实传感器底噪下的误杀率；
  `fault_vs_maintenance` 的**两个方向**（故障窗口 = 正样本；检修窗口 = 合法负样本）
- **验证不了**：仍然**没有逐点异常标签**（官方明说 the dataset is unlabeled，异常要从失败表推导），
  故障窗口只有 3–4 段 → 召回的分母极小，**只够验误杀与判别，不够验召回**
- **接入要写**：一个前置脚本（CSV → 按窗切片 → `SensorSample` canonical JSON + `labels`），
  参考 `src/mm_curation/data/sensor_synth.py` 的样本构造约定，约 100 行
- **坑**：UCI 页面自身对采样率有**两处不一致口径**（0.1Hz / 1Hz），接入前用文件时间戳实点为准，
  别照抄描述

### 第 2 优先 · SKAB —— 逐点标签最规范，体量最小

| 项 | 值 |
|---|---|
| 领域 | `industrial_sensor` |
| 许可 | **GPL-3.0**（`api.github.com/repos/waico/SKAB` 实查）；只当评测数据，**不要入库/分发** |
| 规模 | 34–35 个 CSV，**37,401 点**，8 传感器，仓库 34 MB |
| 探活 | `codeload.github.com/waico/SKAB/zip/refs/heads/master` → **200 OK**（本机实测 ~39 KB/s） |

```bash
git clone --depth 1 https://github.com/waico/SKAB data/raw/real/skab
```

列结构（官方文档核对）：`datetime / Accelerometer1RMS / Accelerometer2RMS / Current /
Pressure / Temperature / Thermocouple / Voltage / RateRMS / anomaly / changepoint`

- **能验证**：`drift`（真实工况漂移）、`range` / `stuck`（真实传感器故障）；
  `changepoint` 标签可以直接测「工况切换 vs 故障」的边界；每个文件是**一次独立实验**，
  天然适合做"逐次实验"的稳定性统计
- **验证不了（重要）**：SKAB 的异常**本身就是人为切换阀门制造出来的**（"after a while by
  switching valves, anomalies are injected"）。所以它给的是"操作引起的真实异常"，
  **提供不了"合法操作不该报警"的阴性面**——别指望它替代 MetroPT-3
- **接入要写**：CSV → 窗口切片 + 标签聚合（任一 `anomaly=1` 点 → 该窗标脏），同一个前置脚本
  可以顺带支持（与 MetroPT-3 结构高度相似）
- **坑**：正负样本极不均衡（每个文件只有一段异常）

### 第 3 优先 · MIMIC-IV Clinical Database Demo —— 医疗唯一免授权的真实数据

| 项 | 值 |
|---|---|
| 领域 | `fhir_resource`（第三模态，已有五算子） |
| 许可 | **ODbL 1.0**，**免 credentialing**（100 名患者子集） |
| 规模 | 解压约 73 MB |
| 获取 | `https://physionet.org/content/mimic-iv-demo/2.2/` → 页面点 "Download the ZIP file"（探活 200 OK） |

- **能验证 4 / 5 个医疗算子**：`code_validity`（ICD-9/10、LOINC 真实编码分布）、
  `unit_normalization`、`temporal_consistency`（真实表间时间关系）、
  `referential_integrity_fhir`（真实主外键）
- **验证不了 `phi_residual`**：demo **明确不含自由文本病历**（"excludes free-text clinical
  notes"）→ PHI 检测没有真实靶子，这条只能继续靠合成。**这是本次选型里最需要提前知道的缺口。**
- 它也不是 FHIR 原生格式（是一堆 CSV 关系表）→ 要写一层 CSV → FHIR R4 映射，
  或者退一步只测"真实表结构下的引用完整性"
- 全量 MIMIC-IV 需要 PhysioNet credentialing（CITI 培训报告 + 推荐人 + 逐个数据集签 DUA），
  周期数天到两周，**不可分享**——demo 是唯一低成本入口

### 备选 · C-MAPSS（工业 RUL）

```bash
# GitHub raw/codeload 不通时用 ghfast.top 代理（本机实测可用）
curl -sL -o data/raw/real/cmapss/train_FD001.txt \
  "https://ghfast.top/https://raw.githubusercontent.com/Ekkohng/CMAPSSData/master/train_FD001.txt"
```

- 优先级最低，两个原因：**① 它是仿真数据不是真实数据**（C-MAPSS 动力学模型生成）；
  **② 无检修事件源**，PROOF_CHAIN 已声明它覆盖不了核心 claim
- **别搞混**：NASA S3 上的 *Turbofan Engine Degradation Simulation Data Set **2*** 是另一套
  （真实飞行条件下的 run-to-failure），实测 `Content-Length ≈ 15.7 GB`，与经典 C-MAPSS
  不是同一个东西

---

## 四、建议的第一次跑法（选 MetroPT-3，一次做完整链条）

不要零散地"下载看看"。做法：

1. 下载 + 解压（命令见上）
2. 写 `scripts/ingest_metropt3.py`：CSV → 按读数窗切片 → `SensorSample` canonical JSON
   → `data/raw/real/metropt3/windows.jsonl`（`labels.fault` 来自故障窗口，
   `labels.maintenance` 来自检修时刻）
3. 加 `configs/funnel_metropt3.yaml`：复用现有 5 个工业算子，**阈值先一行不改**
   —— 这一步的价值正是看"合成语料标定的阈值迁到真实分布会不会爆"
4. 跑漏斗 + 出报告，把结果与合成语料的对比写进 PROOF_CHAIN §五，**退步也照实写**
5. **如果误杀暴涨，这就是最有价值的产出**——复现维基 #65 那次"真实数据照出机制错"的模式

预计工作量：一个前置脚本 + 一个 config + 一次报告，**不含调阈值**。

## 五、下载纪律（本仓约定）

- 真实数据一律落 `data/raw/real/<name>/`，**不入库**（`data/` 已在 .gitignore）
- 每个数据集写清**许可与出处**；SKAB 是 GPL-3.0，尤其注意只在本地当评测数据用
- 引用任何数字必须标注「合成」还是「真实」——接完后同步在 PROOF_CHAIN §七 的分母映射表加行
- 网络实测：本机当前到 UCI / GitHub 的批量下载都很慢（7–39 KB/s 量级），
  大文件挂后台下；`git clone --depth 1` 比整包 zip 稳
