"""从 pip 的 resolve 报告生成 requirements.lock（S6）。

用法：
    1) pip install --dry-run --ignore-installed --report <tmp>/app-resolve.json \
           -r requirements-app.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    2) python scripts/gen_lock.py <tmp>/app-resolve.json

**为什么要一个脚本而不是手写锁文件**：手写的锁文件没人能复核它是不是真的解析结果，
而"锁"的全部价值就在于它可被重放验证。脚本 + 一条命令 = 可复核。
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

HEADER = """# 服务容器依赖精确锁（S6）——由 pip 解析器生成，**不要手改**。
#
# 生成方式（改依赖后重跑这两条，见 scripts/gen_lock.py）：
#   pip install --dry-run --ignore-installed --report report.json \\
#       -r requirements-app.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
#   python scripts/gen_lock.py report.json
# 生成日期：{today}（共 {n} 个包 = 直接依赖 + 其运行时闭包，含 uvicorn[standard] 的 extras）
#
# 这份锁钉的是**解析器当日给出的上游版本**，不是"我本机 venv 里的那一套"。
# 两者会不同（本机 pyarrow 是 23.0.1，这里解析到 25.0.1）。这个区别必须说清：
# 平台轨那些实测数字是在**本机 venv** 上跑的，而镜像构建服从这份锁——
# 「锁」保证的是**构建可复现**，不是「实测版本可复现」。后者要把版本冻结成实测值，
# 那等于把开发机环境当真相源，代价是别人装不上新版本的安全修复。
#
# 直接依赖与范围见 requirements-app.txt；GPU 训练栈刻意不进镜像。
#
# 校验：tests/test_lock_file.py 会检查"每条都是 name==version"且
# "requirements-app.txt 里每个直接依赖都在锁里"。
"""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    from packaging.utils import canonicalize_name

    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    pins = {
        canonicalize_name(i["metadata"]["name"]): i["metadata"]["version"]
        for i in report["install"]
    }
    items = sorted(pins.items())
    body = "".join(f"{n}=={v}\n" for n, v in items)
    text = HEADER.format(today=datetime.date.today().isoformat(), n=len(items)) + "\n" + body
    out = Path("requirements.lock")
    # `newline="\n"`：锁文件要跨平台比对，写入侧就必须钉死 LF（同 dags 生成物）
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写 {out}：{len(items)} 个精确 pin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
