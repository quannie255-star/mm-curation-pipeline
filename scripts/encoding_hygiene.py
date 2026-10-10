"""源文件 UTF-8 卫生门禁。

★ 为什么需要这个门禁（2026-10-05 真实事故）
────────────────────────────────────────────
写入方（Edit / Write / patch 脚本）一旦在某个字符上出问题，
落盘的不是「非法字节」而是**U+FFFD 替换字符本身**（EF BF BD）。
后果极其隐蔽：

1. 文件**能正常解码**（U+FFFD 是合法 UTF-8），所以 `read_text()` 不报错、
   编辑器打开也正常、ruff / dbt 全绿 —— 没有任何工具会拦它；
2. 损坏只出现在中文注释里，**不影响 SQL 语义** → 门禁不会红；
3. 只有逐字节扫才能发现。人工检查必然漏（本轮3 处是靠
   「终端输出里看到乱码」偶然撞见的，不是靠流程发现的）。

所以这个门禁的价值不在于「发现乱码」，而在于**把肉眼才能做的事变成可复跑的门禁** ——
这正是 AGENTS.md 的准入门槛。

判据刻意收窄，避免误报：
- **只扫源码**（`.sql` / `.yml` / `.md` / `.py`），`target/` 等生成物不扫
  （它们由 dbt 从源文件拷贝，源文件干净就一定干净）；
- 只认 U+FFFD 这一个字符，不做「是不是合法中文」的主观判断；
- 退出码非0 即红，可直接接进 CI / make。
"""

from __future__ import annotations

import pathlib
import sys

# 与仓库根目录的关系：scripts/encoding_hygiene.py -> 仓库根
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SCAN_SUFFIXES = {".sql", ".yml", ".yaml", ".md", ".py"}
# 生成物目录：它们的乱码是源文件的镜像，扫它们只会重复报同一处
GENERATED_DIRS = {"target", "dbt_packages", "logs", ".git", "__pycache__"}

# 用码点构造，**不能写字面量**——否则本文件自己就含一个 U+FFFD，
# 门禁会永远报自己这一处，变成「一启动就红」的死门禁
# （2026-10-05 变异测试当场抓到：字面量版本 rc 恒为 1）。
REPLACEMENT_CHAR = chr(0xFFFD)
REPLACEMENT_BYTES = b"\xef\xbf\xbd"


def _is_generated(path: pathlib.Path) -> bool:
    return any(part in GENERATED_DIRS for part in path.parts)


def scan_file(path: pathlib.Path) -> list[tuple[pathlib.Path, int, str]]:
    """单个文件的 U+FFFD 判定。`scan` 与根级 md 都走这里——**判据只有一份**。

    两处各写一份判定是「同一组合抄两遍」的必然结果：改了一处忘了另一处，
    门禁就会在某个调用点上静默失效（2026-10-05 在 dbt 门禁上踩过同款）。
    """
    hits: list[tuple[pathlib.Path, int, str]] = []
    if path.suffix not in SCAN_SUFFIXES or not path.is_file():
        return hits
    raw = path.read_bytes()
    if REPLACEMENT_BYTES not in raw:
        return hits
    text = raw.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), 1):
        if REPLACEMENT_CHAR in line:
            hits.append((path, lineno, line.strip()))
    return hits


def scan(root: pathlib.Path) -> list[tuple[pathlib.Path, int, str]]:
    """返回 [(文件, 行号, 该行)]，每处 U+FFFD 一条。"""
    hits: list[tuple[pathlib.Path, int, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _is_generated(path.relative_to(root)):
            continue
        hits.extend(scan_file(path))
    return hits


def main() -> int:
    # ★ 扫描范围用**显式白名单**，不用「仓库根减排除项」（2026-10-05 改）。
    #   理由：`data/` 整棵是真实数据湖（parquet + 报告 JSON），
    #   「根目录 - 排除项」这种写法一旦有人加了个新数据目录就会漏扫或者误扫，
    #   而漏扫是**静默**的 —— 门禁看起来在跑，实际没看这些文件。
    #   白名单的好处是：新目录默认不在范围内，要纳入必须显式改这里（可评审）。
    #
    #   之前只扫 warehouses/ 与 scripts/，漏掉了 docs/：
    #   2026-10-05 手工改 ROADMAP.md 时手滑写坏了一个字符，
    #   门禁报「2 个目录，未发现」—— **假绿**，因为它根本没看 docs/。
    roots = [
        REPO_ROOT / "warehouses",
        REPO_ROOT / "scripts",
        REPO_ROOT / "docs",
        REPO_ROOT / "src",
        REPO_ROOT / "tests",
        REPO_ROOT / "packages",
    ]
    if len(sys.argv) > 1:
        roots = [pathlib.Path(a) for a in sys.argv[1:]]
    # 根级 *.md（README / AGENTS.md）单独扫：它们不在任何子目录里
    root_files = sorted(REPO_ROOT.glob("*.md"))

    all_hits: list[tuple[pathlib.Path, int, str]] = []
    for root in roots:
        if root.exists():
            all_hits.extend(scan(root))
    for f in root_files:
        all_hits.extend(scan_file(f))

    if not all_hits:
        print(
            f"[OK] UTF-8 卫生：{len(roots)} 个目录 + {len(root_files)} 个根级 md，"
            "未发现替换字符 U+FFFD"
        )
        return 0

    print(f"[RED] UTF-8 卫生：发现 {len(all_hits)} 处替换字符 U+FFFD")
    for path, lineno, line in all_hits:
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            # 支持传仓库外的路径（沙箱/变异测试会这么用）
            rel = path.as_posix()
        print(f"  {rel}:{lineno}")
        print(f"      {line[:100]}")
    print()
    print("修法：这些位置是写入时字符损坏，语义通常无影响（多在中文注释里），")
    print("     但会让文档评审与后续 diff 失真。按上下文补回正确字符即可。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
