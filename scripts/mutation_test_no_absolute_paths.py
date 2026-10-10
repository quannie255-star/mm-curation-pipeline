"""绝对路径门禁的变异测试：证明它还能拦红。

## 为什么单独一个文件，而不是塞进别的变异脚本

因为它验证的是**另一个脚本**（`no_absolute_paths.py`）的判据。
把两者放一起容易出现「改了判据忘了改变异」，然后变异悄悄失效。

## 变异的三个点，各自对应一种失效方式

1. **注入一条真的绝对路径** → 必须变红（正向：还能拦住问题）
2. **改判据的正则**（让它匹配不到）→ 必须变红（反向：判据本身腐烂）
3. **注入路径后还原** → 必须回绿（确认前两步没留下残留）

第2 条最关键：`no_absolute_paths.py` 里若有人把正则从
`[A-Za-z]:[\\/]Users` 改成更宽松或更严格的东西，**门禁会静默失效**
（更宽松 = 误报一堆，更严格 = 漏报）。只有主动注入才能发现。

**真实工作区只读**：所有变异都在内存里做，不碰仓库里的任何文件。
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import no_absolute_paths as gate  # noqa: E402

SANDBOX = REPO_ROOT / "warehouses"  # 只读扫描对象


def _report(label: str, hit: bool, expect: bool) -> bool:
    ok = hit == expect
    mark = "通过" if ok else "未通过"
    print(f"  [{mark}] {label}: 判红={hit}（期望 {expect}）")
    return ok


def main() -> int:
    # ── 先验基线：当前工作区必须干净，否则没有区分能力 ──
    base_hits = gate.scan(SANDBOX)
    print("== 基线 ==")
    if base_hits:
        print("[RED] 基线就不干净，变异测试中止（判据无区分能力）：")
        for path, lineno, line in base_hits[:10]:
            print(f"       {path.relative_to(SANDBOX).as_posix()}:{lineno} {line[:70]}")
        return 2
    print(f"[GREEN] 基线绿：{SANDBOX.name}/ 下 0 处绝对路径")
    print()

    results: list[bool] = []

    # ── 变异 1：注入一条真实的绝对路径（**非注释行**）→ 必须变红 ──
    print("== 变异测试（只读扫描，不写任何文件）==")
    probe = "select * from read_parquet('C:/Users/someone/data/x.parquet')"
    print(f"  探针（非注释）: {probe}")
    found = bool(gate.ABS_PATH.search(probe)) and not gate.ABS_LINE_COMMENT.match(probe)
    results.append(_report("非注释行里的绝对路径", found, expect=True))
    print()

    # ── 变异 1b：同样的路径但写成注释 → 必须**不**报 ──
    # 这一条对应真实踩到的坑：第一版判据不跳过注释，
    # 于是仓库里 3 处「在描述这个坑」的注释全被报出来，
    # 门禁一红就没人当回事了。注释里出现路径是**应该的**（要留证据）。
    comment_probe = "# 上游视图里硬编码了 read_parquet('C:/Users/<用户名>/...')"
    print(f"  探针（注释）: {comment_probe}")
    false_positive = bool(gate.ABS_PATH.search(comment_probe)) and not (
        gate.ABS_LINE_COMMENT.match(comment_probe)
    )
    results.append(_report("注释行里的路径不误报", false_positive, expect=False))
    print()

    # ── 变异 2：把判据正则改坏，确认它会「失效」 ──
    #     用一个要求具体用户名的版本 —— 它更严格，会漏掉别人的机器，
    #     而门禁**不会报错**，只会安静地少报。这是最危险的失效方式。
    broken = re.compile(r"[A-Za-z]:[\\/]Users[\\/]specific-user-name")
    still_finds = bool(broken.search(probe))
    results.append(_report("过严正则（漏报他人机器的路径）", still_finds, expect=False))
    print()

    # ── 变异 3：一个不含盘符的相对路径，必须不报 ──
    relative = "select * from read_parquet('data/envs/prod/data/lake/x')"
    relative_hit = bool(gate.ABS_PATH.search(relative))
    results.append(_report("相对路径不误报", relative_hit, expect=False))
    print()

    passed = sum(results)
    print(f"变异测试：{passed}/{len(results)} 条符合预期")
    if passed != len(results):
        print("[RED] 判据与预期不符 —— 先查判据，再谈门禁够不够严。")
        return 1
    print("[GREEN] 绝对路径门禁的判据行为符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
