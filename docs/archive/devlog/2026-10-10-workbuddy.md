# 2026-10-10 · 收尾（接手 zcode 未跑完的升级）

> 背景：zcode 在推进 ROADMAP 第 2/3 项（数据合成 / Agent 编排）与配套的数据系统、
> Studio 前端、门禁强化，**额度耗尽、未收尾**——工作区堆着 100+ 未提交文件，
> 且测试基线停在旧值。本轮目标 = **把升级收口成一个可复跑的干净状态并入库**。

## 一、实点（全部现跑，不引用记忆）

| 项 | 命令 | 实点 |
|---|---|---|
| 静态检查 | `python -X utf8 -m ruff check .` | 全绿 |
| 主仓测试 | `pytest -q`（junitxml 取准数） | **876 passed / 0 fail / 0 err / 0 skip** |
| 包测试 | `pytest -q`（packages/curation-eval） | **67 passed** |
| 数字门禁 | `scripts/verify_claims.py` | rc=0：**29 claim / 93 门面 / 2 派生** 全 PASS |
| 绝对路径门禁 | `scripts/no_absolute_paths.py` | rc=0（warehouses/ 15 源文件） |
| YAML 粘连门禁 | `scripts/no_yaml_comment_collision.py` | rc=0（19 个 YAML） |
| 编码卫生 | `scripts/encoding_hygiene.py` | rc=0 |
| 干净检出 | `git archive HEAD` → 临时目录 `--collect-only` | HEAD 600 条（含升级前全部已入库测试） |

## 二、本轮修的**真缺陷：基线腐烂**（门禁本轮拦不住的那一类）

- 声明基线 `871` → 实际工作区 **876**：`test_dedup_fast.py` 从 5 条涨到 **10 条**
  （substring_dedup 的 5 条回归用例），但 `claims.json` 的 `baselines.tests_main`
  与 7 份文档仍写 **871**。
- **为什么门禁没拦**：`verify_claims.py` 保证的是「文档 ↔ 注册表**一致**」，
  它**不跑 pytest**（CI 无 junit），所以「注册表自己落后于工作区」这种漂移
  一个门禁都不报。**这正是「门禁解决一致性、不解决真值性」的又一例**——
  必须靠人手实点回写（本轮用 junitxml 实点，非 `-q`）。
- 处置：`claims.json` 基线 → **876 / 67 / 943**；7 份文档字面量同步
  （README / AGENTS / ROADMAP / PLATFORM / PROOF_CHAIN / NARRATIVE / INTERVIEW_SELFTEST）；
  7 条 facade `literal` + 3 条 `must_contain` 同步；`meta.baselines_note` 补记
  「较 2026-10-09 多 5 条 = test_dedup_fast.py」。改完 `verify_claims.py` rc=0。

## 三、入库范围（本轮 `git add` 的边界）

- **入库**：`src/mm_curation/{agent,synthesis,dataset,studio,text_quality_classifier,operators/substring_dedup}`
  + 对应 `tests/` + `scripts/*_gate.py` / 诊断脚本 + `configs/{news_zh_funnel,detection_slo,pipeline.v3_full_coverage}.yaml`
  + `warehouses/`（dbt）+ 6 份新文档（NARRATIVE / STUDIO / DATASET_PRODUCTION / GAP_ANALYSIS /
  PLATFORM_ROADMAP / 评审两件）。
- **不入库**（本轮补的规则）：
  - `datasets/`（`build_dataset.py` 产物）。**这条我改过一次，是本轮最贵的一课**：
    先把 `datasets/` 一并入库，**本地全绿**；推上去 `gate-ci` **红了**——该工作流的
    断言 1 写着「干净检出里 claim 层必须 0 PASS」，理由是**生成物入库会让「报告」与
    「代码」出现两份真相**；而 `claims.json` 有 6 条指向 `datasets/*/manifest.json`，
    一入库它们就在 CI 上真的被校验，断言应声而红。
    ⇒ **`datasets/` 与 `data/reports/` 同类，属生成物，不进仓库**。
    （判据早就写在 workflow 注释里，是我没读它。已 `git rm -r --cached datasets` 撤回，磁盘文件保留。）
  - `data/studio/`（Studio 网页版运行时产物）。此前**没有任何规则覆盖**
    （`data/processed/` 被忽略了，但 `data/studio/` 没有 —— 又一次「忽略某个子目录 ≠ 忽略整棵树」）。
