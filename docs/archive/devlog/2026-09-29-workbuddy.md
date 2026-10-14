# 2026-09-29 · WorkBuddy

> 上一轮（`00504d9` / `f08155b`）的对象在事故中丢失，本文件既是新一轮的记录，
> 也是那两个提交**按工作区重新落地**的说明。

## 一、事故恢复（`.git` 对象库被外部清空；glm 已在 `9c8b2395` 记事故并做了第一轮恢复）

**我只用加法动作，全程无 `prune` / `gc --prune` / `checkout --`。**

1. **先实点定性，不推断**：`00504d9` 只剩 commit 壳（tree `e482dbfb` 已丢）、`f08155b` 整体丢失
   → **两个提交都不可能按对象恢复**；但 worktree 工作区 = `f08155b` 的树内容，**逐字节完整**
   （`verify_claims.py` 的门面函数、`claims.json` 的 66 条门面、10 个测试全在，md5 与备份一致）。
2. **修两类"假规模"噪声**：陈旧 `multi-pack-index` 让 `fsck` 报 **1205 条**假错 → 删该纯缓存文件后
   **1205 → 13**（剩下 13 条全部是引用已丢失对象，符合预期）；坏 ref 让 `show-ref` **静默截断**
   → 改为逐个 `rev-parse --verify` 点名。（机制见 `ENGINEERING_NOTES` #95）
3. **重建**：ref `workbuddy/doc-frontend` **直接写文件**改指 `main`（`9c8b2395`）；
   `.git/worktrees/mmc-doc-frontend/` **手工重建**（`gitdir` / `HEAD` / `commondir`）
   + `git read-tree HEAD` 重建 index。**没有删/重加 worktree。**
4. **以工作区为准重新落地**：把 glm 的 10 个路径**逐个点名**从 `HEAD` 恢复（= rebase 该做的事，
   只碰 glm 的路径），清掉 `operators/robust.py` 这条重命名残留
   → `git status` **恰好只剩我的 9 个文件**，md5 与改动前**逐一相同**。
5. **修掉"门禁自己锚着的腐烂数字"**：实点 MAIN 主仓 **618** / worktree **623** / 包 **67**
   → `baselines` 与 7 个文档共 10 处字面量同步为 **623 / 67 / 690**。
   616 → 618 那 +2 是 glm R1 的两条回归用例（已实点定位到具体测试），**当时未回写**；M2 再 +5。
   详见 `ENGINEERING_NOTES` #93。

**关于事故根因（诚实边界）**：glm 的事故记录写"凶手未定位"。我这边唯一能补的是一条**时间上
重合的线索**——事故窗口内我在 worktree 里执行过 `git rebase main`，它失败于
`error: could not mark as interactive: No such file or directory`（本机 Git Bash 缺 `interactive`
相关的内部文件，`rebase` 走不通）。但**单一 `git rebase` 失败解释不了 `refs/` 与 `worktrees/`
同时消失**，所以只记为**相关线索，不定性为根因**。可确认的一条是：**`rebase` 在本机不可用**，
以后一律用「以工作区为准重新落地 + 逐个点名恢复对方路径」的手工等价流程。

## 二、实点证据（全部 `--junitxml` / 命令输出落文件解析，不看进度点）

| 项 | 结果 |
|---|---|
| MAIN 主仓 | **618 passed / 0 fail / 0 error / 0 skip**（rc=0） |
| worktree 主仓 | 623 collected / 618 passed / **1 fail + 4 skip = 环境性**（CRLF 与缺 `data/`，见 #94） |
| 包 `curation-eval` | **67 passed**（rc=0） |
| `scripts/verify_claims.py` | **14 claim：13 PASS / 0 DRIFT / 1 历史；66 门面：66 PASS / 0 漂移**（rc=0） |
| `tests/test_verify_claims.py` | **10 passed**（含变异测试 `…mutation_goes_red`） |
| `python -m ruff check .` | All checks passed |
| `ruff format --check src tests scripts dags packages` | **3 红 / 237——全部是 glm 的文件**（见下） |

数字可复现路径：主仓/包各 `pytest -q --basetemp <盘符绝对路径且全新> --junitxml <文件>`；
门禁 `python -X utf8 scripts/verify_claims.py`（claim 半场需 `data/reports/`，本机用目录联接
指向主工作区那份；门面半场**不需要**，故 CI 可跑）。

## 三、交接 glm（3 条，都在它的泳道）

1. **格式门禁又红了（#91 的重演，且这次有 3 个文件）**：
   `src/mm_curation/operators/industrial_quality.py`、`tests/test_platform_jobs.py`、
   `tests/test_platform_modeling.py`（`ruff format --check` 实点 **3 / 237**）。
   **注意 `industrial_quality.py` 正是事故前 glm 正在实编辑的文件**——`2600fc4` 是"按工作区重建"
   出来的，很可能把**未格式化的中间态**一起带进了库。
   → 建议 `ruff format <这 3 个文件>` 单独提交。**在它转绿前，"CI 全绿"不能说。**
2. **`.gitattributes`（#94）**：`core.autocrlf` 是 **system 级 `true`**、仓库无 `.gitattributes`，
   linked worktree 里 `requirements.lock` 被检成 CRLF → `test_lock_is_lf_only_…` 红。
   建议加最小面 `requirements.lock text eol=lf`。**属共享配置域，我没有动。**
3. **CI 里接上 `make verify-claims`**：grep 全仓 workflow，**没有任何 workflow 调用它**
   ——门面门禁目前只在本地/手动跑。（该步骤只需 `docs/` 与工作区文件、**不需要 `data/reports/`**，
   所以 CI 能跑，这正是 M2 的设计目标。）

## 四、本轮我改了什么（13 个文件，全部在我的泳道）

- 重新落地（原 `00504d9`）：`README.md`、`docs/COLLAB_PLAN.md`、`docs/DEV_PLAN.md`、
  `docs/ENGINEERING_NOTES.md`、`docs/PROOF_CHAIN.md`、`docs/devlog/2026-09-28-workbuddy.md`
- 重新落地（原 `f08155b`，M2）：`docs/claims.json`（`baselines` + 66 门面）、
  `scripts/verify_claims.py`（门面校验）、`tests/test_verify_claims.py`（4→10 用例）
- 本轮新增：`AGENTS.md`、`docs/PLATFORM.md`（基线数字同步）、`docs/RUNBOOK.md`（§0.2.1）、
  `docs/ENGINEERING_NOTES.md`（#93–#95）、本文件
