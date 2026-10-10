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
  + `warehouses/`（dbt）+ 6 份新文档（NARRATIVE / STUDIO / DATASET_PRODUCTION / GAP_ANALYSIS / PLATFORM_ROADMAP / 评审两件）
  + **`datasets/`（15 文件 / ~2.4 MB）**——它是本轮的**交付物本身**
  （README 的「可直接训练的数据集」），且 `claims.json` 的 `cmp_raw_leaks` 就指向
  `datasets/news_zh_v1_raw/manifest.json`；与 `benchmarks/` 同地位，属入库资产。
- **不入库**：`data/studio/`（Studio 网页版的运行时产物）。本轮补 `.gitignore` 规则
  ——此前它**没有任何规则覆盖**（`data/processed/` 被忽略了，但 `data/studio/` 没有，
  又一次「忽略某个子目录 ≠ 忽略整棵树」）。
- **清掉**：根目录 3 个 0 字节事故文件（`=` 与两个 heredoc 写错位置留下的乱码名文件）。

## 四、下一个人需要知道的前提

1. **基线只能改 `claims.json`**，改完跑 `verify_claims.py` 会列出全部待同步文档；
   `literal` 与 `must_contain` 必须一起改（只改一边撞另一侧红）。
2. **`datasets/` 入库后会随重建变化**：`scripts/build_dataset.py` 重建会改
   parquet 字节 → 产生 diff。若不想每次重建都提交，需在此明确「数据集是快照还是产物」。
   **当前决定：快照入库**（它是对外可展示的交付物）。
3. **未完成未变**：ROADMAP 第 3 项 Agent 编排仍在建；
   「已被注册但长期未复核的 claim 主动标黄」这层棘轮未做；`make verify-claims` 与
   dbt 门禁仍未接 CI（`gate-ci.yml` 已入库，但只覆盖 verify_claims + 5 变异 + 3 静态门禁）。
4. `test_dedup_fast.py` 是**唯一一个被 zcode 改过但未同步基线的测试文件**——
   下次改测试后务必 `--junitxml` 实点回写。
