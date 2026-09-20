# 产品级差距审计（GAP AUDIT）

> 2026-09-18。回答一个问题：**这个项目离"真实可用的产品级项目"还差什么。**
> 本清单每条都**实点过**（附验证命令），不引用记忆、不引用文档自述。
> 分级：P0 会立刻咬人 / P1 产品化定义性缺口 / P2 真实分布适用性 / P3 门面与协作。

---

## 总判断（先给结论）

分两层说，因为这两层的答案完全不同：

**作为求职作品集 / 研究原型：已经远远超过"课程项目"，处于少数头部区间。**
273+49 测试用例、双 CI 工作流（代码门禁 + 数据门禁）、污染器注入 ground truth 的评测闭环、
claims.json 数字版本锁定（漂移即 exit 1）、PROOF_CHAIN 证明链 + 十问索引 + 十条已承认局限。
这套"不给自己辩护的证据体系"，比绝大多数简历项目的代码量都难做。

**作为真实可用的产品：缺口不在代码量，在三类东西——**

1. **产品外壳完全没有**：无鉴权、无配额、无部署镜像、无版本发布、无统一 CLI、无审核队列。
   现在的形态是"能在一台开发机上跑通的完整实验装置"，不是"别人能用的东西"。
2. **真实分布的适用性没有被证明过**——而且今天的数据说明**它当前不成立**。
   合成语料上 100% 召回 / 1.23% 误杀，迁到三个真实数据集变成 **24%~72% 召回 / 16%~28% 误杀**。
   如果对外宣称"能清洗工业传感器数据"，这个落差就是产品的核心风险，不是调参问题。
3. **规模只有 100 万档的实测**，执行器签名是 `run_funnel(samples: list[Sample])`——
   全内存批处理，没有流式、没有分块、没有断点续跑。

一句话：**这是一个证据链很硬的实验装置，还不是一个产品。缺的不是功能，是"产品"这个外壳本身，加上"真实分布下确实有效"这个证明。**

---

## 一、数据到底在哪个文件夹（直接回答）

根目录：**`C:\Users\10393\Desktop\mm-curation-pipeline\data\raw\real\`**

```
data/raw/real/
├── skab/                      14 MB, 38 文件
│   └── SKAB/data/
│       ├── valve1/   16 个 CSV   （逐点异常标签，人为切阀造异常）
│       ├── valve2/    4 个 CSV
│       ├── other/    14 个 CSV
│       └── anomaly-free/anomaly-free.csv   ← 唯一全干净长序列（误杀靶子）
│   └── windows.jsonl (3.3 MB) / windows_w64.jsonl (6.0 MB)   ← 窗口化中间产物
│
├── metropt3/                 332 MB, 4 文件
│   ├── labelled_df.csv        207 MB  ← 全量 1,516,948 行 × 213 天 × 7 通道（主力文件）
│   ├── labelled_df.csv.zip     25 MB  ← 原始压缩包，可留可删
│   ├── metropt3_group14.csv   8.4 MB  ← 只有 59,445 行的分组子集，**别当全量用**
│   └── windows.jsonl          107 MB  ← 窗口化中间产物（41478 条样本）
│
└── cmapss/                    38 MB, 5 文件
    ├── train_FD001.txt    3.5 MB / test_FD001.txt  2.2 MB / RUL_FD001.txt
    ├── train_FD003.txt    4.2 MB  ← 多工况版本（本轮未评测，留作对照组）
    └── windows.jsonl       29 MB  ← 窗口化中间产物（29757 条样本）
