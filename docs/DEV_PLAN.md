# 开发计划（活文档）— AI 协作单一事实源

> **本文件是所有开发会话的入口与出口**：
> - 开工：先读本文件「当前状态」与「下一阶段任务」，按 docs/AI_CODING_PROTOCOL.md 走设计门；
> - 收工：**必须**回写本文件的「开发日志」（加一行）并更新任务状态，否则视为本轮未完成。
> 长期愿景与历史背景看 docs/ROADMAP.md；架构决策看 docs/ARCHITECTURE_V2.md；
> 环境与命令看 docs/RUNBOOK.md。

## 当前状态快照（2026-09-03）

| 阶段 | 状态 | 核心数字 |
|---|---|---|
| Week1-4 主线（图文管道） | ✅ | 清洗 R@1 +21%；分层采样再 +18~24%；486 脏数据召回 100% |
| Phase 2 P1-P10 | ✅（P6 暂缓） | 检测器泛化 87.3%；跨集去污染召回 94.4%；PSI 换源告警 0.36-0.66 |
| V2 α 协议收口 | ✅ | Sample 协议 + 注册表元数据 + Executor 抽象；112+29 测试绿 |
| V2 β 文本语料实例 | ✅（commit 73d0227） | 30.2 万维基语料；去重 10 万档 exact 1.0/near 0.97/21s；微调 ppl 差 +7.5%；全量漏斗 302,002→181,980 |
| V2 γ Ray 执行层 | ✅（本会话） | 等价性三口径全过（kept 集/StageStat/逐 id 分数）；10 万档 local 24s / ray 90s；簇代表 id 规范化（跨运行时确定）；包 34 + 主仓 133 测试绿 |
| V2 δ LLM-judge | ✅（诚实阴性） | 机制全链路通；0.5B 判官 κ(t) 全域 ≈0 不合格——kappa 协议把它筛出来（决策 7 实证）；换大模型只改 base_url |
| V3 ζ 个人微调平台 | 🔄 立项（2026-09-03） | 锚点「专属数据判官」：κ 通用 ~0.013 → 目标 ≥0.5；PRD 见 docs/PRD.md（PRD 首次落盘） |
| V2 ε 数据 CI | ✅（本会话） | data_ci_benchmark 门禁：exact 1.0 / near 0.954 / 误杀 0；劣化注入实测变红；双徽章（代码+数据）；损伤强度按 α 方法论标定 |
| 工程门禁（CI / lint / 文档） | ✅ | CI 覆盖包侧测试 + packages lint；ruff pin；README 同步 V2 β |

**测试基线**：主仓库 133 + 包 34 全绿；ruff check 全绿。任何提交不得低于该基线。
**仓库**：github.com/quannie255-star/mm-curation-pipeline（main；推送偶发网络失败，重试即可）。

## 下一阶段任务分解

顺序：**γ → δ → ε**（γ 是 δ 的依赖：LLM-judge 批量推理需要分布式执行层撑成本模型）。
每个任务开工前先按 AI_CODING_PROTOCOL 写设计表进 docs/design_tables.md，经用户确认后再动码。

### γ Ray 执行层（预估 1 周）

| 任务 | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| γ0 | Ray spike：本机 `pip install ray`（Windows 兼容性实测）+ ray.data/map_batches 跑通 1 个文本算子；若 Windows 不可用则改 WSL2 或降级「多进程池执行器」并记录决策 | spike 结论 + 决策记录（走哪个路线） | 0.5 天 |
| γ1 | `RayDistributedExecutor` 实现 α 已有的 Executor 协议（懒加载 import，不装 ray 零依赖可跑；对照 ARCHITECTURE_V2 决策 2 的方案 B 双实现） | 注册后 config 一行切换运行时；不装 ray 的环境全量回归绿 | 1 天 |
| γ2 | shardable 语义落地：分片算子 map_batches 批间并行；非分片算子（text_minhash 等全局视角）单节点汇聚执行 | 8 级文本漏斗在 Ray 下跑通，去重结果与本地一致 | 1 天 |
| γ3 | 等价性验收：同一 config 本地 vs Ray，cleaned.jsonl 逐条一致 + funnel_stats 各级数字相等；双跑耗时对比表（10 万档） | 等价测试绿 + 基准报告落 data/reports/ | 1 天 |
| γ4 | RUNBOOK γ 段 + ROADMAP 回填 + ENGINEERING_NOTES（Ray 踩坑）+ 本文件日志 | 文档齐，推送 | 0.5 天 |

