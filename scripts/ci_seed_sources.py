"""为容器冒烟（CI）造最小合成源。**幂等**：文件已存在就跳过。

## 为什么需要它

`data/raw/**` 是**不入库**的（真数据 383 MB，且 `news_corpus.jsonl` 本身 9.8 MB），
所以 CI 的 runner 上克隆下来**一个源都没有**。于是容器冒烟会卡在第一步：
平台链路跑不出任何分区 → 没有可晋升的产物 → 容器里的 `/healthz` 因为
契约闸门没有数据可断言而语义模糊。

在这个脚本出现之前，唯一的替代是在 workflow YAML 里内联一段 heredoc。
那样做的问题不是丑，而是**无法本地验证**：那段造数逻辑只有在 CI 上跑过才知道对不对，
而它的失败会表现成"容器没起来"——指不到真正的原因。做成脚本后，
`tests/test_ci_seed_sources.py` 可以本地跑通"造数 → 跑链路 → SUCCESS"这一段。

## 只造两个源，是刻意的

`registry.LAKE_SOURCES` 里登记了 12 个源，这里只落地 2 个
（`metropt3` 传感器 + `news_corpus` 文本）。**其余 10 个走 `SKIPPED` 分支**——
那条分支因此每次 CI 都被覆盖到，而冒烟仍然只要几秒钟。
"造全 12 个源"会让冒烟变慢、变脆，却证明不了更多东西。

幂等同样重要：本机跑它是空操作（真源已经在了），CI 跑它才真的写文件。
**"本机跑有没有副作用"必须能在读代码时判断出来**，所以判据就是"文件在不在"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 传感器源：meta 里带 device_id / channel / unit 与 window_start
# （`registry._DATE_KEYS` 里事件时间的第一优先来源）
_METROPT3 = [
    {
        "id": "ci_0001",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {
            "device_id": "compressor_01",
            "device_type": "air_compressor",
            "channel": "TP2",
            "unit": "degC",
            "sampling_hz": 0.1,
            "operating_mode": "load",
            "window_start": "2020-04-01T00:00:00",
            "window_end": "2020-04-01T00:01:00",
            "score:robust_z": 0.42,
            "score:range_check": 0.9,
        },
    },
    {
        "id": "ci_0002",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {
            "device_id": "compressor_01",
            "device_type": "air_compressor",
            "channel": "Reservoirs",
            "unit": "bar",
            "sampling_hz": 0.1,
            "operating_mode": "load",
            "window_start": "2020-04-01T00:01:00",
            "window_end": "2020-04-01T00:02:00",
            "score:robust_z": 0.11,
        },
    },
    {
        "id": "ci_0003",
        "modality": "industrial_sensor",
        "text": "",
        "meta": {
            "device_id": "compressor_02",
            "device_type": "air_compressor",
            "channel": "TP2",
            "unit": "degC",
            "operating_mode": "unload",
            "window_start": "2020-04-02T00:00:00",
            "window_end": "2020-04-02T00:01:00",
            "score:range_check": 0.75,
        },
    },
]

# 文本源：事件时间从 meta.url 的 `/2026/09-06/` 形态解析（真实语料实测可用）
_NEWS = [
    {
        "id": "ci_news_0001",
        "modality": "text_article",
        "text": "央行宣布下调存款准备金率0.25个百分点，释放长期资金约5000亿元。",
        "meta": {"url": "https://example.com/finance/2026/09-06/a1.html"},
    },
    {
        "id": "ci_news_0002",
        "modality": "text_article",
        "text": "A股三大指数集体高开，券商板块领涨。",
        "meta": {"url": "https://example.com/finance/2026/09-06/a2.html"},
    },
]

# 相对 `data/raw/` 的路径 → 行。这些是 `registry.LAKE_SOURCES` 里**已登记**的路径，
# 所以不必 monkeypatch 注册表（与 tests/conftest.py 的夹具保持同一组口径）。
SOURCES: dict[str, list[dict]] = {
    "real/metropt3/windows.jsonl": _METROPT3,
    "news_corpus.jsonl": _NEWS,
}


def seed(root: str | Path = REPO) -> list[str]:
    """把缺失的源写出来，返回**实际写出**的相对路径列表（已存在的不在列表里）。"""
    base = Path(root) / "data" / "raw"
    written: list[str] = []
    for rel, rows in SOURCES.items():
        p = base / rel
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
        p.write_text(text, encoding="utf-8", newline="\n")
        written.append(rel)
    return written


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else REPO
    written = seed(root)
    if written:
        print(f"已写出 {len(written)} 个合成源到 {root / 'data' / 'raw'}：{written}")
    else:
        print("源已存在，未改动任何文件（幂等）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
