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
| V3 ζ 个人微调平台 | ✅ 首战达标（2026-09-03） | **钱表：通用 κ=-0.024 → 微调 κ=+0.560（R=0.960，解析率 100%）**，PRD 验收 ≥0.5 达成；PRD 见 docs/PRD.md |
| V2 ε 数据 CI | ✅（本会话） | data_ci_benchmark 门禁：exact 1.0 / near 0.954 / 误杀 0；劣化注入实测变红；双徽章（代码+数据）；损伤强度按 α 方法论标定 |
| 工程门禁（CI / lint / 文档） | ✅ | CI 覆盖包侧测试 + packages lint；ruff pin；README 同步 V2 β |

**测试基线**：主仓库 157 + 包 40（2026-09-03 第七会话实点，`pytest --collect-only` 计数；GPU 忙全量未真跑，新增 5 测单独真跑绿）；ruff check 全绿。任何提交不得低于该基线。
> 基线数字历史上被写错过三次（133+34 → 140+40 → 151+40），根因是凭记忆填而不是点数。**改测试后直接用
> `pytest --collect-only -q` 点数回写**，别沿用上一版的数字。

**仓库**：github.com/quannie255-star/mm-curation-pipeline（main）。
> **推送排障见 docs/RUNBOOK.md §0.1**（两个独立故障：SSL 证书墙 / 凭据助手挂起，现象相似但修法无关）。
> 2026-09-03 已根治凭据挂起（global 层空值重置 + wincred），现在裸跑 `git push` 即可，不需要任何 `-c` 参数。
> 一秒自测：`git ls-remote origin main` 秒回 = 网络与 SSL 正常，push 挂住必然是凭据环节。

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
| ε2 | 阈值回归门：threshold_scan 关键算子拐点偏移超警戒即失败（防上游换源静默漂移）→ ✅ **2026-09-03 第七会话收官**：`scripts/threshold_regression_gate.py` 曲线对冻结基线 `configs/threshold_baseline.json` 逐点比对 | 门限逻辑 + 测试 | 1 天 |
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

- ~~跨模态统一：图像漏斗迁移到 V2 Sample 协议~~ → **盘点结论：已完成，债务记录作废**（2026-09-03 第四会话实点）。
  19/19 个注册算子**全部**已声明 V2 元数据（纯图像 8 / 纯文本 7 / 双模态 4），图像漏斗
  `configs/pipeline.example.yaml` 经 `Sample.from_dict` + V2 Executor 执行——「α 只迁了 12 算子中的
  文本/通用部分」是 α 当时的快照，β/γ/δ 期间已补齐，**候补池记录未同步**。
  盘点脚本：`pytest --collect-only` 之外的快速核对见本条目下方命令。
  **但盘点翻出两个此前没记的真缺口**（已立为候补池前两项）：
  盘点命令（下次核对直接跑，别再凭记忆）：

  ```bash
  python -c "
  import sys; sys.path.insert(0,'src'); sys.path.insert(0,'packages/curation-eval/src')
  import mm_curation.operators
  from curation_eval.registry import available_operator_metas as M
  m = M()
  for n in sorted(m): print(n, sorted(m[n].modalities), m[n].cost_class.name, m[n].shardable)
  print('总计', len(m))
  "
  ```

  1. **图像漏斗 × Ray 等价性未验证**：γ3 只跑了 `configs/text_funnel.yaml`，图像漏斗从未在 Ray 下跑过。
     风险集中——图像侧有 3 个 `shardable=False` 全局算子（md5_exact / phash_near / semantic_dedup），
     而 γ3 首跑失败正是栽在全局去重算子上（text_minhash 簇代表依赖块序）。**phash_near 大概率同坑**。
  2. **ε 数据 CI 未覆盖图像去重**：`data_ci_benchmark.py` 只锁了文本 `dedup_fast.dedup_texts`，
     md5/phash 的召回-误杀没有任何门禁，换源漂移不会被抓住。
