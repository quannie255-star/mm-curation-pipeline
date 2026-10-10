"""claims 门禁的变异测试（mutation test）：证明门禁会红，而不是装饰。

「门禁只增不减、且必须做变异测试」是项目的硬纪律。本脚本对 `verify_claims.py`
新增的三道机制逐个做一次性破坏，断言**退出码为 1 且失败理由正确**：

  1. 工程笔记编号重号  → 派生计数 notes_count 报 dup
  2. 工程笔记编号跳号  → 派生计数 notes_count 报 gap
  3. README 条数回退   → 门面 README_md__notes_count 报 doc-stale
  4. 只破坏上下文短语  → 门面报 context-stale（证明 must_contain 不是摆设，
     数字仍在文档里、但已经不是那句话里的那个）
  5. 塞一个未登记数字  → 覆盖率棘轮 grew（只许降不许升）
  6. 删一条门面登记    → 门面条数低于下限 shrunk

安全设计：**全程在临时沙箱里跑**——把注册表触及的文件（README / docs / AGENTS /
verify_claims.py 等）复制到 temp 目录再破坏，真实工作区一个字节都不动。

用法：
    python -X utf8 scripts/mutation_test_claims_gate.py      # 全绿则退出码 0
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def build_sandbox() -> Path:
    """只复制注册表触及的文件，目录结构照搬（verify_claims 的 REPO 由脚本位置推出）。"""
    reg = json.loads((ROOT / "docs" / "claims.json").read_text(encoding="utf-8"))
    need = {"README.md", "AGENTS.md", "docs/claims.json", "scripts/verify_claims.py"}
    for f in reg.get("facades", []):
        need.add(f["doc"])
    need |= set(reg.get("meta", {}).get("coverage_ceiling", {}))
    for d in reg.get("derived", []):
        need.add(d["file"])

    box = Path(tempfile.mkdtemp(prefix="mmc-claims-mutation-"))
    for rel in sorted(need):
        src = ROOT / rel
        if not src.exists():
            continue
        dst = box / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return box


def run_gate(box: Path) -> tuple[int, str]:
    """在沙箱里跑一次门禁。

    ⚠️ **必须传 `--reports-missing skip`**（2026-10-08 实测踩到）：
    沙箱只复制注册表触及的**文本**文件，`data/reports/` 是生成物、不入库，
    所以沙箱里必然「16 条 claim 全部缺报告」→ rc=1。
    而本脚本要验的是 **facade / derived / 覆盖率**三层（它们不依赖 reports），
    claim 层在干净检出上本来就无法校验 —— 这正是 `gate-ci.yml` 里
    显式传 skip 并断言「0 PASS」的原因。

    漏了这个参数的后果很隐蔽：脚本不是「某条变异没拦住」，
    而是**在第一道基线断言就中止**（rc=1 直接 return），
    报告上只写「沙箱基线不绿」—— 看起来像门禁坏了，
    实际是脚本没按 CI 的口径调用门禁。
    """
    p = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(box / "scripts" / "verify_claims.py"),
            "--reports-missing",
            "skip",
        ],
        cwd=str(box),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def mutate(
    box: Path, rel: str, old: str, new: str, label: str, needle: str, expect_rc: int = 1
) -> bool:
    path = box / rel
    raw = path.read_bytes()
    before = _md5(raw)
    text = raw.decode("utf-8")
    if old not in text:
        print(f"  [跳过] {label}：沙箱里找不到锚点 {old!r}")
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
    try:
        rc, out = run_gate(box)
        hit = needle in out
        ok = rc == expect_rc and hit
        flag = "通过" if ok else "未拦住！"
        print(f"  [{flag}] {label}：rc={rc}（期望 {expect_rc}）命中 {needle!r}={hit}")
        if not ok:
            for ln in out.splitlines():
                if needle in ln or "漂移" in ln or "派生" in ln or "覆盖" in ln or "门面" in ln:
                    print(f"        原始输出: {ln[:120]}")
        return ok
    finally:
        path.write_bytes(raw)
        assert _md5(path.read_bytes()) == before, f"沙箱恢复失败：{rel}"


def main() -> int:
    box = build_sandbox()
    print(f"沙箱：{box}\n（真实工作区只读，不会被改动）\n")

    rc, _ = run_gate(box)
    if rc != 0:
        print(f"沙箱基线不绿（rc={rc}）——先让门禁全绿再跑变异测试")
        return 1
    print("沙箱基线：rc=0（绿）\n")

    notes = "docs/ENGINEERING_NOTES.md"
    results = []

    # 笔记编号锚点从源文档现取（取最大编号那一条），不硬编码——
    # 硬编码过一次，笔记重排后就悄悄「跳过」了这条变异（2026-09-30 实测）。
    notes_text = (box / notes).read_text(encoding="utf-8")
    heads = re.findall(r"^### (\d+)\. .*$", notes_text, re.M)
    assert heads, "沙箱里读不到任何笔记标题"
    top = max(int(h) for h in heads)
    top_head = re.search(rf"^### {top}\. .*$", notes_text, re.M).group(0)

    print(f"变异 1：笔记编号重号（#{top} → #{top - 1}）")
    results.append(
        mutate(
            box,
            notes,
            top_head,
            top_head.replace(f"### {top}.", f"### {top - 1}."),
            "重号",
            "派生 notes_count",
        )
    )

    print(f"变异 2：笔记编号跳号（#{top} → #{top + 1}）")
    results.append(
        mutate(
            box,
            notes,
            top_head,
            top_head.replace(f"### {top}.", f"### {top + 1}."),
            "跳号",
            "派生 notes_count",
        )
    )

    # README 的条数锚点从注册表的 must_contain 派生。README 改版把措辞从
    # 「工程发现日志 96 条」改成「96 条工程发现」时，硬编码版静默跳过两条变异。
    reg = json.loads((box / "docs" / "claims.json").read_text(encoding="utf-8"))
    rd_facade = next(f for f in reg["facades"] if f["id"] == "README_md__notes_count")
    anchor = rd_facade["must_contain"]
    assert anchor in (box / "README.md").read_text(encoding="utf-8"), (
        f"注册表 must_contain {anchor!r} 在README 里已不存在——"
        "先同步 README 与 claims.json 再跑变异测试"
    )

    print(f"变异 3：README 条数回退（{anchor} → 条数改小）")
    results.append(
        mutate(
            box,
            "README.md",
            anchor,
            re.sub(r"\d+", "59", anchor),
            "条数回退",
            "README_md__notes_count",
        )
    )

    print(f"变异 4：只破坏上下文短语（{anchor} → 数字不变、只换字）")
    results.append(
        mutate(
            box,
            "README.md",
            anchor,
            re.sub(r"条", "个", anchor),
            "上下文失效",
            "README_md__notes_count",
        )
    )

    print("变异 5：塞一个未登记硬数字 0.4242")
    readme = box / "README.md"
    raw = readme.read_bytes()
    try:
        readme.write_text(raw.decode("utf-8") + "\n<!-- 0.4242 -->\n", encoding="utf-8", newline="")
        rc, out = run_gate(box)
        ok = rc == 1 and "grew" in out
        print(f"  [{'通过' if ok else '未拦住！'}] 未登记数字：rc={rc} grew={'grew' in out}")
        results.append(ok)
    finally:
        readme.write_bytes(raw)

    print("变异 6：删一条门面登记")
    reg_path = box / "docs" / "claims.json"
    raw = reg_path.read_bytes()
    try:
        d = json.loads(raw.decode("utf-8"))
        removed = d["facades"].pop()
        reg_path.write_text(
            json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline=""
        )
        rc, out = run_gate(box)
        ok = rc == 1 and "shrunk" in out
        flag = "通过" if ok else "未拦住！"
        print(f"  [{flag}] 删门面 {removed['id']}：rc={rc} shrunk={'shrunk' in out}")
        results.append(ok)
    finally:
        reg_path.write_bytes(raw)

    rc, _ = run_gate(box)
    print(f"\n沙箱已还原：门禁 rc={rc}（0 = 绿，说明变异都被干净撤销）")
    n_ok = sum(results)
    print(f"变异测试：{n_ok}/{len(results)} 条被正确拦住")
    return 0 if n_ok == len(results) == 6 and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
