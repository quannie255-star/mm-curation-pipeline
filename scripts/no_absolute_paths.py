"""绝对路径门禁：源 SQL 里不许出现硬编码的盘符路径。

## 为什么需要它（不是洁癖）

本项目已经为这件事付过代价：上游视图里硬编码了
`read_parquet('C:/Users/<用户名>/...')`，结果是**换台机器就彻底跑不起来**，
最后靠改成物化快照才解决。

`warehouses/profiles.yml` 的注释把「路径必须相对」写成了硬约束，
但**写注释不等于有门禁** —— 我曾把验证方式指向一个当时并不存在的
`tests/assert_no_absolute_paths.sql`，也就是**把「没有门禁」写成了「有门禁」**。
这个脚本就是那个缺口。

## 判据与它的边界

**扫什么**：`warehouses/` 下的 `.sql` / `.yml`（不含 `target/` 等生成物），
匹配 `[A-Za-z]:[\\/]Users` —— 即Windows 盘符 + 用户目录。

**不扫什么**，以及为什么：
- 不扫 `scripts/*.py`：那里**必须**有绝对路径（venv 路径、dbt 可执行入口），
  扫了就是误报。判据要覆盖真实风险，而不是无差别地抓字符串。
- 不扫 `target/`：那是生成物，源文件干净它就一定干净。
- 不扫 `.md`：文档里**引用**绝对路径是合理的（本README 就有），
  只有「被执行的代码里硬编码」才是问题。

**为什么不用 dbt 的 singular test**：这个检查针对的是**文件内容**
而不是某张表的行；要它生效就得先把它变成一个模型或宏，纯属绕路。
放在 Python 侧、接进 `scripts/dbt_gate.py` 的静态预检层更直接。

## 变异测试

见 `scripts/mutation_test_no_absolute_paths.py` —— 判据自己腐烂比门禁腐烂更难发现，
所以每条判据都要有一处「注入后必须变红」的验证。
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WAREHOUSES = REPO_ROOT / "warehouses"

SCAN_SUFFIXES = {".sql", ".yml", ".yaml"}
GENERATED_DIRS = {"target", "dbt_packages", "logs", ".git", "__pycache__"}

# Windows 盘符 + 用户目录：`C:\Users\...` 或 `C:/Users/...`
# 用 [\\/] 而不是 \/ ：Windows 上两种分隔符都真实出现过。
ABS_PATH = re.compile(r"[A-Za-z]:[\\/]Users")

# ⚠️ 判据必须跳过注释，否则**在正确实现上就会误判**（2026-10-05 实测）。
# 第一版只匹配路径，命中3 处——但逐条看下去**全是注释**：
#   dbt_project.yml:72  `# ... 硬编码 C:/Users/... ，见 PLATFORM.md`
#   _sources.yml:5      `# 上游那 4 张表在库里是 VIEW，定义是 read_parquet('C:/Users/<用户名>/...')`
#   profiles.yml:5      `# 上游的视图定义里硬编码了 read_parquet('C:/Users/<用户名>/...')`
# 三处都是**在描述这个坑**，而这恰恰是最该写清楚的地方。
# 「门禁红着」会被读成「代码有问题」，于是有人要么删注释、要么加白名单 ——
# 门禁就此失去意义。
#
# 为什么按 `#` / `--` 判断注释是安全的：
# dbt 的 Jinja 模板里 `{{ }}` 与 `{% %}` 会先被求值，剩下的才轮到 SQL 解析；
# 而 YAML 的注释只有 `#`。也就是说，在 `.sql`/`.yml` 里
# **以 `#` 或 `--` 开头（或前面只有空白）的行不可能是被执行的路径**。
# 行尾注释（代码后接 `# ...`）不在此列——那种情况整行都会被检出，
# 属于**该让人看见的**：真在代码里写了带盘符的行尾注释，多半是真问题。
ABS_LINE_COMMENT = re.compile(r"^\s*(#|--)")


def _is_generated(path: pathlib.Path) -> bool:
    return any(part in GENERATED_DIRS for part in path.parts)


def scan(root: pathlib.Path) -> list[tuple[pathlib.Path, int, str]]:
    hits: list[tuple[pathlib.Path, int, str]] = []
    # 文件清单来自 `_scannable_files()`（唯一定义处），不在这里重写过滤条件。
    for path in _scannable_files(root):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if ABS_LINE_COMMENT.match(line):
                # 注释行：可能正是在「描述这个坑」，检出它没有意义
                continue
            if ABS_PATH.search(line):
                hits.append((path, lineno, line.strip()))
    return hits


def _scannable_files(root: pathlib.Path) -> list[pathlib.Path]:
    """`scan()` 实际会读到的源文件清单 —— **过滤条件的唯一定义处**。

    ⚠️ 原来我在这里重写了一遍过滤规则（后缀 + 非生成物目录），
    注释还写着「复制一份就会出现分叉」—— 然后当场就复制了。
    这正是本项目反复栽的形态：**注释说了纪律，代码没遵守**。

    正确做法只有一个：把过滤收进这个函数，`scan()` 也调它。
    分叉的方向恰恰是「把没扫的算成扫过的」= 永远朝假绿的方向偏。
    """
    if not root.exists():
        return []
    out: list[pathlib.Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if _is_generated(path.relative_to(root)):
            continue
        out.append(path)
    return out


def _count_scannable(root: pathlib.Path) -> int:
    """实际会被 `scan()` 读到的源文件数（委托给唯一定义处）。"""
    return len(_scannable_files(root))


def main() -> int:
    # ⚠️ 目录不存在时**不能当成「发现问题」**（2026-10-08 实测踩到）。
    #
    # 当时的写法是直接 `[RED] 找不到 .../warehouses` + return 2，理由大概是
    # 「目录都不在了，肯定哪里不对」。但在 CI 上这个目录曾一度**未入库**，
    # 于是干净检出里它必然不存在 → 门禁**永久红**。
    #
    # 这类「因为扫不到东西而红」是最坏的一种门禁失效：
    #   - 报的是「问题」，实际是「装置不可用」（同 `ENV_ERROR_CODES` 那条纪律）
    #   - 修法不该是「把目录硬塞进仓库」，而该是**分清两件事**：
    #     目录缺失 = 装置不可用（报WARN、退出码 0，让CI 不挡路）
    #     目录存在但有硬编码路径 = 真问题（RED、退出码 1）
    #
    # 为什么这里敢让目录缺失也放行：`warehouses/` 现在**已入库**
    # （8 个源文件），所以真缺它= 有人删了源，那属于别处的 diff 可见变更。
    # 而门禁自己的职责范围只是「源文件里不许有硬编码盘符路径」。
    if not WAREHOUSES.exists():
        print(
            f"[WARN] 目录不存在，本门禁**未执行**（不是「通过」）：{WAREHOUSES}\n"
            "       若这是 CI，请确认 warehouses/ 源文件是否已入库。",
            file=sys.stderr,
        )
        return 0

    hits = scan(WAREHOUSES)
    if not hits:
        # ⚠️ 目录在但**一个源文件都没有** =同样是「装置不可用」，不是「通过」。
        # 只判 `not hits` 会让空目录全绿 —— 这是把「没扫」写成「扫过了」。
        # 判据用**实际扫过的文件数**，不用目录是否存在。
        n_scanned = _count_scannable(WAREHOUSES)
        if n_scanned == 0:
            print(
                f"[WARN] {WAREHOUSES.name}/ 存在但没有可扫的源文件，"
                "本门禁**未执行**（不是「通过」）",
                file=sys.stderr,
            )
            return 0
        print(
            f"[OK] 绝对路径门禁：{WAREHOUSES.name}/ 下 {n_scanned} 个源文件，"
            "未发现硬编码盘符路径"
        )
        return 0

    print(f"[RED] 绝对路径门禁：发现 {len(hits)} 处硬编码绝对路径")
    for path, lineno, line in hits:
        print(f"  {path.relative_to(WAREHOUSES).as_posix()}:{lineno}")
        print(f"      {line[:100]}")
    print()
    print("修法：改成相对于仓库根的路径。")
    print("     dbt 从 --project-dir 的上级解析工作目录，")
    print("     所以 `data/envs/...` 会落到仓库根下那个真实文件上。")
    print("     环境变量覆盖入口：MM_WAREHOUSE_DB（见 profiles.yml）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