### δ LLM-judge（预估 1 周）

| 任务 | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| δ0 | 设计门：服务形态（vLLM OpenAI 兼容服务，本机 8GB 显存选小模型如 Qwen2.5-1.5B/3B）、抽样协议（L1/L2 高分歧区间优先抽样）、评分 rubric 与解析格式 | 设计表经用户确认 | 0.5 天 |
| δ1 | `LlmJudgeOp`（CostClass.LLM 档，抽样率配置化）：批量调服务 + 分数解析 + 失败降级策略（服务不可用时跳过不阻塞） | 单测（mock 服务）+ 真实服务冒烟 | 1.5 天 |
| δ2 | 一致性评测：judge vs 规则/模型算子的 Cohen's kappa + 分歧样本抽样分析报告 | kappa 报告落 data/reports/，分歧案例进笔记 | 1.5 天 |
| δ3 | 接入漏斗为可选 L3 级（config 开关）+ 验收测试 + 文档四件套 + 本文件日志 | L3 开启时漏斗跑通，关闭时与 β 基线一致 | 1.5 天 |

### ε 数据 CI（预估 3 天）

| 任务 | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| ε1 | 新 workflow `data-ci.yml`：定时/手动触发小规模污染基准（1 万档），断言 P/R 不低于门限（exact ≥0.99 / near ≥0.90） | Action 绿；人为注入劣化能变红 | 1 天 |
| ε2 | 阈值回归门：threshold_scan 关键算子拐点偏移超警戒即失败（防上游换源静默漂移） | 门限逻辑 + 测试 | 1 天 |
| ε3 | README 徽章 + 失败通知说明 + 文档四件套 | 推送后徽章显示绿 | 1 天 |

### ζ 个人微调平台（首战「专属数据判官」，PRD 见 docs/PRD.md）

| 任务 | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| ζ1 | 数据获取器：新闻源爬取（robots 合规/限速/幂等）→ Sample 协议 | ≥2000 篇正文；重跑不重复 | 2 天 |
| ζ2 | benchmark 构建器：域评测集版本冻结 + 防污染去重 + 准入指标 | ≥300 条 + manifest；泄漏检查测试 | 3-4 天 |
| ζ3 | LoRA 微调器：peft + Qwen2.5-0.5B（本机 8GB）+ 实验 ledger | 本机跑通，配置/loss 落盘 | 3-4 天 |
| ζ4 | 评测 runner 串联 + 端到端案例 | κ ≥0.5（对照 0.013）；域外泛化如实报告 | 2 天 |
| ζ5 | 文档四件套 + RUNBOOK + 收官 | 全链路测试绿，一键复现 | 1 天 |

### 候补池（γδε 完成后或穿插）

- 跨模态统一：图像漏斗迁移到 V2 Sample 协议的 text_article 同款注册（债：α 只迁了 12 算子中的文本/通用部分——开工前先盘点）
- ~~INTERVIEW.md 融合 β 新弹药~~ → ✅ 已完成（2026-09-03 第二会话）：见「七、V2：从一条管道到一个框架」；后续 δ/ε 收口时把 κ 与数据 CI 数字补进该节
- P6 规模扩展（暂缓中，见 ROADMAP；注：β 已把语料规模从 1.6k 推到 30.2 万，P6 的紧迫性下降——面试时可直接用 β 的 187 倍规模跨度作答）

## 协作硬规则（任何 AI/开发者必守）

1. **进场顺序**：本文件 → docs/AI_CODING_PROTOCOL.md → docs/ARCHITECTURE_V2.md（相关决策）→ 对应模块代码。
2. **设计门**：非平凡任务先写设计表（design_tables.md）等确认；<20 行 bug 修复可跳过但须简述。
3. **质量门（每次提交前）**：`python -m ruff check .` + `python -X utf8 -m pytest -q --tb=no`（主仓库）+ `packages/curation-eval` 下同样跑 pytest——三者全绿才算完成。
4. **收工回写（强制）**：更新本文件任务状态 + 开发日志加一行（日期/内容/关键数字/commit）；有面试价值的现象写 docs/ENGINEERING_NOTES.md（格式：现象→根因→决策→话术）；阶段级进展同步 ROADMAP.md 进度表。
5. **环境坑速查**（详见 RUNBOOK/FAQ）：用系统 Python 3.11（.venv 已坏）；中文输出加 `-X utf8`；Git Bash 无 make；HF 走 hf-mirror.com 带浏览器 UA；模型权重加载必须走 `mm_curation/gpt2_weights.py` 的本地 safetensors 入口（CVE-2025-32434，禁止直接 torch.load .bin）；JSONL 读取一律 `read_text().split("\n")`（禁 splitlines）。
6. **提交规范**：中文 commit message，首行阶段前缀（如「V2 γ：…」）；数据/模型/报告产物不入库（.gitignore 已配，报告用 RUNBOOK 命令重生成）。
7. **不做的**：真前端、W&B 云依赖、算子市场等长期愿景（见 ROADMAP 末尾）。