- **清掉**：根目录 3 个 0 字节事故文件（`=` 与两个 heredoc 写错位置留下的乱码名文件）。

## 四、下一个人需要知道的前提

1. **基线只能改 `claims.json`**，改完跑 `verify_claims.py` 会列出全部待同步文档；
   `literal` 与 `must_contain` 必须一起改（只改一边撞另一侧红）。
2. **`datasets/` 不入库（生成物）**：本机跑 `verify_claims.py` 时那 6 条指向
   `datasets/*/manifest.json` 的 claim 能解析；CI 干净检出里它们走 `--reports-missing skip`。
   **不要为了「仓库看起来更完整」把它提交** —— `gate-ci.yml` 的「claim 层必须 0 PASS」
   断言就是为拦这个而存在的（本轮实测踩中一次）。
3. **未完成未变**：ROADMAP 第 3 项 Agent 编排仍在建；
   「已被注册但长期未复核的 claim 主动标黄」这层棘轮未做；`make verify-claims` 与
   dbt 门禁仍未接 CI（`gate-ci.yml` 已入库，但只覆盖 verify_claims + 5 变异 + 3 静态门禁）。
4. `test_dedup_fast.py` 是**唯一一个被 zcode 改过但未同步基线的测试文件**——
   下次改测试后务必 `--junitxml` 实点回写。

## 五、推送后 CI 抓到三处（两真一假）——**本地全绿，CI 全红**

推 `abcfff6` 后：`gate-ci` ✅、`container` ✅，但 **`CI` 与 `Data CI` 红**。逐条查：

| # | 红在哪 | 根因 | 处置 |
|---|---|---|---|
| 1 | `gate-ci` 断言 1（claim 层必须 0 PASS） | `datasets/` 入库 → 6 条指向它的 claim 在 CI 上真被校验 | `datasets/` 改回不入库（§三）|
| 2 | `CI` → `Run tests (主仓库)` | 新测试 `test_verify_claims_meta.py::test_全量注册表的claim都能被渲染不崩` **没带 `--reports-missing skip`** → 干净检出里 28 条 claim 缺报告 → 门禁默认 `fail` → rc=1 | 测试补 `--reports-missing skip`（`data/reports/` 是生成物，CI 口径就该 skip）|
| 3 | `Data CI` → `断言黄金集存在` | `data-ci.yml` 注释写「黄金集是**入库的**」，但 `.gitignore` 有 `data/golden/` **整目录忽略** → 干净检出里永远没有基准 → 断言必红 | 黄金集按**冻结评测资产**入库：`data/golden/*` + 三个 `!` 例外（`golden_set.jsonl` / `golden_meta.json` / `review_skeleton.json`） |

**假红一条（重要的方法论）**：本地用 `git archive HEAD` 复现 CI 时，
`tests/test_lock_file.py::test_lock_is_lf_only...` 也红了。查证：`requirements.lock`
的 **blob 与工作区都是 LF**（CRLF 计数 0/0），是 Windows 上 `git archive` 做了
**eol 转换**（导出成 CRLF）——CI 是 ubuntu，仍是 LF，不红。
⇒ **「模拟干净检出」有已知偏差，eol 是其一**；它复现的是「缺哪些生成物」，
不是「CI 的字节级环境」。判红前先问一句：这条是不是我的装置造出来的？

**这条记录本身就是结论**：三处里有**两处**是「本地永远不会红」的
（第 1 条本地有 `datasets/`、第 2 条本地有 `data/reports/`）——
**推送后读一次 CI 结果，是这套流程里不可省的一步。**

## 六、（同轮继续）推 `f8466a6` 后仍两处红：上一轮我的「修法」没命中根因

`f8466a6` 推上去后：`gate-ci` ✅、`container` ✅，但 **`CI` #91 与 `Data CI` #78 仍红**。
上一轮第 3 处（黄金集入库）确实修好了，但 `CI` 红的**其实是另一件事**。