```

**评测报告在** `data/reports/`：`real_skab.{json,md}`、`real_skab_w64.{json,md}`、
`real_metropt3.{json,md}`、`real_cmapss.{json,md}`。

**⚠️ 立刻需要注意的一件事**：`data/raw/real/` **没有被 .gitignore 忽略**（实测
`git check-ignore` 无输出，`git status` 里它是 `?? data/raw/real/`）。
`.gitignore` 只忽略了 `data/raw/*.jsonl`、`data/raw/images/` 等特定路径，没覆盖 `real/`。
含义：**任何一次 `git add -A` 都会把 383 MB 数据提交进仓库**（本地协作规范禁止 `-A`，
但外部协作者或 AI 未必遵守）。见 P0-1。

**重新下载**：命令已实测写在 `docs/REAL_DATA_PLAN.md`。当前唯一可用的大文件通道是
`ghfast.top` 代理（GitHub raw / codeload 本机完全不通）。

---

## 二、P0 —— 会立刻咬人的（建议今天就处理）

### P0-1. 383 MB 真实数据没有被 git 忽略

- **证据**：`git check-ignore -v data/raw/real/metropt3/labelled_df.csv` 无输出；
  `git status --short` 显示 `?? data/raw/real/`；`.gitignore` 只列了 `data/raw/*.jsonl` /
  `data/raw/images/` / `data/raw/manifest.json` / `data/raw/.cache/` / `data/raw/text_cache/`。
- **痛点**：一次误操作就把仓库膨胀到不可用，且 GitHub 单文件 100 MB 上限会直接**拒绝推送**
  （`labelled_df.csv` 207 MB）。这是"协作规范靠自觉"和"仓库物理约束"之间的敞口。
- **补法**：`.gitignore` 加 `data/raw/real/`，并在 RUNBOOK 写明"真实数据一律脚本重下，不入库"。

### P0-2. 真实数据接入/评测脚本零测试

- **证据**：`grep -rl "ingest_real_sensor\|eval_real_sensor" tests/` 无结果。
- **痛点**：这两个脚本恰好是**踩坑最密集**的地方，当日实测踩出三处：
  ① SKAB 官方 README 写 `RateRMS` + 逗号，实际是 `Volume Flow RateRMS` + **分号**；
  ② MetroPT-3 采样率 UCI 页面自相矛盾，实测 0.1Hz（按 1Hz 算窗口时长错 10 倍）；
  ③ 同目录 59,445 行子集与 151 万行全量混放会重复计数。
  这三点**都是回归测试该锁的东西**，现在全部只靠代码注释和文档记性。
  另外 `_sampling_hz()` 从时间戳实测采样率是个**关键正确性逻辑**，无测试 = 无保护。
- **补法**：加 `tests/test_ingest_real_sensor.py`，用小 fixture 锁分号解析、
  `_sampling_hz()` 的中位数口径、全量文件优选逻辑、`modes/labels` 窗序号索引
  （这个曾 IndexError 一次）。**不需要真数据**，造 10 行 CSV 就够。

### P0-3. 文档主入口在本机不可用

- **证据**：`which make` → 不存在；`which python` → WorkBuddy 的 3.13.14（`import numpy` 失败）。
- **痛点**：`Makefile` 有 35+ 个目标（`make data` / `funnel` / `eval-industrial` /
  `verify-claims` / `repro` / `serve` …），README 与 RUNBOOK 把它们当主入口。
  但本机 Git Bash 没有 `make`，而且 Makefile 里写的是裸 `python`——**即使有 make，
  也会调起无 numpy 的 3.13**。新人 clone 下来第一分钟就卡住，且报错方向是误导的
  （"ModuleNotFoundError: numpy" 而文档明明说装了）。
- **补法**：短期在 README/QUICKSTART 顶部写清"本机无 make，用等价 python 命令"
  （QUICKSTART 已做，README 还没）；中期把 Makefile 的 `python` 换成 `$(PYTHON)` 变量
  并在 README 给出 `PYTHON=C:/Program Files/Python311/python.exe` 的用法；
  更彻底是加 `[project.scripts]` 统一 CLI（见 P1-8）。

---

## 三、P1 —— 产品化的定义性缺口（有这些才叫产品）

### P1-1. 服务层零鉴权、零配额、零限流

- **证据**：`grep -niE "auth|token|api_key|rate|limit|Depends" src/mm_curation/serving/api.py`
  **完全无匹配**。
- **痛点**：`/api/search` 能返回索引内图片，`/api/ingest` 能写入。现在只能 localhost 自用。
  一旦暴露到公网就是**开放的向量检索 + 数据出口**，而且没有任何用量上限——
  `top_k` 上限 100 有，但请求频率无限制，单个客户端可打满。
- **补法**：最简可用版 = 一个静态 Bearer token + 每 IP 令牌桶；再往上才是用户体系。

### P1-2. 全内存批处理，没有流式/分块/断点续跑

- **证据**：`pipeline/runner.py` 的签名是 `def run_funnel(samples: list[Sample], ...)`；
  `scripts/ingest_real_sensor.py` 的 `_read_csv()` 把整个 CSV 读成 `list[dict]`
  （207 MB CSV → 内存里数倍放大）。
- **痛点**：
  - **内存墙**：样本总量必须装进内存；MetroPT-3 全量已要 107 MB 的 windows.jsonl，
    真实生产数据是线性增长的。
  - **无断点续跑**：跑到一半崩了只能从头再来。150 万行数据跑一次是分钟级，
    但如果接的是 TB 级源，这就是致命伤。
  - **无失败隔离**：单条样本异常会中断整批（这一点代码里可能有兜底，但没有
    dead-letter 机制把坏样本挪出去继续跑）。
- **补法**：生成器化的 `iter_samples()` + 分批落 checkpoint（已处理 offset + 中间统计），
  坏样本进 `quarantine.jsonl`。不需要上 Spark，先把"流式 + 可续跑"做出来。

### P1-3. 没有锁文件，依赖全是范围约束

- **证据**：`requirements.txt` 全为 `>=` / `>=x,<y` 范围；无 `requirements.lock` /
  `pip freeze` 产物；`numpy<2` 的理由靠注释解释（"torch 2.2 按 numpy 1.x 编译"）。
- **痛点**：**这是"本地绿 / CI 红"的结构性根源**（这类问题本项目已反复吃过）。
  同一 commit 在不同时间/机器上装出不同版本，测试结果不可复现。
  对一个把"数字可复现"当卖点的项目，这尤其讽刺。
- **补法**：`pip freeze > requirements.lock.txt` 入库，CI 用 lock 装；
  或迁 `uv` / `pip-tools`。低改动、高收益。

### P1-4. 没有应用侧 Dockerfile

- **证据**：`find . -name "Dockerfile*"` → 只有 `docker/Dockerfile.airflow`。
- **痛点**：pipeline 与 FastAPI 服务**没有可交付镜像**。现在"部署"= 在开发机上
  `pip install -e .` 然后 `make serve`。这意味着：无法交付、无法横向扩、环境不可复现。
  雪上加霜的是本机 Docker 拉不到 `registry-1.docker.io`（已记录），所以**镜像只能靠 CI 验**，
  本地连 build 都验不了——这一条必须配 CI 冒烟才有意义。
- **补法**：一个多阶段 Dockerfile（base = python:3.11-slim + CPU torch），
  CI 里加"build 成功 + 容器内 `/healthz` 200"冒烟步骤。

### P1-5. 协议包没有版本声明，schema 演进无法协商

- **证据**：`packages/curation-eval` 版本 `0.4.0`，但代码里 grep 不到任何
  协议版本常量（`PROTOCOL_VERSION` / `SCHEMA_VERSION`）。
- **痛点**：这个包被定位为"协议与算子 SDK 的单一事实源"，主仓 `pip install -e` 依赖它。
  一旦 Sample 协议要破坏性变更（比如加必填字段），**消费方无法检测、无法拒绝**，
  只会在运行时以奇怪方式崩掉。
- **补法**：`curation_eval.PROTOCOL_VERSION` 常量 + `Sample` 校验时比对；
  加上"不兼容变更必须升 major"的约定写进 DOMAIN_PACKS.md。

### P1-6. 没有版本发布与变更记录

- **证据**：主仓 `pyproject.toml` 写作 `version = "0.1.0"`，而项目已推进到 V5；
  无 git tag；无 CHANGELOG；根目录治理文件只有 `LICENSE` + `Makefile`。
- **痛点**：外部使用者无法回答"我拿到的是哪一版、改了什么、能不能升级"。
  对个人项目不是硬伤，但对"产品级"是定义性缺口。另外 `0.1.0` 与 V5 的落差本身
  就会让看仓库的人怀疑文档真实性。
- **补法**：`version` 对齐到 `0.5.0`，打 tag，加 `CHANGELOG.md`（按 V1..V5 补历史）。

### P1-7. 没有人工审核队列——而这恰好是真实轨的必需品

- **证据**：PROOF_CHAIN §九-8 自认「override 无审批流，只有落盘留痕」；
  服务层无审核相关端点。
- **痛点**：**今天的数据把这个缺口从"可以后补"变成了"必须有"**。
  MetroPT-3 上 `sensor_stuck` 的 1337 次丢弃按官方标签 100% 是"误杀"，
  但人工抽检发现是 **7 段连续区间共 163 小时的 7 通道同时完全冻结**（真实缺陷，
  标签体系不覆盖）。这说明：**真实轨的误杀率只能是上界，最终裁决必须靠人。**
  没有审核队列，"可用的清洗产品"就缺了闭环的最后一步。
- **补法**：funnel 产出 `review_queue.jsonl`（通道 + 时间 + 读数极值 + 触发算子 + 分数），
  Streamlit 做一个裁决页，裁决结果回写作为新一轮的弱标签。

### P1-8. 50 个脚本 + 35 个 make 目标，没有统一 CLI

- **证据**：`ls scripts/*.py | wc -l` → 50；Makefile 35+ 目标；`pyproject.toml`
  无 `[project.scripts]`。
- **痛点**：入口靠"记"和"找"。今天要跑真实数据，我得先知道
  `scripts/ingest_real_sensor.py --source metropt3 --unzip` 这种参数组合。
  对使用者而言这是最大的摩擦面，也让文档永远追不上脚本。
- **补法**：`mmc ingest --source …` / `mmc eval --source …` / `mmc verify`，
  scripts/ 保留为内部实现。**改动不大，观感提升巨大。**

### P1-9. 没有端到端"外人可验"的冒烟路径

- **证据**：`docs/QUICKSTART.md` 三条路线：A 合成门禁（免下载免 GPU，✅ 最友好）、
  B 喂自己的 jsonl、C 官方图文全流程（要下载 + GPU）。
- **痛点**：路线 A 验的是合成闭环，**验不到今天暴露的真实分布问题**；
  路线 C 门槛高（GPU + 数百 MB 下载）。缺一条"真实数据 + 纯 CPU + 十分钟内跑完"的路径——
  而这恰恰是现在最有信息量的一条（比如 MetroPT-3 的 151 万行用纯 CPU 都能处理）。
- **补法**：加"路线 D：真实工业传感器小样本"（MetroPT-3 前 5 万行，纯 CPU，约 2 分钟），
  让任何人十分钟内自己看到"合成 100% → 真实 24%"这个落差。

---

## 四、P2 —— 真实分布适用性（今天刚被打出来的）

这一节的特殊性：**它不是"缺功能"，而是"已有功能的正确性在真实数据上不成立"。**
对产品而言这比缺鉴权更严重。

### P2-1. 五个工业算子的判据全部对着合成形态写

| 算子 | 合成判据 | 真实数据里为什么空转 |
|---|---|---|
| `sensor_stuck` | 窗内极差 **≤1e-9**（完美平坦） | 真实传感器永远有噪声，不可能完美平坦 |
| `sensor_range` | 查**内嵌合成** `RANGE_TABLE` | 真实通道不在表内 → 恒 1.0（空转被报告成"通过"） |
| `sensor_drift` | 同组需 **>5 窗**建基线 | 真实单次实验太短（每通道 2~4 窗）→ 建不出基线 |
| `unit_consistency` | 同测点单位混源 | 单位来自数据集文档统一填，无混源 → 恒过 |
| `fault_vs_maintenance` | **-999 哨兵**静默窗 | 真实数据无缺失值、无哨兵 → 恒过 |

**证据**：SKAB 默认窗（256）下五个算子**全部静默**，0 丢弃 / 0 命中 / 0 误杀，
漏斗保留率 100%。报告：`data/reports/real_skab.md`。

### P2-2. 阈值不可迁移，且调参无解

- **证据**：`sensor_drift` 真实召回 24%~72%、误杀 16~28%，合成是 100% / 1.23%。
  SKAB 上试了 5 种「跳窗 + 放宽 σ」组合，最好的也只是 8σ 把误杀压到 27.4%、召回掉到 34.5%。
- **痛点**：**这不是调参问题，是模型假设问题**——`sensor_drift` 假设
  "单一稳态基线 + 窗均值按固定 σ 倍偏离"，而真实工况的*合法*波动本身就超过合成平稳过程的 σ。
  换句话说，**当前阈值体系无法通过任何单调调参达到可用区间**。

### P2-3. 真实数据回答不了"漏了多少"（没有召回的 ground truth）

- **痛点**：三个数据集都只能给弱标签，且各不相同：
  - SKAB 有逐点标签，但**异常是人为切阀造的**，分布不代表自然故障；
  - MetroPT-3 标签可信（社区标签与官方失败表逐行 100% 吻合，29,954=29,954），
    但**极稀疏**——只有 1.97%，且不覆盖数据链路类缺陷；
  - C-MAPSS 是 **NASA 动力学模型的仿真**，不是真实采集。
- **后果**：**"真实场景下的召回率"这个数字，目前无法测。** 现有的 24%/72%/52%
  都是"在某个数据集自带标签口径下的召回"，不能当业务指标用。
  这是整个项目最硬的一处证据缺口。

### P2-4. 「无标签 ≠ 干净」：真实误杀率只能是上界

- **证据**：MetroPT-3 上那 1337 次"误杀"，实为 **7 段连续区间、共约 163 小时**
  （占 213 天全期 3.19%，最长 61.6 小时）**7 个通道同时完全恒定**，且**全在官方故障窗口之外**。
- **含义**：标签体系不覆盖"数据链路冻结"这类缺陷。任何拿数据集标签直接算出的
  误杀率都是**上界**。

### P2-5. 「同形态多含义」：单窗视角注定搞混

- **证据**：C-MAPSS 上 `sensor_stuck` 误杀 7817 条，其中 **7220 条（92.4%）**
  落在 `s1/s5/s10/s16/s18/s19` 六个通道——它们在 **100/100 台发动机上全程不变**（无信息列）。
- **含义**：「完美平坦」既可能是传感器卡死（故障），也可能是**该通道本来就不测量任何东西**（正常）。
  现有算子只看单个窗，**没有全量上下文，无法区分**。
  → 这直接要求算子能访问"该通道在全数据集上的行为"，是架构级需求，不是参数级。

### P2-6. 合成污染器与真实形态差 15 倍，且缺 4 类真实形态

- **证据**：合成注入率 **30%**，MetroPT-3 真实故障率 **1.97%** → 差约 15 倍。
  缺的真实形态：**噪声平坦**（flakiness）、**采样停摆**（心跳缺失而非哨兵值）、
  **无信息通道**（恒定但合法）、**低脏率**。
- **后果**：**合成门禁永远测不到上面这四类问题**，而且 30% 的注入率会让阈值严重乐观。

### P2-7. 医疗/工业门禁数字全部来自合成

- **证据**：PROOF_CHAIN §九-1 自认；`data/raw/` 下 `fhir_synth/` 44 KB（合成）、
  `sensor_synth` 同理。MIMIC-IV demo 未接入（前置是写 CSV→FHIR 映射层）。
- **含义**：这两条模态线目前**只有"框架能接"的证据，没有"接上真实数据也对"的证据**。

### P2-8. 统计效力与规模

- **证据**：PROOF_CHAIN §九-2/§九-5/§九-9——微调级单 seed 无置信区间；
  held_out 查询仅 119 条（±1pp 波动）；规模停留在 100 万档实测，
  100 TB 是推演不是实测；GPU 推理非位级确定。
- **痛点**：这些都是"诚实标注的局限"而非隐瞒，但对"产品级"而言，
  单 seed / 119 条样本 / 100 倍外推都是硬约束。

---

## 五、P3 —— 门面与协作（不致命，但伤信任）

### P3-1. 门面数字腐烂，四处（今日实测）

| 位置 | 现在写的 | 实点 |
|---|---|---|
| `README.md:76` | `175 + 40` | **273 + 49** |
| `docs/ROADMAP.md:428` | `263+54` | **273 + 49** |
| `scripts/showcase_app.py:383` | 硬编码 `267` | **273** |
| `docs/INTERVIEW.md:436` | 「工程笔记有 59 条」 | **66 条** |

另：`docs/INTERVIEW.md` 的进度表停在 **V3**（V4 医疗 / V5 工业两条线未进叙事）。

**为什么算痛点**：这个项目的核心卖点就是"数字可复现、口径自洽"。
门面数字与仓库现状不符，恰好击中自己最想立的那个 flag。而且这是**第四次**同类问题
（此前 133+34 → 140+40 → 151+40 → 175+40 已错过三次）。

**根治建议**：把测试基线也纳入 `claims.json` + `verify_claims.py` 的校验范围，
让 `make verify-claims` 顺带把 README/ROADMAP 里的数字一起校验。**靠人记必然再错第五次。**

### P3-2. 文档重复与层级混乱

- **证据**：`docs/` 23 个 md；`ARCHITECTURE.md` 与 `ARCHITECTURE_V2.md` 并存；
  `PRD.md` / `OPS_PRD.md` / `SLA_README.md` / `ops_backlog.md` / `tasks.md` / `DEV_PLAN.md`
  职责边界不清；`docs_archive/` 另有 **15 个 md**，散在 7 个子目录里
  （`v2-alpha-protocol/` / `v2-beta-tasks.md` / `v3-theta-studio/` / `v4-alpha-fhir/` /
  `phase2-p1-p4/` / `week3-*/`），归档按阶段分目录但无索引。
  - **顺带一条实点教训**：`ls docs_archive/*.md | wc -l` 只返回 **1**（顶层那一个），
    与真实值 15 差 14 倍——**glob 的层级深度会让"实点"也给出错误答案**。
    正确问法是 `find docs_archive -name "*.md" | wc -l`。
    这条本身值得记进 ENGINEERING_NOTES：**实点也要实点对层级**。
- **痛点**：新人（或下一个 AI）不知道该读哪个，DEV_PLAN 号称"唯一事实源"但
  与 ROADMAP/tasks 存在重叠表述。

### P3-3. 协作靠约定而非机制

- **风险**：规范要求"提交必须 `git add <具体文件>`，禁用 `git add -A`"（因为会吞掉
  协作者的未提交工作）。这条**靠自觉**，而本次 `git status` 里同时有 5 个 M 状态的
  协作方在途文件 + 1 个 `?? data/raw/real/`。
  一次违规操作 = 别人的工作被吞 + 383 MB 数据入库。

### P3-4. `docs/ENGINEERING_NOTES.md` 的笔记编号曾是踩坑点

- 历史上已发生编号撞车（#56/#57 被两处重复使用，事后重编号）。
  目前 66 条，无编号唯一性校验。加一个测试即可（`test_verify_claims.py` 已有同类模式）。

---

## 六、如果只能做三件事（我的优先级建议）

按"投入产出比 × 对'产品级'定义的贡献"排序：

**第 1 件：对齐判据 ↔ 真实形态（P2-1 / P2-4 / P2-5）**
理由：这是唯一一个**"不修就等于产品不成立"**的问题。合成 100% → 真实 24% 的落差，
靠加鉴权、加 Docker、加 CLI 都盖不住。具体三步：
① `sensor_stuck` 判据从「极差 ≤1e-9」改成「变化率低于该通道噪声底」+ **全量上下文**
（该通道是否一直不变 → 是则属无信息通道，不判卡死）；
② `sensor_range` 表外通道改记 `None`（无法计分），**不要用 1.0 伪装成"通过"**；
③ `fault_vs_maintenance` 静默判据从「-999 哨兵」改成「采样停摆」。

**第 2 件：补人工审核队列（P1-7）**
理由：真实轨的误杀率只能是上界（163 小时冻结那个证据），
**最终裁决必须靠人**。没有这一步，"清洗结果能用"这件事无法闭环。
同时也是把"评测脚本"升级成"产品功能"的第一步。

**第 3 件：锁文件 + 真实数据入 gitignore + 数字校验入 claims（P0-1 / P1-3 / P3-1）**
理由：三件都是**小改动、一次性根治**，而且都直接服务于项目自身最想立的 flag
（可复现、口径自洽、数字可信）。加起来可能不到一小时。

**不建议现在做**：鉴权、Docker、多租户、规模优化。
理由：这些是"有用户之后"的问题，而现在连"真实数据上是否有效"都还没答上来。
先证明有效性，再谈可用性。

---

## 七、一句话总结"还差什么"

> 差三类东西：**产品外壳**（鉴权/部署/版本/CLI/审核队列，全部为零）、
> **真实分布的适用性证明**（今天的数据说明当前不成立：合成 100%→真实 24%）、
> **规模化能力**（全内存批处理，100 万档实测，无流式无续跑）。
>
> 而**不缺**的是：工程纪律（273+49 测试、双 CI、claims 版本锁）、
> 证据透明度（十问索引 + 十条自认局限）、以及架构的可扩展性
> （"基座 + 领域增强包"的六件套规范已被三种模态走过一遍，
> 加第四种模态确实做到了零特例接入——这套抽象是站得住的）。

---

## 附：本清单的验证命令（可逐条复核）

```bash
# 数据位置与体积
find data/raw/real -maxdepth 2 -type d | sort
du -sh data/raw/real/*

# P0-1 数据是否被忽略（无输出 = 未忽略）
git check-ignore -v data/raw/real/metropt3/labelled_df.csv

# P0-2 新脚本有无测试（无输出 = 无测试）
grep -rl "ingest_real_sensor\|eval_real_sensor" tests/

# P0-3 make 与默认 python
which make; which python; python -c "import numpy"

# P1-1 服务层鉴权（无匹配 = 完全没有）
grep -niE "auth|token|api_key|rate|limit|Depends" src/mm_curation/serving/api.py

# P1-2 全内存签名
grep -n "def run_funnel" src/mm_curation/pipeline/runner.py

# P1-4 Dockerfile
find . -name "Dockerfile*" -not -path "./.git/*"

# P1-6 版本
grep -n "version" pyproject.toml

# P2-1/2 真实轨结果
cat data/reports/real_skab.md data/reports/real_metropt3.md data/reports/real_cmapss.md

# P3-1 门面数字
grep -n "175 + 40" README.md
grep -n "263+54" docs/ROADMAP.md
grep -n "59 条" docs/INTERVIEW.md
grep -n "^#" docs/ENGINEERING_NOTES.md | wc -l

# 测试基线实点
python -X utf8 -m pytest --collect-only -q
python -X utf8 -m pytest packages/curation-eval/tests --collect-only -q
```
