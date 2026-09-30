#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GAP_AUDIT 复审探针（只读）。

用途：把 ``docs/GAP_AUDIT.md`` 里的清单**逐条实点一遍**，输出即表格。
复审的意义在于「不复审的债务清单会持续说谎」——所以这个脚本的作用是让"实点"
这一步**可复跑、可复核**，而不是靠人回忆上一轮写了什么。

    python -X utf8 scripts/gap_audit_probe.py

设计约束（刻意为之）：
- **只读**：不写任何文件、不改任何状态；唯一的外部调用是 `git ls-files/tag`。
- **零依赖**：只用标准库（`re` / `pathlib` / `subprocess`）。Git Bash 的 coreutils
  在本机不可用（无 grep/head/wc），所以所有检索都在 Python 里做。
- **口径写在判据里**：每条都打印它的判据字符串，便于人工复核"这个 ✅ 是怎么来的"。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# scripts/ 的上一级 = 仓库根
ROOT = Path(__file__).resolve().parents[1]

ROWS: list[tuple[str, str, str]] = []


def row(item: str, verdict: str, evidence: str) -> None:
    ROWS.append((item, verdict, evidence))


def read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def hits(rel: str, pattern: str, flags: int = 0) -> int:
    t = read(rel)
    return len(re.findall(pattern, t, flags))


def glob_py(dirname: str, pattern: str) -> list[str]:
    return [
        str(p.relative_to(ROOT))
        for p in (ROOT / dirname).rglob("*.py")
        if re.search(pattern, p.read_text(encoding="utf-8", errors="replace"))
    ]


def git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return "<git 不可用>"


# ---------- P0 ----------
raw = git("ls-files", "data/raw")
row(
    "P0-1 data/raw 忽略纪律",
    "✅" if raw == "data/raw/.gitkeep" else "⚠️ 有产物入库",
    f"git ls-files data/raw -> {raw[:60]!r}",
)

n = len(glob_py("tests", r"ingest_real_sensor|eval_real_sensor"))
row("P0-2 真实数据脚本测试", "✅ 已修" if n else "❌ 仍开", f"tests/ 命中 {n}")

mk = read("Makefile")
bare, var = len(re.findall(r"(?<![$\w.])python\s", mk)), len(re.findall(r"\$\(PYTHON\)", mk))
row(
    "P0-3 Makefile 裸 python",
    "✅ 已修" if (bare == 0 and var) else "❌ 仍开",
    f"裸 `python` {bare} 处 / $(PYTHON) {var} 处",
)

# ---------- P1 ----------
n = hits("src/mm_curation/serving/api.py", r"auth|token|api_key|rate|limit|Depends", re.I)
row("P1-1 服务层鉴权/配额", "✅ 已修" if n >= 5 else "❌ 仍开", f"api.py 命中 {n}")

n = hits("src/mm_curation/pipeline/runner.py", r"checkpoint|resume|quarantine|dead_letter|yield")
sig = re.search(r"def run_funnel\(([^)]*)\)", read("src/mm_curation/pipeline/runner.py"))
row(
    "P1-2 流式/断点续跑",
    "✅ 已修" if n >= 5 else "❌ 仍开",
    f"命中 {n}; run_funnel({(sig.group(1).replace(chr(10), ' ')[:48] if sig else '?')})",
)

pins = len([ln for ln in read("requirements.lock").splitlines() if "==" in ln])
row("P1-3 依赖锁文件", "✅ 已修" if pins >= 15 else "❌ 仍开", f"精确 pin {pins} 条")

dfs = [str(p.relative_to(ROOT)) for p in ROOT.rglob("Dockerfile*") if ".git" not in p.parts]
row(
    "P1-4 应用侧 Dockerfile",
    "✅ 已修" if [d for d in dfs if "airflow" not in d] else "❌ 仍开",
    f"{dfs}",
)

n = len(glob_py("packages", r"PROTOCOL_VERSION|SCHEMA_VERSION"))
row("P1-5 协议版本常量", "✅ 已修" if n else "❌ 仍开", f"包内命中 {n}")

ver = re.search(r'^version\s*=\s*"([^"]+)"', read("pyproject.toml"), re.M)
tags = git("tag", "--list").split()
row(
    "P1-6 版本发布",
    "✅ 已修"
    if (ver and ver.group(1) != "0.1.0" and (ROOT / "CHANGELOG.md").exists() and tags)
    else "🟡 部分",
    f"version={ver.group(1) if ver else '?'} / CHANGELOG={(ROOT / 'CHANGELOG.md').exists()} / "
    f"tag={[t for t in tags if t.startswith('v')][:3]}",
)