- ~~[新] 图像漏斗 × Ray 等价性验证~~ → ✅ 已完成（2026-09-03 第五会话）：设计表入 design_tables.md
  （γ3 三口径 + 新增第四口径 dedup 标记）；实测 **2106 条全量双跑四口径全等**（kept 1608 相等 /
  StageStat 相等 / 逐 id 分数 0 不一致 / dedup 标记 324 条相等），local 65.4s vs ray 137.3s。
  报告 `data/reports/ray_image_funnel_benchmark.{json,md}`；等价性测试 `tests/test_image_ray_equivalence.py`。
  **附带发现**：id 排序约定使簇代表 = 字典序最小 id——注入复制品（id 小于原始图）会成为簇代表，
  与污染模块「种子在前」的输入序约定相反；kept 集质量不受影响（重复各留一张），但 duplicate_of
  的指向会从原始图变为注入样本，审计时需知晓
- ~~[新] ε 门禁扩到图像去重~~ → ✅ 已完成（2026-09-03 第六会话）：`scripts/data_ci_image_benchmark.py`
  锁 md5_exact / phash_near——2800 张合成图（2000 base + 300 exact 字节复制 + 500 near
  裁剪重编码注入）真跑漏斗算子，**exact 1.0 / near 0.994 / 误杀 0/2000 → GATE PASSED**；
  劣化注入（裁剪加重到 10~20%）实测 near 0.152 → **exit 1（门禁会红）**。
  已接入 data-ci.yml。标定花三轮收窄损伤（块状合成图对裁剪比真实照片敏感，
  V1 的 4~10% 参数下 15.6% 超阈，收到 1~5% 后 near 距离 p90=8），门限 0.97
  留 2.4pp 劣化余量。RUNBOOK §1.9.1 / INTERVIEW 杀牌 E 已回写
