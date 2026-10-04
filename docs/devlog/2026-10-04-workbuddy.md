# 2026-10-04 · WorkBuddy

> 项目**收敛轮**。用户指令原话：「目前这个项目我想先精简，收敛一下，把目前的开发升级路线
> 收敛到一条线上，部分历史残余文档能删的就删，比如开发计划这些，最终回归到项目最初始的目标，
> 面向 ai 数据开发和，ai 数据工程 jd 的项目」。
> 同时确认：**glm 已收工，不再双AI 并行**。

## 一句话

**34 份文档 → 19 份；七条版本线 → 一条（脏数据 → 可信训练数据）；回归AI 数据开发/工程 JD 定位。**
顺带抓出**基线已被 glm 改到 624 却没同步文档**（门禁精确列出 7 处），以及**两处「门禁自己腐烂」**。

## 做了什么

### 1. 删 30 个文件（`git rm`，历史靠 git 找）

`docs_archive/` 整棵（15）+ `DEV_PLAN` `COLLAB_PLAN` `design_tables` `STRATEGY_V6`
`PRODUCTIZATION` `GAP_AUDIT` `tasks` `ops_backlog` `AI_CODING_PROTOCOL`
`DATA_SYSTEM_TRACK` `DS_DA_TRACK` `INDUSTRY_BENCHMARK` `REAL_DATA_PLAN` `OPS_PRD` `SLA_README`。

判据：这些文档的唯一用途是**多 AI 并行期的过程管理**（泳道、任务拆解、设计门、周计划、
差距清单）。单写者模式下它们全是噪音，且每个都是一处会腐烂的引用源。

### 2. ROADMAP 重写成一条线（601 行 → 66 行）

主线一句话：**把脏数据变成可信训练数据，每一道闸门都能被第三方复跑。**
第二节 = 现在到哪了（四个能力 + 硬证据）；第三节 = **下一步只做这三件**
（叙事收敛 / 数据合成与增强 / 门禁从「文档一致」推到「结论可信」）；第四节 = 明确不追。

**收敛判据（写进 AGENTS.md 当准入门槛）**：这一步有没有让链路上某个原本靠人判断的环节，
变成可复跑的门禁？答不出来就别做。

### 3. README 首屏重写 + 文档索引 13 → 6

- 标题从「多模态数据质量平台：清洗管道 → 向量检索 → 个人微调」改成
  **「脏数据 → 可信训练数据：一条能被第三方复跑的 AI 数据工程链路」**。
- 首屏补一句岗位定位（AI 数据开发 / 数据工程 / 数据平台开发）+ 判据指针。
- 30秒入口表格的 5 行按**数据工程能力**重排（新增「有运行概念的数据系统」一行，
  原「敢报阴性结果」那行降为表下一句话，因为它不是能力）。
- 文档索引从平铺 13 条改成「**你想看什么 → 去哪里**」6 行表格。

### 4. 基线 623 → 624（门禁抓到的真腐烂）

glm 的 `39946ba`（G2 终局修复）加了 1 条 store 搬迁回归测试，**文档全线没同步**。
我改 `claims.json` 的 `baselines` 后，门禁精确列出 7 处待同步
（README / AGENTS / PLATFORM ×3 / PROOF_CHAIN / INTERVIEW_SELFTEST / 1 条 must_contain），
逐个改完 → **65 门面 65 PASS / 0 漂移**。
新基线：**主仓 624 + 包 67 = 691**（`624 passed / 0 failed / 0 error / 0 skipped` 实点）。

### 5. 修两处「门禁自己腐烂」——本轮最重要的发现

| 门禁 | 腐烂方式 | 后果 | 修法 |
|---|---|---|---|
| `mutation_test_claims_gate.py` | 硬编码锚点 `工程发现日志 96 条`，README 改版后措辞变了 | **6个变异里 2 个静默「跳过」**，脚本仍报 `4/6` 但看起来像正常 | 锚点改为**从 `claims.json` 的 `must_contain` 与笔记最大编号现取**；找不到锚点直接 `assert` 失败而不是跳过 |
| `gap_audit_probe.py` | 判据硬指向 `src/mm_curation/serving/api.py`、`tests/ 里 ingest_real_sensor` | 实现搬到 `platform/service.py`、测试改名后，**报出 3 个假的 ❌**（看起来像能力缺失，其实是判据失效） | 判据改为按**能力**搜（`rglob` + 能力关键词），阈值同步提高；并在注释里写明「找错文件比没有门禁更坏」 |

