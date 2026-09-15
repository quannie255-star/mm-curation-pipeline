r"""OPS 调度安装器（R0）：把 ops_daily.py 注册为 Windows 每日定时任务。

默认只打印将要执行的 schtasks 命令；--arm 才真装（武装是显式动作——
R8 环境冻结与电源设置完成前不要武装，见 docs/OPS_PRD.md 里程碑第 0 周）。

用法（Git Bash）：
    python -X utf8 scripts/ops_install_schedule.py          # 预览命令
    python -X utf8 scripts/ops_install_schedule.py --arm    # 真装（schtasks 需要相应权限）
    python -X utf8 scripts/ops_install_schedule.py --disarm # 删除任务

注意：任务以当前用户身份每日 20:00 触发；「错过补跑」由 schtasks /sc daily 的
默认行为 + 次日幂等增量兜底（ops 每步均可重跑）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASK_NAME = "MMCurationOpsDaily"
RUN_TIME = "20:00"


def build_command(python_exe: str) -> list[str]:
    log_path = REPO / "data" / "ops" / "schtasks.log"
    inner = (
        f"cd /d {REPO} && \"{python_exe}\" -X utf8 scripts\\ops_daily.py "
        f">> \"{log_path}\" 2>&1"
    )
    return [
        "schtasks",
        "/create",
        "/tn",
        TASK_NAME,
        "/sc",
        "daily",
        "/st",
        RUN_TIME,
        "/tr",
        f"cmd /c \"{inner}\"",
        "/f",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arm", action="store_true", help="真的注册定时任务")
    parser.add_argument("--disarm", action="store_true", help="删除定时任务")
    args = parser.parse_args()

    if args.disarm:
        proc = subprocess.run(  # noqa: S603
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(proc.stdout or proc.stderr)
        return proc.returncode

    cmd = build_command(sys.executable)
    print("将注册每日 20:00 定时任务（任务名 {}）：".format(TASK_NAME))
    print(" ".join(cmd))
    if not args.arm:
        print("\n预览模式，未注册。确认环境冻结（R8）完成后加 --arm 执行。")
        return 0

    (REPO / "data" / "ops").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")  # noqa: S603
    print(proc.stdout or proc.stderr)
    if proc.returncode == 0:
        print(f"已注册。明晚 {RUN_TIME} 起自动运行；日志见 data/ops/schtasks.log。")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