取日志的办法（github.com 仍 502，走 api.github.com）：`/actions/jobs/<id>/logs` 会
302 到 Azure Blob，**第二跳绝不能带 Authorization**，否则 `AuthenticationFailed`。

| # | 红在哪 | **真实根因** | 处置 |
|---|---|---|---|
| A | `CI` → `Run tests (主仓库)` 4 failed | `tests/test_studio_backend.py` 4 条端到端测试**真加载 `uer/gpt2-chinese-cluecorpussmall`**（`text_article` 配方含 `perplexity`），CI 干净检出没有 `models/` → `FileNotFoundError: 未找到…本地缓存` | 模块内 `autouse` fixture 注入**确定性桩 scorer**（沿用 `test_text_corpus.py` 的同一注入点 `text_corpus.get_scorer`）。受控实验：把 `models/gpt2-chinese-cluecorpussmall` rename 走后仍 **19 passed** |
| B | `Data CI` → 变异测试 job **6/7** | job 注释写「装 pyyaml 即可，**不拉 torch**」**是错的**：`eval_detection_slo.py` 要 CLIP（~600MB）+ 自训的 `wm_nsfw_cnn.pt`（6MB，**公开源拿不到**）；`agent_routing_gate.py` 要 gpt2-chinese。干净检出**一个都没有** → 基线**崩溃**（rc=1 且 BREACH 0 条），M1–M6 因「装置坏了」假通过，只有 M3 露馅 | 按既有纪律（拿不到产物的门禁不进 CI）**移出 CI、改本地门禁**（RUNBOOK 1.9.3）；`data-ci.yml` 只留冻结黄金集的完整性检查 |
| C | `CI` 第 10 步 `Ruff format check`（**从未执行过**） | zcode 批次留下 **41 个未格式化文件**。该步排在测试之后，而测试一直红 → 被短路，从没跑过 | `ruff format` 清偿（41 files reformatted）|

⚠️ **B 是一条「假完成」**：ROADMAP 第三节第 4 项写「5 个变异测试进 CI」并标 ✅（2026-10-08），
实际**只有 3 个**跑过（gate-ci 那 3 个）。那个 ✅ 是在「脚本本地能跑」之后、
「接进 CI」之前打上的。已：ROADMAP 加更正块、GAP_ANALYSIS 加更正指针、
RESUME / ARCHITECTURE_FOR_REVIEW 同步改数、现象写成 ENGINEERING_NOTES **#99**。

### 本轮实点（收工时）
- `ruff check` ✅ / `ruff format --check` ✅（303 files）/ YAML 粘连 ✅ / 绝对路径 ✅ / 编码卫生 ✅
- gate-ci 的 3 个变异测试：`ci_assertions` **6/6**、`claims_gate` **6/6**、`no_absolute_paths` **4/4**
- `verify_claims.py --reports-missing skip` **rc=0**：29 claim（28 PASS + 1 历史）/ **93 门面全 PASS** / 2 派生 / 覆盖率棘轮 0 越界
- `pytest`（junitxml 实点）：主仓 **876** + 包 **67** = **943**，0 失败 0 错误
- 加 #99 引发 notes_count 98→99 连锁：5 篇文档 + 5 条门面（`literal` 与 `must_contain` **一起**改）

### 给下一个人的话（三条，都会再踩）
1. **`data-ci.yml` 里少一个 job 是有意的**，不是漏了。别看到「5 个变异测试」的说法就往回加 ——
   先读该文件末尾那段移除说明。要真接回去，得先解决「`wm_nsfw_cnn.pt` 公开源拿不到」。
2. **`mutation_test_detection_slo.py` 的基线纪律 `rc0 ∈ {0,1}` 分不清「红」与「崩」**。
   将来若把它接进任何环境，**必须**补一条「输出里出现结论行（`7/7 通过`）」的断言 ——
   否则装置一崩，全部分支都会「通过」。这是本轮的坑，写进 #99 了。
3. `test_studio_backend.py` 现在**不依赖任何模型权重与网络**。若把它改回真加载模型，
   CI 会立刻红 —— 那正是本轮修的东西。