**这类缺陷比缺门禁更危险**：缺门禁你会知道它缺，坏门禁会告诉你「已修」。

### 6. 其他

- `AGENTS.md` 从「双AI 协作须知」重写为「单写者开发须知」：删掉读 COLLAB_PLAN / DEV_PLAN
  的开工步骤，加收敛准入门槛 + `verify_claims.py` 进收工清单。
- 13 个脚本/测试的注释与 docstring 里指向已删文档的引用改到幸存真相源
  （`ENGINEERING_NOTES` / `PLATFORM` / `RUNBOOK`），**含 4 个测试文件的 docstring**（不动测试逻辑）。
- 产品页模板的 GAP_AUDIT 死链换成 ENGINEERING_NOTES + ROADMAP，重新生成
  （正文 26 个数值 **100% 可追溯**）。
- `ENGINEERING_NOTES.md` **故意不改**里面的失效链接（那是历史记录，改= 篡改现场），
  只在顶部加一条说明 + 给 `git show <commit>^:<path>` 的查法。

## 改动的文件

- 新增：无
- 删除（30）：见第一节
- 重写：`docs/ROADMAP.md`、`AGENTS.md`、`README.md`（首屏 + 索引两段）
- 门禁：`docs/claims.json`（facades 74→65、baselines、facade_floor 65）、
  `scripts/verify_claims.py` 未动、`scripts/mutation_test_claims_gate.py`（锚点派生）、
  `scripts/gap_audit_probe.py`（判据改能力导向+ N-5 判据重定义为「README 首屏收敛」）
- 引用修补（13）：`scripts/mmc.py` `ops_daily.py` `ops_install_schedule.py` `judge_studio.py`
  `eval_real_injection.py` `eval_real_sensor.py` `build_product_page.py` `docker/Dockerfile.app`
  `pyproject.toml` `tests/test_lock_file.py` `test_sensor_quality.py` `test_alpha_acceptance.py`
  `test_beta_acceptance.py` + `packages/curation-eval/src/curation_eval/ray_executor.py`
- 文档修补（6）：`docs/FAQ.md` `JD_RESEARCH.md` `PLATFORM.md` `PRD.md` `RUNBOOK.md` `INTERVIEW_SELFTEST.md`
- 生成物：`docs/product.html`

## 质量门（实点，非推断）

```
ruff check .            All checks passed
ruff format --check .   240 files already formatted
pytest 主仓             624 passed / 0 failed / 0 error / 0 skipped
pytest 包               67 passed / 0 failed / 0 error / 0 skipped
verify_claims.py        14 claim 13 PASS / 65 门面 65 PASS / 0漂移 / 2 派生 PASS / 棘轮 0 越界
mutation_test           6/6 拦红
gap_audit_probe         18 项，❌ 4（真缺口，见下）
```

**如实仍开4 项**（探针判据已确认指向真实位置，不是判据失效）：
`P0-3` Makefile 裸 `python` 25 处 · `P1-5` 协议版本常量缺失 · `P1-7` 人工审核队列未做 ·
`P2-5` 滚动基线未做。前三项在 ROADMAP 收敛后优先级下降，未做。

## 下一个人需要知道的前提

1. **唯一事实源变了**：对外数字 → `docs/claims.json`；当前唯一允许推进的事 → `docs/ROADMAP.md` 第三节；
   命令与验收数字 → `docs/RUNBOOK.md`。`DEV_PLAN` / `COLLAB_PLAN` 已不存在，别再找。
2. **门禁改基线的正确顺序**：先改 `claims.json` 的 `baselines` → 跑 `verify_claims.py`
   看它列出哪些文档要同步 → 改文档 → **再把 facade 的 `literal` 与 `must_contain` 一起改**
   （只改文档会撞 `registry-stale`，只改 literal 会撞 `context-stale`）。本次踩了两次。
3. **写变异/审计类脚本时不要硬编码锚点**。从注册表或源文档现取，找不到就 `assert` 失败。
   本轮两个门禁都因为硬编码锚点而静默失效过——这是本项目吃过的第三次同类亏。