"""F3：把预计算 JSON 注入自包含交互式 HTML 单页 → `docs/real_data.html`。

为什么要走「模板 + 注入」而不是直接手写 HTML：预计算 JSON 有 2.4 MB，
手写一份内嵌它的 HTML 既不可维护也不可能 review。构建脚本只做一件事：
把 JSON 塞进模板里的 `__DATA__` 占位符。

页面**零外部依赖**（无 CDN、无字体、无图表库）：这样它才能离线打开、
投屏、发链接、塞进作品集，而不依赖任何网络环境。

改版式改 `scripts/templates/real_data_page.html`，改数字改 `scripts/build_real_interactive.py`
（本脚本只负责把两者拼起来）。

用法：
    python -X utf8 scripts/build_real_data_html.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "data" / "reports" / "real_sensor_interactive.json"
OUT = REPO / "docs" / "real_data.html"

# HTML 模板抽成独立资产：模板里是 492 行 HTML/CSS/JS，内嵌在 .py 里既过不了 ruff
# 的 E501，也没法被任何 HTML 工具链（格式化、校验、diff 高亮）处理。
# 读取用 `read_text`（模板无 \r，二者等价），占位符 `__DATA__` 由 main() 注入。
TEMPLATE_PATH = REPO / "scripts" / "templates" / "real_data_page.html"
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"缺少预计算数据 {SRC}，先跑 scripts/build_real_interactive.py")
    raw = SRC.read_text(encoding="utf-8")
    payload = json.loads(raw)
    payload["meta"]["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False,
                                                    separators=(",", ":")))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"产出: {OUT}（{OUT.stat().st_size / 1e6:.2f} MB，自包含零外部依赖）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