- ~~INTERVIEW.md 融合 β 新弹药~~ → ✅ 已完成（2026-09-03 第二/三会话）：见「七、V2：从一条管道到一个框架」——已含 α/β/γ/δ/ε 五张杀牌（δ 的 κ 与 ε 的数据 CI 数字已于第三会话补入）+「八、V3 个人微调平台」；**ζ 出 κ 数字后需回填第八节进度表与第七节 STAR 表**
- **README 状态段补 V3 ζ**（立项一句话 + 进度 + ζ 数字）——ζ 收官时由 ζ 负责人回填。当前 README 仍写到「V2 全阶段完成」，未提 V3/ζ；第三会话已先对齐两处口径：测试数 129+29 → 140+40、工程笔记 53 → 55 条（与 INTERVIEW.md/ROADMAP 一致）
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
| 2026-09-03（协作方·第八会话） | **V3 ζ 达标叙事回填 + 工程笔记编号撞车修复**：glm ζ 收官（48ce6fc）后发现**笔记编号冲突**——其 ζ 根因两条（协议错位/completion 窗外）用了 #56/#57，与此前 git push 根因（#56）/phash 合成图（#57）撞号。重编号为 **#58/#59**（保持文件出现顺序），修正 5 处引用（PRD ζ5 / ROADMAP ×2 / RUNBOOK 微调红线 / DEV_PLAN ζ 行）；条数 57→59（INTERVIEW ×2 / ROADMAP / README）。**INTERVIEW 第八节回填**：标题与诚实标注改「ζ 收官」，进度表 ζ0-ζ5 全 ✅，新增验收数字段（通用 κ=-0.024 → 微调 **+0.560**，P=0.706/R=0.960/解析率 100%）与「三次训练的根因链」小节（v1 -0.18 协议错位 → v2 -0.007 completion 窗外 → v3 +0.560，loss 是模型在学『某个』任务的证明）；追问预案补验证句；开篇/STAR/杀牌/技术栈/第九节共 6 处「进行中」语气对齐。**README**：状态行 + 核心结果表加 V3 ζ 行 + 灵魂叙事补 V3 段。ROADMAP 测试基线 151+40 → 157+40（glm 用了 ε2 提交前旧数，实点修正） | δ 阴性 → ζ 达标的完整叙事线（协议筛人 → 微调达标）在 INTERVIEW 成形；笔记编号唯一性恢复；测试基线 157+40 | 本次提交 |
| 2026-09-03（协作方·第七会话） | **ε2 阈值回归门收官（ε 阶段最后一个真缺口）**：实点发现 DEV_PLAN 的 ε2（threshold_scan 曲线回归门）从未实现——ROADMAP 标 ✅ 的 ε2 是另一编号体系（损伤强度标定）。新脚本 `scripts/threshold_regression_gate.py`：扫描曲线 import `threshold_scan.scan_operator`（单一定义源，两处永不漂移），对冻结基线 `configs/threshold_baseline.json` **逐点比对**（recall 偏移 >0.05 / 误杀偏移 >0.02 即红），锁轻依赖双主算子 **minhash_lsh（datasketch）/ phash_near（imagehash）**（blur 要 opencv、clip/semantic 要编码器，不入 CI 门禁）。合成语料沿用两条已标定配方（文本 #49 大词表 / 图像 #57 8x8 随机块 + 1~5% 裁剪）。**门禁绿 13s**；基线曲线健康——minhash 拐点在 0.7→0.75（1.0→0.6125）、phash 在 20 过松时误杀飙 26.5%。测试 `tests/test_threshold_regression_gate.py` 5 条（匹配绿 / recall 漂移红 / 误杀漂移红 / 阈值点缺失红 / 语料 label 契约）。已接入 data-ci.yml（补装 datasketch） | CI 三层锁：代码逻辑 → 单点质量数字 → **整条阈值曲线**；换库版本（imagehash/datasketch）静默漂移先被曲线抓住。测试基线 **157+40**（实点；GPU 忙全量未真跑，新增 5 测单独真跑绿）；ζ3-ζ4 在途文件未触碰 | 本次提交 |
| 2026-09-03（协作方·第六会话） | **ε 门禁扩到图像去重（候补池收官）**：新脚本 `scripts/data_ci_image_benchmark.py` 锁 md5_exact / phash_near——2800 张合成图（2000 base 8x8 随机块放大 / 300 exact 字节复制 / 500 near 裁剪+JPEG 重编码注入）真跑漏斗算子，**exact 1.0 / near 0.994 / 误杀 0/2000 → GATE PASSED（去重 40s）**；劣化注入 --crop 0.80,0.90 实测 near 0.152 → **exit 1**；已接入 data-ci.yml。**标定三轮收窄损伤**：V1 near_duplicate_image 的 4~10% 裁剪参数用在块状合成图上 15.6% 距离超阈 12（块网格被裁剪错位，比真实照片敏感），收到 1~5% 后 p90=8，门限 0.97 留 2.4pp 余量——「先标定生成器再定门限」的图像版实践。RUNBOOK §1.9.1、INTERVIEW 杀牌 E 回写 | 候补池两项全收官；数据 CI 双模态全覆盖；劣化注入红、正常绿；ζ3-ζ4 在途文件未触碰 | 本次提交 |
| 2026-09-03（协作方·第五会话） | **图像漏斗 × Ray 等价性验证（候补池第一项收官）**：设计表入 design_tables.md——γ3 三口径 + 新增第四口径（dedup 标记 duplicate_of 逐 id 相等，只比 kept 集看不出"簇代表换了谁"）。实现 `scripts/ray_image_funnel_benchmark.py`（镜像 γ3 脚本，GPU 算子剔除同套路）+ 等价性测试 `tests/test_image_ray_equivalence.py`（importorskip 守卫）。实测 **2106 条全量双跑四口径全等**（kept 1608 / StageStat / 逐 id 分数 0 不一致 / dedup 标记 324 条），local 65.4s vs ray 137.3s（init 11.0s）。**附带发现**：id 排序使簇代表 = 字典序最小 id，注入复制品（id 小）会成为簇代表而非原始图——与污染模块「种子在前」约定相反，审计 duplicate_of 指向时需知晓（kept 质量不受影响）。**坑**：合成测试图两连败（噪声图与单频光栅在 phash 眼里都互相塌缩），终版 8x8 随机块 + 测试内自校验种子（两两距离 ≥16），工程笔记 #57；ray object store 2GB 在内存紧张时 init 失败，降到 1GB | 四口径全等，图像模态等价性债收掉；测试基线 **152+40** 全绿（GPU 空闲，全量 pytest 已跑）；ζ3-ζ4 在途文件未触碰 | 本次提交 |
| 2026-09-03（协作方·第四会话） | **根治 git push 挂起 + 候补池盘点**：(1) 定位推送挂死为凭据环节而非网络——`git ls-remote` 秒回但 push 无输出直到 timeout(124)；根因是 system 层 `helper-selector` 取不到凭据后 git 回退终端交互询问、stdin 阻塞，且 `-c credential.helper=X` 是**追加非替换**（system 那条仍先执行，故单加无效）。修法：global 层空值重置 + wincred，**裸跑 `git push` 现已可用**，卡住的 57d7470 已推送（7a29bcb..57d7470）。RUNBOOK 新增 §0.1「Git 推送排障」分列 SSL / 凭据两个故障。(2) 候补池盘点：**跨模态统一债务记录作废——19/19 算子 V2 元数据全齐**（纯图像 8/纯文本 7/双模态 4），图像漏斗早已走 Sample+Executor，「α 只迁 12 算子」是过时快照。同时翻出两个真缺口并立为候补池前两项：图像漏斗从未在 Ray 下验证等价性（3 个 shardable=False 全局算子是高风险区，phash_near 大概率复现 γ3 的簇代表块序坑）、ε 数据 CI 未覆盖图像去重。(3) 测试基线实点为 **151+40**（原记 133+34 / 140+40 均错，已加「别凭记忆填」的提示）。工程笔记 #56 | 推送恢复 + 候补池记录与仓库现状对齐；**仅动 docs/**（RUNBOOK / DEV_PLAN / ENGINEERING_NOTES），未触碰任何代码，持续避让 ζ3-ζ4 在途文件；本会话未跑全量 pytest（GPU 被 ζ3 训练占满，7855/8188MiB） | 本次提交 |
| 2026-09-03（协作方·第三会话） | **INTERVIEW.md 升级到 δ/ε/V3**：新增杀牌 D（LLM-judge κ 准入门槛——阴性结果敢讲：κ(t) 全阈值域 ±0.015 / judge vs L1 0.056 / L1 参照 0.565）+ 杀牌 E（数据 CI 门禁：exact 1.0 / near 0.954 / 误杀 0 / 0.5s，劣化注入实测 exit 1）；新增「八、V3 个人微调平台」（立项依据＝δ 阴性结果、差异化定位表、ζ 进度表、V2 资产复用表）；**Q5「为什么不用 LLM-judge」由「ROADMAP 预留接口」改写为「已实装但 κ 验收不合格」**（避免与第七节自相矛盾）；STAR 表补 δ/ε 两行并修复 V2 行的三列表格渲染丢列 bug；简历 bullet +3、技术栈 +5、电梯演讲补 δ/ε/V3 三段；笔记条数 53→55；收尾独立为第九节 | 文档口径与 δ/ε/ζ 现状全面对齐；ruff check All checks passed；**仅动 docs/INTERVIEW.md + docs/DEV_PLAN.md，未触碰任何代码**（避让 ζ3-ζ4 进行中的 `finetune_judge_lora.py` / `benchmarks/builder.py` / `tuning/judge_data.py`） | 本次提交 |
| 2026-09-03 | **V3 ζ 首战闭环（ζ0-ζ5）**：PRD 落盘；新闻爬取器（705 篇，robots/限速/幂等/结构解耦，图集页推荐位混入实测修复）；benchmark 构建器（冻结 300 条 + 三重独立性 + SFT 结构性排除）；LoRA 微调器 + 实验 ledger；评测 runner。**三次训练的根因链**：v1 κ=-0.18（裸 prompt 练/模板考，笔记 #58）→ v2 κ=-0.007（completion 窗外截断，loss 健康的静默任务替换，笔记 #59）→ v3 **κ=+0.560/R=0.960/解析 100%** 达标 | 通用 -0.024 → 微调 +0.560；主仓 157 + 包 40 绿 | 见 git log |
| 2026-09-03 | **V3 立项（ζ 个人微调平台）**：与用户对齐产品愿景（个人化「数据→benchmark→模型」三步），确定锚点「专属数据判官」+ 本机算力路线；PRD 首次落盘 docs/PRD.md；ζ1 开工（新闻源爬取） | 通用 0.5B judge κ≈0.013 → 目标 ≥0.5 | 见 git log |
| 2026-09-03 | **ε 数据 CI + V2 收官**：data_ci_benchmark.py 合成语料门禁（exact 1.0/near 0.954/误杀 0，劣化注入实测变红）；损伤强度标定（del=2 时 J 跌破捕获带——门禁测实现不是测生成器）；data-ci.yml 双徽章；RUNBOOK/ROADMAP/README 收官账。另：推送 SSL 证书墙 → schannel 后端修复（RUNBOOK 记录） | 门禁 0.5s 跑完；near 0.954 vs 门限 0.90（4.5pp 余量）；V2 五阶段全落地 | 见 git log |
| 2026-09-03 | **γ Ray 执行层完成（γ0-γ4）**：spike 定路线（Win 原生 ray + 对象列传输协议）；RayDistributedExecutor + 共享批量语义函数 + runtime 配置；等价性测试 5 条；10 万档双跑基准。γ3 首跑失败（条数相等但集合不等——去重簇代表依赖块序）→ 批量算子 id 规范化排序修复（同场发现协作方未提交的 CI 门禁变更，合并提交）。笔记 #51-53；spike 临时脚本已删 | 等价性：kept 集/StageStat/逐 id 分数全等；10 万档 local 24.0s / ray 89.6s（含 init 7.8s）、kept 46,122；包 34 + 主仓 133 绿 | 见 git log |
| 2026-09-03（协作方·第二会话） | INTERVIEW.md 升级到 V2：新增「七、从「一条管道」到「一个框架」」（升级动机 / 三张杀牌：α 协议收口+反向吃狗粮、β 第二模态零特例接入、γ 执行层可替换 / 5 条 V2 追问预案 / 一句话收尾）；同步电梯演讲（补 V2 一拍）、STAR 结果表（+4 行 V2 数字）、技术栈表、简历 bullet（+4 条 V2）；工程笔记条数表述对齐 53（README 同步 50→53）。**避让原则**：未触碰 README 主体与任何代码文件，δ 状态与测试基线留给 δ 负责人回填 | 文档口径与 ROADMAP β/γ 数字全面对齐；质量门复核：主仓 140 + 包 40 全绿（含未提交的 δ 测试）、ruff check/format 干净 | 本次提交 |
| 2026-08-31 | β T0-T7 收官：T6 微调首跑阴性（+0.4%）→ 损伤集重造（UTF-8→GBK 乱码/复读/重复字符/截断，剂量 100%）重跑达标；30.2 万全量漏斗；文档回填；工程笔记 #44-50 | 去重 10 万档 exact 1.0/near 0.97/21s；微调 clean 7.16 vs dirty 7.70（+7.5%）；漏斗 302,002→181,980（保留 60.3%）；129+29 测试绿 | 73d0227 |
| 2026-08-下旬 | β T0-T5：语料接入、4 污染器 + 6 算子对靶修复、perplexity（GPT-2 zh 权重入口化）、dedup_fast 三 bug 修复（U+2028/模板簇撑破 LSH 桶/union-find 合并方向） | near 召回 0.42→0.9714（合并方向修复是决定性一跳）；CVE 墙→ensure_local_gpt2() | 前序提交 |
| 2026-09-03 | 建 DEV_PLAN.md + AGENTS.md 协作入口；tasks.md β 状态对账 | — | 67da7fc |
