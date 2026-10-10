"""探测一个「装齐了本项目依赖」的 Python 解释器，供 Makefile 用。

## 为什么需要它
`make repro` 自我描述是「一键复现证明链」。实测在开发机上，
裸 `python` 解析到 3.13 —— 没有 numpy、没有 mm_curation，
一跑就 `ModuleNotFoundError`，而且**报错出现在深层脚本里**，
现场看半天不知道是「解释器选错了」。

`command -v python` 只回答「路径上有没有 python」，
**不回答「这个 python 能不能 import 本项目依赖」**。后者才是要的。

## 判定标准（刻意保守）
候选必须能 `import mm_curation`。它是本项目的包，
装上它就意味着 `pip install -e .` 做过——而那一步会带进
numpy / pandas / torch / transformers / pyyaml。

只验 numpy 太宽松：一个「装过 numpy 但没装本项目」的 python
仍会在跑到 `scripts/xxx.py` 时才炸，那时已经在漏斗里了。

## 输出
stdout 只有一行：可用的解释器绝对路径。失败时输出空串+ 诊断到 stderr。
**刻意不在 stdout 打任何别的东西** —— Makefile 的 `$(shell)` 会把它
原样塞进命令里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: 必须能 import 的模块。选mm_curation 的理由见模块 docstring。
PROBE_CODE = "import mm_curation, numpy, yaml"

#: PATH 上的候选名。刻意**不**只按版本号排：
#: 一个装齐依赖的 3.11 远好过一个「版本更高但没装依赖」的 3.13。
CANDIDATE_NAMES = ("python3.11", "python3", "python", "py")

#: 不在 PATH 里的「系统安装」位置。**漏掉这一类是本脚本最大的坑**：
#: Windows 上 `C:\Program Files\Python311\` 里的解释器完全合格，
#: 但常不在 PATH（PATH 里的 `python` 反而指向另一个没装依赖的版本）。
#: 只查 PATH 会得出「找不到能用的解释器」的错误结论——
#: 明明有一个满血的就摆在磁盘上。
_GLOBS = (
    # Windows:官方安装器默认位置
    r"C:\Program Files\Python3*\python.exe",
    r"C:\Python3*\python.exe",
    r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe",
    # POSIX
    "/usr/bin/python3*",
    "/usr/local/bin/python3*",
    "/opt/homebrew/bin/python3*",
)

#: 首选版本。3.11 是本项目跑全量门禁的基线解释器（RUNBOOK §0）。
_PREFERRED = (3, 11)


def _run(name: str, code: str, timeout: int) -> subprocess.CompletedProcess[str] | None:
    """跑一次探测子进程。

    刻意用二进制捕获再手动解码：`text=True` 在Windows 上按 locale
    （GBK）解码子进程输出，遇到候选解释器往 stdout 打中文就抛
    `UnicodeDecodeError`——**探测失败会被误报成「解释器不可用」**。
    """
    try:
        r = subprocess.run([name, "-c", code], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    r.stdout = (r.stdout or b"").decode("utf-8", errors="replace")
    r.stderr = (r.stderr or b"").decode("utf-8", errors="replace")
    return r


def _try_one(name: str) -> str | None:
    """若 `name` 能 import 关键依赖，返回其绝对路径。"""
    r = _run(name, PROBE_CODE, timeout=25)
    if r is None or r.returncode != 0:
        return None
    r2 = _run(name, "import sys; print(sys.executable)", timeout=15)
    if r2 is None or r2.returncode != 0:
        return None
    return r2.stdout.strip() or None


def _venv_candidates() -> list[str]:
    out: list[str] = []
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        for rel in ("Scripts/python.exe", "bin/python"):
            p = Path(venv) / rel
            if p.exists():
                out.append(str(p))
    # 项目本地 .venv 优先级最高——它才是这个项目真正的运行环境
    local = Path(__file__).resolve().parents[1] / ".venv"
    for rel in ("Scripts/python.exe", "bin/python"):
        p = local / rel
        if p.exists():
            out.append(str(p))
    return out


def _ver_key(path: str) -> tuple[int, ...]:
    """从路径里的 `python3.11` 抠出版本元组，用于排序。抠不到 → (0,)。"""
    parts = [p for p in Path(path).stem.lower().replace("-", ".").split(".") if p.isdigit()]
    return tuple(int(p) for p in parts) or (0,)


def _installed_candidates() -> list[str]:
    """扫「不在 PATH 里的系统安装」，按 版本偏好 → 版本号降序 排。

    这类解释器在Windows 上很常见（装Python 时勾了 PATH 才进 PATH，
    没勾就只躺在 `C:\\Program Files\\Python311\\`）。
    """
    import glob

    found: list[str] = []
    for pat in _GLOBS:
        for hit in glob.glob(os.path.expandvars(pat)):
            if Path(hit).is_file():
                found.append(hit)
    # 首选 3.11 排最前，其余按版本号降序（新的通常依赖更全）
    return sorted(
        set(found), key=lambda p: (0 if _ver_key(p) == _PREFERRED else 1, [-v for v in _ver_key(p)])
    )


def main() -> int:
    tried: list[tuple[str, str]] = []

    for cand in [*_venv_candidates(), *_installed_candidates(), *CANDIDATE_NAMES]:
        if any(c == cand for c, _ in tried):
            continue
        got = _try_one(cand)
        if got:
            print(got)
            return 0
        tried.append((cand, "无依赖或不存在"))

    # 失败：诊断写 **stderr**（stdout 必须是空的，否则会污染 Makefile 变量）
    print("", file=sys.stderr)
    print("找不到能 import 本项目依赖的 Python 解释器。", file=sys.stderr)
    print(f"  试过：{', '.join(n for n, _ in tried)}", file=sys.stderr)
    print("", file=sys.stderr)
    print("修复（任选一种解释器，装齐依赖即可）：", file=sys.stderr)
    print("  # A. 用项目约定的 3.11 新建 venv", file=sys.stderr)
    print("  python3.11 -m venv .venv && . .venv/bin/activate", file=sys.stderr)
    print("  python -m pip install -r requirements.txt", file=sys.stderr)
    print("  python -m pip install -e packages/curation-eval", file=sys.stderr)
    print("  python -m pip install -e .", file=sys.stderr)
    print("", file=sys.stderr)
    print("  # B. 手动指定（跳过探测）", file=sys.stderr)
    print("  make PYTHON=/你的/python repro", file=sys.stderr)
    print("", file=sys.stderr)
    print("  # C. 只想知道缺什么，就跑体检（它会给完整清单）", file=sys.stderr)
    print("  python3 -X utf8 scripts/doctor.py", file=sys.stderr)
    print(json.dumps({"found": False, "tried": [n for n, _ in tried]}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
