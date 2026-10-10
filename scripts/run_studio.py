#!/usr/bin/env python
"""一键启动多模态数据清洗 Studio（本地网页界面）。

用法：
    python scripts/run_studio.py              # 起服务 + 自动开浏览器
    python scripts/run_studio.py --port 9000  # 换端口
    python scripts/run_studio.py --no-browser # 不开浏览器

为什么有这个小脚本：`python -m mm_curation.studio.serve` 需要 `src/` 在
`sys.path` 里，而外行用户不该关心 PYTHONPATH 怎么设。这个脚本负责
把路径摆好，顺便**先自检**（缺依赖/端口被占用时给一句人话），
而不是让用户看着一个 traceback 不知道怎么办。
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _preflight(port: int, host: str = "127.0.0.1") -> None:
    """启动前自检：只报**用户能照做**的问题。"""
    missing = []
    for mod, pkg in (("transformers", "transformers"),
                     ("datasets", "datasets"),
                     ("numpy", "numpy")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"✗ 缺少依赖：{', '.join(missing)}")
        print("  安装：pip install " + " ".join(missing))
        raise SystemExit(1)

    # ⚠️ 端口占用检查**必须真的 listen，而且不能开 SO_REUSEADDR**。
    # 实测栽过：带 SO_REUSEADDR 时 `bind` 在 Windows 上**永远成功**
    #（同一端口可被重复绑定）→ 这条自检恒真，占用时报错起不来。
    # 正确做法：不设 REUSEADDR + 真的 listen()，再立刻关掉。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            s.listen(1)
        except OSError as e:
            print(f"✗ 端口 {port} 已被占用（{e.strerror or type(e).__name__}）")
            print(f"  换一个端口：python scripts/run_studio.py --port {port + 1}")
            print("  或者先关掉已经开着的一个 Studio 窗口。")
            raise SystemExit(1) from None

    # 算子注册表靠 import 副作用 —— 忘了import 会返回 0 个算子
    from curation_eval.registry import available_operator_metas

    import mm_curation.operators  # noqa: F401

    n = len(available_operator_metas())
    if n == 0:
        print("✗ 算子注册表为空 —— 这会让场景卡全都点不开。")
        print("  这是内部错误，请反馈。")
        raise SystemExit(1)
    print(f"  自检通过：{n} 个清洗算子已注册")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    print(f"\n  多模态数据清洗 Studio\n{'─' * 44}")
    _preflight(a.port, a.host)

    from mm_curation.studio.serve import main as serve_main

    serve_main(["--port", str(a.port), "--host", a.host]
               + (["--no-browser"] if a.no_browser else []))


if __name__ == "__main__":
    main()