n = len(glob_py("src", r"review_queue|arbitration|approve"))
row("P1-7 人工审核队列", "✅ 已修" if n else "❌ 仍开", f"src/ 命中 {n}")

has_ps = bool(re.search(r"\[project\.scripts\]", read("pyproject.toml")))
row(
    "P1-8 统一 CLI",
    "✅ 已修" if has_ps else "🟡 半修",
    f"[project.scripts]={'有' if has_ps else '无'} / scripts/*.py="
    f"{len(list((ROOT / 'scripts').glob('*.py')))}",
)

has_d = bool(re.search(r"路线\s*D", read("docs/QUICKSTART.md")))
row(
    "P1-9 十分钟可验路径",
    "✅ 已修" if has_d else "❌ 仍开",
    f"QUICKSTART 路线 D={'有' if has_d else '无'}",
)

# ---------- P2 / P3 ----------
n = len(glob_py("src", r"rolling|ewma|moving_window")) + len(
    glob_py("packages", r"rolling|ewma|moving_window")
)
row("P2-5 滚动基线(单窗视角)", "✅ 已修" if n else "❌ 仍开", f"命中 {n}")

STALE = [r"175\s*\+\s*40", r"328\s*\+\s*67", r"452\s*\+\s*67", r"263\s*\+\s*54"]
stale_hits: list[str] = []
for rel in ("README.md", "docs/ROADMAP.md", "docs/INTERVIEW.md", "docs/DEV_PLAN.md", "AGENTS.md"):
    for i, line in enumerate(read(rel).splitlines(), 1):
        if any(re.search(p, line) for p in STALE):
            stale_hits.append(f"{rel}:{i} {line.strip()[:70]}")
n_facade = len(re.findall(r'"literal"', read("docs/claims.json")))
row(
    "P3-1 基线数字腐烂",
    "✅ 无残留" if not stale_hits else f"⚠️ 人工判定 {len(stale_hits)} 处",
    f"facade 注册数={n_facade}；命中：{stale_hits[:3]}",
)

notes = re.findall(r"^#{2,3}\s*(\d+)[\.、]", read("docs/ENGINEERING_NOTES.md"), re.M)
dup = sorted({x for x in notes if notes.count(x) > 1}, key=int)
row(
    "P3-4 笔记编号唯一性",
    "✅ 无重复" if not dup else f"❌ 重复 {dup}",
    f"标题 {len(notes)} / 唯一 {len(set(notes))}",
)

# ---------- N-3 门禁覆盖面：结构性计数有没有被注册 ----------
notes_n = len(notes)
stale_notes = [
    rel
    for rel in ("README.md", "docs/ROADMAP.md", "docs/INTERVIEW.md")
    if re.search(r"59\s*条", read(rel))
]
row(
    "N-3 结构性计数被注册？",
    "✅" if not stale_notes else f"❌ {len(stale_notes)} 处腐烂",
    f"「59 条」残留 {stale_notes}；实点 {notes_n} 条",
)

# ---------- N-4 可演示面 ----------
tracked_html = git("ls-files", "*.html").split()
row(
    "N-4 可演示面入库",
    "ℹ️",
    f"入库 html={tracked_html}; docs/real_data.html ignored="
    f"{'是' if git('check-ignore', 'docs/real_data.html') else '否'}",
)

# ---------- N-5 README 叙事覆盖 ----------
rd = read("README.md")
covered = [v for v in ("V4", "V5", "V6", "V7") if v in rd]
missing = sorted({"V4", "V5", "V6", "V7"} - set(covered))
has_online_demo = bool(re.search(r"在线\s*Demo|在线演示", rd))
row(
    "N-5 README 叙事覆盖",
    "✅" if (not missing and has_online_demo) else "❌",
    f"README 提到 {covered or '无 V4–V7'}；缺 {missing}；在线 Demo 提法={has_online_demo}",
)

width = max(len(r[0]) for r in ROWS)
print(f"GAP_AUDIT 复审探针（只读）  repo={ROOT}\n")
for item, verdict, ev in ROWS:
    print(f"{item:<{width}}  {verdict:<20}  {ev}")
print(f"\n合计 {len(ROWS)} 项；❌/⚠️ = {sum(1 for r in ROWS if r[1][0] in '❌⚠')}")
sys.exit(0)