## 开发日志（新会话在前，每次开发必加一行）

| 日期 | 会话内容 | 关键数字/结论 | commit |
|---|---|---|---|
| 2026-09-03 | **V3 立项（ζ 个人微调平台）**：与用户对齐产品愿景（个人化「数据→benchmark→模型」三步），确定锚点「专属数据判官」+ 本机算力路线；PRD 首次落盘 docs/PRD.md；ζ1 开工（新闻源爬取） | 通用 0.5B judge κ≈0.013 → 目标 ≥0.5 | 见 git log |
| 2026-09-03 | **ε 数据 CI + V2 收官**：data_ci_benchmark.py 合成语料门禁（exact 1.0/near 0.954/误杀 0，劣化注入实测变红）；损伤强度标定（del=2 时 J 跌破捕获带——门禁测实现不是测生成器）；data-ci.yml 双徽章；RUNBOOK/ROADMAP/README 收官账。另：推送 SSL 证书墙 → schannel 后端修复（RUNBOOK 记录） | 门禁 0.5s 跑完；near 0.954 vs 门限 0.90（4.5pp 余量）；V2 五阶段全落地 | 见 git log |
| 2026-09-03 | **γ Ray 执行层完成（γ0-γ4）**：spike 定路线（Win 原生 ray + 对象列传输协议）；RayDistributedExecutor + 共享批量语义函数 + runtime 配置；等价性测试 5 条；10 万档双跑基准。γ3 首跑失败（条数相等但集合不等——去重簇代表依赖块序）→ 批量算子 id 规范化排序修复（同场发现协作方未提交的 CI 门禁变更，合并提交）。笔记 #51-53；spike 临时脚本已删 | 等价性：kept 集/StageStat/逐 id 分数全等；10 万档 local 24.0s / ray 89.6s（含 init 7.8s）、kept 46,122；包 34 + 主仓 133 绿 | 见 git log |
| 2026-09-03（协作方·第二会话） | INTERVIEW.md 升级到 V2：新增「七、从「一条管道」到「一个框架」」（升级动机 / 三张杀牌：α 协议收口+反向吃狗粮、β 第二模态零特例接入、γ 执行层可替换 / 5 条 V2 追问预案 / 一句话收尾）；同步电梯演讲（补 V2 一拍）、STAR 结果表（+4 行 V2 数字）、技术栈表、简历 bullet（+4 条 V2）；工程笔记条数表述对齐 53（README 同步 50→53）。**避让原则**：未触碰 README 主体与任何代码文件，δ 状态与测试基线留给 δ 负责人回填 | 文档口径与 ROADMAP β/γ 数字全面对齐；质量门复核：主仓 140 + 包 40 全绿（含未提交的 δ 测试）、ruff check/format 干净 | 本次提交 |
| 2026-08-31 | β T0-T7 收官：T6 微调首跑阴性（+0.4%）→ 损伤集重造（UTF-8→GBK 乱码/复读/重复字符/截断，剂量 100%）重跑达标；30.2 万全量漏斗；文档回填；工程笔记 #44-50 | 去重 10 万档 exact 1.0/near 0.97/21s；微调 clean 7.16 vs dirty 7.70（+7.5%）；漏斗 302,002→181,980（保留 60.3%）；129+29 测试绿 | 73d0227 |
| 2026-08-下旬 | β T0-T5：语料接入、4 污染器 + 6 算子对靶修复、perplexity（GPT-2 zh 权重入口化）、dedup_fast 三 bug 修复（U+2028/模板簇撑破 LSH 桶/union-find 合并方向） | near 召回 0.42→0.9714（合并方向修复是决定性一跳）；CVE 墙→ensure_local_gpt2() | 前序提交 |
| 2026-09-03 | 建 DEV_PLAN.md + AGENTS.md 协作入口；tasks.md β 状态对账 | — | 67da7fc |
